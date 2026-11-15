"""
Unit tests for Twitch Account & Network Diagnostic Suite.
Tests:
1. Valid currentUser extraction and session verification from gql.twitch.tv
2. ViewerDropsDashboard campaign returns and IntegrityCheckFailed error detection
3. Container IP / network restriction, WAF 403 blocks, and Kasada 429 rate limit detection
"""

import json
import unittest
from unittest import mock
import urllib.error

from src.twitch_drops import diagnostics


class TwitchDiagnosticsTests(unittest.TestCase):

    @mock.patch("src.twitch_drops.diagnostics._make_http_request")
    def test_check_current_user_valid(self, mock_http):
        mock_http.return_value = (
            200,
            json.dumps({
                "data": {
                    "currentUser": {
                        "id": "130742243",
                        "login": "test_streamer",
                        "displayName": "TestStreamer",
                        "email": "test@example.com",
                    }
                }
            }).encode("utf-8"),
            {},
        )
        res = diagnostics.check_current_user("valid_test_token_123")
        self.assertEqual(res["status"], "OK")
        self.assertTrue(res["valid"])
        self.assertEqual(res["user_id"], "130742243")
        self.assertEqual(res["login"], "test_streamer")
        self.assertEqual(res["display_name"], "TestStreamer")
        self.assertIn("Valid currentUser verified", res["message"])

    @mock.patch("src.twitch_drops.diagnostics._make_http_request")
    def test_check_current_user_null(self, mock_http):
        mock_http.return_value = (
            200,
            json.dumps({"data": {"currentUser": None}}).encode("utf-8"),
            {},
        )
        res = diagnostics.check_current_user("stale_token_abc")
        self.assertEqual(res["status"], "FAIL")
        self.assertFalse(res["valid"])
        self.assertEqual(res["error"], "currentUser_null")
        self.assertIn("currentUser=null", res["message"])

    @mock.patch("src.twitch_drops.diagnostics._make_http_request")
    def test_check_current_user_unauthorized_401(self, mock_http):
        mock_http.return_value = (
            401,
            json.dumps({"error": "Unauthorized", "status": 401, "message": "Invalid OAuth token"}).encode("utf-8"),
            {},
        )
        res = diagnostics.check_current_user("revoked_token_xyz")
        self.assertEqual(res["status"], "FAIL")
        self.assertFalse(res["valid"])
        self.assertEqual(res["error"], "http_401_unauthorized")
        self.assertIn("HTTP 401 Unauthorized", res["message"])

    def test_check_current_user_missing_token(self):
        res = diagnostics.check_current_user("")
        self.assertEqual(res["status"], "FAIL")
        self.assertFalse(res["valid"])
        self.assertEqual(res["error"], "missing_token")

    @mock.patch("src.twitch_drops.diagnostics._make_http_request")
    def test_check_viewer_drops_dashboard_success(self, mock_http):
        # 1st call: /integrity endpoint
        # 2nd call: GQL ViewerDropsDashboard
        mock_http.side_effect = [
            (200, json.dumps({"token": "v4.local.valid_tok"}).encode("utf-8"), {}),
            (
                200,
                json.dumps({
                    "data": {
                        "currentUser": {
                            "dropCampaigns": [
                                {"id": "c1", "name": "Rust Drops Campaign", "game": {"displayName": "Rust"}},
                                {"id": "c2", "name": "Overwatch 2 Drops", "game": {"displayName": "Overwatch 2"}},
                            ]
                        }
                    }
                }).encode("utf-8"),
                {},
            ),
        ]
        res = diagnostics.check_viewer_drops_dashboard("test_token_123")
        self.assertEqual(res["status"], "OK")
        self.assertFalse(res["integrity_failed"])
        self.assertEqual(res["campaigns_count"], 2)
        self.assertIn("Rust Drops Campaign", res["campaign_samples"])
        self.assertIn("integrity check PASSED", res["message"])

    @mock.patch("src.twitch_drops.diagnostics._make_http_request")
    def test_check_viewer_drops_dashboard_integrity_failed(self, mock_http):
        mock_http.side_effect = [
            (200, json.dumps({"token": "v4.local.test"}).encode("utf-8"), {}),
            (
                200,
                json.dumps({
                    "errors": [
                        {
                            "message": "failed integrity check",
                            "extensions": {"code": "IntegrityCheckFailed"},
                        }
                    ]
                }).encode("utf-8"),
                {},
            ),
        ]
        res = diagnostics.check_viewer_drops_dashboard("test_token_123")
        self.assertEqual(res["status"], "INTEGRITY_FAILED")
        self.assertTrue(res["integrity_failed"])
        self.assertEqual(res["campaigns_count"], 0)
        self.assertIn("IntegrityCheckFailed", res["message"])
        self.assertIn("Kasada anti-bot", res["message"])

    @mock.patch("src.twitch_drops.diagnostics._make_http_request")
    def test_check_network_and_ip_restrictions_clean(self, mock_http):
        # 3 calls: www.twitch.tv, /integrity, /gql
        mock_http.side_effect = [
            (200, b"<html>OK</html>", {}),
            (200, json.dumps({"token": "v4.local.token_ok"}).encode("utf-8"), {}),
            (200, json.dumps({"data": {"__typename": "Query"}}).encode("utf-8"), {}),
        ]
        res = diagnostics.check_network_and_ip_restrictions()
        self.assertEqual(res["status"], "OK")
        self.assertFalse(res["is_blocked"])
        self.assertFalse(res["is_rate_limited"])
        self.assertTrue(res["all_reachable"])
        self.assertTrue(res["integrity_token_acquired"])
        self.assertIn("clean and unrestricted", res["message"])

    @mock.patch("src.twitch_drops.diagnostics._make_http_request")
    def test_check_network_and_ip_restrictions_blocked_403(self, mock_http):
        # Edge returns HTTP 403 (Fastly / WAF IP block)
        mock_http.side_effect = [
            (403, b"Forbidden", {}),
            (403, b"Forbidden", {}),
            (403, b"Forbidden", {}),
        ]
        res = diagnostics.check_network_and_ip_restrictions()
        self.assertEqual(res["status"], "BLOCKED")
        self.assertTrue(res["is_blocked"])
        self.assertIn("Twitch IP Block Detected", res["message"])
        self.assertIn("Remedies", res["recommendation"])

    @mock.patch("src.twitch_drops.diagnostics._make_http_request")
    def test_check_network_and_ip_restrictions_rate_limited_429(self, mock_http):
        mock_http.side_effect = [
            (429, b"Too Many Requests", {}),
            (429, b"Too Many Requests", {}),
            (429, b"Too Many Requests", {}),
        ]
        res = diagnostics.check_network_and_ip_restrictions()
        self.assertEqual(res["status"], "RATE_LIMITED")
        self.assertTrue(res["is_rate_limited"])
        self.assertIn("Rate Limit Active", res["message"])
        self.assertIn("15-30 minutes", res["recommendation"])

    @mock.patch("src.twitch_drops.diagnostics.check_viewer_drops_dashboard")
    @mock.patch("src.twitch_drops.diagnostics.check_current_user")
    @mock.patch("src.twitch_drops.diagnostics.check_network_and_ip_restrictions")
    def test_run_twitch_diagnostics_healthy_aggregation(self, mock_net, mock_user, mock_dash):
        mock_net.return_value = {"status": "OK", "is_blocked": False, "message": "Network OK"}
        mock_user.return_value = {"status": "OK", "valid": True, "message": "User OK"}
        mock_dash.return_value = {"status": "OK", "campaigns_count": 5, "message": "Dash OK"}

        diag = diagnostics.run_twitch_diagnostics(token="valid_tok", verbose=False)
        self.assertTrue(diag["healthy"])
        self.assertEqual(diag["network"]["status"], "OK")
        self.assertEqual(diag["user_session"]["status"], "OK")
        self.assertEqual(diag["drops_dashboard"]["status"], "OK")

    @mock.patch("src.twitch_drops.diagnostics.check_viewer_drops_dashboard")
    @mock.patch("src.twitch_drops.diagnostics.check_current_user")
    @mock.patch("src.twitch_drops.diagnostics.check_network_and_ip_restrictions")
    def test_run_twitch_diagnostics_unhealthy_aggregation(self, mock_net, mock_user, mock_dash):
        mock_net.return_value = {"status": "OK", "is_blocked": False, "message": "Network OK"}
        mock_user.return_value = {"status": "FAIL", "valid": False, "message": "User Expired"}
        mock_dash.return_value = {"status": "FAIL", "campaigns_count": 0, "message": "Dash 401"}

        diag = diagnostics.run_twitch_diagnostics(token="expired_tok", verbose=False)
        self.assertFalse(diag["healthy"])


if __name__ == "__main__":
    unittest.main()
