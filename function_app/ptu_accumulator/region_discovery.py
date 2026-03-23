"""
Region discovery for PTU Capacity Sniper.
Queries the Azure Model Capacities API to find which regions support a given model + SKU.
Falls back to a static mapping from Microsoft Learn docs if the API fails.
"""
import os
import logging
import requests
from azure.identity import DefaultAzureCredential


# EU and US region mappings
EU_REGIONS = {
    "swedencentral", "westeurope", "francecentral", "germanywestcentral",
    "polandcentral", "spaincentral", "italynorth", "norwayeast",
    "uksouth", "switzerlandnorth", "switzerlandwest", "northeurope",
}

US_REGIONS = {
    "eastus", "eastus2", "westus", "westus3", "centralus",
    "northcentralus", "southcentralus",
}

# Default top 3 per zone
EU_DEFAULTS = ["swedencentral", "westeurope", "francecentral"]
US_DEFAULTS = ["eastus2", "centralus", "southcentralus"]

# Static fallback: known GPT-5.2 Data Zone Provisioned EU regions (from our research)
STATIC_REGIONS = {
    "gpt-5.2": {
        "DataZoneProvisionedManaged": {
            "eu": ["swedencentral", "westeurope", "francecentral", "germanywestcentral",
                   "polandcentral", "spaincentral", "italynorth"],
            "us": ["eastus", "eastus2", "centralus", "northcentralus",
                   "southcentralus", "westus", "westus3"],
        },
        "GlobalProvisionedManaged": {
            "eu": ["swedencentral", "westeurope", "francecentral", "germanywestcentral",
                   "polandcentral", "spaincentral", "italynorth", "norwayeast",
                   "uksouth", "switzerlandnorth"],
            "us": ["eastus", "eastus2", "centralus", "northcentralus",
                   "southcentralus", "westus", "westus3"],
        },
        "Standard": {
            "eu": ["swedencentral", "francecentral"],
            "us": ["eastus", "eastus2", "centralus"],
        },
    },
    "gpt-5.1": {
        "DataZoneProvisionedManaged": {
            "eu": ["swedencentral", "westeurope", "francecentral", "germanywestcentral",
                   "polandcentral", "spaincentral", "italynorth"],
            "us": ["eastus", "eastus2", "centralus", "northcentralus",
                   "southcentralus", "westus", "westus3"],
        },
    },
}


def discover_regions_api(subscription_id, model_name, model_version, sku_name, zone="eu"):
    """
    Query the Model Capacities API to find available regions.
    Returns list of region names, or empty list on failure.
    """
    try:
        credential = DefaultAzureCredential()
        token = credential.get_token("https://management.azure.com/.default").token
        headers = {"Authorization": "Bearer " + token}

        url = (
            "https://management.azure.com/subscriptions/" + subscription_id
            + "/providers/Microsoft.CognitiveServices/modelCapacities"
            + "?api-version=2024-06-01-preview"
            + "&modelFormat=OpenAI"
            + "&modelName=" + model_name
            + "&modelVersion=" + model_version
        )

        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code != 200:
            logging.warning("Model Capacities API returned %d: %s", resp.status_code, resp.text[:200])
            return []

        data = resp.json()
        regions = []
        zone_filter = EU_REGIONS if zone == "eu" else US_REGIONS if zone == "us" else None

        for item in data.get("value", []):
            location = item.get("location", "").lower().replace(" ", "")
            sku_info = item.get("skuName", "")
            # Check if the SKU matches
            if sku_name and sku_info and sku_name.lower() not in sku_info.lower():
                # Try matching by deployment type in properties
                props = item.get("properties", {})
                deployment_type = props.get("skuName", "")
                if sku_name.lower() not in deployment_type.lower():
                    continue
            if zone_filter and location not in zone_filter:
                continue
            if location and location not in regions:
                regions.append(location)

        return sorted(regions)
    except Exception as e:
        logging.warning("Model Capacities API failed: %s", str(e))
        return []


def discover_regions(subscription_id="", model_name="gpt-5.2", model_version="2025-12-11",
                     sku_name="DataZoneProvisionedManaged", zone="eu"):
    """
    Discover available regions for a model + SKU + data zone.
    Tries the API first, falls back to static mapping.
    Returns (regions_list, source) where source is 'api' or 'static'.
    """
    # Try API first
    if subscription_id:
        api_regions = discover_regions_api(subscription_id, model_name, model_version, sku_name, zone)
        if api_regions:
            return api_regions, "api"

    # Fall back to static mapping
    model_map = STATIC_REGIONS.get(model_name, {})
    sku_map = model_map.get(sku_name, {})
    static_regions = sku_map.get(zone, [])

    if static_regions:
        return static_regions, "static"

    # Last resort: return all known regions for the zone
    if zone == "eu":
        return sorted(list(EU_REGIONS)), "fallback"
    elif zone == "us":
        return sorted(list(US_REGIONS)), "fallback"
    else:
        return sorted(list(EU_REGIONS | US_REGIONS)), "fallback"


def get_default_regions(zone="eu", count=3):
    """Return the default top N regions for a zone."""
    if zone == "eu":
        return EU_DEFAULTS[:count]
    elif zone == "us":
        return US_DEFAULTS[:count]
    else:
        return (EU_DEFAULTS + US_DEFAULTS)[:count]
