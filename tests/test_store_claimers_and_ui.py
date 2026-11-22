import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer
from unittest.mock import patch

from src.free_games.auto_claimer import (
    consume_pending_2fa,
    generate_totp,
    load_store_claimer_status,
    save_pending_2fa,
    save_store_claimer_status,
)
from src.free_games.sync_claimed import collect_claimed_records
from src.web import server as web_server


class TestStoreClaimersAndUI(unittest.TestCase):
    def test_generate_totp_rfc6238_and_normalization(self):
        # RFC 6238 test vector for SHA1 with secret "12345678901234567890" (base32 GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ)
        secret_b32 = "GEZD GNBV-GY3T QOJQ GEZD GNBV GY3T QOJQ".lower()
        code = generate_totp(secret_b32, timestamp=59, interval=30, digits=8)
        self.assertEqual(code, "94287082")
        code_6 = generate_totp(secret_b32, timestamp=59, interval=30, digits=6)
        self.assertEqual(code_6, "287082")

    def test_sync_claimed_multi_store_logs_and_lowdb(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fgc_dir = os.path.join(tmpdir, "data", "fgc")
            prof_dir = os.path.join(fgc_dir, "chromium_profile")
            os.makedirs(prof_dir, exist_ok=True)

            # Ignored metadata files & profile files
            with open(os.path.join(tmpdir, "data", "store_claimer_status.json"), "w", encoding="utf-8") as f:
                json.dump({"epic": {"title": "ShouldBeIgnored", "status": "claimed"}}, f)
            with open(os.path.join(prof_dir, "ignored_cache.json"), "w", encoding="utf-8") as f:
                json.dump([{"title": "ProfileIgnored", "claimed": True}], f)

            # 2-level FGC lowdb JSONs
            with open(os.path.join(fgc_dir, "prime-gaming.json"), "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "user@example.com": {
                            "Fallout 76": {"title": "Fallout 76", "status": "claimed"},
                            "Failed Game": {"title": "Failed Game", "status": "failed: need account linking"},
                        }
                    },
                    f,
                )
            with open(os.path.join(fgc_dir, "gog.json"), "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "goguser": {
                            "The Whisperer": {"title": "The Whisperer", "status": "existed"},
                        }
                    },
                    f,
                )

            # Multi-store free-games.log with ANSI colors
            log_path = os.path.join(tmpdir, "data", "free-games.log")
            with open(log_path, "w", encoding="utf-8") as f:
                f.write(
                    "[EPIC] Claiming Epic Games Store freebies...\n"
                    "Current free game: \x1b[34mControl\x1b[0m\n"
                    "Claimed successfully!\n"
                    "[AMAZON] Claiming Amazon Prime Gaming freebies...\n"
                    "Current free game: Tomb Raider - https://gaming.amazon.com/tomb\n"
                    "Already in library! Nothing to claim.\n"
                    "2026-11-21 started checking gog\n"
                    "Current free game: Sunblaze\n"
                    "Claimed successfully!\n"
                )

            records = collect_claimed_records(base_dir=tmpdir)
            self.assertIn("Control", records["epic"])
            self.assertIn("Fallout 76", records["amazon"])
            self.assertIn("Tomb Raider", records["amazon"])
            self.assertNotIn("Failed Game", records["amazon"])
            self.assertIn("The Whisperer", records["gog"])
            self.assertIn("Sunblaze", records["gog"])
            self.assertNotIn("ShouldBeIgnored", records["epic"])
            self.assertNotIn("ProfileIgnored", records["epic"])

    def test_store_status_and_pending_2fa_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            st = load_store_claimer_status(base_dir=tmpdir)
            st["running"] = True
            st["stores"]["gog"]["status"] = "needs_2fa"
            save_store_claimer_status(st, base_dir=tmpdir)

            reloaded = load_store_claimer_status(base_dir=tmpdir)
            self.assertTrue(reloaded["running"])
            self.assertEqual(reloaded["stores"]["gog"]["status"], "needs_2fa")

            save_pending_2fa("prime", " 123 456 ", ttl_seconds=60, base_dir=tmpdir)
            code = consume_pending_2fa("amazon", base_dir=tmpdir)
            self.assertEqual(code, "123456")
            self.assertIsNone(consume_pending_2fa("amazon", base_dir=tmpdir))

            save_pending_2fa("gog", "9876", ttl_seconds=30, base_dir=tmpdir)
            with patch("src.free_games.auto_claimer.time.time", return_value=time.time() + 120):
                self.assertIsNone(consume_pending_2fa("gog", base_dir=tmpdir))

    def test_is_store_claim_due_logic(self):
        env_off = {"FREE_GAMES_AUTO_CLAIM": "0", "GOG_EMAIL": "a@b.com"}
        self.assertFalse(web_server.is_store_claim_due(env=env_off, status={"running": False}))

        env_none = {"FREE_GAMES_AUTO_CLAIM": "1"}
        self.assertFalse(web_server.is_store_claim_due(env=env_none, status={"running": False}))

        env_on = {"FREE_GAMES_AUTO_CLAIM": "1", "FREE_GAMES_INTERVAL_HOURS": "6", "PG_EMAIL": "a@b.com"}
        self.assertFalse(web_server.is_store_claim_due(env=env_on, status={"running": True}))
        self.assertTrue(web_server.is_store_claim_due(env=env_on, status={"running": False, "last_run_at": None}))

        now = 1_700_000_000.0
        recent_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 3600))
        old_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 7 * 3600))
        self.assertFalse(web_server.is_store_claim_due(env=env_on, status={"running": False, "last_run_at": recent_iso}, now_ts=now))
        self.assertTrue(web_server.is_store_claim_due(env=env_on, status={"running": False, "last_run_at": old_iso}, now_ts=now))

    def test_server_endpoints_status_2fa_and_test_service(self):
        orig_sessions = dict(web_server.ACTIVE_SESSIONS)
        web_server.ACTIVE_SESSIONS["test_tok"] = time.time() + 3600
        httpd = HTTPServer(("127.0.0.1", 0), web_server.DashboardHandler)
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            hdrs = {"Cookie": "session_id=test_tok", "Content-Type": "application/json"}

            # 1. /api/status includes store_claimer & per-store claimer_status
            with patch("src.web.server.build_status_payload", return_value={
                "twitch": {},
                "epic": {"claimer_status": {"status": "ok"}},
                "amazon": {"claimer_status": {"status": "ok"}},
                "gog": {"claimer_status": {"status": "needs_2fa"}},
                "store_claimer": {"running": False, "auto_claim_enabled": True, "interval_hours": 6},
                "system": {},
            }):
                req = urllib.request.Request(f"http://127.0.0.1:{port}/api/status", headers=hdrs)
                with urllib.request.urlopen(req, timeout=5) as r:
                    st_data = json.loads(r.read().decode("utf-8"))
                self.assertIn("store_claimer", st_data)
                self.assertEqual(st_data["gog"]["claimer_status"]["status"], "needs_2fa")

            # 2. /api/stores/2fa-code rejects invalid code with 400
            bad_req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/stores/2fa-code",
                data=json.dumps({"store": "gog", "code": "12"}).encode("utf-8"),
                headers=hdrs,
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(bad_req, timeout=5)
            self.assertEqual(ctx.exception.code, 400)

            # 3. /api/stores/2fa-code accepts valid code & triggers claimer
            with patch("src.web.server.save_pending_2fa") as mock_save, patch(
                "src.web.server.trigger_store_claimer", return_value={"success": True, "message": "Triggered GOG"}
            ) as mock_trig:
                ok_req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/stores/2fa-code",
                    data=json.dumps({"store": "gog", "code": "1234", "trigger": True}).encode("utf-8"),
                    headers=hdrs,
                    method="POST",
                )
                with urllib.request.urlopen(ok_req, timeout=5) as r:
                    resp = json.loads(r.read().decode("utf-8"))
                self.assertTrue(resp["success"])
                mock_save.assert_called_once()
                mock_trig.assert_called_once()

            # 4. /api/test-service for amazon & gog
            class _FakeResp:
                status = 200
                def __enter__(self): return self
                def __exit__(self, *a): return False

            import http.client
            for svc in ("amazon", "gog"):
                with patch("src.web.server.urllib.request.urlopen", return_value=_FakeResp()):
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    conn.request("POST", "/api/test-service", body=json.dumps({"service": svc}).encode("utf-8"), headers=hdrs)
                    resp_obj = conn.getresponse()
                    t_resp = json.loads(resp_obj.read().decode("utf-8"))
                    conn.close()
                self.assertTrue(t_resp["success"])
        finally:
            httpd.shutdown()
            httpd.server_close()
            web_server.ACTIVE_SESSIONS.clear()
            web_server.ACTIVE_SESSIONS.update(orig_sessions)

    def test_templates_campaign_views_and_zero_cdn(self):
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        index_path = os.path.join(repo_root, "src", "web", "templates", "index.html")
        login_path = os.path.join(repo_root, "src", "web", "templates", "login.html")

        with open(index_path, "r", encoding="utf-8") as f:
            index_html = f.read()
        with open(login_path, "r", encoding="utf-8") as f:
            login_html = f.read()

        for marker in (
            "campaign-view-switcher",
            "autoloot_campaign_view",
            "drops-view-grid",
            "drops-view-compact",
            "drops-view-list",
            "setCampaignView",
            "toggleCampaignDrops",
            "expandedCampaignIds",
            "submitStore2faCode",
            ".accounts-grid",
            ".account-card",
            "cover_url",
            "getGameCoverFallback",
            "drop-thumb",
            "d.image_url",
            'referrerpolicy="no-referrer"',
            "_calcCampaignProgress",
        ):
            self.assertIn(marker, index_html)

        self.assertNotIn("lastTelemetryData", index_html)

        for html in (index_html, login_html):
            for cdn in ("googleapis.com", "cdnjs.cloudflare.com", "unpkg.com", "jsdelivr.net"):
                self.assertNotIn(cdn, html)


if __name__ == "__main__":
    unittest.main()
