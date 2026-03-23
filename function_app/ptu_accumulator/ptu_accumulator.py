"""
PTU Accumulator (Capacity Sniper) -- Azure Function (Timer Trigger)
Aggressively grabs any freed PTU capacity and accumulates toward target.
Runs every 5 minutes. Works with any model available through Azure AI Foundry
that supports Provisioned Throughput deployments.

Strategy:
  - Quota-aware: checks subscription quota BEFORE attempting deploys; if quota
    is 100% used for a SKU, skips it immediately and redirects to fallback
  - Greedy increments: tries large chunks first (+50, +25, +15, +10, +5)
  - DataZone-aware: DataZone/Global SKUs share one capacity pool per zone,
    so only one region endpoint is hit
  - Capacity pre-check: queries Model Capacities API before blind PUT attempts
  - Cross-SKU fallback: DataZone PTU -> Global PTU -> TPM
  - 409 parsing: distinguishes quota-exhausted from no-physical-capacity
  - Reservation alerting: reminds to purchase reservation when PTUs land

Usage:
  1. Deploy as Azure Function with Timer Trigger
  2. Configure environment variables (see REQUIRED ENV VARS below)
  3. Ensure managed identity has Cognitive Services Contributor on the resource
  4. Monitor logs for SUCCESS alerts
  5. DISABLE immediately when target is reached

REQUIRED ENV VARS:
  AZURE_SUBSCRIPTION_ID     - Your Azure subscription ID
  AZURE_RESOURCE_GROUP      - Resource group containing the Foundry resource
  AZURE_ACCOUNT_NAME        - Foundry resource name (e.g. my-ai-services-account)
  PTU_TARGET                - Total PTU target (default: 74)
  PTU_MODEL_NAME            - Model name (default: gpt-5.2)
  PTU_MODEL_VERSION         - Model version (default: 2025-12-11)
  PTU_SKU_NAME              - SKU name (default: DataZoneProvisionedManaged)
  PTU_MAX_DEPLOYMENTS       - Max parallel deployments (default: 4)
  TEAMS_WEBHOOK_URL         - (Optional) Teams webhook for success alerts
"""

import os
import json
import logging
import requests
from azure.identity import DefaultAzureCredential

# ---------------------------------------------------------------------------
# Configuration from environment variables (with safe defaults)
# ---------------------------------------------------------------------------
SUBSCRIPTION_ID = os.environ.get("AZURE_SUBSCRIPTION_ID", "")
RESOURCE_GROUP = os.environ.get("AZURE_RESOURCE_GROUP", "")
ACCOUNT_NAME = os.environ.get("AZURE_ACCOUNT_NAME", "")
API_VERSION = "2024-10-01-preview"

MODEL_NAME = os.environ.get("PTU_MODEL_NAME", "gpt-5.2")
MODEL_VERSION = os.environ.get("PTU_MODEL_VERSION", "2025-12-11")

# PTU (Provisioned) config
PTU_SKU_NAME = os.environ.get("PTU_SKU_NAME", "DataZoneProvisionedManaged")
SKU_NAME = PTU_SKU_NAME  # Backward compat
TARGET_PTUS = int(os.environ.get("PTU_TARGET", "74"))
MIN_PTU = 15          # Minimum deployment size for DZ Provisioned
INCREMENT = 5         # Smallest scale increment (snipe unit) for DZ Provisioned
# Greedy increments: try large chunks first, fall back to smaller
GREEDY_INCREMENTS = [50, 25, 15, 10, 5]
MAX_DEPLOYMENTS = int(os.environ.get("PTU_MAX_DEPLOYMENTS", "4"))
DEPLOYMENT_PREFIX = "gpt52-ptu-accum"

# Cross-SKU fallback: if primary PTU SKU has no capacity, try these in order
# Only used when CROSS_SKU_FALLBACK is enabled
CROSS_SKU_FALLBACK_ENABLED = os.environ.get("CROSS_SKU_FALLBACK", "true").lower() == "true"
FALLBACK_SKU_CHAIN = {
    "DataZoneProvisionedManaged": ["GlobalProvisionedManaged"],
    "GlobalProvisionedManaged": ["DataZoneProvisionedManaged"],
    # Regional SKUs don't have a PTU fallback
    "ProvisionedManaged": [],
}

# Multi-region config -- JSON list of {account, resource_group, region} targets
# If set, the sniper cycles through all targets each run.
# Format: [{"account":"name1","rg":"rg1","region":"swedencentral"},...]
SNIPE_TARGETS_JSON = os.environ.get("SNIPE_TARGETS", "")
DATA_ZONE = os.environ.get("DATA_ZONE", "eu")  # eu, us, all
SELECTED_REGIONS_JSON = os.environ.get("SELECTED_REGIONS", "")  # JSON list of region names

# TPM (Standard / pay-per-token) config -- fallback if PTU unavailable
# NOTE: Data Zone Standard is NOT available for GPT-5.2 in EU regions.
# Use Regional Standard ("Standard") instead for EU-compliant TPM fallback.
TPM_SKU_NAME = os.environ.get("TPM_SKU_NAME", "Standard")  # Regional Standard (EU-compliant)
TPM_DEPLOYMENT_NAME = os.environ.get("TPM_DEPLOYMENT_NAME", "gpt52-tpm-fallback")
TPM_CAPACITY = int(os.environ.get("TPM_CAPACITY", "300"))  # TPM quota in thousands (300 = 300K TPM)
TPM_ENABLED = os.environ.get("TPM_ENABLED", "true").lower() == "true"

# EU Data Zone Standard availability flag
# Set to "true" only if Data Zone Standard becomes available for the target model in EU
DZ_STANDARD_AVAILABLE_EU = os.environ.get("DZ_STANDARD_AVAILABLE_EU", "false").lower() == "true"
TEAMS_WEBHOOK_URL = os.environ.get("TEAMS_WEBHOOK_URL", "")
SNIPE_ALL_EACH_CYCLE = True  # Try every deployment every cycle for max aggressiveness


# ---------------------------------------------------------------------------
# Validation — fail fast if misconfigured
# ---------------------------------------------------------------------------
def validate_config() -> list[str]:
    """Return list of configuration errors. Empty list = valid."""
    errors = []
    if not SUBSCRIPTION_ID:
        errors.append("AZURE_SUBSCRIPTION_ID is not set")
    if not RESOURCE_GROUP:
        errors.append("AZURE_RESOURCE_GROUP is not set")
    if not ACCOUNT_NAME:
        errors.append("AZURE_ACCOUNT_NAME is not set")
    if TARGET_PTUS < MIN_PTU:
        errors.append(f"PTU_TARGET ({TARGET_PTUS}) is below minimum ({MIN_PTU})")
    if TARGET_PTUS > 1000:
        errors.append(f"PTU_TARGET ({TARGET_PTUS}) seems unreasonably high — verify")
    if MAX_DEPLOYMENTS < 1 or MAX_DEPLOYMENTS > 10:
        errors.append(f"PTU_MAX_DEPLOYMENTS ({MAX_DEPLOYMENTS}) must be 1-10")
    if INCREMENT < 1:
        errors.append(f"INCREMENT ({INCREMENT}) must be at least 1")
    if MODEL_NAME not in ("gpt-5.2", "gpt-5.1", "gpt-5.4", "gpt-5", "gpt-4.1"):
        errors.append(f"PTU_MODEL_NAME ({MODEL_NAME}) not in known model list — verify")
    return errors


def is_zone_pooled_sku(sku_name: str) -> bool:
    """Check if SKU uses a shared capacity pool (DataZone/Global).
    For these SKUs, capacity shown in multiple regions is the SAME pool.
    Multi-region sniping is pointless — one endpoint is enough.
    Regional SKUs (ProvisionedManaged, Standard) have independent per-region pools."""
    return sku_name in ("DataZoneProvisionedManaged", "GlobalProvisionedManaged",
                        "DataZoneStandard")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def get_headers() -> dict[str, str]:
    """Get authorization headers using managed identity or local credentials."""
    credential = DefaultAzureCredential()
    token = credential.get_token("https://management.azure.com/.default").token
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# Deployment helpers
# ---------------------------------------------------------------------------
def _deployment_url(account: str, rg: str, deployment_name: str) -> str:
    return (
        f"https://management.azure.com/subscriptions/{SUBSCRIPTION_ID}"
        f"/resourceGroups/{rg}"
        f"/providers/Microsoft.CognitiveServices/accounts/{account}"
        f"/deployments/{deployment_name}"
        f"?api-version={API_VERSION}"
    )


def get_current_ptus(headers: dict, account: str, rg: str, deployment_name: str) -> int:
    """Check if deployment exists and return current PTU count. Returns 0 if not found."""
    url = _deployment_url(account, rg, deployment_name)
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            capacity = data.get("sku", {}).get("capacity", 0)
            return int(capacity)
        if resp.status_code == 404:
            return 0
        logging.warning(
            "Unexpected GET status %d for %s: %s",
            resp.status_code, deployment_name, resp.text[:200],
        )
    except (requests.exceptions.RequestException, Exception) as exc:
        logging.error("GET request failed for %s: %s", deployment_name, exc)
    return 0


def attempt_deployment(
    headers: dict, account: str, rg: str, deployment_name: str, ptus: int,
    sku_override: str = None,
) -> tuple[int, str]:
    """
    Try to create or update deployment with specified PTU count.
    Returns (status_code, response_text).
    sku_override: if set, use this SKU instead of the default (for cross-SKU fallback).
    """
    url = _deployment_url(account, rg, deployment_name)
    body = {
        "sku": {
            "name": sku_override or SKU_NAME,
            "capacity": ptus,
        },
        "properties": {
            "model": {
                "format": "OpenAI",
                "name": MODEL_NAME,
                "version": MODEL_VERSION,
            }
        },
    }
    try:
        resp = requests.put(url, headers=headers, json=body, timeout=60)
        return resp.status_code, resp.text[:500]
    except (requests.exceptions.RequestException, Exception) as exc:
        logging.error("PUT request failed for %s: %s", deployment_name, exc)
        return 0, str(exc)


def attempt_tpm_deployment(
    headers: dict, account: str, rg: str, deployment_name: str, capacity: int
) -> tuple[int, str]:
    """
    Try to create a TPM (pay-per-token) deployment as fallback.
    Uses Regional Standard by default (EU-compliant).
    Only uses Data Zone Standard if DZ_STANDARD_AVAILABLE_EU is true.
    Returns (status_code, response_text).
    """
    sku = TPM_SKU_NAME
    # Safety check: if someone set DataZoneStandard but EU flag is off, override to Standard
    if sku == "DataZoneStandard" and not DZ_STANDARD_AVAILABLE_EU:
        logging.warning(
            "DataZoneStandard not available in EU for this model. "
            "Falling back to Regional Standard."
        )
        sku = "Standard"

    url = _deployment_url(account, rg, deployment_name)
    body = {
        "sku": {
            "name": sku,
            "capacity": capacity,
        },
        "properties": {
            "model": {
                "format": "OpenAI",
                "name": MODEL_NAME,
                "version": MODEL_VERSION,
            }
        },
    }
    try:
        resp = requests.put(url, headers=headers, json=body, timeout=60)
        return resp.status_code, resp.text[:500]
    except (requests.exceptions.RequestException, Exception) as exc:
        logging.error("TPM PUT request failed for %s: %s", deployment_name, exc)
        return 0, str(exc)


def get_total_ptus(headers: dict, account: str, rg: str) -> tuple[int, dict[str, int]]:
    """
    Sum PTUs across all accumulator deployments.
    Returns (total, {deployment_name: ptus}).
    """
    breakdown: dict[str, int] = {}
    total = 0
    for i in range(MAX_DEPLOYMENTS):
        name = f"{DEPLOYMENT_PREFIX}-{i}"
        current = get_current_ptus(headers, account, rg, name)
        if current > 0:
            breakdown[name] = current
            total += current
    return total, breakdown


# ---------------------------------------------------------------------------
# Capacity pre-check via Model Capacities API
# ---------------------------------------------------------------------------
def check_available_capacity(headers: dict) -> dict:
    """Query Model Capacities API to check available PTU capacity.
    Returns dict with 'available' (int), 'sku_available' (dict of sku->capacity),
    and 'checked' (bool indicating if API call succeeded)."""
    result = {"available": -1, "sku_available": {}, "checked": False}
    try:
        url = (
            f"https://management.azure.com/subscriptions/{SUBSCRIPTION_ID}"
            f"/providers/Microsoft.CognitiveServices/modelCapacities"
            f"?api-version=2024-06-01-preview"
            f"&modelFormat=OpenAI"
            f"&modelName={MODEL_NAME}"
            f"&modelVersion={MODEL_VERSION}"
        )
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code != 200:
            logging.warning("Capacity pre-check API returned %d", resp.status_code)
            return result

        data = resp.json()
        result["checked"] = True

        for item in data.get("value", []):
            sku_info = item.get("skuName", "")
            avail = item.get("properties", {}).get("availableCapacity", 0)
            if sku_info:
                if sku_info not in result["sku_available"]:
                    result["sku_available"][sku_info] = 0
                result["sku_available"][sku_info] += int(avail)

        # Primary SKU availability
        result["available"] = result["sku_available"].get(PTU_SKU_NAME, 0)
        return result
    except Exception as e:
        logging.warning("Capacity pre-check failed: %s", str(e))
        return result


# ---------------------------------------------------------------------------
# Quota pre-check via Usages API
# ---------------------------------------------------------------------------
def check_quota(headers: dict) -> dict:
    """Query the Cognitive Services Usages API to check quota limits per SKU.
    Returns dict with:
      'checked': bool — whether the API call succeeded
      'quotas': dict of sku_name -> {'used': int, 'limit': int, 'pct': float}
      'blocked_skus': list of SKU names that are 100% used (quota ceiling)
    """
    result = {"checked": False, "quotas": {}, "blocked_skus": []}
    try:
        url = (
            f"https://management.azure.com/subscriptions/{SUBSCRIPTION_ID}"
            f"/providers/Microsoft.CognitiveServices/locations/"
            f"{DATA_ZONE}zone/usages"
            f"?api-version=2024-10-01-preview"
        )
        # Try the usages API — this may fail if location format is wrong
        # Fall back to account-level usages if subscription-level doesn't work
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code != 200:
            # Try account-level usages instead
            url = (
                f"https://management.azure.com/subscriptions/{SUBSCRIPTION_ID}"
                f"/resourceGroups/{RESOURCE_GROUP}"
                f"/providers/Microsoft.CognitiveServices/accounts/{ACCOUNT_NAME}"
                f"/usages?api-version={API_VERSION}"
            )
            resp = requests.get(url, headers=headers, timeout=30)
            if resp.status_code != 200:
                logging.warning("Quota check API returned %d", resp.status_code)
                return result

        data = resp.json()
        result["checked"] = True

        for item in data.get("value", []):
            name_obj = item.get("name", {})
            display_name = name_obj.get("localizedValue", "") or name_obj.get("value", "")
            current = int(item.get("currentValue", 0))
            limit = int(item.get("limit", 0))
            pct = (current / limit * 100) if limit > 0 else 0

            # Map display names to SKU names
            sku_map = {
                "DataZoneProvisionedManaged": "DataZoneProvisionedManaged",
                "Data Zone Provisioned Managed": "DataZoneProvisionedManaged",
                "GlobalProvisionedManaged": "GlobalProvisionedManaged",
                "Global Provisioned Managed": "GlobalProvisionedManaged",
                "ProvisionedManaged": "ProvisionedManaged",
                "Provisioned Managed": "ProvisionedManaged",
            }
            for pattern, sku in sku_map.items():
                if pattern.lower() in display_name.lower():
                    result["quotas"][sku] = {
                        "used": current, "limit": limit, "pct": round(pct, 1),
                        "display_name": display_name,
                    }
                    if pct >= 100 and limit > 0:
                        if sku not in result["blocked_skus"]:
                            result["blocked_skus"].append(sku)
                    break

        return result
    except Exception as e:
        logging.warning("Quota check failed: %s", str(e))
        return result


def parse_409_reason(response_text: str) -> str:
    """Parse a 409 response body to determine the reason.
    Returns 'quota_exceeded', 'no_capacity', or 'unknown'."""
    text_lower = response_text.lower()
    if "quota" in text_lower or "limit" in text_lower or "exceeded" in text_lower:
        return "quota_exceeded"
    if "capacity" in text_lower or "insufficient" in text_lower:
        return "no_capacity"
    return "unknown"


# ---------------------------------------------------------------------------
# Alerting
# ---------------------------------------------------------------------------
def send_teams_alert(message: str) -> None:
    """Send alert to Teams webhook if configured."""
    if not TEAMS_WEBHOOK_URL:
        logging.info("No TEAMS_WEBHOOK_URL configured — skipping alert")
        return
    payload = {
        "text": message,
    }
    try:
        resp = requests.post(TEAMS_WEBHOOK_URL, json=payload, timeout=10)
        if resp.status_code == 200:
            logging.info("Teams alert sent successfully")
        else:
            logging.warning("Teams alert failed: %d %s", resp.status_code, resp.text[:100])
    except requests.exceptions.RequestException as exc:
        logging.warning("Teams alert request failed: %s", exc)


# ---------------------------------------------------------------------------
# Main accumulation logic
# ---------------------------------------------------------------------------
def run_accumulator() -> dict:
    """
    Core logic. Can be called from Azure Function trigger or standalone.
    Returns a result dict with status and details.
    """
    # Validate config
    errors = validate_config()
    if errors:
        error_msg = "Configuration errors: " + "; ".join(errors)
        logging.error(error_msg)
        return {"status": "error", "message": error_msg}

    headers = get_headers()

    # Check current state
    total_landed, breakdown = get_total_ptus(headers, ACCOUNT_NAME, RESOURCE_GROUP)
    remaining = TARGET_PTUS - total_landed

    logging.info(
        "Current state: %d/%d PTU landed (%d remaining). Breakdown: %s",
        total_landed, TARGET_PTUS, remaining, json.dumps(breakdown),
    )

    if remaining <= 0:
        msg = (
            f"🎯 TARGET REACHED — {total_landed}/{TARGET_PTUS} PTU landed. "
            f"DISABLE THIS FUNCTION. Breakdown: {json.dumps(breakdown)}"
        )
        logging.critical(msg)
        send_teams_alert(msg)
        return {"status": "target_reached", "total": total_landed, "breakdown": breakdown}

    # --- QUOTA PRE-CHECK: Are we blocked by quota ceiling? ---
    quota_info = check_quota(headers)
    primary_quota_blocked = False
    if quota_info["checked"]:
        primary_q = quota_info["quotas"].get(PTU_SKU_NAME)
        if primary_q:
            logging.info(
                "Quota check: %s = %d/%d PTU (%.1f%%). %s",
                PTU_SKU_NAME, primary_q["used"], primary_q["limit"], primary_q["pct"],
                "⛔ BLOCKED — quota exhausted!" if primary_q["pct"] >= 100 else "✅ Quota available.",
            )
            if primary_q["pct"] >= 100:
                primary_quota_blocked = True
                send_teams_alert(
                    f"⚠️ QUOTA BLOCKED — {PTU_SKU_NAME} is at {primary_q['used']}/{primary_q['limit']} PTU "
                    f"(100%). Sniper cannot add more without a quota increase. "
                    f"Request increase at: https://aka.ms/oai/quotaincrease"
                )
        for sku, q in quota_info["quotas"].items():
            if sku != PTU_SKU_NAME:
                logging.info("  Quota: %s = %d/%d (%.1f%%)", sku, q["used"], q["limit"], q["pct"])
    else:
        logging.info("Quota pre-check unavailable — will attempt deploys and parse 409 responses.")

    # --- CAPACITY PRE-CHECK: Query available capacity before blind PUTs ---
    capacity_info = check_available_capacity(headers)
    if capacity_info["checked"]:
        avail = capacity_info["available"]
        logging.info(
            "Capacity pre-check: %d PTU available for %s. All SKUs: %s",
            avail, PTU_SKU_NAME, json.dumps(capacity_info["sku_available"]),
        )
        if avail == 0:
            logging.info("No capacity available for %s — checking fallback SKUs.", PTU_SKU_NAME)
    else:
        logging.info("Capacity pre-check unavailable — proceeding with blind attempts.")

    # GREEDY SNIPING: Try every deployment every cycle.
    # For existing deployments, try large increments first (+50, +25, +15, +10, +5).
    # For empty slots, attempt to create at minimum size (15 PTU).
    # This maximizes the grab when capacity appears.
    actions_taken = []

    def _try_deploy(dep_name, current_ptus, sku_override=None):
        """Try deploying with greedy increments. Returns (gained, action_dict) or (0, None)."""
        nonlocal total_landed, remaining
        orig_sku = SKU_NAME
        actual_sku = sku_override or SKU_NAME

        if current_ptus == 0:
            # New deployment — must meet minimum (15 PTU)
            attempt_size = MIN_PTU
            action = "create"
            if sku_override:
                action = "create_fallback_" + actual_sku

            # Cap at target
            if total_landed + attempt_size > TARGET_PTUS:
                return 0, None

            logging.info(
                "Attempting %s on %s: 0 -> %d PTU (SKU: %s)",
                action, dep_name, attempt_size, actual_sku,
            )

            status_code, resp_text = attempt_deployment(
                headers, ACCOUNT_NAME, RESOURCE_GROUP, dep_name, attempt_size,
                sku_override=sku_override,
            )

            if status_code in (200, 201):
                total_landed += attempt_size
                remaining -= attempt_size
                msg = (
                    f"🎯 SNIPED — {dep_name}: 0 -> {attempt_size} PTU (+{attempt_size}). "
                    f"Total: {total_landed}/{TARGET_PTUS} (SKU: {actual_sku})"
                )
                logging.critical(msg)
                send_teams_alert(msg)
                return attempt_size, {
                    "deployment": dep_name, "action": action,
                    "previous": 0, "new": attempt_size,
                    "gained": attempt_size, "sku": actual_sku,
                }
            elif status_code == 409:
                reason = parse_409_reason(resp_text)
                if reason == "quota_exceeded":
                    logging.warning("  %s — QUOTA EXCEEDED (409). Need quota increase.", dep_name)
                else:
                    logging.info("  %s @ %d PTU — no capacity (409).", dep_name, attempt_size)
            else:
                logging.warning("  %s — unexpected %d: %s", dep_name, status_code, resp_text[:200])
            return 0, None
        else:
            # Existing deployment — try greedy increments (largest first)
            for inc in GREEDY_INCREMENTS:
                if remaining <= 0:
                    break
                attempt_size = current_ptus + inc
                # Don't overshoot target
                if total_landed + inc > TARGET_PTUS:
                    continue
                # Round to INCREMENT boundary
                if inc % INCREMENT != 0:
                    continue

                action = "snipe"
                if sku_override:
                    action = "snipe_fallback_" + actual_sku

                logging.info(
                    "Attempting %s on %s: %d -> %d PTU (+%d, SKU: %s)",
                    action, dep_name, current_ptus, attempt_size, inc, actual_sku,
                )

                status_code, resp_text = attempt_deployment(
                    headers, ACCOUNT_NAME, RESOURCE_GROUP, dep_name, attempt_size,
                    sku_override=sku_override,
                )

                if status_code in (200, 201):
                    total_landed += inc
                    remaining -= inc
                    msg = (
                        f"🎯 SNIPED — {dep_name}: {current_ptus} -> {attempt_size} PTU (+{inc}). "
                        f"Total: {total_landed}/{TARGET_PTUS} (SKU: {actual_sku})"
                    )
                    logging.critical(msg)
                    send_teams_alert(msg)
                    return inc, {
                        "deployment": dep_name, "action": action,
                        "previous": current_ptus, "new": attempt_size,
                        "gained": inc, "sku": actual_sku,
                    }
                elif status_code == 409:
                    reason = parse_409_reason(resp_text)
                    if reason == "quota_exceeded":
                        logging.warning(
                            "  %s — QUOTA EXCEEDED (409). Need quota increase for %s.",
                            dep_name, actual_sku,
                        )
                        break  # No point trying smaller increments if quota is the issue
                    logging.info(
                        "  %s @ %d PTU — no capacity (409). Trying smaller increment.",
                        dep_name, attempt_size,
                    )
                    continue  # Try next smaller increment
                else:
                    logging.warning("  %s — unexpected %d: %s", dep_name, status_code, resp_text[:200])
                    break  # Don't retry on unexpected errors
            return 0, None

    # Skip primary SKU entirely if quota is blocked — go straight to fallback
    if primary_quota_blocked:
        logging.warning(
            "Skipping %s — quota is 100%% used. Will try cross-SKU fallback.",
            PTU_SKU_NAME,
        )
    else:
        for i in range(MAX_DEPLOYMENTS):
            if remaining <= 0:
                break

            name = f"{DEPLOYMENT_PREFIX}-{i}"
            current = get_current_ptus(headers, ACCOUNT_NAME, RESOURCE_GROUP, name)

            gained, action_dict = _try_deploy(name, current)
            if action_dict:
                actions_taken.append(action_dict)
                if remaining <= 0:
                    target_msg = (
                        f"🎯 TARGET REACHED — {total_landed}/{TARGET_PTUS} PTU. "
                        f"DISABLE THIS FUNCTION."
                    )
                    logging.critical(target_msg)
                    send_teams_alert(target_msg)
                    break

    # --- CROSS-SKU FALLBACK: If primary SKU got nothing (or was blocked), try fallback SKUs ---
    ptu_landed_this_cycle = sum(a.get("gained", 0) for a in actions_taken if a.get("action", "") != "tpm_fallback")
    if (ptu_landed_this_cycle == 0 or primary_quota_blocked) and CROSS_SKU_FALLBACK_ENABLED and remaining > 0:
        fallback_skus = FALLBACK_SKU_CHAIN.get(PTU_SKU_NAME, [])
        for fallback_sku in fallback_skus:
            if remaining <= 0:
                break
            # Check if fallback SKU is also quota-blocked
            if quota_info["checked"] and fallback_sku in quota_info["blocked_skus"]:
                fb_q = quota_info["quotas"].get(fallback_sku, {})
                logging.warning(
                    "Fallback SKU %s also quota-blocked (%d/%d). Skipping.",
                    fallback_sku, fb_q.get("used", 0), fb_q.get("limit", 0),
                )
                continue
            # Check if fallback has capacity (if pre-check data available)
            if capacity_info["checked"]:
                fb_avail = capacity_info["sku_available"].get(fallback_sku, 0)
                if fb_avail == 0:
                    logging.info("Fallback SKU %s has 0 capacity — skipping.", fallback_sku)
                    continue
                logging.info("Fallback SKU %s has %d capacity — attempting.", fallback_sku, fb_avail)

            for i in range(MAX_DEPLOYMENTS):
                if remaining <= 0:
                    break
                fb_name = f"{DEPLOYMENT_PREFIX}-fb-{i}"
                fb_current = get_current_ptus(headers, ACCOUNT_NAME, RESOURCE_GROUP, fb_name)
                gained, action_dict = _try_deploy(fb_name, fb_current, sku_override=fallback_sku)
                if action_dict:
                    actions_taken.append(action_dict)
                    if remaining <= 0:
                        target_msg = (
                            f"🎯 TARGET REACHED — {total_landed}/{TARGET_PTUS} PTU (via {fallback_sku}). "
                            f"DISABLE THIS FUNCTION."
                        )
                        logging.critical(target_msg)
                        send_teams_alert(target_msg)
                        break

    # --- TPM FALLBACK: If no PTU capacity landed this cycle, try Regional Standard ---
    ptu_landed_this_cycle = sum(a.get("gained", 0) for a in actions_taken if a.get("action", "") != "tpm_fallback")
    if ptu_landed_this_cycle == 0 and TPM_ENABLED and remaining > 0:
        logging.info(
            "No PTU capacity available this cycle. Attempting TPM fallback (%s)...",
            TPM_SKU_NAME if (TPM_SKU_NAME != "DataZoneStandard" or DZ_STANDARD_AVAILABLE_EU)
            else "Standard (Regional)",
        )

        # Check if TPM deployment already exists
        tpm_current = get_current_ptus(headers, ACCOUNT_NAME, RESOURCE_GROUP, TPM_DEPLOYMENT_NAME)
        if tpm_current > 0:
            logging.info(
                "TPM fallback deployment %s already exists with capacity %d. Skipping.",
                TPM_DEPLOYMENT_NAME, tpm_current,
            )
        else:
            tpm_status, tpm_text = attempt_tpm_deployment(
                headers, ACCOUNT_NAME, RESOURCE_GROUP, TPM_DEPLOYMENT_NAME, TPM_CAPACITY,
            )
            if tpm_status in (200, 201):
                msg = (
                    f"🎯 TPM SNIPED — {TPM_DEPLOYMENT_NAME}: Regional Standard "
                    f"with {TPM_CAPACITY}K TPM capacity. "
                    f"PTU target still {remaining}/{TARGET_PTUS} remaining."
                )
                logging.critical(msg)
                send_teams_alert(msg)
                actions_taken.append({
                    "deployment": TPM_DEPLOYMENT_NAME,
                    "action": "tpm_fallback",
                    "previous": 0,
                    "new": TPM_CAPACITY,
                    "gained": TPM_CAPACITY,
                    "type": "tpm",
                })
            elif tpm_status == 409:
                logging.info(
                    "TPM fallback %s — no capacity or not available (409).",
                    TPM_DEPLOYMENT_NAME,
                )
            else:
                logging.warning(
                    "TPM fallback %s — unexpected %d: %s",
                    TPM_DEPLOYMENT_NAME, tpm_status, tpm_text[:200],
                )

    # --- RESERVATION REMINDER: If PTUs were acquired, remind about cost savings ---
    ptu_landed_this_cycle = sum(a.get("gained", 0) for a in actions_taken
                                if a.get("action", "") != "tpm_fallback")
    if total_landed >= MIN_PTU and ptu_landed_this_cycle > 0:
        send_teams_alert(
            f"💰 RESERVATION REMINDER — You now have {total_landed} PTU deployed. "
            f"PTUs are eligible for Azure Reservations (1-month or 1-year) "
            f"which can save 20-63%% vs hourly billing. "
            f"Purchase at: https://portal.azure.com/#view/Microsoft_Azure_Reservations"
        )

    return {
        "status": "completed_cycle",
        "total_landed": total_landed,
        "target": TARGET_PTUS,
        "remaining": max(0, remaining),
        "actions": actions_taken,
        "breakdown": breakdown,
        "capacity_info": capacity_info if capacity_info["checked"] else None,
        "quota_info": quota_info if quota_info["checked"] else None,
        "primary_quota_blocked": primary_quota_blocked,
    }


def _parse_targets():
    """Parse multi-region targets from env var or fall back to single account."""
    if SNIPE_TARGETS_JSON:
        try:
            return json.loads(SNIPE_TARGETS_JSON)
        except Exception:
            pass
    # Fall back to single account from env vars
    if ACCOUNT_NAME and RESOURCE_GROUP:
        return [{"account": ACCOUNT_NAME, "rg": RESOURCE_GROUP, "region": "default"}]
    return []


def run_multi_region():
    """
    Run snipe cycle across multiple region/account targets.
    DataZone-aware: for DataZone/Global SKUs, only hits one region endpoint
    since all regions share the same capacity pool.
    For regional SKUs (ProvisionedManaged), cycles through each target.
    Returns combined result with per-region breakdown.
    """
    targets = _parse_targets()
    if not targets:
        return run_accumulator()  # Fall back to single account

    # DataZone-aware: if SKU uses a shared pool, only hit one target
    if is_zone_pooled_sku(PTU_SKU_NAME) and len(targets) > 1:
        logging.info(
            "SKU %s uses shared capacity pool — hitting only first target "
            "(multi-region is redundant for DataZone/Global SKUs).",
            PTU_SKU_NAME,
        )
        targets = targets[:1]

    global ACCOUNT_NAME, RESOURCE_GROUP
    original_account = ACCOUNT_NAME
    original_rg = RESOURCE_GROUP

    all_actions = []
    total_landed_global = 0
    regions_tried = []

    for target in targets:
        account = target.get("account", "")
        rg = target.get("rg", "")
        region = target.get("region", "unknown")

        if not account or not rg:
            continue

        regions_tried.append(region)

        # Temporarily swap globals for this target
        ACCOUNT_NAME = account
        RESOURCE_GROUP = rg

        logging.info("Sniping region %s (account: %s)...", region, account)

        try:
            result = run_accumulator()
            for action in result.get("actions", []):
                action["region"] = region
                action["account"] = account
                all_actions.append(action)
            total_landed_global += result.get("total_landed", 0)
        except Exception as e:
            logging.error("Error sniping %s: %s", region, str(e))
            all_actions.append({
                "deployment": "error",
                "action": "error",
                "region": region,
                "account": account,
                "previous": 0,
                "new": 0,
                "gained": 0,
                "error": str(e),
            })

    # Restore originals
    ACCOUNT_NAME = original_account
    RESOURCE_GROUP = original_rg

    combined = {
        "status": "completed_cycle",
        "total_landed": total_landed_global,
        "target": TARGET_PTUS,
        "remaining": max(0, TARGET_PTUS - total_landed_global),
        "actions": all_actions,
        "regions_tried": regions_tried,
        "targets_count": len(targets),
    }

    # Log to history if available
    try:
        import snipe_history
        snipe_history.log_cycle(combined, regions_tried)
    except Exception:
        pass  # History logging is best-effort

    return combined


# ---------------------------------------------------------------------------
# Azure Function entry point (v1 programming model — uses function.json)
# ---------------------------------------------------------------------------
def main(timer) -> None:
    """Azure Function timer trigger -- runs every 5 minutes.
    Entry point for v1 programming model (function.json + scriptFile)."""
    try:
        import azure.functions as func
        if hasattr(timer, 'past_due') and timer.past_due:
            logging.warning("Timer is past due -- running anyway")
    except ImportError:
        pass
    result = run_multi_region()
    logging.info("Cycle result: %s", json.dumps(result, default=str))


# ---------------------------------------------------------------------------
# Standalone execution (for testing or cron/task scheduler)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    # Quick config validation
    errors = validate_config()
    if errors:
        print("❌ Configuration errors:")
        for e in errors:
            print(f"   - {e}")
        print("\nSet required environment variables before running.")
        exit(1)

    print(f"PTU Accumulator — Target: {TARGET_PTUS} PTU")
    print(f"Model: {MODEL_NAME} ({MODEL_VERSION})")
    print(f"SKU: {SKU_NAME}")
    print(f"Resource: {ACCOUNT_NAME} in {RESOURCE_GROUP}")
    print(f"Max deployments: {MAX_DEPLOYMENTS}")
    print()

    result = run_accumulator()
    print(json.dumps(result, indent=2))
