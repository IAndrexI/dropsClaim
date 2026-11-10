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


if __name__ == "__main__":
    unittest.main()
