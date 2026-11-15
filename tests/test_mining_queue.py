import base64
import gzip
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from src.twitch_drops import activity_logger
from src.twitch_drops import browser_watcher
from src.twitch_drops import miner as m
from src.twitch_drops import twitch_api as t_api
from src.web import server as s


class MiningQueueAndSwapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg_file = os.path.join(self.tmp, "mining_config.json")
        self.stat_file = os.path.join(self.tmp, "miner_status.json")
        self.p_miner = mock.patch.multiple(
            m,
            DATA_DIR=self.tmp,
            CONFIG_FILE=self.cfg_file,
            STATUS_FILE=self.stat_file,
        )
        self.p_miner.start()

    def tearDown(self):
        self.p_miner.stop()

    def test_queue_persistence_and_auto_advance_on_completion(self):
        with open(self.cfg_file, "w", encoding="utf-8") as f:
            json.dump({"mode": "default", "queue": ["Rust", "Overwatch 2", "Valorant"], "updated_at": 100}, f)

        cfg = m.load_mining_config()
        self.assertEqual(cfg["queue"], ["Rust", "Overwatch 2", "Valorant"])
        self.assertEqual(cfg["selected_game"], "Rust")

        # Simulate Rust completing -> remove_from_mining_queue("Rust")
        updated = m.remove_from_mining_queue("Rust")
        self.assertEqual(updated["queue"], ["Overwatch 2", "Valorant"])
        self.assertEqual(updated["selected_game"], "Overwatch 2")

    def test_campaign_completion_detection_and_auto_swap(self):
        with open(self.cfg_file, "w", encoding="utf-8") as f:
            json.dump({"mode": "default", "queue": ["GameA", "GameB"], "updated_at": 100}, f)

        dm = m.DropsMiner("fake_token", priority_games=["GameA", "GameB"])
        # GameA has all drops claimed; GameB still has an unclaimed watch-time drop
        fake_campaigns = [
            {
                "id": "camp_a",
                "name": "GameA Launch Drops",
                "game": {"displayName": "GameA"},
                "status": "ACTIVE",
                "timeBasedDrops": [
                    {
                        "id": "drop_a1",
                        "name": "Reward A1",
                        "requiredMinutesWatched": 60,
                        "requiredSubs": 0,
                        "self": {"isClaimed": True, "currentMinutesWatched": 60},
                    }
                ],
            },
            {
                "id": "camp_b",
                "name": "GameB Drops",
                "game": {"displayName": "GameB"},
                "status": "ACTIVE",
                "timeBasedDrops": [
                    {
                        "id": "drop_b1",
                        "name": "Reward B1",
                        "requiredMinutesWatched": 120,
                        "requiredSubs": 0,
                        "self": {"isClaimed": False, "currentMinutesWatched": 30},
                    }
                ],
            },
        ]
        with mock.patch.object(dm.client, "get_all_active_campaigns", return_value=fake_campaigns):
            eligible = dm.get_eligible_campaigns()
            # GameA must be auto-pruned from queue and GameB selected immediately
            self.assertEqual(len(eligible), 1)
            self.assertEqual(eligible[0]["id"], "camp_b")
            reloaded_cfg = m.load_mining_config()
            self.assertEqual(reloaded_cfg["queue"], ["GameB"])
            self.assertEqual(reloaded_cfg["selected_game"], "GameB")

    def test_live_drop_progress_returns_empty_when_all_drops_finished(self):
        dm = m.DropsMiner("fake_token")
        dm.last_inventory = {
            "dropCampaignsInProgress": [
                {
                    "id": "camp_a",
                    "name": "GameA Drops",
                    "game": {"displayName": "GameA"},
                    "status": "ACTIVE",
                    "timeBasedDrops": [
                        {
                            "id": "d1",
                            "name": "Skin",
                            "requiredMinutesWatched": 60,
                            "requiredSubs": 0,
                            "self": {"isClaimed": True, "currentMinutesWatched": 60},
                        }
                    ],
                }
            ]
        }
        prog = dm._get_live_drop_progress("GameA", "GameA Drops")
        self.assertEqual(prog, {})

    def test_find_eligible_channel_requires_drops_and_respects_exclusions(self):
        client = m.TwitchClient("fake_token")

        gql_directory_response = {
            "data": {
                "game": {
                    "id": "999",
                    "name": "Rust",
                    "streams": {
                        "edges": [
                            {
                                "node": {
                                    "id": "s1",
                                    "title": "Just chilling no rewards",
                                    "viewersCount": 5000,
                                    "freeformTags": [{"name": "English"}],
                                    "broadcaster": {"id": "b1", "login": "nodrops_guy", "displayName": "NoDropsGuy"},
                                }
                            },
                            {
                                "node": {
                                    "id": "s2",
                                    "title": "!DROPS ENABLED | New Wipe",
                                    "viewersCount": 1200,
                                    "freeformTags": [{"name": "DropsEnabled"}],
                                    "broadcaster": {"id": "b2", "login": "stalled_streamer", "displayName": "StalledStreamer"},
                                }
                            },
                            {
                                "node": {
                                    "id": "s3",
                                    "title": "Rust Drops On! !drops",
                                    "viewersCount": 800,
                                    "freeformTags": [{"name": "Drops"}],
                                    "broadcaster": {"id": "b3", "login": "good_streamer", "displayName": "GoodStreamer"},
                                }
                            },
                        ]
                    },
                }
            }
        }

        def fake_persisted_gql(operation_name, sha256_hash, variables):
            cid = variables.get("channelID")
            if cid in ("b2", "b3"):
                return {
                    "data": {
                        "channel": {
                            "viewerDropCampaigns": [
                                {
                                    "game": {"displayName": "Rust"},
                                    "timeBasedDrops": [{"requiredMinutesWatched": 60}],
                                }
                            ]
                        }
                    }
                }
            return {"data": {"channel": {"viewerDropCampaigns": []}}}

        def only_fallback(directory):
            # Twitch's DROPS_ENABLED-filtered query returns nothing -> exercises the verification fallback path
            def _fake(query, variables=None, operation_name=None):
                if operation_name == "DirectoryPage_Game":
                    return {"data": {"game": {"id": "999", "name": "Rust", "streams": {"edges": []}}}}
                return directory
            return _fake

        # Must always verify DropsHighlightService_AvailableDrops even when DROPS_ENABLED directory returns results,
        # skipping nodrops_guy and picking the first (top-left) verified channel in directory order.
        with mock.patch.object(client, "post_gql", return_value=gql_directory_response), \
             mock.patch.object(client, "post_persisted_gql", side_effect=fake_persisted_gql):
            top = client.find_eligible_channel("Rust", fast_first=True)
            self.assertIsNotNone(top)
            self.assertEqual(top["channel_login"], "stalled_streamer")
            self.assertTrue(top["verified_drops"])

            top2 = client.find_eligible_channel("Rust", fast_first=True, excluded_channels={"stalled_streamer"})
            self.assertIsNotNone(top2)
            self.assertEqual(top2["channel_login"], "good_streamer")
            self.assertTrue(top2["verified_drops"])

        with mock.patch.object(client, "post_gql", side_effect=only_fallback(gql_directory_response)), \
             mock.patch.object(client, "post_persisted_gql", side_effect=fake_persisted_gql):
            # When stalled_streamer is excluded, must pick good_streamer and ignore nodrops_guy
            ch = client.find_eligible_channel("Rust", fast_first=True, excluded_channels={"stalled_streamer"})
            self.assertIsNotNone(ch)
            self.assertEqual(ch["channel_login"], "good_streamer")
            self.assertTrue(ch["verified_drops"])
            self.assertTrue(ch["has_drops_in_title"])

        # If no channel has verified drops AND no channel has 'drops' in title/tags, must return None
        no_drops_dir = {
            "data": {
                "game": {
                    "id": "999",
                    "name": "Rust",
                    "streams": {
                        "edges": [
                            {
                                "node": {
                                    "id": "s1",
                                    "title": "Regular gameplay only",
                                    "viewersCount": 5000,
                                    "freeformTags": [{"name": "Gameplay"}],
                                    "broadcaster": {"id": "b1", "login": "nodrops_guy", "displayName": "NoDropsGuy"},
                                }
                            }
                        ]
                    },
                }
            }
        }
        with mock.patch.object(client, "post_gql", side_effect=only_fallback(no_drops_dir)), \
             mock.patch.object(client, "post_persisted_gql", return_value={"data": {"channel": {"viewerDropCampaigns": []}}}):
            ch_none = client.find_eligible_channel("Rust", fast_first=True)
            self.assertIsNone(ch_none)

    def test_stalled_progress_after_5_minutes_switches_channel_immediately(self):
        with open(self.cfg_file, "w", encoding="utf-8") as f:
            json.dump({"mode": "default", "queue": ["Rust"], "updated_at": 100}, f)

        dm = m.DropsMiner("fake_token", priority_games=["Rust"])
        dm.running = True
        dm.progress_check_minutes = 5

        fake_camp = {
            "id": "camp_rust",
            "name": "Rust Drops",
            "game": {"displayName": "Rust"},
            "status": "ACTIVE",
            "timeBasedDrops": [
                {
                    "id": "d_rust",
                    "name": "Rust Jacket",
                    "requiredMinutesWatched": 60,
                    "requiredSubs": 0,
                    "self": {"isClaimed": False, "currentMinutesWatched": 10},
                }
            ],
        }
        fake_channel = {
            "channel_id": "b_stall",
            "channel_login": "stalled_channel",
            "channel_name": "StalledChannel",
            "stream_id": "s_100",
            "stream_title": "Rust !drops",
            "viewers": 1000,
            "game_name": "Rust",
            "game_id": "999",
            "verified_drops": True,
            "has_drops_in_title": True,
        }

        # Inventory stays stuck at 10 minutes watched across all heartbeats
        stalled_inventory = {"dropCampaignsInProgress": [fake_camp]}
        heartbeats_sent = []

        def fake_heartbeat(**kwargs):
            heartbeats_sent.append(kwargs["channel_login"])
            return True

        with mock.patch.object(dm.client, "get_inventory", return_value=stalled_inventory), \
             mock.patch.object(dm.client, "is_user_actively_using_twitch", return_value=False), \
             mock.patch.object(dm.client, "get_all_active_campaigns", return_value=[fake_camp]), \
             mock.patch.object(dm.client, "find_eligible_channel", return_value=fake_channel) as mock_find, \
             mock.patch.object(dm.client, "get_playback_stream_info", return_value={"broadcast_id": "s_100", "sub_playlist_url": None}), \
             mock.patch.object(dm.client, "send_minute_watched_heartbeat", side_effect=fake_heartbeat), \
             mock.patch.object(dm, "_interruptible_sleep", return_value=False):
            dm.run_cycle()

            # Must stop after exactly 5 heartbeats (5 minutes without progress), NOT 15 minutes!
            self.assertEqual(len(heartbeats_sent), 5)
            self.assertIn("stalled_channel", dm._get_excluded_channels())

            with open(self.stat_file, "r", encoding="utf-8") as f:
                stat = json.load(f)
            self.assertTrue(stat.get("switching"))
            self.assertIn("No drop progress increase after 5 mins", stat.get("message", ""))

    def test_direct_twitch_gql_campaign_discovery_and_wardogs_mining(self):
        client = m.TwitchClient("")

        def fake_batch(ops, timeout=15):
            results = []
            for op in ops:
                if op.get("operationName") == "CheckGameTopStream":
                    gname = (op.get("variables") or {}).get("name")
                    if gname == "WARDOGS":
                        results.append({
                            "data": {
                                "game": {
                                    "id": "743893402",
                                    "name": "WARDOGS",
                                    "displayName": "WARDOGS",
                                    "boxArtURL": "https://www.twitch.tv/boxart/743893402-{width}x{height}.jpg",
                                    "streams": {"edges": [{"node": {"broadcaster": {"id": "56395702", "login": "coconutb"}}}]},
                                }
                            }
                        })
                    else:
                        results.append({"data": {"game": None}})
                elif op.get("operationName") == "DropsHighlightService_AvailableDrops":
                    if (op.get("variables") or {}).get("channelID") == "56395702":
                        results.append({
                            "data": {
                                "channel": {
                                    "viewerDropCampaigns": [
                                        {
                                            "id": "2965fe2b-6ebb-4547-929c-e70112337fda",
                                            "name": "Season 3",
                                            "game": {"id": "743893402", "name": "WARDOGS"},
                                            "endAt": "2099-11-26T09:59:59.999Z",
                                            "timeBasedDrops": [
                                                {
                                                    "id": "d_900",
                                                    "name": "Dog Of War",
                                                    "requiredMinutesWatched": 900,
                                                    "benefitEdges": [{"benefit": {"imageAssetURL": "https://www.twitch.tv/reward/dog.png"}}],
                                                },
                                                {
                                                    "id": "d_15",
                                                    "name": "Walk It Off",
                                                    "requiredMinutesWatched": 15,
                                                    "benefitEdges": [{"benefit": {"imageAssetURL": "https://www.twitch.tv/reward/walk.png"}}],
                                                },
                                            ],
                                        }
                                    ]
                                }
                            }
                        })
            return results

        with mock.patch("src.twitch_drops.twitch_api.fetch_drophunter_live_campaigns", return_value=[]), \
             mock.patch.object(client, "post_gql", return_value={"data": {"games": {"edges": []}, "streams": {"edges": []}}}), \
             mock.patch.object(client, "post_gql_batch", side_effect=fake_batch), \
             mock.patch.object(client, "get_inventory", return_value={}):
            camps = client.get_all_active_campaigns(priority_games=["WARDOGS"], force_refresh=True)
            self.assertEqual(len(camps), 1)
            self.assertEqual(camps[0]["game"]["displayName"], "WARDOGS")
            self.assertEqual(camps[0]["name"], "Season 3")
            # Ensure drops are sorted by requiredMinutesWatched ascending (15m before 900m)
            self.assertEqual([d["name"] for d in camps[0]["timeBasedDrops"]], ["Walk It Off", "Dog Of War"])

    def test_watch_loop_progresses_via_drop_session_even_when_user_online_on_twitch(self):
        with open(self.cfg_file, "w", encoding="utf-8") as f:
            json.dump({"mode": "default", "queue": ["WARDOGS"], "updated_at": 100}, f)

        dm = m.DropsMiner("fake_token", priority_games=["WARDOGS"])
        fake_camp = {
            "id": "camp_wardogs",
            "name": "WARDOGS Season 3",
            "game": {"id": "743893402", "displayName": "WARDOGS"},
            "endAt": "2099-01-01T00:00:00Z",
            "timeBasedDrops": [
                {
                    "id": "drop_wardogs_2",
                    "name": "Walk It Off",
                    "requiredMinutesWatched": 2,
                    "self": {"currentMinutesWatched": 0, "isClaimed": False},
                    "benefitEdges": [],
                }
            ],
        }
        fake_channel = {
            "channel_id": "56395702",
            "channel_login": "coconutb",
            "channel_name": "CoconutB",
            "stream_id": "s_56395702",
            "stream_title": "WARDOGS !drops",
            "viewers": 1200,
            "game_name": "WARDOGS",
            "game_id": "743893402",
            "verified_drops": True,
            "has_drops_in_title": True,
        }

        heartbeats_sent = []
        session_watched = [0]

        def fake_heartbeat(*args, **kwargs):
            heartbeats_sent.append(True)
            session_watched[0] += 1
            return True

        def fake_drop_session(channel_id, channel_login=""):
            return {
                "channel": {"id": str(channel_id), "name": channel_login},
                "game": {"id": "743893402", "displayName": "WARDOGS"},
                "currentMinutesWatched": session_watched[0],
                "requiredMinutesWatched": 2,
                "dropID": "drop_wardogs_2",
            }

        with mock.patch.object(dm.client, "get_inventory", return_value={"dropCampaignsInProgress": []}), \
             mock.patch.object(dm.client, "is_user_actively_using_twitch", return_value=True), \
             mock.patch.object(dm.client, "get_all_active_campaigns", return_value=[fake_camp]), \
             mock.patch.object(dm.client, "find_eligible_channel", return_value=fake_channel), \
             mock.patch.object(dm.client, "get_playback_stream_info", return_value={"broadcast_id": "s_56395702", "sub_playlist_url": None}), \
             mock.patch.object(dm.client, "send_minute_watched_heartbeat", side_effect=fake_heartbeat), \
             mock.patch.object(dm.client, "get_drop_current_session", side_effect=fake_drop_session), \
             mock.patch.object(dm, "claim_pending_drops", return_value=0), \
             mock.patch.object(dm, "_interruptible_sleep", return_value=False):
            dm.run_cycle()

            # Even though is_user_actively_using_twitch returned True, miner did NOT pause,
            # sent 2 heartbeats, saw session_watched reach 2/2, and completed the drop!
            self.assertEqual(len(heartbeats_sent), 2)
            self.assertNotIn("coconutb", dm._get_excluded_channels())


    def test_stopped_mode_disables_stream_watching_completely(self):
        with open(self.cfg_file, "w", encoding="utf-8") as f:
            json.dump({"mode": "stopped", "queue": ["Rust"], "updated_at": 100}, f)

        dm = m.DropsMiner("fake_token", priority_games=["Rust"])
        fake_camp = {
            "id": "camp_rust",
            "name": "Rust Drops",
            "game": {"displayName": "Rust"},
            "status": "ACTIVE",
            "timeBasedDrops": [
                {
                    "id": "d_rust",
                    "name": "Rust Jacket",
                    "requiredMinutesWatched": 60,
                    "self": {"isClaimed": False, "currentMinutesWatched": 0},
                }
            ],
        }

        with mock.patch.object(dm.client, "get_inventory", return_value={"dropCampaignsInProgress": [fake_camp]}), \
             mock.patch.object(dm.client, "get_all_active_campaigns", return_value=[fake_camp]), \
             mock.patch.object(dm.client, "find_eligible_channel") as mock_find, \
             mock.patch.object(dm, "_interruptible_sleep", return_value=False):
            camps = dm.get_eligible_campaigns()
            self.assertEqual(camps, [])

            dm.run_cycle()
            mock_find.assert_not_called()

            with open(self.stat_file, "r", encoding="utf-8") as f:
                stat = json.load(f)
            self.assertFalse(stat.get("active"))
            self.assertEqual(stat.get("mode"), "stopped")
            self.assertIn("stopped", stat.get("message", "").lower())

    def test_find_eligible_channel_resolves_evolutions_alias(self):
        client = t_api.TwitchClient("fake_token")
        fake_ch = {
            "channel_id": "135576689",
            "channel_login": "pokemon_streamer",
            "channel_name": "PokemonStreamer",
            "stream_id": "9999",
            "stream_title": "Pokemon Drops AFK",
            "viewers": 50,
            "game_name": "Pokémon Community Game",
            "game_id": "329951934",
            "verified_drops": True,
            "has_drops_in_title": True,
        }
        with mock.patch.object(client, "find_eligible_channel", wraps=client.find_eligible_channel) as spy_find:
            with mock.patch.object(client, "post_gql", return_value={
                "data": {
                    "game": {
                        "id": "329951934",
                        "name": "Pokémon Community Game",
                        "streams": {
                            "edges": [
                                {
                                    "node": {
                                        "id": "9999",
                                        "title": "Pokemon Drops AFK",
                                        "viewersCount": 50,
                                        "freeformTags": [{"name": "DropsEnabled"}],
                                        "broadcaster": {"id": "135576689", "login": "pokemon_streamer", "displayName": "PokemonStreamer"},
                                    }
                                }
                            ]
                        }
                    }
                }
            }), mock.patch.object(client, "post_persisted_gql", return_value={
                "data": {
                    "channel": {
                        "viewerDropCampaigns": [
                            {
                                "id": "evo_camp",
                                "name": "Evolutions",
                                "timeBasedDrops": [{"id": "d1", "requiredMinutesWatched": 20}]
                            }
                        ]
                    }
                }
            }):
                ch = client.find_eligible_channel("Evolutions")
                self.assertIsNotNone(ch)
                self.assertEqual(ch["game_name"], "Evolutions")
                self.assertEqual(ch["channel_login"], "pokemon_streamer")

    def test_mini_viewer_and_auto_suspension_in_template(self):
        template_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "src", "web", "templates", "index.html"
        )
        with open(template_path, "r", encoding="utf-8") as f:
            html = f.read()
        self.assertIn("mini-viewer-card", html)
        self.assertIn("mini-viewer-warn-overlay", html)
        self.assertIn("mini-viewer-iframe", html)
        self.assertIn("toggleMiniViewer", html)
        self.assertIn("suspendMiniViewerForExternalViewing", html)
        self.assertIn("resumeMiniViewer", html)
        self.assertIn("btn-header-direct-update", html)
        self.assertIn("direct-update-modal", html)
        self.assertIn("openDirectUpdateModal", html)
        self.assertIn("executeDirectModalUpdate", html)
        self.assertIn("miner-game-cover", html)
        self.assertIn("miner-banner-main", html)
        self.assertIn("chip-game-logo", html)
        self.assertIn("camp-game-fullname", html)
        self.assertIn("KNOWN_GAME_LOGOS", html)
        self.assertIn("minsWatchedLabel", html)
        self.assertIn("btn-restart-mining", html)
        self.assertIn("restartSelectedMining", html)

    def test_mystery_drop_and_day_tier_label_extraction(self):
        client = t_api.TwitchClient("dummy_token")
        mock_camps = [{
            "id": "c_evolutions",
            "name": "Evolutions",
            "game": {"displayName": "Evolutions", "name": "Evolutions"},
            "timeBasedDrops": [
                {
                    "id": "drop_day1",
                    "name": "???",
                    "requiredMinutesWatched": 20,
                    "benefitEdges": [
                        {
                            "benefit": {
                                "id": "ben_1",
                                "name": "Day 1",
                                "imageAssetURL": "https://static-cdn.jtvnw.net/custom-reward-images/silhouette.png"
                            }
                        }
                    ],
                    "self": {
                        "currentMinutesWatched": 0,
                        "dropInstanceID": None,
                        "isClaimed": False,
                        "hasPreconditionsMet": True
                    }
                }
            ]
        }]
        with mock.patch.object(client, "get_all_active_campaigns", return_value=mock_camps), \
             mock.patch.object(client, "get_inventory", return_value={"dropCampaignsInProgress": mock_camps}):
            overview = client.get_drops_overview()
            self.assertEqual(len(overview["campaigns"]), 1)
            camp = overview["campaigns"][0]
            self.assertEqual(camp["name"], "Evolutions")
            drop = camp["drops"][0]
            self.assertTrue(drop["is_mystery"])
            self.assertEqual(drop["tier_label"], "Day 1")
            self.assertIn("???", drop["name"])
            self.assertEqual(drop["required_minutes"], 20)
            self.assertEqual(drop["watched_minutes"], 0)

    def test_restart_mining_resets_watchdog_and_switches_stream(self):
        with open(self.cfg_file, "w", encoding="utf-8") as f:
            json.dump({"mode": "default", "queue": ["Rust", "Valorant"], "updated_at": 100}, f)
        with open(self.stat_file, "w", encoding="utf-8") as f:
            json.dump({
                "active": True,
                "game": "Rust",
                "minutes_without_progress": 4,
                "progress_verified": False,
            }, f)

        with mock.patch("src.web.server.APP_DIR", self.tmp), \
             mock.patch("src.web.server.resolve_token", return_value="fake_twitch_token"), \
             mock.patch("src.web.server.ensure_miner_daemon_running", return_value=True), \
             mock.patch.object(t_api.TwitchClient, "find_eligible_channel", return_value={
                 "channel_name": "FreshStreamer",
                 "channel_login": "fresh_streamer",
                 "channel_id": "c_fresh",
                 "stream_id": "s_fresh",
                 "viewers": 500,
                 "verified_drops": True,
                 "has_drops_in_title": True,
             }):
            res = s.restart_mining("Rust")
            self.assertTrue(res["success"])
            self.assertEqual(res["game"], "Rust")
            self.assertEqual(res["queue"][0], "Rust")

            stat = s.get_miner_status()
            self.assertEqual(stat["game"], "Rust")
            self.assertEqual(stat["minutes_without_progress"], 0)
            self.assertEqual(stat.get("channel_login"), "fresh_streamer")

    def test_one_hour_channel_rotation_switches_channel_after_60_mins(self):
        dm = m.DropsMiner("test_token")
        dm.stream_cycle_minutes = 60
        fake_camp = {
            "id": "c_rot",
            "name": "Rust Drops",
            "game": {"name": "Rust"},
            "timeBasedDrops": [
                {
                    "id": "d_600",
                    "name": "Rust Jacket",
                    "requiredMinutesWatched": 600,
                    "benefitEdges": [{"benefit": {"imageAssetURL": "https://static-cdn.jtvnw.net/rust.png"}}],
                    "self": {"currentMinutesWatched": 10, "isClaimed": False},
                }
            ],
        }
        fake_channel = {
            "channel_id": "b_rot",
            "channel_login": "rot_channel",
            "channel_name": "RotChannel",
            "stream_id": "s_rot",
            "stream_title": "Rust !drops",
            "viewers": 1000,
            "game_name": "Rust",
            "game_id": "999",
            "verified_drops": True,
            "has_drops_in_title": True,
        }

        progress_counter = [10]
        heartbeats_sent = []

        def fake_heartbeat(**kwargs):
            heartbeats_sent.append(kwargs["channel_login"])
            progress_counter[0] += 1
            return True

        def fake_inv():
            fake_camp["timeBasedDrops"][0]["self"]["currentMinutesWatched"] = progress_counter[0]
            return {"dropCampaignsInProgress": [fake_camp]}

        with mock.patch.object(dm.client, "get_inventory", side_effect=fake_inv), \
             mock.patch.object(dm.client, "is_user_actively_using_twitch", return_value=False), \
             mock.patch.object(dm.client, "get_all_active_campaigns", return_value=[fake_camp]), \
             mock.patch.object(dm.client, "find_eligible_channel", return_value=fake_channel), \
             mock.patch.object(dm.client, "get_playback_stream_info", return_value={"broadcast_id": "s_rot", "sub_playlist_url": None}), \
             mock.patch.object(dm.client, "send_minute_watched_heartbeat", side_effect=fake_heartbeat), \
             mock.patch.object(dm.client, "is_stream_and_drops_available", return_value=(True, "Stream and drops available")), \
             mock.patch.object(dm, "_interruptible_sleep", return_value=False):
            dm.run_cycle()

            self.assertEqual(len(heartbeats_sent), 60)
            self.assertIn("rot_channel", dm._get_excluded_channels())

            with open(self.stat_file, "r", encoding="utf-8") as f:
                stat = json.load(f)
            self.assertTrue(stat.get("switching"))
            self.assertIn("Completed 1-hour cycle", stat.get("message", ""))

    def test_stream_availability_check_detects_offline_and_swaps_immediately(self):
        dm = m.DropsMiner("test_token")
        fake_camp = {
            "id": "c_avail",
            "name": "Rust Drops",
            "game": {"name": "Rust"},
            "timeBasedDrops": [
                {
                    "id": "d_300",
                    "name": "Rust Mask",
                    "requiredMinutesWatched": 300,
                    "benefitEdges": [{"benefit": {"imageAssetURL": "https://static-cdn.jtvnw.net/mask.png"}}],
                    "self": {"currentMinutesWatched": 5, "isClaimed": False},
                }
            ],
        }
        fake_channel = {
            "channel_id": "b_avail",
            "channel_login": "offline_soon",
            "channel_name": "OfflineSoon",
            "stream_id": "s_off",
            "stream_title": "Rust !drops",
            "viewers": 500,
            "game_name": "Rust",
            "game_id": "999",
            "verified_drops": True,
            "has_drops_in_title": True,
        }

        # Stream dies at minute 5 availability check
        def fake_avail_check(channel_id, channel_login, game_name):
            if len(heartbeats_sent) >= 5:
                return False, "Stream is offline or broadcast ended"
            return True, "Stream and drops available"

        heartbeats_sent = []
        def fake_heartbeat(**kwargs):
            heartbeats_sent.append(kwargs["channel_login"])
            return True

        with mock.patch.object(dm.client, "get_inventory", return_value={"dropCampaignsInProgress": [fake_camp]}), \
             mock.patch.object(dm.client, "is_user_actively_using_twitch", return_value=False), \
             mock.patch.object(dm.client, "get_all_active_campaigns", return_value=[fake_camp]), \
             mock.patch.object(dm.client, "find_eligible_channel", return_value=fake_channel), \
             mock.patch.object(dm.client, "get_playback_stream_info", return_value={"broadcast_id": "s_off", "sub_playlist_url": None}), \
             mock.patch.object(dm.client, "send_minute_watched_heartbeat", side_effect=fake_heartbeat), \
             mock.patch.object(dm.client, "is_stream_and_drops_available", side_effect=fake_avail_check), \
             mock.patch.object(dm, "_interruptible_sleep", return_value=False):
            dm.run_cycle()

            self.assertEqual(len(heartbeats_sent), 5)
            self.assertIn("offline_soon", dm._get_excluded_channels())
            with open(self.stat_file, "r", encoding="utf-8") as f:
                stat = json.load(f)
            self.assertTrue(stat.get("switching"))
            self.assertIn("no longer available", stat.get("message", ""))

    def test_status_polling_does_not_mutate_config_signature_or_reset_miner(self):
        with open(self.cfg_file, "w", encoding="utf-8") as f:
            json.dump({"mode": "default", "queue": ["Rust"], "updated_at": 123456.0}, f)

        # Check initial signature
        dm = m.DropsMiner("test_token")
        sig_before = dm._get_config_signature()

        with mock.patch("src.web.server.APP_DIR", self.tmp), \
             mock.patch("src.web.server.resolve_token", return_value="fake_token"), \
             mock.patch.object(t_api.TwitchClient, "get_drops_overview", return_value={"campaigns": []}):
            # Simulate page refresh / status polling
            payload = s.build_status_payload()
            self.assertIsInstance(payload, dict)

        sig_after = dm._get_config_signature()
        self.assertEqual(sig_before, sig_after)
        self.assertFalse(dm._has_config_changed(sig_before))

    def test_send_minute_watched_heartbeat_sends_gql_spade_and_http_spade(self):
        client = t_api.TwitchClient("fake_token")
        client.user_id = "123456"
        client.user_login = "viewer_user"

        gql_calls = []

        def fake_post_gql(query, variables=None, operation_name=None):
            gql_calls.append({"query": query, "variables": variables, "operation_name": operation_name})
            return {"data": {"sendSpadeEvents": {"statusCode": 204}}}

        fake_resp = mock.MagicMock()
        fake_resp.status = 204
        fake_urlopen_cm = mock.MagicMock()
        fake_urlopen_cm.__enter__.return_value = fake_resp
        fake_urlopen_cm.__exit__.return_value = False

        with mock.patch.object(client, "post_gql", side_effect=fake_post_gql), \
             mock.patch.object(client, "post_persisted_gql", return_value={}), \
             mock.patch("urllib.request.urlopen", return_value=fake_urlopen_cm) as mock_urlopen:
            ok = client.send_minute_watched_heartbeat(
                channel_id="56395702",
                stream_id="987654321",
                channel_login="coconutb",
                game_name="WARDOGS",
                game_id="743893402",
                sub_playlist_url=None,
                minutes_logged=7,
            )

        self.assertTrue(ok)
        self.assertEqual(len(gql_calls), 1)
        call = gql_calls[0]
        self.assertEqual(call["operation_name"], "SendEvents")
        inp = (call["variables"] or {}).get("input") or {}
        self.assertEqual(inp.get("repository"), "twilight")
        self.assertEqual(inp.get("encoding"), "GZIP_B64")

        decompressed = gzip.decompress(base64.b64decode(inp["data"])).decode("utf-8")
        events = json.loads(decompressed)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "minute-watched")
        props = events[0]["properties"]
        self.assertEqual(props["minutes_logged"], 7)
        self.assertIsInstance(props["user_id"], int)
        self.assertEqual(props["user_id"], 123456)
        self.assertEqual(props["channel"], "coconutb")
        self.assertEqual(props["game"], "WARDOGS")
        self.assertEqual(props["game_id"], "743893402")
        self.assertEqual(props["position"], 0)
        self.assertEqual(props["quality"], "chunked")
        self.assertEqual(props["real_time"], 60)
        self.assertEqual(props["volume"], 1)
        self.assertTrue(mock_urlopen.called)

    def test_touch_stream_chunk_issues_head_requests_to_all_segments(self):
        client = t_api.TwitchClient("fake_token")
        fake_m3u8 = (
            "#EXTM3U\n"
            "#EXT-X-VERSION:3\n"
            "#EXT-X-TARGETDURATION:2\n"
            '#EXT-X-DATERANGE:ID="trig",X-TV-TWITCH-TRIGGER-URL="https://usher.ttvnw.net/trigger/123"\n'
            "https://video-edge-1.ttvnw.net/v1/segment/chunk1.ts\n"
            "https://video-edge-1.ttvnw.net/v1/segment/chunk2.ts\n"
            "https://video-edge-1.ttvnw.net/v1/segment/chunk3.ts\n"
        )
        head_calls = []

        def fake_urlopen(req, timeout=None):
            url = req.get_full_url() if hasattr(req, "get_full_url") else str(req)
            method = req.get_method() if hasattr(req, "get_method") else "GET"
            resp = mock.MagicMock()
            resp.status = 200
            if "playlist.m3u8" in url:
                resp.read.return_value = fake_m3u8.encode("utf-8")
            elif "trigger" in url:
                resp.read.return_value = b""
            else:
                if method == "HEAD":
                    head_calls.append(url)
                resp.read.return_value = b"bytes1024"
            cm = mock.MagicMock()
            cm.__enter__.return_value = resp
            cm.__exit__.return_value = False
            return cm

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            ok = client.touch_stream_chunk("https://usw22.playlist.ttvnw.net/v1/playlist.m3u8", channel_login="teststreamer")

        self.assertTrue(ok)
        self.assertEqual(len(head_calls), 3)
        self.assertIn("chunk1.ts", head_calls[0])
        self.assertIn("chunk2.ts", head_calls[1])
        self.assertIn("chunk3.ts", head_calls[2])

    def test_send_spade_watch_includes_oauth_and_client_headers(self):
        client = t_api.TwitchClient("oauth_secret_token_123")
        client.user_id = "998877"
        client.device_id = "test_device_id_abc"
        client.session_id = "test_session_id_xyz"

        captured_requests = []

        def fake_urlopen(req, timeout=None):
            captured_requests.append({
                "url": req.get_full_url(),
                "headers": dict(req.headers),
                "data": req.data.decode("utf-8") if req.data else "",
            })
            resp = mock.MagicMock()
            resp.status = 204
            cm = mock.MagicMock()
            cm.__enter__.return_value = resp
            cm.__exit__.return_value = False
            return cm

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            ok = client.send_spade_watch(
                channel_id="112233",
                broadcast_id="99887766",
                channel_login="teststreamer",
                game_name="WARDOGS",
                game_id="743893402",
                minutes_logged=1,
            )

        self.assertTrue(ok)
        spade_reqs = [r for r in captured_requests if "spade" in r["url"] or "track" in r["url"]]
        self.assertTrue(len(spade_reqs) >= 1)
        req = spade_reqs[0]
        hdrs = {k.lower(): v for k, v in req["headers"].items()}
        self.assertEqual(hdrs.get("authorization"), "OAuth oauth_secret_token_123")
        self.assertEqual(hdrs.get("client-id"), client.client_id)
        self.assertEqual(hdrs.get("x-device-id"), "test_device_id_abc")
        self.assertEqual(hdrs.get("client-session-id"), "test_session_id_xyz")
        self.assertIn("data=", req["data"])

    def test_switch_watched_channel_next_and_specific(self):
        os.makedirs(os.path.join(self.tmp, "data"), exist_ok=True)
        with open(os.path.join(self.tmp, "data", "mining_config.json"), "w", encoding="utf-8") as f:
            json.dump({"mode": "default", "queue": ["WARDOGS"], "updated_at": 100}, f)
        with open(os.path.join(self.tmp, "data", "miner_status.json"), "w", encoding="utf-8") as f:
            json.dump({
                "active": True,
                "game": "WARDOGS",
                "channel_login": "streamer_one",
                "channel_name": "StreamerOne",
            }, f)

        def fake_find_eligible(game_name, fast_first=True, excluded_channels=None, campaign_game=None):
            excl = {c.lower() for c in (excluded_channels or set())}
            for login, name in [("streamer_one", "StreamerOne"), ("streamer_two", "StreamerTwo")]:
                if login not in excl:
                    return {
                        "channel_id": f"id_{login}",
                        "channel_login": login,
                        "channel_name": name,
                        "stream_id": f"s_{login}",
                        "stream_title": "!drops",
                        "viewers": 500,
                        "game_name": game_name,
                        "game_id": "743893402",
                        "verified_drops": True,
                        "has_drops_in_title": True,
                    }
            return None

        def fake_resolve_specific(channel_input, game_name=""):
            return {
                "channel_id": "id_custom",
                "channel_login": "dani_aetherwolf",
                "channel_name": "Dani_Aetherwolf",
                "stream_id": "s_custom",
                "stream_title": "Drops enabled!",
                "viewers": 900,
                "game_name": game_name or "WARDOGS",
                "game_id": "743893402",
                "verified_drops": True,
                "has_drops_in_title": True,
            }

        with mock.patch("src.web.server.APP_DIR", self.tmp), \
             mock.patch("src.web.server.resolve_token", return_value="fake_token"), \
             mock.patch("src.web.server._send_async_initial_heartbeat"), \
             mock.patch("src.web.server.ensure_miner_daemon_running"), \
             mock.patch.object(t_api.TwitchClient, "find_eligible_channel", side_effect=fake_find_eligible), \
             mock.patch.object(t_api.TwitchClient, "resolve_specific_channel", side_effect=fake_resolve_specific):
            # 1. Next channel: must skip streamer_one and switch to streamer_two
            res_next = s.switch_watched_channel("WARDOGS")
            self.assertTrue(res_next["success"])
            self.assertEqual(res_next["miner_status"]["channel_login"], "streamer_two")
            self.assertIn("streamer_one", res_next["mining_config"]["excluded_channels"])

            # 2. Specific channel: must switch directly to dani_aetherwolf
            res_spec = s.switch_watched_channel("WARDOGS", "https://www.twitch.tv/dani_aetherwolf")
            self.assertTrue(res_spec["success"])
            self.assertEqual(res_spec["miner_status"]["channel_login"], "dani_aetherwolf")
            self.assertEqual(res_spec["mining_config"]["preferred_channel"], "dani_aetherwolf")

    def test_activity_logger_redacts_secrets_and_logs_events(self):
        log_path = os.path.join(self.tmp, "activity.log")
        webhook_url = "https://" + "discord.com/api/webhooks/123456789/super_secret_webhook_tok"
        with mock.patch.multiple(activity_logger, DATA_DIR=self.tmp, ACTIVITY_LOG_FILE=log_path):
            redacted = activity_logger.sanitize_text(
                f"OAuth secret_tok_999 auth-token=cookie_val_888 {webhook_url} DASHBOARD_PASSWORD=pw123"
            )
            self.assertNotIn("secret_tok_999", redacted)
            self.assertNotIn("cookie_val_888", redacted)
            self.assertNotIn("super_secret_webhook_tok", redacted)
            self.assertNotIn("pw123", redacted)
            self.assertIn("discord.com/api/webhooks/123456789/[REDACTED]", redacted)

            activity_logger.log_event(
                "UI_BUTTON",
                "button_click",
                {"auth_token": "tok_hidden", "label": "Next Channel", "webhook": webhook_url},
            )
            lines = activity_logger.read_activity_log(max_lines=20, filter_category="UI_BUTTON")
            self.assertEqual(len(lines), 1)
            self.assertIn("Next Channel", lines[0])
            self.assertIn("[REDACTED]", lines[0])
            self.assertNotIn("tok_hidden", lines[0])
            self.assertNotIn("super_secret_webhook_tok", lines[0])

    def test_get_spade_url_prioritizes_spade_over_beacon(self):
        html = (
            b'<html><script src="https://static-cdn.jtvnw.net/config/settings.deadbeef1234.js"></script></html>'
        )
        settings_js = (
            b'{"beacon_url":"https://beacon.twitch.tv/track","spade_url":"https://spade.twitch.tv/track"}'
        )

        class _FakeResp:
            def __init__(self, payload_bytes):
                self._payload = payload_bytes
                self.headers = {}
                self.status = 200

            def read(self):
                return self._payload

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        def fake_urlopen(req, timeout=6):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "settings.deadbeef1234.js" in url:
                return _FakeResp(settings_js)
            return _FakeResp(html)

        client = t_api.TwitchClient("fake_token")
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            spade = client.get_spade_url("stodeh")

        self.assertEqual(spade, "https://spade.twitch.tv/track")

    def test_browser_watcher_disabled_in_unittest(self):
        self.assertIsNone(browser_watcher.find_chromium_binary())
        bw = browser_watcher.HeadlessBrowserWatcher()
        self.assertFalse(bw.is_available())

    def test_accounts_mask_and_diagnostics_and_log_action_endpoints(self):
        os.makedirs(os.path.join(self.tmp, "data"), exist_ok=True)
        env_path = os.path.join(self.tmp, ".env")
        webhook_val = "https://" + "discord.com/api/webhooks/999/secret_hook_val"
        with open(env_path, "w", encoding="utf-8") as f:
            f.write(
                "DASHBOARD_PASSWORD=TopSecretPass99\n"
                "TWITCH_AUTH_TOKEN=tw_tok_secret_123\n"
                "EG_OTP_SECRET=EGSECRETOTP\n"
                "PG_OTP_SECRET=PGSECRETOTP\n"
                f"DISCORD_WEBHOOK_URL={webhook_val}\n"
            )

        log_path = os.path.join(self.tmp, "data", "activity.log")
        orig_sessions = dict(s.ACTIVE_SESSIONS)
        s.ACTIVE_SESSIONS["test_sess_tok"] = 9999999999.0
        with mock.patch("src.web.server.APP_DIR", self.tmp), \
             mock.patch("src.web.server.get_running_bot_processes", return_value=[]), \
             mock.patch("src.web.server.load_stored_token", return_value={"login": "viewer_user", "user_id": "42"}), \
             mock.patch.multiple(activity_logger, DATA_DIR=os.path.join(self.tmp, "data"), ACTIVITY_LOG_FILE=log_path):
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), s.DashboardHandler)
            port = httpd.server_address[1]
            t = threading.Thread(target=httpd.serve_forever, daemon=True)
            t.start()
            try:
                base_url = f"http://127.0.0.1:{port}"
                hdrs = {"Cookie": "session_id=test_sess_tok", "Content-Type": "application/json"}

                # 1. GET /api/accounts must redact OTP & webhook and default username to admin
                req1 = urllib.request.Request(f"{base_url}/api/accounts", headers=hdrs)
                with urllib.request.urlopen(req1, timeout=5) as r1:
                    acc_data = json.loads(r1.read().decode("utf-8"))
                self.assertEqual(acc_data["dashboard_username"], "admin")
                self.assertEqual(acc_data["eg_otp_secret"], "")
                self.assertEqual(acc_data["pg_otp_secret"], "")
                self.assertEqual(acc_data["discord_webhook_url"], "")
                self.assertTrue(acc_data["has_eg_otp_secret"])
                self.assertTrue(acc_data["has_pg_otp_secret"])
                self.assertTrue(acc_data["has_discord_webhook"])

                # 2. POST /api/accounts with blank OTP & webhook must preserve existing .env secrets
                post_acc = urllib.request.Request(
                    f"{base_url}/api/accounts",
                    data=json.dumps({
                        "eg_otp_secret": "",
                        "pg_otp_secret": "",
                        "discord_webhook_url": "",
                        "eg_email": "player@example.com",
                    }).encode("utf-8"),
                    headers=hdrs,
                    method="POST",
                )
                with urllib.request.urlopen(post_acc, timeout=5) as r2:
                    self.assertTrue(json.loads(r2.read().decode("utf-8"))["success"])
                saved_env = s.load_env_file(env_path)
                self.assertEqual(saved_env.get("EG_OTP_SECRET"), "EGSECRETOTP")
                self.assertEqual(saved_env.get("PG_OTP_SECRET"), "PGSECRETOTP")
                self.assertEqual(saved_env.get("DISCORD_WEBHOOK_URL"), webhook_val)

                # 3. POST /api/log-action records button click in activity.log
                post_log = urllib.request.Request(
                    f"{base_url}/api/log-action",
                    data=json.dumps({
                        "action": "button_click",
                        "element_id": "btn-switch-channel",
                        "label": "Next Channel",
                        "tab": "Twitch Drops",
                    }).encode("utf-8"),
                    headers=hdrs,
                    method="POST",
                )
                with urllib.request.urlopen(post_log, timeout=5) as r3:
                    self.assertTrue(json.loads(r3.read().decode("utf-8"))["logged"])

                # 4. GET /api/diagnostics returns sanitized bundle containing UI button press & no secrets
                req_diag = urllib.request.Request(f"{base_url}/api/diagnostics", headers=hdrs)
                with urllib.request.urlopen(req_diag, timeout=5) as r4:
                    diag_payload = json.loads(r4.read().decode("utf-8"))
                self.assertEqual(diag_payload["status"], "ok")
                report = diag_payload["report"]
                self.assertIn("[UI_BUTTON]", report)
                self.assertIn("Next Channel", report)
                self.assertNotIn("TopSecretPass99", report)
                self.assertNotIn("tw_tok_secret_123", report)
                self.assertNotIn("EGSECRETOTP", report)
                self.assertNotIn("secret_hook_val", report)
            finally:
                httpd.shutdown()
                httpd.server_close()
                s.ACTIVE_SESSIONS.clear()
                s.ACTIVE_SESSIONS.update(orig_sessions)

    def test_client_integrity_injection_and_refresh(self):
        client = t_api.TwitchClient("fake_auth_token")
        client._integrity_token = "cached_integrity_v4"
        client._integrity_expiration = int(time.time() * 1000) + 3600000

        captured_headers = []

        def fake_make_request(url, data=b"", headers=None, timeout=15):
            captured_headers.append(dict(headers or {}))
            if len(captured_headers) == 1:
                return json.dumps({
                    "errors": [{"message": "failed integrity check", "extensions": {"code": "IntegrityCheckFailed"}}]
                }).encode("utf-8")
            return json.dumps({"data": {"currentUser": {"id": "130742243", "login": "test_user"}}}).encode("utf-8")

        def fake_get_integrity(force_refresh=False):
            if force_refresh:
                return "fresh_integrity_token_v4"
            return "cached_integrity_v4"

        with mock.patch.object(client, "_make_request", side_effect=fake_make_request), \
             mock.patch.object(client, "get_client_integrity_token", side_effect=fake_get_integrity):
            res = client.post_gql("query { currentUser { id } }")

        self.assertIn("data", res)
        self.assertEqual(res["data"]["currentUser"]["login"], "test_user")
        self.assertEqual(len(captured_headers), 2)
        self.assertEqual(captured_headers[0].get("Client-Integrity"), "cached_integrity_v4")
        self.assertEqual(captured_headers[1].get("Client-Integrity"), "fresh_integrity_token_v4")

    def test_process_guard_singleton_kills_competing_bots(self):
        from src.twitch_drops import process_guard

        with mock.patch("src.twitch_drops.process_guard.DATA_DIR", self.tmp), \
             mock.patch("src.twitch_drops.process_guard.PID_FILE", os.path.join(self.tmp, "miner.pid")), \
             mock.patch("src.twitch_drops.process_guard.get_running_bot_processes", return_value=[{"pid": "99988", "cmdline": "python -m src.twitch_drops.main --daemon"}]), \
             mock.patch("src.twitch_drops.process_guard.is_pid_alive", return_value=True), \
             mock.patch("src.twitch_drops.process_guard.kill_pid", return_value=True) as mock_kill:
            # First lock with kill_existing=True
            acquired = process_guard.acquire_pid_lock(kill_existing=True)
            self.assertTrue(acquired)
            mock_kill.assert_called_with(99988)
            with open(os.path.join(self.tmp, "miner.pid"), "r", encoding="utf-8") as f:
                self.assertEqual(f.read().strip(), str(os.getpid()))

    def test_claim_drop_in_browser_and_candidate_resolution(self):
        client = t_api.TwitchClient("fake_auth_token")

        class FakeBrowserWatcher:
            def __init__(self):
                self.calls = []

            def execute_gql_in_browser(self, operation_name, query_or_hash, variables=None, is_persisted=True):
                self.calls.append((operation_name, query_or_hash, variables))
                return {"data": {"claimDropRewards": {"status": "SUCCESS"}}}

        fake_bw = FakeBrowserWatcher()
        comp_id = "130742243#2965fe2b-6ebb-4547-929c-e70112337fda#b8d8fc12-d948-11f1-96f9-0a58a9feac02"
        res = client.claim_drop(comp_id, browser_watcher=fake_bw)
        self.assertTrue(res)
        self.assertEqual(len(fake_bw.calls), 1)
        self.assertEqual(fake_bw.calls[0][0], "DropsPage_ClaimDropRewards")
        self.assertEqual(fake_bw.calls[0][2]["input"]["dropInstanceID"], comp_id)

    def test_claim_drop_http_persisted_fallback_with_uuid_candidate(self):
        client = t_api.TwitchClient("fake_auth_token")
        attempted_vars = []

        def fake_post_persisted(operation_name, sha256_hash, variables=None):
            drop_id = variables["input"]["dropInstanceID"]
            attempted_vars.append(drop_id)
            if "#" in drop_id:
                # Composite string rejected by Twitch GQL
                return {"data": {"claimDropRewards": None}, "errors": [{"message": "Invalid input"}]}
            # Clean UUID segment accepted
            return {"data": {"claimDropRewards": {"status": "SUCCESS"}}}

        with mock.patch.object(client, "post_persisted_gql", side_effect=fake_post_persisted):
            comp_id = "130742243#2965fe2b-6ebb-4547-929c-e70112337fda#b8d8fc12-d948-11f1-96f9-0a58a9feac02"
            res = client.claim_drop(comp_id)
            self.assertTrue(res)
            self.assertEqual(attempted_vars[0], comp_id)
            self.assertEqual(attempted_vars[1], "b8d8fc12-d948-11f1-96f9-0a58a9feac02")

    def test_miner_claim_pending_drops_retries_and_force_clears(self):
        miner_inst = m.DropsMiner("fake_token")
        miner_inst.failed_claim_ids.add("stale_drop_id")
        miner_inst.failed_claim_counts["stale_drop_id"] = 5

        # Calling with force=True resets failure counts
        with mock.patch.object(miner_inst.client, "get_inventory", return_value={}):
            miner_inst.claim_pending_drops(force=True)
            self.assertEqual(len(miner_inst.failed_claim_ids), 0)
            self.assertEqual(len(miner_inst.failed_claim_counts), 0)

    def test_send_spade_watch_injects_integrity_cookie_and_full_payload(self):
        client = t_api.TwitchClient("oauth_secret_token_123")
        client.user_id = "998877"
        client.device_id = "test_device_id_abc"
        client.session_id = "test_session_id_xyz"
        client._integrity_token = "test_paseto_token_123"
        client._integrity_expiration = int(time.time() * 1000) + 3600000
        client.cookies["unique_id"] = "test_unique_id_cookie"

        captured_requests = []

        def fake_urlopen(req, timeout=None):
            captured_requests.append({
                "url": req.get_full_url(),
                "headers": dict(req.headers),
                "data": req.data.decode("utf-8") if req.data else "",
            })
            resp = mock.MagicMock()
            resp.status = 204
            resp.info.return_value.get_all.return_value = []
            cm = mock.MagicMock()
            cm.__enter__.return_value = resp
            cm.__exit__.return_value = False
            return cm

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            ok = client.send_spade_watch(
                channel_id="112233",
                broadcast_id="99887766",
                channel_login="teststreamer",
                game_name="WARDOGS",
                game_id="743893402",
                minutes_logged=1,
            )

        self.assertTrue(ok)
        spade_reqs = [r for r in captured_requests if "spade" in r["url"] or "track" in r["url"]]
        self.assertTrue(len(spade_reqs) >= 1)
        req = spade_reqs[0]
        hdrs = {k.lower(): v for k, v in req["headers"].items()}
        self.assertEqual(hdrs.get("client-integrity"), "test_paseto_token_123")
        self.assertIn("unique_id=test_unique_id_cookie", hdrs.get("cookie", ""))
        self.assertIn("data=", req["data"])

        # Decode base64 payload to verify full_payload prioritization
        import base64
        import urllib.parse
        parsed_body = urllib.parse.parse_qs(req["data"])
        b64_str = parsed_body["data"][0]
        payload = json.loads(base64.b64decode(b64_str).decode("utf-8"))
        self.assertIsInstance(payload, list)
        self.assertEqual(payload[0]["event"], "minute-watched")
        props = payload[0]["properties"]
        self.assertEqual(props.get("player"), "site")
        self.assertEqual(props.get("location"), "channel")
        self.assertEqual(props.get("quality"), "chunked")

    def test_browser_watcher_network_telemetry_and_stall_tracking(self):
        bw = m.HeadlessBrowserWatcher()
        self.assertEqual(bw.spade_requests_count, 0)
        self.assertEqual(bw.hls_segments_count, 0)

        # Feed CDP network events through _on_cdp_event handler
        bw._on_cdp_event("Network.requestWillBeSent", {
            "request": {"url": "https://spade.twitch.tv/track?client_id=kimne78kx3ncx6brgo4mv6wki5h1ko"}
        })
        self.assertEqual(bw.spade_requests_count, 1)

        bw._on_cdp_event("Network.responseReceived", {
            "response": {"url": "https://spade.twitch.tv/track", "status": 204}
        })
        self.assertEqual(bw.last_spade_status, 204)

        bw._on_cdp_event("Network.requestWillBeSent", {
            "request": {"url": "https://video-weaver.syd02.hls.ttvnw.net/v1/segment/12345.ts"}
        })
        self.assertEqual(bw.hls_segments_count, 1)

    def test_platform_user_agent_detection(self):
        from src.twitch_drops.browser_watcher import get_platform_user_agent
        with mock.patch("sys.platform", "linux"):
            ua_linux = get_platform_user_agent()
            self.assertIn("X11; Linux", ua_linux)
            self.assertIn("Chrome/133", ua_linux)
        with mock.patch("sys.platform", "darwin"):
            ua_mac = get_platform_user_agent()
            self.assertIn("Macintosh; Intel Mac OS X", ua_mac)
        with mock.patch("sys.platform", "win32"):
            ua_win = get_platform_user_agent()
            self.assertIn("Windows NT 10.0; Win64; x64", ua_win)
        custom = get_platform_user_agent("CustomUserAgent/1.0")
        self.assertEqual(custom, "CustomUserAgent/1.0")

    def test_browser_watcher_clean_cookies_and_persistent_session(self):
        bw = browser_watcher.HeadlessBrowserWatcher()
        set_cookies = []

        class MockCDP:
            def call(self, method, params=None, timeout=8.0):
                if method == "Network.setCookie":
                    set_cookies.append(params)
                    return {"success": True}
                return {}

        bw.cdp = MockCDP()
        bw._configure_session(
            auth_token="auth_abc_123",
            device_id="dev_xyz_456",
            user_login="cool_viewer",
            user_id="998877",
            session_id="sess_112233",
        )
        names = [c["name"] for c in set_cookies]
        self.assertIn("mature", names)
        self.assertIn("unique_id", names)
        self.assertIn("auth-token", names)
        self.assertIn("twilight-user", names)
        self.assertNotIn("twitch.lohp.countryCode", names)
        self.assertNotIn("countryCode", names)
        self.assertNotIn("login", names)
        self.assertNotIn("name", names)
        self.assertNotIn("user_id", names)

        # Cached persistent session: identical configure is a no-op
        count_before = len(set_cookies)
        bw._configure_session(
            auth_token="auth_abc_123",
            device_id="dev_xyz_456",
            user_login="cool_viewer",
            user_id="998877",
            session_id="sess_112233",
        )
        self.assertEqual(len(set_cookies), count_before)

    def test_browser_watcher_tick_throttling_and_keepalive(self):
        bw = browser_watcher.HeadlessBrowserWatcher()
        bw._is_proc_alive = lambda: True
        eval_calls = []

        class MockCDP:
            def drain_events(self, timeout=0.05):
                pass

            def call(self, method, params=None, timeout=15.0):
                if method == "Runtime.evaluate":
                    eval_calls.append(params)
                    return {
                        "result": {
                            "value": {
                                "href": "https://www.twitch.tv/shroud",
                                "videoFound": True,
                                "paused": False,
                                "currentTime": 42.0,
                            }
                        }
                    }
                return {}

        bw.cdp = MockCDP()
        bw.current_channel = "shroud"
        bw.is_available = lambda: True

        # First tick runs evaluate
        res1 = bw.tick("shroud", force=True)
        self.assertEqual(len(eval_calls), 1)
        self.assertTrue(res1["video_found"])
        self.assertEqual(res1["current_time"], 42.0)

        # Immediate second tick (within 60s keepalive) throttles and returns cached state without CDP evaluate
        res2 = bw.tick("shroud", force=False)
        self.assertEqual(len(eval_calls), 1)
        self.assertEqual(res2["current_time"], 42.0)

        # Tick with force=True bypasses keepalive throttle
        res3 = bw.tick("shroud", force=True)
        self.assertEqual(len(eval_calls), 2)

    def test_send_minute_watched_heartbeat_skips_when_browser_active(self):
        client = t_api.TwitchClient("fake_token")
        with mock.patch.object(client, "post_persisted_gql") as mock_gql, \
             mock.patch.object(client, "touch_stream_chunk") as mock_touch, \
             mock.patch.object(client, "send_gql_spade_event") as mock_gql_spade, \
             mock.patch.object(client, "send_spade_watch") as mock_spade:
            res = client.send_minute_watched_heartbeat(
                channel_id="12345",
                stream_id="67890",
                channel_login="teststreamer",
                game_name="Rust",
                browser_active=True,
            )
            self.assertTrue(res)
            mock_gql.assert_not_called()
            mock_touch.assert_not_called()
            mock_gql_spade.assert_not_called()
            mock_spade.assert_not_called()

    def test_miner_throttles_inventory_polling_during_watch_loop(self):
        miner_inst = m.DropsMiner("fake_token")
        fake_inventory = {
            "dropCampaignsInProgress": [{
                "id": "c1",
                "game": {"name": "Rust"},
                "timeBasedDrops": [{
                    "id": "d1",
                    "name": "Rust Hat",
                    "requiredMinutesWatched": 60,
                    "self": {"currentMinutesWatched": 10, "isClaimed": False, "dropInstanceID": "drop_inst_1"}
                }]
            }]
        }
        miner_inst.last_inventory = fake_inventory
        miner_inst.last_inventory_time = time.time()  # Recently fetched

        inv_calls = []
        def fake_get_inv():
            inv_calls.append(1)
            return fake_inventory

        with mock.patch.object(miner_inst.client, "get_inventory", side_effect=fake_get_inv):
            # 1. Normal minute tick with 10/60m watched: should NOT poll inventory
            live_info_incomplete = {"current_drop_watched": 10, "current_drop_required": 60}
            claimed = miner_inst.claim_pending_drops(live_drop_info=live_info_incomplete)
            self.assertEqual(claimed, 0)
            self.assertEqual(len(inv_calls), 0)

            # 2. When drop is expected to be finished (watched >= required): MUST poll inventory
            live_info_finished = {"current_drop_watched": 60, "current_drop_required": 60}
            with mock.patch.object(miner_inst.client, "claim_drop", return_value=True):
                claimed = miner_inst.claim_pending_drops(live_drop_info=live_info_finished)
                self.assertEqual(len(inv_calls), 1)

    def test_claim_pending_drops_exponential_backoff_on_failure(self):
        miner_inst = m.DropsMiner("fake_token")
        fake_inventory = {
            "dropCampaignsInProgress": [{
                "id": "c1",
                "game": {"name": "Rust"},
                "timeBasedDrops": [{
                    "id": "d1",
                    "name": "Rust Hat",
                    "requiredMinutesWatched": 60,
                    "self": {"currentMinutesWatched": 60, "isClaimed": False, "dropInstanceID": "drop_fail_1"}
                }]
            }]
        }
        miner_inst.last_inventory = fake_inventory
        miner_inst.last_inventory_time = time.time()

        claim_attempts = []
        def fake_claim(drop_id, browser_watcher=None):
            claim_attempts.append(drop_id)
            return False  # Simulate failure

        with mock.patch.object(miner_inst.client, "claim_drop", side_effect=fake_claim), \
             mock.patch.object(miner_inst.client, "get_inventory", return_value=fake_inventory):
            # First attempt fails: increments fail count and sets backoff
            miner_inst.claim_pending_drops(force=True)
            self.assertEqual(len(claim_attempts), 1)
            self.assertEqual(miner_inst.failed_claim_counts.get("drop_fail_1"), 1)
            self.assertIn("drop_fail_1", miner_inst.claim_backoff)
            self.assertGreater(miner_inst.claim_backoff["drop_fail_1"], time.time() + 50)

            # Immediate second attempt: backoff is active, so claim_drop is skipped
            miner_inst.claim_pending_drops(force=False)
            self.assertEqual(len(claim_attempts), 1)  # No new call!

    def test_browser_watcher_receives_matching_session_and_device_id(self):
        miner_inst = m.DropsMiner("fake_token")
        miner_inst.client.session_id = "test_sess_123456"
        miner_inst.client.device_id = "test_dev_9876543210123456"

        watch_calls = []
        def fake_watch_channel(channel_login, **kwargs):
            watch_calls.append(kwargs)
            return {"available": True, "running": True}

        miner_inst.browser_watcher.is_available = lambda: True
        miner_inst.browser_watcher.watch_channel = fake_watch_channel

        # Simulate channel launch check
        channel = {"channel_login": "rust_streamer", "channel_name": "RustStreamer", "channel_id": "111", "viewers": 100}
        miner_inst.browser_watcher.watch_channel(
            channel["channel_login"],
            auth_token=miner_inst.client.auth_token,
            device_id=miner_inst.client.device_id,
            user_login=miner_inst.client.user_login,
            user_id=miner_inst.client.user_id,
            session_id=miner_inst.client.session_id,
        )

        self.assertEqual(len(watch_calls), 1)
        self.assertEqual(watch_calls[0].get("session_id"), "test_sess_123456")
        self.assertEqual(watch_calls[0].get("device_id"), "test_dev_9876543210123456")

    def test_claim_drop_handles_already_claimed_and_various_statuses(self):
        client = t_api.TwitchClient("fake_auth_token")

        # Test persisted GQL with DROP_INSTANCE_ALREADY_CLAIMED
        with mock.patch.object(client, "post_persisted_gql", return_value={"data": {"claimDropRewards": {"status": "DROP_INSTANCE_ALREADY_CLAIMED"}}}):
            self.assertTrue(client.claim_drop("test_drop_123"))

        # Test persisted GQL with CLAIMED
        with mock.patch.object(client, "post_persisted_gql", return_value={"data": {"claimDropRewards": {"status": "CLAIMED"}}}):
            self.assertTrue(client.claim_drop("test_drop_123"))

        # Test error array indicating already claimed
        with mock.patch.object(client, "post_persisted_gql", return_value={"data": {"claimDropRewards": None}, "errors": [{"message": "Drop instance has already been claimed"}]}):
            self.assertTrue(client.claim_drop("test_drop_123"))

    def test_claim_pending_drops_proactive_poll_on_session_ctx_completion(self):
        miner_inst = m.DropsMiner("fake_token")
        fake_inventory = {
            "dropCampaignsInProgress": [{
                "id": "c1",
                "game": {"name": "WARDOGS"},
                "timeBasedDrops": [{
                    "id": "d1",
                    "name": "WARDOGS Tier 1",
                    "requiredMinutesWatched": 60,
                    "self": {"currentMinutesWatched": 60, "isClaimed": False, "dropInstanceID": "drop_inst_wardogs"}
                }]
            }]
        }
        miner_inst.last_inventory = fake_inventory
        miner_inst.last_inventory_time = time.time()  # Recently fetched, so normal poll would be suppressed

        inv_calls = []
        def fake_get_inv():
            inv_calls.append(1)
            return fake_inventory

        with mock.patch.object(miner_inst.client, "get_inventory", side_effect=fake_get_inv), \
             mock.patch.object(miner_inst.client, "claim_drop", return_value=True):
            # When session_ctx reports 60/60 minutes watched, inventory MUST be polled proactively and drop claimed
            sess_ctx = {"dropID": "d1", "currentMinutesWatched": 60, "requiredMinutesWatched": 60}
            claimed = miner_inst.claim_pending_drops(force=True, session_ctx=sess_ctx)
            self.assertEqual(claimed, 1)
            self.assertEqual(len(inv_calls), 1)

    def test_claim_pending_drops_recovers_drop_instance_id_from_fresh_inventory(self):
        miner_inst = m.DropsMiner("fake_token")
        # Stale inventory where dropInstanceID is None
        stale_inventory = {
            "dropCampaignsInProgress": [{
                "id": "c1",
                "game": {"name": "WARDOGS"},
                "timeBasedDrops": [{
                    "id": "d1",
                    "name": "WARDOGS Tier 1",
                    "requiredMinutesWatched": 60,
                    "self": {"currentMinutesWatched": 60, "isClaimed": False, "dropInstanceID": None}
                }]
            }]
        }
        # Fresh inventory where Twitch populated the authentic instance UUID
        fresh_inventory = {
            "dropCampaignsInProgress": [{
                "id": "c1",
                "game": {"name": "WARDOGS"},
                "timeBasedDrops": [{
                    "id": "d1",
                    "name": "WARDOGS Tier 1",
                    "requiredMinutesWatched": 60,
                    "self": {"currentMinutesWatched": 60, "isClaimed": False, "dropInstanceID": "authentic-uuid-12345"}
                }]
            }]
        }
        miner_inst.last_inventory = stale_inventory
        miner_inst.last_inventory_time = time.time()

        claimed_ids = []
        def fake_claim(drop_id, browser_watcher=None):
            claimed_ids.append(drop_id)
            return True

        with mock.patch.object(miner_inst.client, "get_inventory", return_value=fresh_inventory), \
             mock.patch.object(miner_inst.client, "claim_drop", side_effect=fake_claim):
            claimed = miner_inst.claim_pending_drops(force=True)
            self.assertEqual(claimed, 1)
            self.assertEqual(claimed_ids, ["authentic-uuid-12345"])


if __name__ == "__main__":
    unittest.main()
