"""
Continuous Twitch Drops background miner daemon.
Lightweight, zero-telemetry, streamless execution using Python standard library.
Ensures watching 1 stream at a time under the user's authentic account.
Supports both Default mode (mine all active drops) and Selective mode (only mine activated games).
"""

import atexit
import json
import logging
import os
import random
import signal
import sys
import time
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional

from .twitch_api import TwitchClient, is_campaign_expired

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(BASE_DIR, "data")
STATUS_FILE = os.path.join(DATA_DIR, "miner_status.json")
CONFIG_FILE = os.path.join(DATA_DIR, "mining_config.json")
PID_FILE = os.path.join(DATA_DIR, "miner.pid")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] (TwitchDrops) %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("drops_miner")


from .process_guard import acquire_pid_lock, release_pid_lock, is_pid_alive



def load_mining_config() -> Dict[str, Any]:

    """Load mining preferences (mode and activated games list)."""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return {
                        "mode": data.get("mode", "default"),
                        "activated_games": [g.strip() for g in data.get("activated_games", []) if g.strip()],
                    }
        except Exception:
            pass
    return {"mode": "default", "activated_games": []}


def save_miner_status(status_dict: Dict[str, Any]):
    """Persist current stream status to disk for dashboard telemetry."""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(status_dict, f, indent=2)
    except Exception as e:
        logger.debug("Could not write miner status: %s", e)


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
        self.current_stream: Optional[Dict[str, Any]] = None
        self.last_campaign_refresh: float = 0.0

    def notify(self, message: str):
        """Send notification to user-configured webhook (Discord/Telegram compatible)."""
        logger.info("[NOTIFICATION] %s", message)
        if not self.webhook_url:
            return
        try:
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
            campaigns = inv.get("dropCampaignsInProgress") or inv.get("dropCampaignInProgress") or []
            if isinstance(campaigns, dict):
                campaigns = [campaigns]

            for camp in campaigns:
                game_name = camp.get("game", {}).get("displayName") or camp.get("game", {}).get("name") or "Unknown"
                for drop in camp.get("timeBasedDrops", []):
                    self_data = drop.get("self", {})
                    is_claimed = self_data.get("isClaimed", False)
                    drop_id = self_data.get("dropInstanceID") or self_data.get("dropInstanceId")
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
        """
        Select the highest priority campaign that still has unclaimed drops.
        Respects 'mode' (default vs selective) and 'activated_games'.
        Periodically refreshes campaign roster (at least hourly / daily) to discover new drops.
        """
        config = load_mining_config()
        mode = config.get("mode", "default")
        activated_games = [g.lower().strip() for g in config.get("activated_games", []) if g.strip()]
        if not activated_games and self.priority_games:
            activated_games = self.priority_games

        now = time.time()
        force_refresh = (now - self.last_campaign_refresh >= 3600)  # Hourly / daily refresh
        campaigns = self.client.get_all_active_campaigns(priority_games=activated_games, force_refresh=force_refresh)
        if force_refresh:
            self.last_campaign_refresh = now
            logger.info("[MINER] Refreshed active drop campaigns roster (Found %d campaigns)", len(campaigns))

        if not campaigns:
            logger.info("No active drop campaigns found on Twitch right now.")
            return None

        # Filter out campaigns where all drops are already claimed or campaign is expired
        eligible = []
        for camp in campaigns:
            if is_campaign_expired(camp):
                continue
            drops = camp.get("timeBasedDrops", [])
            has_unclaimed = any(
                not d.get("self", {}).get("isClaimed", False)
                for d in drops
            )
            # If no drops array but has time_left or valid campaign
            if has_unclaimed or (not drops and camp.get("name")):
                eligible.append(camp)


        if not eligible:
            logger.info("All drops in currently active campaigns have already been claimed! You are all caught up.")
            return None

        # If in 'selective' mode, strictly restrict to user-activated games
        if mode == "selective":
            if not activated_games:
                logger.info("[SELECTIVE MODE] No games are currently activated for mining. Waiting for user selection.")
                return None

            filtered = []
            for camp in eligible:
                game_obj = camp.get("game") or {}
                gname = (game_obj.get("displayName") or game_obj.get("name") or "").lower()
                cname = (camp.get("name") or "").lower()
                if any(ag in gname or ag in cname for ag in activated_games):
                    filtered.append(camp)

            if not filtered:
                logger.info("[SELECTIVE MODE] No active drops match your activated games %s right now.", activated_games)
                return None
            return filtered[0]

        # 'default' mode: Prioritize activated games, then fallback to any active drop campaign
        if activated_games:
            for p_game in activated_games:
                for camp in eligible:
                    game_obj = camp.get("game") or {}
                    gname = (game_obj.get("displayName") or game_obj.get("name") or "").lower()
                    cname = (camp.get("name") or "").lower()
                    if p_game in gname or p_game in cname:
                        return camp

        # Fallback to the first available campaign with unclaimed items
        return eligible[0]

    def run_cycle(self):
        """Single iteration of the mining loop - watches ONLY 1 stream at a time."""
        # 1. Claim anything ready
        self.claim_pending_drops()

        # 2. Check if user is currently using Twitch on any browser or device
        if self.client.is_user_actively_using_twitch():
            logger.info("[MINER] User is currently using Twitch. Pausing miner to ensure ONLY 1 viewing of Twitch.")
            save_miner_status({
                "active": False,
                "paused": True,
                "user_active": True,
                "message": "Miner paused: You are currently using Twitch. Mining will resume automatically when you finish.",
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            })
            time.sleep(60)
            return

        # 3. Pick a single campaign
        campaign = self.select_active_campaign()
        if not campaign:
            save_miner_status({"active": False, "message": "No eligible campaigns to mine at this moment."})
            logger.info("Sleeping for 10 minutes before checking for active campaigns...")
            time.sleep(600)
            return

        game_name = campaign.get("game", {}).get("displayName") or campaign.get("game", {}).get("name", "Unknown")
        camp_name = campaign.get("name", game_name)
        logger.info("[MINER] Target campaign: '%s' for game '%s'", camp_name, game_name)

        # 4. Find exactly 1 live stream with drops enabled (Only 1 stream at a time)
        channel = self.client.find_eligible_channel(game_name)
        if not channel:
            logger.warning("[MINER] No live streams with drops enabled for '%s'. Checking next game in 3 minutes...", game_name)
            save_miner_status({
                "active": False,
                "game": game_name,
                "campaign": camp_name,
                "message": f"No live streams currently online with drops enabled for {game_name}.",
            })
            time.sleep(180)
            return

        stream_url = f"https://www.twitch.tv/{channel['channel_login']}"
        logger.info(
            "[MINER] Watching 1 stream: %s (@%s) with %d viewers | Drops Verified: %s | URL: %s",
            channel["channel_name"], channel["channel_login"], channel["viewers"],
            channel.get("verified_drops", False), stream_url
        )

        # Pre-fetch authentic playback stream info (Usher m3u8 playlist and sub-playlist url)
        playback_info = self.client.get_playback_stream_info(channel["channel_login"])
        broadcast_id = playback_info.get("broadcast_id") or channel.get("stream_id", "")
        sub_playlist_url = playback_info.get("sub_playlist_url")

        # 5. Streamless watch loop under user account (sending heartbeat every ~60s)
        # Strictly watches ONLY this single stream for a block of up to 15 minutes
        for minute in range(15):
            # Check if user started using Twitch mid-stream
            if self.client.is_user_actively_using_twitch():
                logger.info("[MINER] User started using Twitch. Stopping stream viewing immediately to ensure ONLY 1 viewing.")
                save_miner_status({
                    "active": False,
                    "paused": True,
                    "user_active": True,
                    "message": "Miner paused: You started using Twitch. Mining will resume automatically when you finish.",
                    "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                })
                break

            success = self.client.send_minute_watched_heartbeat(
                channel_id=channel["channel_id"],
                stream_id=broadcast_id,
                channel_login=channel["channel_login"],
                game_name=game_name,
                game_id=channel.get("game_id"),
                sub_playlist_url=sub_playlist_url,
            )

            current_status = {
                "active": True,
                "paused": False,
                "user_active": False,
                "game": game_name,
                "campaign": camp_name,
                "channel_name": channel["channel_name"],
                "channel_login": channel["channel_login"],
                "stream_url": stream_url,
                "viewers": channel["viewers"],
                "minutes_watched": minute + 1,
                "user_login": self.client.user_login,
                "user_id": self.client.user_id,
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            save_miner_status(current_status)

            if success:
                logger.info(
                    "[MINER] Heartbeat minute %d/15 sent successfully for %s on channel %s",
                    minute + 1, game_name, channel["channel_name"]
                )
            else:
                logger.warning("[MINER] Heartbeat returned non-204, verifying stream status...")

            # Claim any drops completed during this minute
            self.claim_pending_drops()

            # Randomize delay between 58 and 62 seconds to mimic authentic browser behavior
            time.sleep(random.uniform(58.0, 62.0))

        # Reset active status when switching streams
        save_miner_status({"active": False, "message": "Switching or checking for next campaign."})

    def start(self):
        """Start the miner daemon."""
        logger.info("Starting Twitch Drops Miner Daemon...")
        if not acquire_pid_lock():
            logger.critical("Aborting startup: Another Twitch Drops Miner process is already active. Only 1 viewing instance is allowed.")
            sys.exit(1)

        atexit.register(release_pid_lock)

        def handle_signal(sig, frame):
            logger.info("Received termination signal (%s). Exiting gracefully...", sig)
            self.running = False
            release_pid_lock()
            save_miner_status({"active": False, "message": "Miner stopped by system signal."})
            sys.exit(0)

        try:
            signal.signal(signal.SIGINT, handle_signal)
            signal.signal(signal.SIGTERM, handle_signal)
        except Exception:
            pass

        if not self.client.validate_session():
            logger.critical("Authentication failed! Please verify your TWITCH_AUTH_TOKEN.")
            release_pid_lock()
            sys.exit(1)

        self.running = True
        logger.info(
            "Twitch session valid! Mining as: %s (ID: %s) [Single viewing enforced]",
            self.client.user_login, self.client.user_id
        )

        while self.running:
            try:
                self.run_cycle()
            except KeyboardInterrupt:
                logger.info("Stopping miner daemon upon user request.")
                self.running = False
                release_pid_lock()
                save_miner_status({"active": False, "message": "Miner stopped by user."})
                break
            except Exception as err:
                logger.error("Unexpected error in mining loop: %s. Retrying in 60s...", err, exc_info=True)
                save_miner_status({"active": False, "message": f"Error: {err}"})
                time.sleep(60)

        release_pid_lock()

