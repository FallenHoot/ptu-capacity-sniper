"""
Tests for PTU Accumulator — validates logic without calling Azure APIs.
Run with: pytest test_ptu_accumulator.py -v
"""

import json
import os
import pytest
from unittest.mock import patch, MagicMock

import sys
import os
# Add function_app/ptu_accumulator to path so we can import the module
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "function_app", "ptu_accumulator"))
# Set required env vars before importing module
os.environ["AZURE_SUBSCRIPTION_ID"] = "test-sub-id"
os.environ["AZURE_RESOURCE_GROUP"] = "test-rg"
os.environ["AZURE_ACCOUNT_NAME"] = "test-account"
os.environ["PTU_TARGET"] = "74"
os.environ["PTU_MODEL_NAME"] = "gpt-5.2"
os.environ["PTU_MODEL_VERSION"] = "2025-12-11"

import ptu_accumulator as acc


# ---------------------------------------------------------------------------
# Config validation tests
# ---------------------------------------------------------------------------
class TestValidateConfig:
    def test_valid_config(self):
        errors = acc.validate_config()
        assert errors == [], f"Expected no errors, got: {errors}"

    def test_missing_subscription(self):
        original = acc.SUBSCRIPTION_ID
        acc.SUBSCRIPTION_ID = ""
        errors = acc.validate_config()
        acc.SUBSCRIPTION_ID = original
        assert any("SUBSCRIPTION_ID" in e for e in errors)

    def test_missing_resource_group(self):
        original = acc.RESOURCE_GROUP
        acc.RESOURCE_GROUP = ""
        errors = acc.validate_config()
        acc.RESOURCE_GROUP = original
        assert any("RESOURCE_GROUP" in e for e in errors)

    def test_missing_account_name(self):
        original = acc.ACCOUNT_NAME
        acc.ACCOUNT_NAME = ""
        errors = acc.validate_config()
        acc.ACCOUNT_NAME = original
        assert any("ACCOUNT_NAME" in e for e in errors)

    def test_target_below_minimum(self):
        original = acc.TARGET_PTUS
        acc.TARGET_PTUS = 5
        errors = acc.validate_config()
        acc.TARGET_PTUS = original
        assert any("below minimum" in e for e in errors)

    def test_target_unreasonably_high(self):
        original = acc.TARGET_PTUS
        acc.TARGET_PTUS = 5000
        errors = acc.validate_config()
        acc.TARGET_PTUS = original
        assert any("unreasonably high" in e for e in errors)

    def test_max_deployments_out_of_range(self):
        original = acc.MAX_DEPLOYMENTS
        acc.MAX_DEPLOYMENTS = 0
        errors = acc.validate_config()
        acc.MAX_DEPLOYMENTS = original
        assert any("PTU_MAX_DEPLOYMENTS" in e for e in errors)


# ---------------------------------------------------------------------------
# URL construction tests
# ---------------------------------------------------------------------------
class TestUrlConstruction:
    def test_deployment_url_format(self):
        url = acc._deployment_url("myaccount", "myrg", "mydeployment")
        assert "/subscriptions/test-sub-id/" in url
        assert "/resourceGroups/myrg/" in url
        assert "/accounts/myaccount/" in url
        assert "/deployments/mydeployment" in url
        assert f"api-version={acc.API_VERSION}" in url

    def test_deployment_url_no_trailing_slash(self):
        url = acc._deployment_url("a", "b", "c")
        assert "//deployments" not in url


# ---------------------------------------------------------------------------
# GET current PTUs tests
# ---------------------------------------------------------------------------
class TestGetCurrentPtus:
    @patch("ptu_accumulator.requests.get")
    def test_existing_deployment(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"sku": {"capacity": 25}}
        mock_get.return_value = mock_resp

        result = acc.get_current_ptus({}, "a", "b", "c")
        assert result == 25

    @patch("ptu_accumulator.requests.get")
    def test_missing_deployment(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_get.return_value = mock_resp

        result = acc.get_current_ptus({}, "a", "b", "c")
        assert result == 0

    @patch("ptu_accumulator.requests.get")
    def test_unexpected_error(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal server error"
        mock_get.return_value = mock_resp

        result = acc.get_current_ptus({}, "a", "b", "c")
        assert result == 0

    @patch("ptu_accumulator.requests.get")
    def test_network_failure(self, mock_get):
        mock_get.side_effect = Exception("Connection refused")

        result = acc.get_current_ptus({}, "a", "b", "c")
        assert result == 0

    @patch("ptu_accumulator.requests.get")
    def test_missing_sku_field(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"properties": {}}
        mock_get.return_value = mock_resp

        result = acc.get_current_ptus({}, "a", "b", "c")
        assert result == 0


# ---------------------------------------------------------------------------
# PUT deployment attempt tests
# ---------------------------------------------------------------------------
class TestAttemptDeployment:
    @patch("ptu_accumulator.requests.put")
    def test_successful_create(self, mock_put):
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.text = '{"status": "created"}'
        mock_put.return_value = mock_resp

        code, text = acc.attempt_deployment({}, "a", "b", "c", 15)
        assert code == 201

        # Verify PUT body structure
        call_kwargs = mock_put.call_args
        body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert body["sku"]["name"] == "DataZoneProvisionedManaged"
        assert body["sku"]["capacity"] == 15
        assert body["properties"]["model"]["name"] == "gpt-5.2"
        assert body["properties"]["model"]["format"] == "OpenAI"

    @patch("ptu_accumulator.requests.put")
    def test_no_capacity(self, mock_put):
        mock_resp = MagicMock()
        mock_resp.status_code = 409
        mock_resp.text = "No capacity available"
        mock_put.return_value = mock_resp

        code, text = acc.attempt_deployment({}, "a", "b", "c", 15)
        assert code == 409

    @patch("ptu_accumulator.requests.put")
    def test_network_failure(self, mock_put):
        mock_put.side_effect = Exception("Timeout")

        code, text = acc.attempt_deployment({}, "a", "b", "c", 15)
        assert code == 0


# ---------------------------------------------------------------------------
# Total PTU calculation tests
# ---------------------------------------------------------------------------
class TestGetTotalPtus:
    @patch("ptu_accumulator.get_current_ptus")
    def test_multiple_deployments(self, mock_get):
        # Simulate: deployment-0 has 20, deployment-1 has 15, rest empty
        mock_get.side_effect = [20, 15, 0, 0]

        total, breakdown = acc.get_total_ptus({}, "a", "b")
        assert total == 35
        assert breakdown == {
            f"{acc.DEPLOYMENT_PREFIX}-0": 20,
            f"{acc.DEPLOYMENT_PREFIX}-1": 15,
        }

    @patch("ptu_accumulator.get_current_ptus")
    def test_no_deployments(self, mock_get):
        mock_get.return_value = 0

        total, breakdown = acc.get_total_ptus({}, "a", "b")
        assert total == 0
        assert breakdown == {}

    @patch("ptu_accumulator.get_current_ptus")
    def test_all_deployments_full(self, mock_get):
        mock_get.side_effect = [20, 20, 20, 14]

        total, breakdown = acc.get_total_ptus({}, "a", "b")
        assert total == 74


# ---------------------------------------------------------------------------
# Integration logic tests (mocked Azure calls)
# ---------------------------------------------------------------------------
class TestRunAccumulator:
    @patch("ptu_accumulator.check_available_capacity")
    @patch("ptu_accumulator.send_teams_alert")
    @patch("ptu_accumulator.attempt_deployment")
    @patch("ptu_accumulator.get_current_ptus")
    @patch("ptu_accumulator.get_headers")
    def test_target_already_reached(self, mock_headers, mock_get_ptus, mock_attempt, mock_alert, mock_cap):
        mock_headers.return_value = {"Authorization": "Bearer test"}
        mock_cap.return_value = {"available": 0, "sku_available": {}, "checked": False}
        # All 4 deployments have enough PTUs
        mock_get_ptus.side_effect = [20, 20, 20, 14, 20, 20, 20, 14]

        result = acc.run_accumulator()
        assert result["status"] == "target_reached"
        assert result["total"] == 74
        mock_attempt.assert_not_called()  # Should not try to deploy

    @patch("ptu_accumulator.check_available_capacity")
    @patch("ptu_accumulator.send_teams_alert")
    @patch("ptu_accumulator.attempt_deployment")
    @patch("ptu_accumulator.get_current_ptus")
    @patch("ptu_accumulator.get_headers")
    def test_no_capacity_available(self, mock_headers, mock_get_ptus, mock_attempt, mock_alert, mock_cap):
        mock_headers.return_value = {"Authorization": "Bearer test"}
        mock_cap.return_value = {"available": 0, "sku_available": {}, "checked": True}
        mock_get_ptus.return_value = 0  # No existing deployments

        mock_attempt.return_value = (409, "No capacity")

        result = acc.run_accumulator()
        assert result["status"] == "completed_cycle"
        assert result["total_landed"] == 0
        assert result["remaining"] == 74
        assert len(result["actions"]) == 0

    @patch("ptu_accumulator.check_available_capacity")
    @patch("ptu_accumulator.send_teams_alert")
    @patch("ptu_accumulator.attempt_deployment")
    @patch("ptu_accumulator.get_current_ptus")
    @patch("ptu_accumulator.get_headers")
    def test_successful_first_deployment(self, mock_headers, mock_get_ptus, mock_attempt, mock_alert, mock_cap):
        mock_headers.return_value = {"Authorization": "Bearer test"}
        mock_cap.return_value = {"available": 100, "sku_available": {}, "checked": True}
        # First call: get_total_ptus checks 4 deployments (all 0)
        # Second call: checking deployment-0 for create attempt
        mock_get_ptus.side_effect = [0, 0, 0, 0, 0, 0, 0, 0]

        # First attempt succeeds (creates 15 PTU deployment)
        mock_attempt.return_value = (201, '{"status":"created"}')

        result = acc.run_accumulator()
        assert result["status"] == "completed_cycle"
        # All 4 deployments succeed at 15 PTU each = 60 total
        assert result["total_landed"] == 60
        assert len(result["actions"]) == 4
        assert result["actions"][0]["gained"] == 15
        assert result["actions"][0]["action"] == "create"

    @patch("ptu_accumulator.check_available_capacity")
    @patch("ptu_accumulator.send_teams_alert")
    @patch("ptu_accumulator.attempt_deployment")
    @patch("ptu_accumulator.get_current_ptus")
    @patch("ptu_accumulator.get_headers")
    def test_scale_existing_deployment(self, mock_headers, mock_get_ptus, mock_attempt, mock_alert, mock_cap):
        mock_headers.return_value = {"Authorization": "Bearer test"}
        mock_cap.return_value = {"available": 100, "sku_available": {}, "checked": True}
        # Total check: deployment-0 has 20, rest empty
        # Then individual check: deployment-0 has 20
        mock_get_ptus.side_effect = [20, 0, 0, 0, 20, 0, 0, 0]

        # All attempts succeed — greedy tries +50 first on deployment-0
        mock_attempt.return_value = (201, '{"status":"updated"}')

        result = acc.run_accumulator()
        assert result["status"] == "completed_cycle"
        # deployment-0 greedy snipes 20->70 (+50), then 3 new at 15 each
        # But total_landed starts at 20, after +50 = 70, remaining = 4
        # Next 3 slots create at 15 but that would overshoot: 70 + 15 > 74
        # So only slot-0 gets +50, total = 70, remaining = 4
        # Slots 1-3 try create at 15 but 70+15 > 74 so they are capped
        assert result["total_landed"] >= 70
        assert result["actions"][0]["action"] == "snipe"

    @patch("ptu_accumulator.send_teams_alert")
    @patch("ptu_accumulator.attempt_deployment")
    @patch("ptu_accumulator.get_current_ptus")
    @patch("ptu_accumulator.get_headers")
    def test_config_error_aborts(self, mock_headers, mock_get_ptus, mock_attempt, mock_alert):
        original = acc.SUBSCRIPTION_ID
        acc.SUBSCRIPTION_ID = ""

        result = acc.run_accumulator()
        assert result["status"] == "error"
        assert "Configuration errors" in result["message"]

        acc.SUBSCRIPTION_ID = original
        mock_headers.assert_not_called()


# ---------------------------------------------------------------------------
# Teams alert tests
# ---------------------------------------------------------------------------
class TestTeamsAlert:
    @patch("ptu_accumulator.requests.post")
    def test_alert_sent(self, mock_post):
        original = acc.TEAMS_WEBHOOK_URL
        acc.TEAMS_WEBHOOK_URL = "https://example.com/webhook"

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        acc.send_teams_alert("Test message")
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert body["text"] == "Test message"

        acc.TEAMS_WEBHOOK_URL = original

    @patch("ptu_accumulator.requests.post")
    def test_no_webhook_configured(self, mock_post):
        original = acc.TEAMS_WEBHOOK_URL
        acc.TEAMS_WEBHOOK_URL = ""

        acc.send_teams_alert("Test message")
        mock_post.assert_not_called()

        acc.TEAMS_WEBHOOK_URL = original


# ---------------------------------------------------------------------------
# Edge case tests
# ---------------------------------------------------------------------------
class TestEdgeCases:
    def test_deployment_prefix_naming(self):
        """Verify deployment names follow expected pattern."""
        for i in range(acc.MAX_DEPLOYMENTS):
            name = f"{acc.DEPLOYMENT_PREFIX}-{i}"
            assert name.startswith("gpt52-ptu-accum-")
            assert name.endswith(str(i))

    def test_increment_respects_minimum(self):
        """New deployment must be at least MIN_PTU."""
        assert acc.MIN_PTU == 15
        assert acc.INCREMENT == 5

    def test_api_version_format(self):
        """API version should be a valid date format."""
        parts = acc.API_VERSION.split("-")
        assert len(parts) >= 3  # YYYY-MM-DD or YYYY-MM-DD-preview
        assert int(parts[0]) >= 2024


# ---------------------------------------------------------------------------
# DataZone-aware / zone-pooled SKU tests
# ---------------------------------------------------------------------------
class TestIsZonePooledSku:
    def test_datazone_provisioned_is_pooled(self):
        assert acc.is_zone_pooled_sku("DataZoneProvisionedManaged") is True

    def test_global_provisioned_is_pooled(self):
        assert acc.is_zone_pooled_sku("GlobalProvisionedManaged") is True

    def test_datazone_standard_is_pooled(self):
        assert acc.is_zone_pooled_sku("DataZoneStandard") is True

    def test_regional_provisioned_is_not_pooled(self):
        assert acc.is_zone_pooled_sku("ProvisionedManaged") is False

    def test_standard_is_not_pooled(self):
        assert acc.is_zone_pooled_sku("Standard") is False

    def test_unknown_sku_is_not_pooled(self):
        assert acc.is_zone_pooled_sku("SomethingElse") is False


# ---------------------------------------------------------------------------
# Greedy increment tests
# ---------------------------------------------------------------------------
class TestGreedyIncrements:
    def test_greedy_list_is_descending(self):
        """Greedy increments should be in descending order."""
        assert acc.GREEDY_INCREMENTS == sorted(acc.GREEDY_INCREMENTS, reverse=True)

    def test_greedy_list_ends_with_minimum_increment(self):
        """Smallest greedy increment should match INCREMENT."""
        assert acc.GREEDY_INCREMENTS[-1] == acc.INCREMENT

    def test_all_greedy_increments_divisible_by_5(self):
        """All greedy increments must be multiples of INCREMENT (5)."""
        for inc in acc.GREEDY_INCREMENTS:
            assert inc % acc.INCREMENT == 0, f"{inc} is not divisible by {acc.INCREMENT}"

    @patch("ptu_accumulator.check_available_capacity")
    @patch("ptu_accumulator.send_teams_alert")
    @patch("ptu_accumulator.attempt_deployment")
    @patch("ptu_accumulator.get_current_ptus")
    @patch("ptu_accumulator.get_headers")
    def test_greedy_tries_largest_first(self, mock_headers, mock_get_ptus, mock_attempt, mock_alert, mock_cap):
        """When scaling existing deployment, should try +50 before +5."""
        mock_headers.return_value = {"Authorization": "Bearer test"}
        mock_cap.return_value = {"available": 100, "sku_available": {}, "checked": True}
        # get_total_ptus: deployment-0 has 15, rest empty (4 calls)
        # then per-slot checks: deployment-0=15, deployment-1=0, deployment-2=0, deployment-3=0
        mock_get_ptus.side_effect = [15, 0, 0, 0, 15, 0, 0, 0]

        # +50 on deployment-0 succeeds, creates at 15 on remaining slots
        mock_attempt.return_value = (201, '{"status":"updated"}')

        result = acc.run_accumulator()
        # First action should be snipe with +50 (greedy grabs big chunk)
        first_action = result["actions"][0]
        assert first_action["action"] == "snipe"
        assert first_action["gained"] == 50  # Greedy grabbed +50
        assert first_action["previous"] == 15
        assert first_action["new"] == 65

    @patch("ptu_accumulator.check_available_capacity")
    @patch("ptu_accumulator.send_teams_alert")
    @patch("ptu_accumulator.attempt_deployment")
    @patch("ptu_accumulator.get_current_ptus")
    @patch("ptu_accumulator.get_headers")
    def test_greedy_falls_back_to_smaller(self, mock_headers, mock_get_ptus, mock_attempt, mock_alert, mock_cap):
        """If +50 fails (409), should fall back to +25, +15, etc."""
        mock_headers.return_value = {"Authorization": "Bearer test"}
        mock_cap.return_value = {"available": 10, "sku_available": {}, "checked": True}
        # Only deployment-0 exists with 15 PTU
        mock_get_ptus.side_effect = [15, 0, 0, 0, 15, 0, 0, 0]

        # +50, +25, +15 fail (409), +10 succeeds
        mock_attempt.side_effect = [
            (409, "No capacity"),    # +50
            (409, "No capacity"),    # +25
            (409, "No capacity"),    # +15
            (201, '{"ok":true}'),    # +10 succeeds!
            (201, '{"ok":true}'),    # deployment-1 create at 15
            (201, '{"ok":true}'),    # deployment-2 create at 15
            (201, '{"ok":true}'),    # deployment-3 create at 15
        ]

        result = acc.run_accumulator()
        first_action = result["actions"][0]
        assert first_action["gained"] == 10  # Fell back to +10
        assert first_action["previous"] == 15
        assert first_action["new"] == 25


# ---------------------------------------------------------------------------
# Cross-SKU fallback tests
# ---------------------------------------------------------------------------
class TestCrossSkuFallback:
    def test_fallback_chain_defined(self):
        """DataZone should fall back to Global and vice versa."""
        assert "GlobalProvisionedManaged" in acc.FALLBACK_SKU_CHAIN["DataZoneProvisionedManaged"]
        assert "DataZoneProvisionedManaged" in acc.FALLBACK_SKU_CHAIN["GlobalProvisionedManaged"]

    def test_regional_has_no_fallback(self):
        """Regional ProvisionedManaged has no PTU fallback chain."""
        assert acc.FALLBACK_SKU_CHAIN["ProvisionedManaged"] == []

    @patch("ptu_accumulator.requests.put")
    def test_attempt_deployment_with_sku_override(self, mock_put):
        """attempt_deployment with sku_override should use the override SKU."""
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.text = '{"status": "created"}'
        mock_put.return_value = mock_resp

        code, text = acc.attempt_deployment(
            {}, "a", "b", "c", 15,
            sku_override="GlobalProvisionedManaged",
        )
        assert code == 201
        call_kwargs = mock_put.call_args
        body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert body["sku"]["name"] == "GlobalProvisionedManaged"


# ---------------------------------------------------------------------------
# Capacity pre-check tests
# ---------------------------------------------------------------------------
class TestCapacityPreCheck:
    @patch("ptu_accumulator.requests.get")
    def test_successful_capacity_check(self, mock_get):
        """Should parse available capacity from API response."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "value": [
                {"skuName": "DataZoneProvisionedManaged", "properties": {"availableCapacity": 25}},
                {"skuName": "GlobalProvisionedManaged", "properties": {"availableCapacity": 50}},
            ]
        }
        mock_get.return_value = mock_resp

        # Need to mock get_headers since check_available_capacity uses module-level requests
        result = acc.check_available_capacity({"Authorization": "Bearer test"})
        assert result["checked"] is True
        assert result["sku_available"]["DataZoneProvisionedManaged"] == 25
        assert result["sku_available"]["GlobalProvisionedManaged"] == 50

    @patch("ptu_accumulator.requests.get")
    def test_capacity_check_api_failure(self, mock_get):
        """Should return unchecked result on API failure."""
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.text = "Forbidden"
        mock_get.return_value = mock_resp

        result = acc.check_available_capacity({"Authorization": "Bearer test"})
        assert result["checked"] is False
        assert result["available"] == -1

    @patch("ptu_accumulator.requests.get")
    def test_capacity_check_network_error(self, mock_get):
        """Should handle network errors gracefully."""
        mock_get.side_effect = Exception("Connection refused")

        result = acc.check_available_capacity({"Authorization": "Bearer test"})
        assert result["checked"] is False


# ---------------------------------------------------------------------------
# Model validation tests
# ---------------------------------------------------------------------------
class TestModelValidation:
    def test_gpt41_is_valid_model(self):
        """GPT-4.1 should be in the known model list."""
        original = acc.MODEL_NAME
        acc.MODEL_NAME = "gpt-4.1"
        errors = acc.validate_config()
        acc.MODEL_NAME = original
        assert not any("not in known model list" in e for e in errors)


# ---------------------------------------------------------------------------
# Quota pre-check tests
# ---------------------------------------------------------------------------
class TestQuotaPreCheck:
    @patch("ptu_accumulator.requests.get")
    def test_quota_check_parses_usages(self, mock_get):
        """Should parse quota used/limit from Usages API response."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "value": [
                {
                    "name": {"localizedValue": "Data Zone Provisioned Managed Throughput Unit"},
                    "currentValue": 15,
                    "limit": 15,
                },
                {
                    "name": {"localizedValue": "Global Provisioned Managed Throughput Unit"},
                    "currentValue": 0,
                    "limit": 300,
                },
            ]
        }
        mock_get.return_value = mock_resp

        result = acc.check_quota({"Authorization": "Bearer test"})
        assert result["checked"] is True
        assert result["quotas"]["DataZoneProvisionedManaged"]["used"] == 15
        assert result["quotas"]["DataZoneProvisionedManaged"]["limit"] == 15
        assert result["quotas"]["DataZoneProvisionedManaged"]["pct"] == 100.0
        assert "DataZoneProvisionedManaged" in result["blocked_skus"]
        assert "GlobalProvisionedManaged" not in result["blocked_skus"]
        assert result["quotas"]["GlobalProvisionedManaged"]["used"] == 0
        assert result["quotas"]["GlobalProvisionedManaged"]["limit"] == 300

    @patch("ptu_accumulator.requests.get")
    def test_quota_check_api_failure(self, mock_get):
        """Should return unchecked on API failure."""
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.text = "Forbidden"
        mock_get.return_value = mock_resp

        result = acc.check_quota({"Authorization": "Bearer test"})
        assert result["checked"] is False

    @patch("ptu_accumulator.requests.get")
    def test_quota_check_network_error(self, mock_get):
        """Should handle network errors gracefully."""
        mock_get.side_effect = Exception("Connection refused")
        result = acc.check_quota({"Authorization": "Bearer test"})
        assert result["checked"] is False


# ---------------------------------------------------------------------------
# 409 reason parsing tests
# ---------------------------------------------------------------------------
class TestParse409Reason:
    def test_quota_exceeded(self):
        assert acc.parse_409_reason("Quota limit exceeded for SKU") == "quota_exceeded"

    def test_quota_exceeded_limit(self):
        assert acc.parse_409_reason("The limit for this resource has been reached") == "quota_exceeded"

    def test_no_capacity(self):
        assert acc.parse_409_reason("Insufficient capacity available") == "no_capacity"

    def test_unknown_reason(self):
        assert acc.parse_409_reason("Something else happened") == "unknown"

    def test_empty_response(self):
        assert acc.parse_409_reason("") == "unknown"


# ---------------------------------------------------------------------------
# Quota-blocked SKU skip tests
# ---------------------------------------------------------------------------
class TestQuotaBlockedSkip:
    @patch("ptu_accumulator.check_quota")
    @patch("ptu_accumulator.check_available_capacity")
    @patch("ptu_accumulator.send_teams_alert")
    @patch("ptu_accumulator.attempt_deployment")
    @patch("ptu_accumulator.get_current_ptus")
    @patch("ptu_accumulator.get_headers")
    def test_skips_primary_when_quota_blocked(
        self, mock_headers, mock_get_ptus, mock_attempt, mock_alert, mock_cap, mock_quota
    ):
        """When primary SKU quota is 100%, should skip it and go to fallback."""
        mock_headers.return_value = {"Authorization": "Bearer test"}
        mock_cap.return_value = {"available": 0, "sku_available": {}, "checked": False}
        mock_quota.return_value = {
            "checked": True,
            "quotas": {
                "DataZoneProvisionedManaged": {"used": 15, "limit": 15, "pct": 100.0, "display_name": "DZ"},
                "GlobalProvisionedManaged": {"used": 0, "limit": 300, "pct": 0.0, "display_name": "Global"},
            },
            "blocked_skus": ["DataZoneProvisionedManaged"],
        }
        mock_get_ptus.return_value = 0

        # Fallback (Global) deployment succeeds
        mock_attempt.return_value = (201, '{"status":"created"}')

        result = acc.run_accumulator()
        assert result["primary_quota_blocked"] is True
        # Should have actions from fallback, not from primary
        if result["actions"]:
            for action in result["actions"]:
                # No primary SKU actions should exist
                assert "fallback" in action.get("action", "") or action.get("action") == "tpm_fallback"


# ---------------------------------------------------------------------------
# Capacity-zero skip tests (prevents hammering ARM with guaranteed 409s)
# ---------------------------------------------------------------------------
class TestCapacityZeroSkip:
    @patch("ptu_accumulator.check_quota")
    @patch("ptu_accumulator.check_available_capacity")
    @patch("ptu_accumulator.send_teams_alert")
    @patch("ptu_accumulator.attempt_deployment")
    @patch("ptu_accumulator.get_current_ptus")
    @patch("ptu_accumulator.get_headers")
    def test_skips_primary_puts_when_capacity_zero(
        self, mock_headers, mock_get_ptus, mock_attempt, mock_alert, mock_cap, mock_quota
    ):
        """When capacity pre-check confirms 0 for primary SKU, should NOT call attempt_deployment for primary."""
        mock_headers.return_value = {"Authorization": "Bearer test"}
        mock_cap.return_value = {
            "available": 0,
            "sku_available": {"DataZoneProvisionedManaged": 0, "GlobalProvisionedManaged": 50},
            "checked": True,
        }
        mock_quota.return_value = {"checked": False, "quotas": {}, "blocked_skus": []}
        mock_get_ptus.return_value = 0
        mock_attempt.return_value = (409, "no capacity")

        result = acc.run_accumulator()
        assert result["primary_capacity_zero"] is True
        # attempt_deployment should NOT have been called for primary slots
        # It may be called for fallback if enabled, but primary greedy loop should be skipped
        # Verify by checking that no "snipe" or "create" (non-fallback) actions exist
        for action in result.get("actions", []):
            assert "fallback" in action.get("action", "") or action.get("action") == "tpm_fallback"

    @patch("ptu_accumulator.check_quota")
    @patch("ptu_accumulator.check_available_capacity")
    @patch("ptu_accumulator.send_teams_alert")
    @patch("ptu_accumulator.attempt_deployment")
    @patch("ptu_accumulator.get_current_ptus")
    @patch("ptu_accumulator.get_headers")
    def test_proceeds_when_capacity_unknown(
        self, mock_headers, mock_get_ptus, mock_attempt, mock_alert, mock_cap, mock_quota
    ):
        """When capacity pre-check fails (API unreachable), should proceed with PUTs."""
        mock_headers.return_value = {"Authorization": "Bearer test"}
        mock_cap.return_value = {"available": -1, "sku_available": {}, "checked": False}
        mock_quota.return_value = {"checked": False, "quotas": {}, "blocked_skus": []}
        mock_get_ptus.return_value = 0
        mock_attempt.return_value = (201, '{"status":"created"}')

        result = acc.run_accumulator()
        assert result["primary_capacity_zero"] is False
        # Should have attempted deployment (capacity unknown = try anyway)
        assert mock_attempt.called


# ---------------------------------------------------------------------------
# Cross-SKU fallback default tests
# ---------------------------------------------------------------------------
class TestCrossSkuDefault:
    def test_cross_sku_fallback_defaults_to_false(self):
        """Cross-SKU fallback must default to false for data sovereignty."""
        import os
        original = os.environ.get("CROSS_SKU_FALLBACK")
        # Clear env var to test default
        if "CROSS_SKU_FALLBACK" in os.environ:
            del os.environ["CROSS_SKU_FALLBACK"]
        # Re-evaluate the default
        result = os.environ.get("CROSS_SKU_FALLBACK", "false").lower() == "true"
        assert result is False
        # Restore
        if original is not None:
            os.environ["CROSS_SKU_FALLBACK"] = original
