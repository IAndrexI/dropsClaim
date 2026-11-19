import copy
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from src.auth import twitch_auth
from src.twitch_drops import activity_logger
from src.twitch_drops import browser_watcher
from src.twitch_drops import miner as m
from src.twitch_drops import pubsub
from src.twitch_drops import twitch_api as t_api
from src.web import server as s


def _reset_shared_state():
    t_api._TWITCH_RATE_LIMITER.reset()
    with t_api._DEVICE_ID_LOCK:
        t_api._PERSISTENT_DEVICE_ID = ""
        t_api._PERSISTENT_DEVICE_ID_SOURCE = ""
    with t_api._INTEGRITY_LOCK:
        t_api._INTEGRITY_CACHE.clear()
        t_api._INTEGRITY_FAIL_UNTIL = 0.0
        t_api._INTEGRITY_FAIL_STREAK = 0
    t_api._ENABLE_INTEGRITY_COOLDOWN_IN_TEST = False
    t_api._VIEWER_DROPS_DASHBOARD_BLOCKED_UNTIL = 0.0

    with s._SHARED_CLIENT_LOCK:
        s._SHARED_TWITCH_CLIENT = None
        s._SHARED_TWITCH_TOKEN = ""
    with s._TWITCH_OVERVIEW_LOCK:
        s._TWITCH_OVERVIEW_CACHE.clear()
        s._TWITCH_OVERVIEW_CACHE.update({
            "ts": 0.0,
            "token": "",
            "username": None,
            "user_id": None,
            "overview": None,
            "last_force_ts": 0.0,
        })
    s._ENABLE_STATUS_CACHE_IN_TEST = False


class BrowserWatcherAntiSpamTests(unittest.TestCase):
    def setUp(self):
        _reset_shared_state()
        self.tmp = tempfile.mkdtemp()
        self.orig_bw_data_dir = browser_watcher.DATA_DIR

    def tearDown(self):
        browser_watcher.DATA_DIR = self.orig_bw_data_dir
        _reset_shared_state()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cdp_capture_headers_expiration_and_stop_reset(self):
        bw = browser_watcher.HeadlessBrowserWatcher(profile_dir=self.tmp)
        bw._on_cdp_event("Network.requestWillBeSent", {
            "request": {
                "url": "https://gql.twitch.tv/gql",
                "headers": {
                    "Client-Integrity": "v4.local.captured_tok_abc",
                    "client-session-id": "sess_cdp_999",
                    "X-DEVICE-ID": "dev_cdp_777",
                },
            }
        })

        self.assertEqual(bw.captured_integrity_token, "v4.local.captured_tok_abc")
        self.assertGreater(bw.captured_integrity_time, 0.0)
        self.assertEqual(bw.captured_client_session_id, "sess_cdp_999")
        self.assertEqual(bw.captured_device_id, "dev_cdp_777")
        self.assertEqual(bw.get_captured_integrity_token(12600.0), "v4.local.captured_tok_abc")

        # Expired token must return empty string
        bw.captured_integrity_time = time.time() - 13000.0
        self.assertEqual(bw.get_captured_integrity_token(12600.0), "")

        # stop() must reset captured integrity/session/rate-limit fields
        bw.last_gql_rate_limited_time = time.time()
        bw.stop()
        self.assertEqual(bw.captured_integrity_token, "")
        self.assertEqual(bw.captured_integrity_time, 0.0)
        self.assertEqual(bw.captured_client_session_id, "")
        self.assertEqual(bw.last_gql_rate_limited_time, 0.0)

    def test_persist_integrity_cache_writes_json_atomically(self):
        bw = browser_watcher.HeadlessBrowserWatcher(profile_dir=self.tmp)
        bw.captured_integrity_token = "v4.local.secret_persist_tok"
        bw.captured_client_session_id = "sess_persist"
        bw.captured_device_id = "dev_persist"
        now = 1700000000.0
        with mock.patch.object(browser_watcher, "DATA_DIR", self.tmp):
            bw._persist_integrity_cache(now)
        cache_path = os.path.join(self.tmp, "twitch_integrity_cache.json")
        self.assertTrue(os.path.exists(cache_path))
        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["token"], "v4.local.secret_persist_tok")
        self.assertEqual(data["captured_at"], now)
        self.assertEqual(data["expires_at"], now + 12600)
        self.assertEqual(data["client_session_id"], "sess_persist")
        self.assertEqual(data["device_id"], "dev_persist")

    def test_cdp_429_detection_and_is_recently_rate_limited(self):
        bw = browser_watcher.HeadlessBrowserWatcher(profile_dir=self.tmp)
        self.assertFalse(bw.is_recently_rate_limited())

        with mock.patch.object(browser_watcher, "log_event") as mock_log:
            bw._on_cdp_event("Network.responseReceived", {
                "response": {
                    "url": "https://gql.twitch.tv/gql",
                    "status": 429,
                }
            })

        self.assertEqual(bw.last_gql_status, 429)
        self.assertTrue(bw.is_recently_rate_limited(window_secs=300.0))
        mock_log.assert_called_with(
            "BROWSER_WATCHER",
            "gql_rate_limited_429",
            {"url": "https://gql.twitch.tv/gql"},
            level="WARNING",
        )

        # Outside window returns False
        bw.last_gql_rate_limited_time = time.time() - 400.0
        self.assertFalse(bw.is_recently_rate_limited(window_secs=300.0))

    def test_execute_gql_in_browser_injects_captured_integrity_and_session(self):
        bw = browser_watcher.HeadlessBrowserWatcher(profile_dir=self.tmp)
        bw.is_available = lambda: True
        bw._is_proc_alive = lambda: True
        bw.captured_integrity_token = "v4.local.injected_integrity"
        bw.captured_integrity_time = time.time()
        bw.captured_client_session_id = "sess_live_cdp"
        bw.captured_device_id = "dev_live_cdp"

        call_log = []
        drained = []

        class FakeCDP:
            def drain_events(self, timeout=0.05):
                drained.append(True)

            def call(self, method, params=None, timeout=12.0):
                call_log.append((method, params))
                return {"result": {"value": {"data": {"claimDropRewards": {"status": "SUCCESS"}}}}}

        bw.cdp = FakeCDP()
        res = bw.execute_gql_in_browser(
            "DropsPage_ClaimDropRewards",
            "hash_abc",
            variables={"input": {"dropInstanceID": "drop_1"}},
            is_persisted=True,
            auth_token="auth_tok_1",
        )
        self.assertEqual(res, {"data": {"claimDropRewards": {"status": "SUCCESS"}}})
        self.assertEqual(len(drained), 1)
        self.assertEqual(len(call_log), 1)
        method, params = call_log[0]
        self.assertEqual(method, "Runtime.evaluate")
        expr = params["expression"]
        self.assertIn("v4.local.injected_integrity", expr)
        self.assertIn("sess_live_cdp", expr)
        self.assertIn("dev_live_cdp", expr)
        self.assertIn("hdrs['Client-Integrity'] = integrityTok", expr)

    def test_configure_session_ignores_session_id_and_preserves_client_session_id(self):
        bw = browser_watcher.HeadlessBrowserWatcher(profile_dir=self.tmp)
        calls = []

        class FakeCDP:
            def call(self, method, params=None, timeout=8.0):
                calls.append((method, params))
                return {"success": True}

        bw.cdp = FakeCDP()
        bw._configure_session(
            auth_token="tok_1",
            device_id="dev_1",
            user_login="viewer1",
            user_id="42",
            session_id="sess_a",
        )
        first_count = len(calls)
        init_scripts = [p["source"] for m_name, p in calls if m_name == "Page.addScriptToEvaluateOnNewDocument"]
        self.assertEqual(len(init_scripts), 1)
        self.assertIn('!localStorage.getItem("client_session_id")', init_scripts[0])

        # Changing only session_id must NOT re-inject cookies
        bw._configure_session(
            auth_token="tok_1",
            device_id="dev_1",
            user_login="viewer1",
            user_id="42",
            session_id="sess_b",
        )
        self.assertEqual(len(calls), first_count)

    def test_pubsub_uses_platform_ua_and_exponential_backoff(self):
        pw = pubsub.TwitchPubSubWatcher()
        self.assertEqual(pw.user_agent, browser_watcher.get_platform_user_agent())

        sleep_calls = []
        pw._running = True

        def fake_sleep(seconds):
            sleep_calls.append(seconds)
            if len(sleep_calls) >= 4:
                pw._running = False
                return False
            return True

        with mock.patch.object(pw, "_connect_and_handshake", return_value=None), \
             mock.patch.object(pw, "_sleep_interruptible", side_effect=fake_sleep):
            pw._run_loop()

        self.assertEqual(sleep_calls, [10, 20, 40, 60])


class TwitchApiAntiSpamTests(unittest.TestCase):
    def setUp(self):
        _reset_shared_state()
        self.tmp = tempfile.mkdtemp()
        self.orig_dh_cache = copy.deepcopy(t_api.DROPHUNTER_CACHE)
        self.p_api = mock.patch.multiple(
            t_api,
            DATA_DIR=self.tmp,
            DEVICE_ID_FILE=os.path.join(self.tmp, "twitch_device_id.json"),
            INTEGRITY_CACHE_FILE=os.path.join(self.tmp, "twitch_integrity_cache.json"),
            INVENTORY_CACHE_FILE=os.path.join(self.tmp, "twitch_inventory_cache.json"),
            CAMPAIGNS_CACHE_FILE=os.path.join(self.tmp, "twitch_campaigns_cache.json"),
        )
        self.p_api.start()
        self.p_tok = mock.patch("src.auth.twitch_auth.load_token", return_value=None)
        self.p_tok.start()

    def tearDown(self):
        self.p_tok.stop()
        self.p_api.stop()
        t_api.DROPHUNTER_CACHE.clear()
        t_api.DROPHUNTER_CACHE.update(self.orig_dh_cache)
        _reset_shared_state()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_activity_logger_redacts_integrity_and_ua_unified(self):
        self.assertIn("integrity", activity_logger._SENSITIVE_KEYS)
        redacted = activity_logger.sanitize_dict({
            "client_integrity": "v4.local.supersecret_integrity",
            "integrity": "v4.local.raw_tok",
            "channel": "shroud",
        })
        self.assertEqual(redacted["client_integrity"], "[REDACTED]")
        self.assertEqual(redacted["integrity"], "[REDACTED]")
        self.assertEqual(redacted["channel"], "shroud")

        expected_ua = browser_watcher.get_platform_user_agent()
        self.assertEqual(twitch_auth.DEVICE_USER_AGENT, expected_ua)
        client = t_api.TwitchClient("fake_tok")
        self.assertEqual(client.user_agent, expected_ua)

    def test_persistent_device_id_shared_and_reloaded(self):
        c1 = t_api.TwitchClient("tok_a")
        c2 = t_api.TwitchClient("tok_b")
        self.assertEqual(len(c1.device_id), 32)
        self.assertEqual(c1.device_id, c2.device_id)
        self.assertTrue(os.path.exists(t_api.DEVICE_ID_FILE))

        # Reset in-memory global and confirm it reloads the persisted device_id
        with t_api._DEVICE_ID_LOCK:
            t_api._PERSISTENT_DEVICE_ID = ""
            t_api._PERSISTENT_DEVICE_ID_SOURCE = ""
        c3 = t_api.TwitchClient("tok_c")
        self.assertEqual(c3.device_id, c1.device_id)

    def test_shared_integrity_cache_disk_memory_and_fail_cooldown(self):
        # 1. Disk cache hit makes 0 HTTP calls
        with open(t_api.INTEGRITY_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "token": "v4.local.from_browser_disk",
                "expires_at": time.time() + 3600.0,
            }, f)

        c1 = t_api.TwitchClient("shared_tok")
        with mock.patch("urllib.request.urlopen") as mock_open:
            tok = c1.get_client_integrity_token()
            self.assertEqual(tok, "v4.local.from_browser_disk")
            mock_open.assert_not_called()

        # 2. In-memory _INTEGRITY_CACHE shared across clients
        os.remove(t_api.INTEGRITY_CACHE_FILE)
        c1._integrity_token = ""
        c1._integrity_expiration = 0
        with t_api._INTEGRITY_LOCK:
            t_api._INTEGRITY_CACHE.clear()

        fake_resp = mock.MagicMock()
        fake_resp.read.return_value = json.dumps({
            "token": "v4.local.live_shared_mem",
            "expiration": int(time.time() * 1000) + 3600000,
        }).encode("utf-8")
        cm = mock.MagicMock()
        cm.__enter__.return_value = fake_resp
        cm.__exit__.return_value = False

        with mock.patch("urllib.request.urlopen", return_value=cm) as mock_open:
            tok1 = c1.get_client_integrity_token(force_refresh=True)
            self.assertEqual(tok1, "v4.local.live_shared_mem")
            self.assertEqual(mock_open.call_count, 1)

            c2 = t_api.TwitchClient("shared_tok")
            tok2 = c2.get_client_integrity_token()
            self.assertEqual(tok2, "v4.local.live_shared_mem")
            self.assertEqual(mock_open.call_count, 1)

        # 3. HTTP 429 on /integrity trips cooldown & skips subsequent /integrity calls
        t_api._ENABLE_INTEGRITY_COOLDOWN_IN_TEST = True
        with t_api._INTEGRITY_LOCK:
            t_api._INTEGRITY_CACHE.clear()
        c3 = t_api.TwitchClient("other_tok")
        err_429 = urllib.error.HTTPError("https://gql.twitch.tv/integrity", 429, "Too Many Requests", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err_429) as mock_open:
            tok_fail = c3.get_client_integrity_token(force_refresh=True)
            self.assertEqual(tok_fail, "")
            self.assertTrue(c3.is_rate_limited())
            self.assertEqual(mock_open.call_count, 1)

            # Second call during fail cooldown must skip urlopen completely
            tok_skipped = c3.get_client_integrity_token(force_refresh=True)
            self.assertEqual(tok_skipped, "")
            self.assertEqual(mock_open.call_count, 1)

    def test_rate_limiter_trips_on_429_403_and_repeated_integrity_check_failed(self):
        rl = t_api.TwitchRateLimiter()
        c1 = rl.record_rate_limit("http_429", base_cooldown=300.0)
        c2 = rl.record_rate_limit("http_429", base_cooldown=300.0)
        c3 = rl.record_rate_limit("http_429", base_cooldown=300.0)
        c4 = rl.record_rate_limit("http_429", base_cooldown=300.0)
        self.assertEqual((c1, c2, c3, c4), (300.0, 600.0, 1200.0, 1800.0))
        self.assertTrue(rl.is_rate_limited())
        self.assertGreater(rl.cooldown_remaining(), 0.0)
        rl.rate_limited_until = time.time() - 1.0
        rl.record_success()
        self.assertEqual(rl.strikes, 0)

        # Client _make_request trips global limiter on 429 and 403 on gql.twitch.tv
        client = t_api.TwitchClient("tok_rl")
        err_429 = urllib.error.HTTPError("https://gql.twitch.tv/gql", 429, "Too Many Requests", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err_429):
            with self.assertRaises(urllib.error.HTTPError):
                client._make_request("https://gql.twitch.tv/gql", b"{}", {})
        self.assertTrue(client.is_rate_limited())

        t_api._TWITCH_RATE_LIMITER.reset()
        err_403 = urllib.error.HTTPError("https://gql.twitch.tv/gql", 403, "Forbidden", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err_403):
            with self.assertRaises(urllib.error.HTTPError):
                client._make_request("https://gql.twitch.tv/gql", b"{}", {})
        self.assertTrue(client.is_rate_limited())

        # Repeated IntegrityCheckFailed (2 consecutive) trips rate limiter
        t_api._TWITCH_RATE_LIMITER.reset()
        integrity_err_payload = json.dumps({
            "errors": [{"message": "failed integrity check", "extensions": {"code": "IntegrityCheckFailed"}}]
        }).encode("utf-8")
        with mock.patch.object(client, "get_client_integrity_token", return_value="same_tok"), \
             mock.patch.object(client, "_make_request", return_value=integrity_err_payload):
            client.post_gql("query { currentUser { id } }", operation_name="Op1")
            self.assertFalse(client.is_rate_limited())
            client.post_gql("query { currentUser { id } }", operation_name="Op2")
            self.assertTrue(client.is_rate_limited())

    def test_viewer_drops_dashboard_6h_cooldown_and_60m_campaign_cache(self):
        client = t_api.TwitchClient("tok_dash")
        persisted_ops = []

        def fake_post_persisted(operation_name, sha256_hash, variables=None):
            persisted_ops.append(operation_name)
            return {"errors": [{"message": "failed integrity check", "extensions": {"code": "IntegrityCheckFailed"}}]}

        with mock.patch.object(client, "post_persisted_gql", side_effect=fake_post_persisted), \
             mock.patch.object(client, "post_gql", return_value={"data": {"games": {"edges": []}, "streams": {"edges": []}}}):
            client.fetch_twitch_gql_live_campaigns()
            self.assertEqual(persisted_ops.count("ViewerDropsDashboard"), 1)
            self.assertGreater(t_api._VIEWER_DROPS_DASHBOARD_BLOCKED_UNTIL, time.time() + 21000.0)

            # Second call within 6h must skip ViewerDropsDashboard completely
            client.fetch_twitch_gql_live_campaigns()
            self.assertEqual(persisted_ops.count("ViewerDropsDashboard"), 1)

        # 60m campaign cache TTL: 30m old cache must NOT hit drophunter or Twitch GQL
        t_api.DROPHUNTER_CACHE["timestamp"] = time.time() - 1800.0
        t_api.DROPHUNTER_CACHE["campaigns"] = [{
            "id": "camp_rust",
            "name": "Rust Drops",
            "game": {"displayName": "Rust"},
            "status": "ACTIVE",
            "timeBasedDrops": [{"id": "d1", "name": "Skin", "requiredMinutesWatched": 60, "requiredSubs": 0}],
        }]
        with mock.patch("urllib.request.urlopen") as mock_open, \
             mock.patch.object(client, "fetch_twitch_gql_live_campaigns") as mock_gql_live:
            dh = t_api.fetch_drophunter_live_campaigns()
            self.assertEqual(len(dh), 1)
            mock_open.assert_not_called()

            camps = client.get_all_active_campaigns(priority_games=["Rust"], inv_override={})
            self.assertEqual(len(camps), 1)
            mock_gql_live.assert_not_called()

    def test_find_eligible_channel_sequential_early_exit(self):
        client = t_api.TwitchClient("tok_seq")
        edges = [
            {
                "node": {
                    "id": f"s{i}",
                    "title": "Rust !drops",
                    "viewersCount": 1000 - i,
                    "freeformTags": [{"name": "DropsEnabled"}],
                    "broadcaster": {"id": f"b{i}", "login": f"streamer_{i}", "displayName": f"Streamer{i}"},
                }
            }
            for i in range(1, 6)
        ]
        checked_bids = []

        def fake_post_persisted(operation_name, sha256_hash, variables=None):
            bid = (variables or {}).get("channelID")
            checked_bids.append(bid)
            if bid == "b2":
                return {
                    "data": {
                        "channel": {
                            "viewerDropCampaigns": [{
                                "id": "c1",
                                "name": "Rust Drops",
                                "game": {"displayName": "Rust"},
                                "status": "ACTIVE",
                                "timeBasedDrops": [{"id": "d1", "requiredMinutesWatched": 60, "requiredSubs": 0}],
                            }]
                        }
                    }
                }
            return {"data": {"channel": {"viewerDropCampaigns": []}}}

        with mock.patch.object(client, "post_gql", return_value={"data": {"game": {"id": "999", "name": "Rust", "streams": {"edges": edges}}}}), \
             mock.patch.object(client, "post_persisted_gql", side_effect=fake_post_persisted):
            chosen = client.find_eligible_channel("Rust")

        self.assertIsNotNone(chosen)
        self.assertEqual(chosen["channel_login"], "streamer_2")
        self.assertEqual(checked_bids, ["b1", "b2"])

    def test_list_eligible_channels_single_batch(self):
        client = t_api.TwitchClient("tok_batch")
        edges = [
            {
                "node": {
                    "id": f"s{i}",
                    "title": "Rust !drops",
                    "viewersCount": 500 - i,
                    "freeformTags": [{"name": "DropsEnabled"}],
                    "broadcaster": {"id": f"b{i}", "login": f"ch_{i}", "displayName": f"Ch{i}"},
                }
            }
            for i in range(1, 5)
        ]
        batch_calls = []

        def fake_batch(ops, timeout=15):
            batch_calls.append(ops)
            return [
                {
                    "data": {
                        "channel": {
                            "viewerDropCampaigns": [{
                                "id": "c1",
                                "name": "Rust Drops",
                                "game": {"displayName": "Rust"},
                                "status": "ACTIVE",
                                "timeBasedDrops": [{"id": "d1", "requiredMinutesWatched": 60, "requiredSubs": 0}],
                            }]
                        }
                    }
                }
                for _ in ops
            ]

        with mock.patch.object(client, "post_gql", return_value={"data": {"game": {"id": "999", "name": "Rust", "streams": {"edges": edges}}}}), \
             mock.patch.object(client, "post_gql_batch", side_effect=fake_batch), \
             mock.patch.object(client, "post_persisted_gql") as mock_persisted:
            channels = client.list_eligible_channels("Rust", limit=12)

        self.assertEqual(len(channels), 4)
        self.assertEqual(len(batch_calls), 1)
        self.assertEqual(len(batch_calls[0]), 4)
        mock_persisted.assert_not_called()

    def test_browser_routed_get_inventory_and_drop_current_session(self):
        client = t_api.TwitchClient("tok_bw")

        class FakeBW:
            def __init__(self, responses):
                self.responses = list(responses)
                self.calls = []

            def is_watching(self):
                return True

            def execute_gql_in_browser(self, operation_name, query_or_hash, variables=None, is_persisted=True):
                self.calls.append((operation_name, variables))
                return self.responses.pop(0)

        bw_ok = FakeBW([
            {"data": {"currentUser": {"inventory": {"dropCampaignsInProgress": [{"id": "c1"}]}}}},
            {"data": {"currentUser": {"dropCurrentSession": {"dropID": "d1", "currentMinutesWatched": 12, "requiredMinutesWatched": 60}}}},
        ])
        with mock.patch.object(client, "post_persisted_gql") as mock_http:
            inv = client.get_inventory(browser_watcher=bw_ok)
            sess = client.get_drop_current_session("1234", "streamer1", browser_watcher=bw_ok)
            mock_http.assert_not_called()
        self.assertEqual(inv["dropCampaignsInProgress"][0]["id"], "c1")
        self.assertEqual(sess["currentMinutesWatched"], 12)
        self.assertEqual([c[0] for c in bw_ok.calls], ["Inventory", "DropCurrentSessionContext"])

        # Integrity error in browser aborts without HTTP fallback
        bw_err = FakeBW([
            {"errors": [{"extensions": {"code": "IntegrityCheckFailed"}, "message": "failed integrity check"}]},
            {"status": 429, "error": "Too Many Requests"},
        ])
        with mock.patch.object(client, "post_persisted_gql") as mock_http:
            self.assertEqual(client.get_inventory(browser_watcher=bw_err), {})
            self.assertEqual(client.get_drop_current_session("1234", "streamer1", browser_watcher=bw_err), {})
            mock_http.assert_not_called()

        # HTTP path must NOT fire second channelLogin fallback when first returns GQL errors
        with mock.patch.object(client, "post_persisted_gql", return_value={"errors": [{"message": "failed integrity check"}]}) as mock_http:
            self.assertEqual(client.get_drop_current_session("1234", "streamer1"), {})
            self.assertEqual(mock_http.call_count, 1)

    def test_claim_drop_aborts_immediately_on_integrity_or_429(self):
        client = t_api.TwitchClient("tok_claim")
        comp_id = "130742243#camp123#uuid-456"

        # 1. Browser IntegrityCheckFailed -> immediate False, 1 browser call, 0 HTTP calls
        bw_calls = []

        class FakeBW:
            def is_watching(self):
                return True

            def execute_gql_in_browser(self, operation_name, query_or_hash, variables=None, is_persisted=True):
                bw_calls.append(variables["input"]["dropInstanceID"])
                return {"errors": [{"message": "failed integrity check", "extensions": {"code": "IntegrityCheckFailed"}}]}

        with mock.patch.object(client, "post_persisted_gql") as mock_p, \
             mock.patch.object(client, "post_gql") as mock_g:
            self.assertFalse(client.claim_drop(comp_id, browser_watcher=FakeBW()))
            self.assertEqual(len(bw_calls), 1)
            mock_p.assert_not_called()
            mock_g.assert_not_called()

        # 2. HTTP IntegrityCheckFailed -> immediate False after 1 persisted call, no candidate cascade or raw fallback
        with mock.patch.object(
            client,
            "post_persisted_gql",
            return_value={"errors": [{"message": "failed integrity check", "extensions": {"code": "IntegrityCheckFailed"}}]},
        ) as mock_p, mock.patch.object(client, "post_gql") as mock_g:
            self.assertFalse(client.claim_drop(comp_id))
            self.assertEqual(mock_p.call_count, 1)
            mock_g.assert_not_called()

        # 3. HTTP 429 -> immediate False after 1 persisted call
        err_429 = urllib.error.HTTPError("https://gql.twitch.tv/gql", 429, "Too Many Requests", {}, None)
        with mock.patch.object(client, "post_persisted_gql", side_effect=err_429) as mock_p, \
             mock.patch.object(client, "post_gql") as mock_g:
            self.assertFalse(client.claim_drop(comp_id))
            self.assertEqual(mock_p.call_count, 1)
            mock_g.assert_not_called()


class DropsMinerAntiSpamTests(unittest.TestCase):
    def setUp(self):
        _reset_shared_state()
        self.tmp = tempfile.mkdtemp()
        self.cfg_file = os.path.join(self.tmp, "mining_config.json")
        self.stat_file = os.path.join(self.tmp, "miner_status.json")
        self.claim_req_file = os.path.join(self.tmp, "claim_request.json")
        self.inv_cache_file = os.path.join(self.tmp, "twitch_inventory_cache.json")
        with open(self.cfg_file, "w", encoding="utf-8") as f:
            json.dump({"mode": "default", "queue": ["Rust"], "updated_at": 100}, f)
        self.p_miner = mock.patch.multiple(
            m,
            DATA_DIR=self.tmp,
            CONFIG_FILE=self.cfg_file,
            STATUS_FILE=self.stat_file,
            CLAIM_REQUEST_FILE=self.claim_req_file,
        )
        self.p_api = mock.patch.multiple(
            t_api,
            DATA_DIR=self.tmp,
            INVENTORY_CACHE_FILE=self.inv_cache_file,
            DEVICE_ID_FILE=os.path.join(self.tmp, "twitch_device_id.json"),
        )
        self.p_miner.start()
        self.p_api.start()
        self.p_tok = mock.patch("src.auth.twitch_auth.load_token", return_value=None)
        self.p_tok.start()

    def tearDown(self):
        self.p_tok.stop()
        self.p_api.stop()
        self.p_miner.stop()
        _reset_shared_state()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_camp_and_channel(self, watched=0, required=60):
        camp = {
            "id": "camp_rust",
            "name": "Rust Drops",
            "game": {"displayName": "Rust"},
            "status": "ACTIVE",
            "timeBasedDrops": [{
                "id": "drop_rust_1",
                "name": "Rust Crate",
                "requiredMinutesWatched": required,
                "requiredSubs": 0,
                "self": {
                    "isClaimed": False,
                    "currentMinutesWatched": watched,
                    "dropInstanceID": "inst_rust_1",
                },
            }],
        }
        channel = {
            "channel_id": "555111",
            "channel_login": "rust_pro",
            "channel_name": "RustPro",
            "stream_id": "stream_555",
            "stream_title": "Rust !drops",
            "viewers": 1200,
            "game_name": "Rust",
            "game_id": "999",
            "verified_drops": True,
            "has_drops_in_title": True,
        }
        return camp, channel

    def test_skips_pubsub_session_presence_and_5m_avail_when_browser_active(self):
        dm = m.DropsMiner("fake_token")
        dm.stream_cycle_minutes = 6
        dm.client.user_id = "42"
        dm.client.user_login = "viewer"
        camp, channel = self._make_camp_and_channel(watched=0, required=60)

        dm.browser_watcher.is_available = lambda: True
        dm.browser_watcher.watch_channel = mock.MagicMock(return_value={"running": True, "video_found": True})
        dm.browser_watcher.is_watching = lambda login="": True
        dm.browser_watcher.tick = mock.MagicMock(return_value={"running": True, "video_found": True, "paused": False, "playback_advancing": True})
        dm.browser_watcher.last_state = {"running": True, "video_found": True, "paused": False, "playback_advancing": True}

        sess_minutes = []

        def fake_get_sess(cid, clogin="", browser_watcher=None):
            sess_minutes.append(len(sess_minutes) + 1)
            return {
                "dropID": "drop_rust_1",
                "currentMinutesWatched": len(sess_minutes),
                "requiredMinutesWatched": 60,
            }

        with mock.patch.dict(os.environ, {"TWITCH_PAUSE_WHEN_USER_ACTIVE": "false"}), \
             mock.patch.object(dm, "_refresh_auth_if_needed", return_value=True), \
             mock.patch.object(dm.client, "get_inventory", return_value={"dropCampaignsInProgress": [camp]}), \
             mock.patch.object(dm.client, "get_all_active_campaigns", return_value=[camp]), \
             mock.patch.object(dm.client, "find_eligible_channel", return_value=channel), \
             mock.patch.object(dm.client, "new_session_id") as mock_new_sess, \
             mock.patch.object(dm.pubsub_watcher, "start") as mock_pubsub_start, \
             mock.patch.object(dm.pubsub_watcher, "stop") as mock_pubsub_stop, \
             mock.patch.object(dm.client, "open_browser_stream_session") as mock_open_sess, \
             mock.patch.object(dm.client, "get_playback_stream_info") as mock_pb, \
             mock.patch.object(dm.client, "get_user_presence_info") as mock_presence, \
             mock.patch.object(dm.client, "is_stream_and_drops_available") as mock_avail, \
             mock.patch.object(dm.client, "get_drop_current_session", side_effect=fake_get_sess), \
             mock.patch.object(dm.client, "send_minute_watched_heartbeat") as mock_hb, \
             mock.patch.object(dm, "_interruptible_sleep", return_value=False):
            dm.run_cycle()

        mock_new_sess.assert_not_called()
        mock_pubsub_start.assert_not_called()
        self.assertGreaterEqual(mock_pubsub_stop.call_count, 1)
        mock_open_sess.assert_not_called()
        mock_pb.assert_not_called()
        mock_presence.assert_not_called()
        mock_avail.assert_not_called()
        mock_hb.assert_not_called()

    def test_throttles_drop_current_session_to_3m_and_polls_minute1_and_remaining_le_2(self):
        # Part A: 6-minute run with remaining=10 -> polls only minute 0, 2, 5 (mins 1, 3, 6)
        dm = m.DropsMiner("fake_token")
        dm.stream_cycle_minutes = 6
        dm.client.user_id = "42"
        dm.client.user_login = "viewer"
        camp, channel = self._make_camp_and_channel(watched=10, required=20)

        dm.browser_watcher.is_available = lambda: True
        dm.browser_watcher.watch_channel = mock.MagicMock(return_value={"running": True})
        dm.browser_watcher.is_watching = lambda login="": True
        dm.browser_watcher.tick = mock.MagicMock(return_value={"running": True, "video_found": True, "paused": False, "playback_advancing": True})
        dm.browser_watcher.last_state = {"running": True, "video_found": True, "paused": False, "playback_advancing": True}

        loop_minute = {"idx": -1}
        sess_poll_indices = []

        def fake_sleep(total_secs, sig):
            loop_minute["idx"] += 1
            return False

        def fake_get_sess(cid, clogin="", browser_watcher=None):
            sess_poll_indices.append(loop_minute["idx"])
            return {
                "dropID": "drop_rust_1",
                "currentMinutesWatched": 11 + len(sess_poll_indices),
                "requiredMinutesWatched": 20,
            }

        with mock.patch.dict(os.environ, {"TWITCH_PAUSE_WHEN_USER_ACTIVE": "false"}), \
             mock.patch.object(dm, "_refresh_auth_if_needed", return_value=True), \
             mock.patch.object(dm.client, "get_inventory", return_value={"dropCampaignsInProgress": [camp]}), \
             mock.patch.object(dm.client, "get_all_active_campaigns", return_value=[camp]), \
             mock.patch.object(dm.client, "find_eligible_channel", return_value=channel), \
             mock.patch.object(dm.client, "get_drop_current_session", side_effect=fake_get_sess), \
             mock.patch.object(dm, "_interruptible_sleep", side_effect=fake_sleep):
            dm.run_cycle()

        self.assertEqual(sess_poll_indices, [0, 2, 5])

        # Part B: When remaining <= 2, polls every minute (including minutes 1, 3, 4)
        dm2 = m.DropsMiner("fake_token")
        dm2.stream_cycle_minutes = 4
        dm2.client.user_id = "42"
        dm2.client.user_login = "viewer"
        camp2, channel2 = self._make_camp_and_channel(watched=18, required=20)
        dm2.browser_watcher.is_available = lambda: True
        dm2.browser_watcher.watch_channel = mock.MagicMock(return_value={"running": True})
        dm2.browser_watcher.is_watching = lambda login="": True
        dm2.browser_watcher.tick = mock.MagicMock(return_value={"running": True, "video_found": True, "paused": False, "playback_advancing": True})
        dm2.browser_watcher.last_state = {"running": True, "video_found": True, "paused": False, "playback_advancing": True}

        loop_m2 = {"idx": -1}
        poll_m2 = []

        def fake_sleep2(total_secs, sig):
            loop_m2["idx"] += 1
            return False

        def fake_get_sess2(cid, clogin="", browser_watcher=None):
            poll_m2.append(loop_m2["idx"])
            return {
                "dropID": "drop_rust_1",
                "currentMinutesWatched": 18 if loop_m2["idx"] == 0 else 19,
                "requiredMinutesWatched": 20,
            }

        with mock.patch.dict(os.environ, {"TWITCH_PAUSE_WHEN_USER_ACTIVE": "false"}), \
             mock.patch.object(dm2, "_refresh_auth_if_needed", return_value=True), \
             mock.patch.object(dm2.client, "get_inventory", return_value={"dropCampaignsInProgress": [camp2]}), \
             mock.patch.object(dm2.client, "get_all_active_campaigns", return_value=[camp2]), \
             mock.patch.object(dm2.client, "find_eligible_channel", return_value=channel2), \
             mock.patch.object(dm2.client, "get_drop_current_session", side_effect=fake_get_sess2), \
             mock.patch.object(dm2, "_interruptible_sleep", side_effect=fake_sleep2):
            dm2.run_cycle()

        self.assertEqual(poll_m2, [0, 1, 2, 3])

    def test_refuses_channel_hop_during_rate_limit_and_persists_inventory_cache(self):
        dm = m.DropsMiner("fake_token")
        dm.stream_cycle_minutes = 6
        dm.progress_check_minutes = 5
        dm.client.user_id = "42"
        dm.client.user_login = "viewer"
        camp, channel = self._make_camp_and_channel(watched=0, required=60)

        dm.browser_watcher.is_available = lambda: True
        dm.browser_watcher.watch_channel = mock.MagicMock(return_value={"running": True})
        dm.browser_watcher.is_watching = lambda login="": True
        dm.browser_watcher.tick = mock.MagicMock(return_value={"running": True, "video_found": False})
        dm.browser_watcher.last_state = {"running": True, "video_found": False}

        # Trip rate limiter before watch loop (after channel selection)
        def fake_find_channel(*args, **kwargs):
            t_api._TWITCH_RATE_LIMITER.record_rate_limit("http_429", base_cooldown=300.0)
            return channel

        saved_statuses = []
        orig_save_status = m.save_miner_status

        def spy_save_status(st):
            saved_statuses.append(dict(st))
            return orig_save_status(st)

        with mock.patch.dict(os.environ, {"TWITCH_PAUSE_WHEN_USER_ACTIVE": "false"}), \
             mock.patch.object(dm, "_refresh_auth_if_needed", return_value=True), \
             mock.patch.object(dm.client, "get_inventory", return_value={"dropCampaignsInProgress": [camp]}), \
             mock.patch.object(dm.client, "get_all_active_campaigns", return_value=[camp]), \
             mock.patch.object(dm.client, "find_eligible_channel", side_effect=fake_find_channel), \
             mock.patch.object(dm.client, "is_stream_and_drops_available") as mock_avail, \
             mock.patch.object(dm, "_mark_channel_unproductive") as mock_unprod, \
             mock.patch.object(m, "save_miner_status", side_effect=spy_save_status), \
             mock.patch.object(dm, "_interruptible_sleep", return_value=False):
            dm.run_cycle()

        mock_avail.assert_not_called()
        mock_unprod.assert_not_called()
        self.assertEqual(dm.unproductive_channels, {})

        # All 6 minutes ran without stall-hopping, and per-minute status recorded rate_limited=True
        minute_statuses = [st for st in saved_statuses if st.get("active") and st.get("rate_limited")]
        self.assertGreaterEqual(len(minute_statuses), 6)
        self.assertEqual(minute_statuses[-1]["minutes_watched"], 6)
        self.assertGreater(minute_statuses[-1]["rate_limit_cooldown_secs"], 0)

        # Inventory cache must be persisted and readable via load_inventory_cache
        bundle = t_api.load_inventory_cache(max_age_secs=300.0)
        self.assertIsNotNone(bundle)
        self.assertEqual(bundle["inventory"]["dropCampaignsInProgress"][0]["id"], "camp_rust")

    def test_processes_external_claim_request_json_direct_and_in_sleep(self):
        dm = m.DropsMiner("fake_token")
        dm.running = True
        camp, _ = self._make_camp_and_channel(watched=60, required=60)
        dm.last_inventory = {"dropCampaignsInProgress": [camp]}
        dm.browser_watcher.is_watching = lambda login="": True

        with open(self.claim_req_file, "w", encoding="utf-8") as f:
            json.dump({"drop_instance_id": "inst_rust_1", "requested_at": time.time()}, f)

        with mock.patch.object(dm.client, "claim_drop", return_value=True) as mock_claim:
            claimed = dm.process_external_claim_request()

        self.assertEqual(claimed, 1)
        self.assertFalse(os.path.exists(self.claim_req_file))
        mock_claim.assert_called_once_with("inst_rust_1", browser_watcher=dm.browser_watcher)
        self.assertTrue(dm.last_inventory["dropCampaignsInProgress"][0]["timeBasedDrops"][0]["self"]["isClaimed"])
        self.assertTrue(os.path.exists(self.inv_cache_file))

        # Empty drop_instance_id inside _interruptible_sleep triggers claim_pending_drops(force=True)
        with open(self.claim_req_file, "w", encoding="utf-8") as f:
            json.dump({"drop_instance_id": "", "requested_at": time.time()}, f)

        sig = dm._get_config_signature()
        with mock.patch.object(dm, "claim_pending_drops", return_value=2) as mock_pending:
            dm._interruptible_sleep(0.25, sig)
        mock_pending.assert_called_once_with(force=True)
        self.assertFalse(os.path.exists(self.claim_req_file))


class WebServerAndTemplateAntiSpamTests(unittest.TestCase):
    def setUp(self):
        _reset_shared_state()
        self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmp, "data"), exist_ok=True)
        with open(os.path.join(self.tmp, "data", "mining_config.json"), "w", encoding="utf-8") as f:
            json.dump({"mode": "default", "queue": ["Rust"], "updated_at": 100}, f)
        with open(os.path.join(self.tmp, "data", "miner_status.json"), "w", encoding="utf-8") as f:
            json.dump({
                "active": True,
                "game": "Rust",
                "channel_login": "rust_pro",
                "channel_name": "RustPro",
                "minutes_watched": 3,
                "cycle_minutes": 60,
            }, f)

    def tearDown(self):
        _reset_shared_state()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_shared_twitch_client_reused_for_same_token(self):
        c1 = s._get_shared_twitch_client("tok_shared_1")
        c2 = s._get_shared_twitch_client("tok_shared_1")
        c3 = s._get_shared_twitch_client("tok_shared_2")
        self.assertIs(c1, c2)
        self.assertIsNot(c1, c3)

    def test_build_status_payload_caches_180s_uses_inv_cache_and_skips_session_poll(self):
        cached_inv = {
            "dropCampaignsInProgress": [{
                "id": "camp_1",
                "name": "Rust Drops",
                "game": {"displayName": "Rust"},
                "status": "ACTIVE",
                "timeBasedDrops": [{
                    "id": "drop_1",
                    "name": "Rust Door",
                    "requiredMinutesWatched": 60,
                    "self": {"currentMinutesWatched": 15, "isClaimed": False},
                }],
            }]
        }
        fake_bundle = {
            "inventory": cached_inv,
            "session_ctx": {"currentMinutesWatched": 19},
            "updated_at": time.time(),
        }

        ov_calls = []

        def fake_overview(self_client, priority_games=None, force_refresh=False, inv_override=None):
            ov_calls.append({"priority_games": priority_games, "inv_override": inv_override})
            return {
                "campaigns": [{
                    "id": "camp_1",
                    "name": "Rust Drops",
                    "game": "Rust",
                    "status": "ACTIVE",
                    "is_expired": False,
                    "drops": [{
                        "id": "drop_1",
                        "name": "Rust Door",
                        "status": "IN_PROGRESS",
                        "watched_minutes": 15,
                        "required_minutes": 60,
                        "progress_percent": 25,
                        "remaining_minutes": 45,
                    }],
                }],
                "collected_items": [],
                "needs_collection_drops": [],
                "unavailable_drops": [],
                "total_campaigns": 1,
                "active_campaigns_count": 1,
                "needs_collection_count": 1,
                "unavailable_count": 0,
            }

        with mock.patch("src.web.server.APP_DIR", self.tmp), \
             mock.patch("src.web.server._ENABLE_STATUS_CACHE_IN_TEST", True), \
             mock.patch("src.web.server.load_env_file", return_value={"TWITCH_AUTH_TOKEN": "tok_status"}), \
             mock.patch("src.web.server.resolve_token", return_value="tok_status"), \
             mock.patch("src.web.server.load_stored_token", return_value=None), \
             mock.patch("src.web.server.ensure_miner_daemon_running", return_value=True), \
             mock.patch("src.web.server.is_miner_daemon_alive", return_value=True), \
             mock.patch("src.web.server.load_inventory_cache", return_value=fake_bundle), \
             mock.patch("src.web.server.fetch_epic_freebies", return_value=([], [])), \
             mock.patch("src.web.server.get_claimed_store_titles", return_value={}), \
             mock.patch("src.web.server.get_git_status", return_value={}), \
             mock.patch("src.web.server.get_running_bot_processes", return_value=[]), \
             mock.patch("src.web.server.get_autostart_status", return_value={}), \
             mock.patch.object(t_api.TwitchClient, "validate_session", return_value=True) as mock_val, \
             mock.patch.object(t_api.TwitchClient, "get_inventory") as mock_inv, \
             mock.patch.object(t_api.TwitchClient, "get_drop_current_session") as mock_sess, \
             mock.patch.object(t_api.TwitchClient, "get_drops_overview", autospec=True, side_effect=fake_overview):
            mock_val.side_effect = lambda: True

            # Call 1: populates cache using inv_override from disk cache, 0 get_inventory, 0 get_drop_current_session
            p1 = s.build_status_payload()
            self.assertEqual(len(ov_calls), 1)
            self.assertEqual(ov_calls[0]["inv_override"], cached_inv)
            mock_inv.assert_not_called()
            mock_sess.assert_not_called()
            self.assertEqual(p1["twitch"]["miner_status"]["current_drop_watched"], 19)

            # Call 2 within 180s: served completely from _TWITCH_OVERVIEW_CACHE (0 new API calls)
            p2 = s.build_status_payload()
            self.assertEqual(len(ov_calls), 1)
            self.assertEqual(mock_val.call_count, 1)
            self.assertEqual(p2["twitch"]["total_campaigns"], 1)

            # Call 3 with force_refresh=True within 15s debounce: still served from cache
            s._TWITCH_OVERVIEW_CACHE["last_force_ts"] = time.time()
            s.build_status_payload(force_refresh=True)
            self.assertEqual(len(ov_calls), 1)

            # Call 4 with force_refresh=True after 15s debounce expired: refreshes
            s._TWITCH_OVERVIEW_CACHE["last_force_ts"] = time.time() - 20.0
            s.build_status_payload(force_refresh=True)
            self.assertEqual(len(ov_calls), 2)

            # Call 5 with normal refresh after 180s TTL expired: refreshes
            s._TWITCH_OVERVIEW_CACHE["ts"] = time.time() - 185.0
            s.build_status_payload(force_refresh=False)
            self.assertEqual(len(ov_calls), 3)
            mock_sess.assert_not_called()

    def test_skips_async_initial_heartbeat_when_chromium_available(self):
        fake_channel = {
            "channel_id": "111",
            "channel_login": "rust_pro",
            "channel_name": "RustPro",
            "stream_id": "s111",
            "stream_title": "Rust !drops",
            "viewers": 100,
            "game_name": "Rust",
            "game_id": "999",
            "verified_drops": True,
            "has_drops_in_title": True,
        }

        with mock.patch("src.web.server.APP_DIR", self.tmp), \
             mock.patch("src.web.server.load_env_file", return_value={"TWITCH_AUTH_TOKEN": "tok_hb"}), \
             mock.patch("src.web.server.resolve_token", return_value="tok_hb"), \
             mock.patch("src.web.server.ensure_miner_daemon_running", return_value=True), \
             mock.patch.object(t_api.TwitchClient, "find_eligible_channel", return_value=fake_channel), \
             mock.patch("src.web.server._send_async_initial_heartbeat") as mock_hb:
            # When Chromium is available -> NO initial Python heartbeat spawned
            with mock.patch("src.web.server.find_chromium_binary", return_value="/usr/bin/chromium"):
                s.save_mining_config(mode="default", activated_games=["Rust"], resolve_stream=True)
                s.switch_watched_channel("Rust")
                time.sleep(0.05)
                mock_hb.assert_not_called()

            # When Chromium is NOT available -> initial heartbeat IS spawned
            with mock.patch("src.web.server.find_chromium_binary", return_value=None):
                s.switch_watched_channel("Rust")
                time.sleep(0.05)
                self.assertGreaterEqual(mock_hb.call_count, 1)

    def test_api_claim_drops_routing_and_status_force_param(self):
        orig_sessions = dict(s.ACTIVE_SESSIONS)
        s.ACTIVE_SESSIONS["test_sess_tok"] = 9999999999.0

        with mock.patch("src.web.server.APP_DIR", self.tmp), \
             mock.patch("src.web.server.load_env_file", return_value={"TWITCH_AUTH_TOKEN": "tok_claim_api"}), \
             mock.patch("src.web.server.resolve_token", return_value="tok_claim_api"):
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), s.DashboardHandler)
            port = httpd.server_address[1]
            th = threading.Thread(target=httpd.serve_forever, daemon=True)
            th.start()
            try:
                base_url = f"http://127.0.0.1:{port}"
                hdrs = {"Cookie": "session_id=test_sess_tok", "Content-Type": "application/json"}

                # 1. GET /api/status?force=1 and /api/status
                with mock.patch("src.web.server.build_status_payload", return_value={"ok": True}) as mock_bsp:
                    with urllib.request.urlopen(urllib.request.Request(f"{base_url}/api/status", headers=hdrs), timeout=5) as r:
                        self.assertTrue(json.loads(r.read().decode("utf-8"))["ok"])
                    with urllib.request.urlopen(urllib.request.Request(f"{base_url}/api/status?force=1", headers=hdrs), timeout=5) as r:
                        self.assertTrue(json.loads(r.read().decode("utf-8"))["ok"])
                self.assertEqual(
                    [c.kwargs.get("force_refresh") for c in mock_bsp.call_args_list],
                    [False, True],
                )

                # 2. /api/claim-drops with active miner + Chromium + rate limited -> queues claim_request.json immediately
                t_api._TWITCH_RATE_LIMITER.record_rate_limit("http_429", base_cooldown=300.0)
                with mock.patch("src.web.server.is_miner_daemon_alive", return_value=True), \
                     mock.patch("src.web.server.find_chromium_binary", return_value="/usr/bin/chromium"):
                    req = urllib.request.Request(
                        f"{base_url}/api/claim-drops",
                        data=json.dumps({"drop_instance_id": "drop_inst_rl"}).encode("utf-8"),
                        headers=hdrs,
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=5) as r:
                        resp_rl = json.loads(r.read().decode("utf-8"))
                self.assertIn("Queued in-browser claim via active miner", resp_rl["message"])
                claim_req_path = os.path.join(self.tmp, "data", "claim_request.json")
                self.assertTrue(os.path.exists(claim_req_path))
                with open(claim_req_path, "r", encoding="utf-8") as cf:
                    self.assertEqual(json.load(cf)["drop_instance_id"], "drop_inst_rl")
                os.remove(claim_req_path)
                t_api._TWITCH_RATE_LIMITER.reset()

                # 3. /api/claim-drops with active miner + Chromium (consumed by miner) -> dispatched
                def consume_req():
                    for _ in range(30):
                        if os.path.exists(claim_req_path):
                            os.remove(claim_req_path)
                            return
                        time.sleep(0.05)

                consumer = threading.Thread(target=consume_req, daemon=True)
                consumer.start()
                with mock.patch("src.web.server.is_miner_daemon_alive", return_value=True), \
                     mock.patch("src.web.server.find_chromium_binary", return_value="/usr/bin/chromium"):
                    req = urllib.request.Request(
                        f"{base_url}/api/claim-drops",
                        data=json.dumps({"drop_instance_id": "drop_inst_ok"}).encode("utf-8"),
                        headers=hdrs,
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=5) as r:
                        resp_ok = json.loads(r.read().decode("utf-8"))
                consumer.join(timeout=2)
                self.assertEqual(resp_ok["message"], "In-browser claim dispatched to active miner!")

                # 4. /api/claim-drops without miner: resets overview cache ONLY when claimed > 0
                s._TWITCH_OVERVIEW_CACHE["ts"] = 123456.0
                with mock.patch("src.web.server.is_miner_daemon_alive", return_value=False), \
                     mock.patch("src.web.server.find_chromium_binary", return_value=None), \
                     mock.patch.object(t_api.TwitchClient, "claim_drop", return_value=False):
                    req = urllib.request.Request(
                        f"{base_url}/api/claim-drops",
                        data=json.dumps({"drop_instance_id": "drop_fail"}).encode("utf-8"),
                        headers=hdrs,
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=5) as r:
                        resp_fail = json.loads(r.read().decode("utf-8"))
                self.assertEqual(resp_fail["claimed"], 0)
                self.assertEqual(s._TWITCH_OVERVIEW_CACHE["ts"], 123456.0)

                with mock.patch("src.web.server.is_miner_daemon_alive", return_value=False), \
                     mock.patch("src.web.server.find_chromium_binary", return_value=None), \
                     mock.patch.object(t_api.TwitchClient, "claim_drop", return_value=True):
                    req = urllib.request.Request(
                        f"{base_url}/api/claim-drops",
                        data=json.dumps({"drop_instance_id": "drop_win"}).encode("utf-8"),
                        headers=hdrs,
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=5) as r:
                        resp_win = json.loads(r.read().decode("utf-8"))
                self.assertEqual(resp_win["claimed"], 1)
                self.assertEqual(s._TWITCH_OVERVIEW_CACHE["ts"], 0.0)
            finally:
                httpd.shutdown()
                httpd.server_close()
                s.ACTIVE_SESSIONS.clear()
                s.ACTIVE_SESSIONS.update(orig_sessions)

    def test_index_html_uses_30000ms_interval_and_visibility_guard(self):
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        html_path = os.path.join(repo_root, "src", "web", "templates", "index.html")
        with open(html_path, "r", encoding="utf-8") as f:
            html = f.read()

        self.assertIn("setInterval(() => { if (!document.hidden) fetchData(true); }, 30000);", html)
        self.assertNotIn("fetchData(true), 10000", html)
        self.assertIn('document.addEventListener("visibilitychange"', html)
        self.assertIn("lastStatusFetchAt", html)
        self.assertIn("/api/status?force=1", html)
        self.assertIn("await fetchData(false, true);", html)
        self.assertIn("miner-rate-limit-badge", html)
        self.assertIn("minerStat.rate_limited", html)


if __name__ == "__main__":
    unittest.main()
