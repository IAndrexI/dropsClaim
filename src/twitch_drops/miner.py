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

from .twitch_api import TwitchClient, extract_game_name, is_campaign_expired, is_timestamp_expired

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
    """Load mining preferences (mode and single selected game — strictly 1 at a time)."""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    raw_games = [g.strip() for g in data.get("activated_games", []) if g.strip()]
                    single_game = raw_games[:1]
                    return {
                        "mode": data.get("mode", "default"),
                        "activated_games": single_game,
                        "selected_game": single_game[0] if single_game else "",
                        "updated_at": data.get("updated_at", 0),
                    }
        except Exception:
            pass
    return {"mode": "default", "activated_games": [], "selected_game": "", "updated_at": 0}


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
        clean_prio = [g.strip().lower() for g in (priority_games or []) if g.strip()]
        self.priority_games = clean_prio[:1]  # Strictly at most 1 selected game at a time
        self.webhook_url = webhook_url
        self.running = False
        self.current_stream: Optional[Dict[str, Any]] = None
        self.last_campaign_refresh: float = 0.0
        self.failed_claim_ids: set = set()
        self.last_inventory: Dict[str, Any] = {}
        self.last_config_sig: Optional[tuple] = None

    def _get_config_signature(self) -> tuple:
        """Return a comparable signature of the current mining config to detect user changes immediately."""
        cfg = load_mining_config()
        mode = cfg.get("mode", "default")
        games = tuple(g.lower().strip() for g in cfg.get("activated_games", [])[:1] if g.strip())
        updated_at = cfg.get("updated_at", 0)
        return (mode, games, updated_at)

    def _has_config_changed(self, initial_sig: tuple) -> bool:
        """Check if the user changed the selected game or mining mode."""
        return self._get_config_signature() != initial_sig

    def _interruptible_sleep(self, total_seconds: float, initial_sig: tuple) -> bool:
        """
        Sleep up to total_seconds in 0.2-second slices, waking up immediately if
        mining selection/mode changed or daemon stopped.
        Returns True if interrupted by a config change or stop signal.
        """
        end_time = time.time() + total_seconds
        while time.time() < end_time:
            if not self.running or self._has_config_changed(initial_sig):
                return True
            remaining = end_time - time.time()
            if remaining > 0:
                time.sleep(min(0.2, remaining))
        return not self.running or self._has_config_changed(initial_sig)

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
            self.last_inventory = inv or {}
            campaigns = inv.get("dropCampaignsInProgress") or inv.get("dropCampaignInProgress") or []
            if isinstance(campaigns, dict):
                campaigns = [campaigns]

            for camp in campaigns:
                game_name = extract_game_name(camp, "Unknown")
                camp_status = str(camp.get("status") or "").upper()
                camp_end = str(camp.get("endAt") or "")
                # Skip campaigns whose claim window (>24h past endAt) has already expired
                if camp_status == "EXPIRED" and camp_end and is_timestamp_expired(camp_end, grace_seconds=86400):
                    continue

                for drop in camp.get("timeBasedDrops", []):
                    self_data = drop.get("self") or {}
                    is_claimed = bool(self_data.get("isClaimed", False))
                    drop_id = self_data.get("dropInstanceID") or self_data.get("dropInstanceId")
                    watched = int(self_data.get("currentMinutesWatched") or 0)
                    required = int(drop.get("requiredMinutesWatched") or 0)
                    drop_end = str(drop.get("endAt") or camp_end)

                    if not drop_id or is_claimed or drop_id in self.failed_claim_ids:
                        continue
                    if drop_end and is_timestamp_expired(drop_end, grace_seconds=86400):
                        self.failed_claim_ids.add(drop_id)
                        continue

                    if watched >= required and required > 0:
                        drop_name = drop.get("name", "Drop Reward")
                        logger.info("Found claimable drop: %s (%s) [%d/%d min]", drop_name, game_name, watched, required)
                        if self.client.claim_drop(drop_id):
                            claimed_count += 1
                            self.notify(f"Successfully claimed **{drop_name}** for **{game_name}**!")
                        else:
                            logger.warning(
                                "Drop %s (%s) could not be claimed (claim window expired or unavailable). Skipping future retries.",
                                drop_name, game_name
                            )
                            self.failed_claim_ids.add(drop_id)
        except Exception as err:
            logger.error("Error checking pending drops: %s", err)
        return claimed_count

    def _get_live_drop_progress(self, game_name: str, camp_name: str, fallback_camp: Optional[dict] = None) -> Dict[str, Any]:
        """Extract the active unclaimed drop's current watch progress for telemetry."""
        inv_camps = (self.last_inventory or {}).get("dropCampaignsInProgress") or []
        if isinstance(inv_camps, dict):
            inv_camps = [inv_camps]

        target_camps = []
        for ic in inv_camps:
            if str(ic.get("status") or "").upper() == "EXPIRED" or is_campaign_expired(ic):
                continue
            igame = extract_game_name(ic, "").lower()
            icname = str(ic.get("name") or "").lower()
            if igame == game_name.lower() or (camp_name and icname == camp_name.lower()):
                target_camps.append(ic)

        if not target_camps and fallback_camp:
            target_camps = [fallback_camp]

        best_drop = None
        for tc in target_camps:
            for d in tc.get("timeBasedDrops") or []:
                s = d.get("self") or {}
                if s.get("isClaimed"):
                    continue
                req = int(d.get("requiredMinutesWatched") or 0)
                subs = int(d.get("requiredSubs") or 0)
                if req <= 0 or subs > 0:
                    continue
                watched = int(s.get("currentMinutesWatched") or 0)
                pct = min(100, int((watched / req) * 100)) if req > 0 else 0
                candidate = {
                    "current_drop_name": d.get("name") or "Drop Reward",
                    "current_drop_watched": watched,
                    "current_drop_required": req,
                    "current_drop_percent": pct,
                    "current_drop_remaining": max(0, req - watched),
                }
                if best_drop is None or (watched > 0 and best_drop["current_drop_watched"] == 0):
                    best_drop = candidate
                    if watched > 0 and watched < req:
                        return best_drop
        return best_drop or {}

    def get_eligible_campaigns(self) -> List[dict]:
        """
        Return ordered list of eligible campaigns based on current mining config.
        Enforces strictly 1 selected game at a time when a game is selected.
        """
        config = load_mining_config()
        mode = config.get("mode", "default")
        activated_games = [g.lower().strip() for g in config.get("activated_games", [])[:1] if g.strip()]
        if not activated_games and self.priority_games and mode != "selective":
            activated_games = self.priority_games[:1]

        selected_game = activated_games[0] if activated_games else ""

        now = time.time()
        force_refresh = (now - self.last_campaign_refresh >= 3600)
        campaigns = self.client.get_all_active_campaigns(
            priority_games=activated_games,
            force_refresh=force_refresh,
            include_expired_inventory=False,
        )
        if force_refresh:
            self.last_campaign_refresh = now
            logger.info("[MINER] Refreshed active drop campaigns roster (Found %d campaigns)", len(campaigns))

        if not campaigns:
            logger.info("No active drop campaigns found on Twitch right now.")
            return []

        eligible = []
        for camp in campaigns:
            if is_campaign_expired(camp):
                continue
            drops = camp.get("timeBasedDrops", [])
            has_unclaimed_watchable = any(
                not d.get("self", {}).get("isClaimed", False)
                and int(d.get("requiredMinutesWatched") or 0) > 0
                and int(d.get("requiredSubs") or 0) == 0
                for d in drops
            )
            if has_unclaimed_watchable:
                eligible.append(camp)

        if not eligible:
            logger.info("All watch-time drops in currently active campaigns have already been claimed! You are all caught up.")
            return []

        # If a specific game is selected (or in selective mode), match the single selected game
        if mode == "selective":
            if not selected_game:
                logger.info("[SELECTIVE MODE] No game is currently selected for mining. Waiting for user selection.")
                return []

            filtered = []
            for camp in eligible:
                gname = extract_game_name(camp, "").lower()
                cname = (camp.get("name") or "").lower()
                if selected_game == gname or selected_game in gname or selected_game in cname:
                    filtered.append(camp)

            if not filtered:
                logger.info("[SELECTIVE MODE] No active watch-time drops match your selected game '%s' right now.", selected_game)
                return []
            return filtered[:1]

        # 'default' mode: If user selected a single active game, put it first (or strictly mine it first)
        if selected_game:
            prioritized = []
            others = []
            for camp in eligible:
                gname = extract_game_name(camp, "").lower()
                cname = (camp.get("name") or "").lower()
                if selected_game == gname or selected_game in gname or selected_game in cname:
                    prioritized.append(camp)
                else:
                    others.append(camp)
            return prioritized + others

        return eligible

    def select_active_campaign(self) -> Optional[dict]:
        """Select the highest priority single campaign that still has unclaimed watch-time drops."""
        eligible = self.get_eligible_campaigns()
        return eligible[0] if eligible else None

    def _handle_mid_cycle_swap(self, old_channel_name: str, old_game_name: str):
        """Log swap and only set switching=True if server.py hasn't already resolved the new active stream."""
        new_cfg = load_mining_config()
        logger.info(
            "[MINER] Active mine swapped (mode=%s, selected=%s)! Instantly leaving %s (%s) and switching to new stream.",
            new_cfg.get("mode"), new_cfg.get("selected_game") or "Auto", old_channel_name, old_game_name
        )
        try:
            if os.path.exists(STATUS_FILE):
                with open(STATUS_FILE, "r", encoding="utf-8") as f:
                    curr_stat = json.load(f) or {}
                if curr_stat.get("active") and (time.time() - float(curr_stat.get("switched_at") or 0)) < 30:
                    return
        except Exception:
            pass

        save_miner_status({
            "active": False,
            "switching": True,
            "game": new_cfg.get("selected_game") or "",
            "selected_game": new_cfg.get("selected_game") or "",
            "mode": new_cfg.get("mode", "default"),
            "message": f"Cancelled {old_channel_name} ({old_game_name}). Switching to selected option: {new_cfg.get('selected_game') or 'Auto-Mine'}...",
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })

    def run_cycle(self):
        """Single iteration of the mining loop - watches ONLY 1 stream at a time and instants-swaps on selection change."""
        cycle_sig = self._get_config_signature()
        config_just_swapped = (self.last_config_sig is not None and self.last_config_sig != cycle_sig)
        self.last_config_sig = cycle_sig

        config = load_mining_config()
        selected_game_cfg = config.get("selected_game", "")
        mode_cfg = config.get("mode", "default")

        # 1. If we did NOT just swap the active mine, claim pending drops and check user activity
        if not config_just_swapped:
            self.claim_pending_drops()
            if self._has_config_changed(cycle_sig):
                logger.info("[MINER] Selection changed during claim check. Switching immediately.")
                return

            if self.client.is_user_actively_using_twitch():
                logger.info("[MINER] User is currently using Twitch. Pausing miner to ensure ONLY 1 viewing of Twitch.")
                save_miner_status({
                    "active": False,
                    "paused": True,
                    "user_active": True,
                    "selected_game": selected_game_cfg,
                    "mode": mode_cfg,
                    "message": "Miner paused: You are currently using Twitch. Mining will resume automatically when you finish.",
                    "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                })
                self._interruptible_sleep(60, cycle_sig)
                return

        # 2. Pick eligible campaign(s) and find 1 live stream
        eligible_campaigns = self.get_eligible_campaigns()
        if not eligible_campaigns:
            msg = (
                f"Selected game '{selected_game_cfg}' has no active watch-time drops right now."
                if (mode_cfg == "selective" and selected_game_cfg)
                else (
                    "Selective mode active: Select 1 game below to start mining."
                    if mode_cfg == "selective"
                    else "No eligible campaigns to mine at this moment."
                )
            )
            save_miner_status({
                "active": False,
                "selected_game": selected_game_cfg,
                "mode": mode_cfg,
                "message": msg,
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            })
            logger.info("[MINER] %s Waiting (will wake immediately if selection changes)...", msg)
            self._interruptible_sleep(600, cycle_sig)
            return

        chosen_campaign = None
        channel = None
        game_name = "Unknown"
        camp_name = "Unknown"

        # Check if web server already pre-resolved the live channel for this swap
        pre_switched_stat = {}
        if os.path.exists(STATUS_FILE):
            try:
                with open(STATUS_FILE, "r", encoding="utf-8") as f:
                    pre_switched_stat = json.load(f) or {}
            except Exception:
                pre_switched_stat = {}

        # If a specific game is selected by the user, strictly try that selected game first
        for idx, candidate_camp in enumerate(eligible_campaigns):
            if self._has_config_changed(cycle_sig):
                logger.info("[MINER] Selection changed while searching for stream. Switching immediately...")
                return
            cand_game = extract_game_name(candidate_camp, "Unknown")
            cand_name = candidate_camp.get("name", cand_game)
            if selected_game_cfg and idx > 0 and selected_game_cfg.lower() not in cand_game.lower() and selected_game_cfg.lower() not in cand_name.lower():
                break

            # Adopt pre-resolved stream from server.py if fresh (<60s) and matches cand_game
            if (
                pre_switched_stat.get("active")
                and pre_switched_stat.get("channel_login")
                and str(pre_switched_stat.get("game") or "").lower() == cand_game.lower()
                and (time.time() - float(pre_switched_stat.get("switched_at") or 0)) < 60
            ):
                chosen_campaign = candidate_camp
                channel = {
                    "channel_id": str(pre_switched_stat.get("channel_id") or ""),
                    "channel_login": pre_switched_stat["channel_login"],
                    "channel_name": pre_switched_stat.get("channel_name") or pre_switched_stat["channel_login"],
                    "stream_id": str(pre_switched_stat.get("stream_id") or ""),
                    "viewers": int(pre_switched_stat.get("viewers") or 0),
                    "game_name": cand_game,
                    "game_id": str(pre_switched_stat.get("game_id") or ""),
                    "verified_drops": True,
                }
                game_name = cand_game
                camp_name = cand_name
                logger.info("[MINER] Adopted instant-switched stream %s (@%s) for '%s'", channel["channel_name"], channel["channel_login"], game_name)
                break

            logger.info("[MINER] Checking live streams with drops enabled for '%s' (%s)...", cand_game, cand_name)
            found_ch = self.client.find_eligible_channel(cand_game, fast_first=True)
            if found_ch:
                chosen_campaign = candidate_camp
                channel = found_ch
                game_name = cand_game
                camp_name = cand_name
                break

        if not chosen_campaign or not channel:
            first_camp = eligible_campaigns[0]
            game_name = extract_game_name(first_camp, "Unknown")
            camp_name = first_camp.get("name", game_name)
            logger.warning("[MINER] No live streams with drops enabled for '%s'. Retrying in 3 minutes (or immediately on selection change)...", game_name)
            save_miner_status({
                "active": False,
                "game": game_name,
                "campaign": camp_name,
                "selected_game": selected_game_cfg or game_name,
                "mode": mode_cfg,
                "message": f"No live streams currently online with drops enabled for {game_name}.",
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            })
            self._interruptible_sleep(180, cycle_sig)
            return

        stream_url = f"https://www.twitch.tv/{channel['channel_login']}"
        logger.info(
            "[MINER] Watching 1 stream: %s (@%s) for '%s' with %d viewers | Drops Verified: %s | URL: %s",
            channel["channel_name"], channel["channel_login"], game_name, channel["viewers"],
            channel.get("verified_drops", False), stream_url
        )

        # Immediately write active stream status BEFORE fetching playback info or sending heartbeat so UI updates with 0 lag
        initial_drop_info = self._get_live_drop_progress(game_name, camp_name, fallback_camp=chosen_campaign)
        immediate_status = {
            "active": True,
            "switching": False,
            "paused": False,
            "user_active": False,
            "game": game_name,
            "selected_game": selected_game_cfg or game_name,
            "mode": mode_cfg,
            "campaign": camp_name,
            "channel_id": channel.get("channel_id", ""),
            "channel_name": channel["channel_name"],
            "channel_login": channel["channel_login"],
            "stream_id": channel.get("stream_id", ""),
            "game_id": channel.get("game_id", ""),
            "stream_url": stream_url,
            "viewers": channel["viewers"],
            "minutes_watched": 1,
            "user_login": self.client.user_login,
            "user_id": self.client.user_id,
            "switched_at": pre_switched_stat.get("switched_at") or time.time(),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        immediate_status.update(initial_drop_info)
        save_miner_status(immediate_status)

        # Pre-fetch authentic playback stream info (Usher m3u8 playlist and sub-playlist url)
        playback_info = self.client.get_playback_stream_info(channel["channel_login"])
        broadcast_id = playback_info.get("broadcast_id") or channel.get("stream_id", "")
        sub_playlist_url = playback_info.get("sub_playlist_url")

        # 5. Streamless watch loop under user account (sending heartbeat every ~60s)
        # Strictly watches ONLY this single stream, and immediately cancels if user selects another option!
        for minute in range(15):
            if self._has_config_changed(cycle_sig):
                self._handle_mid_cycle_swap(channel["channel_name"], game_name)
                return

            # Check if user started using Twitch mid-stream
            if self.client.is_user_actively_using_twitch():
                logger.info("[MINER] User started using Twitch. Stopping stream viewing immediately to ensure ONLY 1 viewing.")
                save_miner_status({
                    "active": False,
                    "paused": True,
                    "user_active": True,
                    "selected_game": selected_game_cfg or game_name,
                    "mode": mode_cfg,
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

            # Claim any drops completed during this minute & refresh live inventory progress
            self.claim_pending_drops()
            live_drop_info = self._get_live_drop_progress(game_name, camp_name, fallback_camp=chosen_campaign)

            # Re-verify config hasn't changed while claiming/fetching inventory
            if self._has_config_changed(cycle_sig):
                self._handle_mid_cycle_swap(channel["channel_name"], game_name)
                return

            current_status = {
                "active": True,
                "switching": False,
                "paused": False,
                "user_active": False,
                "game": game_name,
                "selected_game": selected_game_cfg or game_name,
                "mode": mode_cfg,
                "campaign": camp_name,
                "channel_id": channel.get("channel_id", ""),
                "channel_name": channel["channel_name"],
                "channel_login": channel["channel_login"],
                "stream_id": broadcast_id,
                "game_id": channel.get("game_id", ""),
                "stream_url": stream_url,
                "viewers": channel["viewers"],
                "minutes_watched": minute + 1,
                "user_login": self.client.user_login,
                "user_id": self.client.user_id,
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            current_status.update(live_drop_info)
            save_miner_status(current_status)

            if success:
                drop_prog_str = ""
                if live_drop_info.get("current_drop_required"):
                    drop_prog_str = (
                        f" | Drop: {live_drop_info.get('current_drop_name')} "
                        f"[{live_drop_info.get('current_drop_watched', 0)}/{live_drop_info.get('current_drop_required', 0)}m "
                        f"({live_drop_info.get('current_drop_percent', 0)}%)]"
                    )
                logger.info(
                    "[MINER] Heartbeat minute %d/15 sent successfully for %s on channel %s%s",
                    minute + 1, game_name, channel["channel_name"], drop_prog_str
                )
            else:
                logger.warning("[MINER] Heartbeat returned non-204, verifying stream status...")

            # Interruptible ~60s sleep: wakes within 0.2s if user changes selected stream/game or mode!
            if self._interruptible_sleep(random.uniform(58.0, 62.0), cycle_sig):
                self._handle_mid_cycle_swap(channel["channel_name"], game_name)
                return

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

