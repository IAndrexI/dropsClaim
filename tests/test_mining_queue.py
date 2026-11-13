import json
import os
import tempfile
import unittest
from unittest import mock

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

        with mock.patch.object(client, "post_gql", return_value=gql_directory_response), \
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
        with mock.patch.object(client, "post_gql", return_value=no_drops_dir), \
             mock.patch.object(client, "post_persisted_gql", return_value={"data": {"channel": {"viewerDropCampaigns": []}}}):
            ch_none = client.find_eligible_channel("Rust", fast_first=True)
            self.assertIsNone(ch_none)

    def test_stalled_progress_after_3_minutes_switches_channel_immediately(self):
        with open(self.cfg_file, "w", encoding="utf-8") as f:
            json.dump({"mode": "default", "queue": ["Rust"], "updated_at": 100}, f)

        dm = m.DropsMiner("fake_token", priority_games=["Rust"])
        dm.running = True
        dm.progress_check_minutes = 3

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

            # Must stop after exactly 3 heartbeats (3 minutes without progress), NOT 15 minutes!
            self.assertEqual(len(heartbeats_sent), 3)
            self.assertIn("stalled_channel", dm._get_excluded_channels())

            with open(self.stat_file, "r", encoding="utf-8") as f:
                stat = json.load(f)
            self.assertTrue(stat.get("switching"))
            self.assertIn("No drop progress increase after 3 mins", stat.get("message", ""))

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


if __name__ == "__main__":
    unittest.main()
