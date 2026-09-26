"""
Continuous Twitch Drops background miner daemon.
Lightweight, zero-telemetry, streamless execution using Python standard library.
"""

import json
import logging
import random
import sys
import time
import urllib.request
import urllib.error
from typing import List, Optional

from .twitch_api import TwitchClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] (TwitchDrops) %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("drops_miner")


class DropsMiner:
    def __init__(
        self,
        auth_token: str,
        priority_games: Optional[List[str]] = None,
        webhook_url: Optional[str] = None,
    ):
        self.client = TwitchClient(auth_token=auth_token)
        self.priority_games = [g.strip().lower() for g in (priority_games or []) if g.strip()]
        self.webhook_url = webhook_url
        self.running = False

    def notify(self, message: str):
        """Send notification to user-configured webhook (Discord/Telegram compatible)."""
        logger.info("[NOTIFICATION] %s", message)
        if not self.webhook_url:
            return
        try:
            # Discord webhook format
            payload = {"content": f"[Twitch Drops Miner]: {message}"}
            body = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                self.webhook_url,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "AutoLootClaimer/1.0",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=8):
                pass
        except Exception as e:
            logger.warning("Failed to send webhook notification: %s", e)

    def claim_pending_drops(self) -> int:
        """Scan inventory for any drops that reached 100% but were not yet claimed."""
        claimed_count = 0
        try:
            inv = self.client.get_inventory()
            campaigns = inv.get("dropCampaignInProgress") or []
            if isinstance(campaigns, dict):
                campaigns = [campaigns]

            for camp in campaigns:
                game_name = camp.get("game", {}).get("displayName", "Unknown")
                for drop in camp.get("timeBasedDrops", []):
                    self_data = drop.get("self", {})
                    is_claimed = self_data.get("isClaimed", False)
                    drop_id = self_data.get("dropInstanceId")
                    watched = self_data.get("currentMinutesWatched", 0)
                    required = drop.get("requiredMinutesWatched", 0)

                    if drop_id and not is_claimed and watched >= required and required > 0:
                        drop_name = drop.get("name", "Drop Reward")
                        logger.info("Found claimable drop: %s (%s) [%d/%d min]", drop_name, game_name, watched, required)
                        if self.client.claim_drop(drop_id):
                            claimed_count += 1
                            self.notify(f"Successfully claimed **{drop_name}** for **{game_name}**!")
        except Exception as err:
            logger.error("Error checking pending drops: %s", err)
        return claimed_count

    def select_active_campaign(self) -> Optional[dict]:
        """Select the highest priority campaign that still has unclaimed drops."""
        campaigns = self.client.get_all_active_campaigns()
        if not campaigns:
            logger.info("No active drop campaigns found on Twitch right now.")
            return None

        # Filter out campaigns where all drops are already claimed
        eligible = []
        for camp in campaigns:
            drops = camp.get("timeBasedDrops", [])
            has_unclaimed = any(
                not d.get("self", {}).get("isClaimed", False)
                for d in drops
            )
            if has_unclaimed:
                eligible.append(camp)

        if not eligible:
            logger.info("All drops in currently active campaigns have already been claimed! You are all caught up.")
            return None

        # Sort by user priority if provided
        if self.priority_games:
            for p_game in self.priority_games:
                for camp in eligible:
                    game_title = camp.get("game", {}).get("name", "").lower()
                    if p_game in game_title:
                        return camp

        # Fallback to the first available campaign with the most viewers / earliest end
        return eligible[0]

    def run_cycle(self):
        """Single iteration of the mining loop."""
        # 1. Claim anything ready
        self.claim_pending_drops()

        # 2. Pick a campaign
        campaign = self.select_active_campaign()
        if not campaign:
            logger.info("Sleeping for 15 minutes before checking for new campaigns...")
            time.sleep(900)
            return

        game_name = campaign.get("game", {}).get("name", "Unknown")
        camp_name = campaign.get("name", game_name)
        logger.info("Target campaign: '%s' for game '%s'", camp_name, game_name)

        # 3. Find a live stream with drops enabled
        channel = self.client.find_eligible_channel(game_name)
        if not channel:
            logger.warning("No live streams with drops enabled for '%s'. Waiting 5 minutes...", game_name)
            time.sleep(300)
            return

        logger.info(
            "Mining on channel: %s (%s) with %d viewers",
            channel["channel_name"], channel["channel_login"], channel["viewers"]
        )

        # 4. Streamless watch loop (sending heartbeat every ~60s)
        # Run for up to 15 minutes or until stream goes offline
        for minute in range(15):
            success = self.client.send_minute_watched_heartbeat(
                channel_id=channel["channel_id"],
                stream_id=channel["stream_id"],
            )
            if success:
                logger.info(
                    "Heartbeat %d/15 sent successfully for %s (%s)",
                    minute + 1, game_name, channel["channel_name"]
                )
            else:
                logger.warning("Heartbeat failed, checking if stream is still active...")

            # Check if drop completed during this interval
            self.claim_pending_drops()

            # Randomize delay between 57 and 63 seconds to match realistic browser behavior
            time.sleep(random.uniform(57.0, 63.0))

    def start(self):
        """Start the miner daemon."""
        logger.info("Starting Twitch Drops Miner Daemon...")
        if not self.client.validate_session():
            logger.critical("Authentication failed! Please verify your TWITCH_AUTH_TOKEN.")
            sys.exit(1)

        self.running = True
        logger.info("Twitch session valid! Priorities: %s", self.priority_games or "Auto (All)")

        while self.running:
            try:
                self.run_cycle()
            except KeyboardInterrupt:
                logger.info("Stopping miner daemon upon user request.")
                self.running = False
                break
            except Exception as err:
                logger.error("Unexpected error in mining loop: %s. Retrying in 60s...", err, exc_info=True)
                time.sleep(60)
