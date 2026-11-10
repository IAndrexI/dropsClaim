import json
import os
import tempfile
import unittest
from unittest import mock

from src.twitch_drops import miner as m
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


if __name__ == "__main__":
    unittest.main()
