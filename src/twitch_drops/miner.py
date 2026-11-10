"""
Continuous Twitch Drops background miner daemon.
Lightweight, zero-telemetry, streamless execution using Python standard library.
Ensures watching strictly 1 stream at a time under the user's authentic account.
Supports ordered Mining Queue (1 active stream at a time + automatic queue advance upon completion).
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
from typing import Any, Dict, List, Optional, Set

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
    """Load mining preferences and ordered queue (strictly 1 mined at a time, advancing through queue)."""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    raw_queue = data.get("queue")
                    if not isinstance(raw_queue, list):
                        raw_queue = data.get("activated_games", [])
                    seen = set()
                    clean_queue = []
                    for g in raw_queue:
                        if isinstance(g, str) and g.strip():
                            gs = g.strip()
                            if gs.lower() not in seen:
                                seen.add(gs.lower())
                                clean_queue.append(gs)
                    return {
                        "mode": data.get("mode", "default"),
                        "queue": clean_queue,
                        "activated_games": clean_queue,
                        "selected_game": clean_queue[0] if clean_queue else "",
                        "updated_at": data.get("updated_at", 0),
                    }
        except Exception:
            pass
    return {"mode": "default", "queue": [], "activated_games": [], "selected_game": "", "updated_at": 0}


def remove_from_mining_queue(game_name: str) -> Dict[str, Any]:
    """Remove a completed game from the mining queue on disk and return the updated config."""
    cfg = load_mining_config()
    if not game_name:
        return cfg
    target_low = game_name.strip().lower()
    old_queue = cfg.get("queue", [])
    new_queue = [
        g for g in old_queue
        if g.lower() != target_low and g.lower() not in target_low and target_low not in g.lower()
    ]
    if len(new_queue) == len(old_queue):
        return cfg
    now_ts = time.time()
    new_cfg = {
        "mode": cfg.get("mode", "default"),
        "queue": new_queue,
        "activated_games": new_queue,
        "selected_game": new_queue[0] if new_queue else "",
        "updated_at": now_ts,
    }
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(new_cfg, f, indent=2)
    except Exception as e:
        logger.debug("Could not update mining queue file: %s", e)
    return new_cfg


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
        seen = set()
        clean_prio = []
        for g in (priority_games or []):
            if isinstance(g, str) and g.strip():
                gs = g.strip()
                if gs.lower() not in seen:
                    seen.add(gs.lower())
                    clean_prio.append(gs)
        self.priority_games = clean_prio
        self.webhook_url = webhook_url
        self.running = False
        self.current_stream: Optional[Dict[str, Any]] = None
        self.last_campaign_refresh: float = 0.0
        self.failed_claim_ids: set = set()
        self.last_inventory: Dict[str, Any] = {}
        self.last_config_sig: Optional[tuple] = None
        # Track channels that failed to increase drop progress within 2-5 mins (login.lower() -> timestamp)
        self.unproductive_channels: Dict[str, float] = {}
        self.unproductive_cooldown_secs: float = 1800.0  # 30 minutes cooldown before retrying a stalled channel
        try:
            env_check_mins = int(os.environ.get("TWITCH_PROGRESS_CHECK_MINS", "3"))
        except Exception:
            env_check_mins = 3
        self.progress_check_minutes: int = max(2, min(5, env_check_mins))

    def _get_excluded_channels(self) -> Set[str]:
        """Return currently blacklisted/unproductive channel logins, pruning expired cooldowns."""
        now = time.time()
        expired = [
            ch for ch, ts in self.unproductive_channels.items()
            if (now - ts) >= self.unproductive_cooldown_secs
        ]
        for ch in expired:
            self.unproductive_channels.pop(ch, None)
        return set(self.unproductive_channels.keys())

    def _mark_channel_unproductive(self, channel_login: str):
        """Temporarily blacklist a channel that failed to award drop progress within 2-5 minutes."""
        if channel_login and str(channel_login).strip():
            self.unproductive_channels[str(channel_login).strip().lower()] = time.time()

    def _get_campaign_total_watched(
        self,
        game_name: str,
        camp_name: str,
        fallback_camp: Optional[dict] = None,
        live_drop_info: Optional[dict] = None,
    ) -> int:
        """
        Compute total watched minutes across drops in the active campaign so any 1-minute progress increase
        is accurately detected during the 2-5 minute watchdog check.
        """
        inv = self.last_inventory or {}
        inv_camps = inv.get("dropCampaignsInProgress") or inv.get("dropCampaignInProgress") or []
        if isinstance(inv_camps, dict):
            inv_camps = [inv_camps]

        total_watched = 0
        matched = False
        for ic in inv_camps:
            if str(ic.get("status") or "").upper() == "EXPIRED" or is_campaign_expired(ic):
                continue
            igame = extract_game_name(ic, "").lower()
            icname = str(ic.get("name") or "").lower()
            if igame == game_name.lower() or (camp_name and icname == camp_name.lower()):
                matched = True
                for d in ic.get("timeBasedDrops") or []:
                    s = d.get("self") or {}
                    total_watched += int(s.get("currentMinutesWatched") or 0)

        if not matched and fallback_camp:
            for d in fallback_camp.get("timeBasedDrops") or []:
                s = d.get("self") or {}
                total_watched += int(s.get("currentMinutesWatched") or 0)

        if live_drop_info:
            total_watched = max(total_watched, int(live_drop_info.get("current_drop_watched") or 0))
        return total_watched

    def _get_config_signature(self) -> tuple:
        """Return a comparable signature of the current mining config to detect user changes immediately."""
        cfg = load_mining_config()
        mode = cfg.get("mode", "default")
        queue = tuple(g.lower().strip() for g in cfg.get("queue", []) if g.strip())
        updated_at = cfg.get("updated_at", 0)
        return (mode, queue, updated_at)

    def _has_config_changed(self, initial_sig: tuple) -> bool:
        """Check if the user changed the selected game/queue or mining mode."""
        return self._get_config_signature() != initial_sig

    def _interruptible_sleep(self, total_seconds: float, initial_sig: tuple) -> bool:
        """
        Sleep up to total_seconds in 0.2-second slices, waking up immediately if
        mining selection/queue/mode changed or daemon stopped.
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
                            self_data["isClaimed"] = True
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
        """
        Extract the active unclaimed drop's current watch progress for telemetry.
        Cross-references gameEventDrops and drop进度 to accurately detect when all drops in a campaign are completed.
        """
        inv = self.last_inventory or {}
        inv_camps = inv.get("dropCampaignsInProgress") or inv.get("dropCampaignInProgress") or []
        if isinstance(inv_camps, dict):
            inv_camps = [inv_camps]

        # Build claimed set from gameEventDrops
        claimed_ids = set()
        for ce in (inv.get("gameEventDrops") or []):
            if isinstance(ce, dict) and ce.get("id"):
                cid = str(ce.get("id")).lower()
                claimed_ids.add(cid)
                if "_custom_id_" in cid:
                    for part in cid.split("_custom_id_"):
                        claimed_ids.add(part)

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
                d_id = str(d.get("id") or "").lower()
                drop_inst_id = str(s.get("dropInstanceID") or s.get("dropInstanceId") or "")
                req = int(d.get("requiredMinutesWatched") or 0)
                subs = int(d.get("requiredSubs") or 0)
                if req <= 0 or subs > 0:
                    continue
                watched = int(s.get("currentMinutesWatched") or 0)
                is_claimed = bool(s.get("isClaimed")) or (d_id and d_id in claimed_ids)
                if is_claimed or (drop_inst_id and drop_inst_id in self.failed_claim_ids):
                    continue
                # If watched >= req and we already ran claim_pending_drops(), treat as completed so miner swaps immediately
                if watched >= req:
                    continue

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

    def _campaign_has_unclaimed_watchable_drops(self, camp: dict) -> bool:
        """Check if a campaign still has any watch-time drops that need watching."""
        if is_campaign_expired(camp):
            return False
        inv = self.last_inventory or {}
        claimed_ids = set()
        for ce in (inv.get("gameEventDrops") or []):
            if isinstance(ce, dict) and ce.get("id"):
                cid = str(ce.get("id")).lower()
                claimed_ids.add(cid)
                if "_custom_id_" in cid:
                    for part in cid.split("_custom_id_"):
                        claimed_ids.add(part)

        for d in camp.get("timeBasedDrops", []):
            req = int(d.get("requiredMinutesWatched") or 0)
            subs = int(d.get("requiredSubs") or 0)
            if req <= 0 or subs > 0:
                continue
            s = d.get("self") or {}
            d_id = str(d.get("id") or "").lower()
            drop_inst_id = str(s.get("dropInstanceID") or s.get("dropInstanceId") or "")
            if s.get("isClaimed", False) or (d_id and d_id in claimed_ids):
                continue
            if drop_inst_id and drop_inst_id in self.failed_claim_ids:
                continue
            watched = int(s.get("currentMinutesWatched") or 0)
            if watched < req:
                return True
        return False

    def get_eligible_campaigns(self) -> List[dict]:
        """
        Return ordered list of eligible campaigns based on the user's mining queue and mode.
        Automatically pops completed games from the front of the queue so mining swaps immediately!
        """
        config = load_mining_config()
        mode = config.get("mode", "default")
        queue_games = [g.strip() for g in config.get("queue", []) if g.strip()]
        if not queue_games and self.priority_games and mode != "selective":
            queue_games = list(self.priority_games)

        now = time.time()
        force_refresh = (now - self.last_campaign_refresh >= 3600)
        campaigns = self.client.get_all_active_campaigns(
            priority_games=[g.lower() for g in queue_games],
            force_refresh=force_refresh,
            include_expired_inventory=False,
            inv_override=self.last_inventory if self.last_inventory else None,
        )
        if force_refresh:
            self.last_campaign_refresh = now
            logger.info("[MINER] Refreshed active drop campaigns roster (Found %d campaigns)", len(campaigns))

        if not campaigns:
            logger.info("No active drop campaigns found on Twitch right now.")
            return []

        eligible = [c for c in campaigns if self._campaign_has_unclaimed_watchable_drops(c)]

        # Auto-prune any queued games that have completed all watch-time drops (or have no active watchable campaigns)
        if queue_games and campaigns:
            for qg in list(queue_games):
                qg_low = qg.lower()
                matching_eligible = [
                    c for c in eligible
                    if qg_low == extract_game_name(c, "").lower()
                    or qg_low in extract_game_name(c, "").lower()
                    or qg_low in (c.get("name") or "").lower()
                ]
                if not matching_eligible:
                    # Check if this game exists in campaigns at all (meaning it finished)
                    was_in_all = any(
                        qg_low == extract_game_name(c, "").lower()
                        or qg_low in extract_game_name(c, "").lower()
                        or qg_low in (c.get("name") or "").lower()
                        for c in campaigns
                    )
                    if was_in_all:
                        logger.info("[QUEUE] '%s' has completed all watchable drops! Removing from queue and advancing to next game.", qg)
                        remove_from_mining_queue(qg)
                        queue_games = [g for g in queue_games if g.lower() != qg_low]

        if not eligible:
            logger.info("All watch-time drops in currently active campaigns have already been claimed! You are all caught up.")
            return []

        # Order eligible campaigns strictly by queue order first
        if mode == "selective":
            if not queue_games:
                logger.info("[SELECTIVE MODE] Queue is empty. Add games to the queue to start mining.")
                return []

            ordered_selective = []
            seen_ids = set()
            for qg in queue_games:
                qg_low = qg.lower()
                for camp in eligible:
                    cid = camp.get("id") or id(camp)
                    if cid in seen_ids:
                        continue
                    gname = extract_game_name(camp, "").lower()
                    cname = (camp.get("name") or "").lower()
                    if qg_low == gname or qg_low in gname or qg_low in cname:
                        ordered_selective.append(camp)
                        seen_ids.add(cid)

            if not ordered_selective:
                logger.info("[SELECTIVE MODE] No active watch-time drops match your queued games (%s) right now.", ", ".join(queue_games))
                return []
            return ordered_selective

        # 'default' mode: Mine queued games in exact queue order first, then auto-mine remaining eligible campaigns
        if queue_games:
            prioritized = []
            others = []
            seen_ids = set()
            for qg in queue_games:
                qg_low = qg.lower()
                for camp in eligible:
                    cid = camp.get("id") or id(camp)
                    if cid in seen_ids:
                        continue
                    gname = extract_game_name(camp, "").lower()
                    cname = (camp.get("name") or "").lower()
                    if qg_low == gname or qg_low in gname or qg_low in cname:
                        prioritized.append(camp)
                        seen_ids.add(cid)
            for camp in eligible:
                cid = camp.get("id") or id(camp)
                if cid not in seen_ids:
                    others.append(camp)
                    seen_ids.add(cid)
            return prioritized + others

        return eligible

    def select_active_campaign(self) -> Optional[dict]:
        """Select the highest priority single campaign that still has unclaimed watch-time drops."""
        eligible = self.get_eligible_campaigns()
        return eligible[0] if eligible else None

    def _handle_mid_cycle_swap(self, old_channel_name: str, old_game_name: str):
        """Log swap and only set switching=True if server.py hasn't already resolved the new active stream."""
        new_cfg = load_mining_config()
        next_target = new_cfg.get("selected_game") or "Auto-Queue"
        logger.info(
            "[MINER] Active mine swapped (mode=%s, next=%s, queue=%d)! Instantly leaving %s (%s) and switching to new stream.",
            new_cfg.get("mode"), next_target, len(new_cfg.get("queue", [])), old_channel_name, old_game_name
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
            "queue": new_cfg.get("queue", []),
            "mode": new_cfg.get("mode", "default"),
            "message": f"Left {old_channel_name} ({old_game_name}). Switching to next target: {next_target}...",
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })

    def run_cycle(self):
        """Single iteration of the mining loop - watches ONLY 1 stream at a time and swaps on completion or queue change."""
        cycle_sig = self._get_config_signature()
        config_just_swapped = (self.last_config_sig is not None and self.last_config_sig != cycle_sig)
        self.last_config_sig = cycle_sig

        config = load_mining_config()
        queue_cfg = config.get("queue", [])
        selected_game_cfg = queue_cfg[0] if queue_cfg else ""
        mode_cfg = config.get("mode", "default")

        # 1. Always refresh inventory & claim pending drops (unless user just clicked switch in last second)
        if not config_just_swapped:
            self.claim_pending_drops()
            if self._has_config_changed(cycle_sig):
                logger.info("[MINER] Queue/selection changed during claim check. Switching immediately.")
                return

            if self.client.is_user_actively_using_twitch():
                logger.info("[MINER] User is currently using Twitch. Pausing miner to ensure ONLY 1 viewing of Twitch.")
                save_miner_status({
                    "active": False,
                    "paused": True,
                    "user_active": True,
                    "selected_game": selected_game_cfg,
                    "queue": queue_cfg,
                    "mode": mode_cfg,
                    "message": "Miner paused: You are currently using Twitch. Mining will resume automatically when you finish.",
                    "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                })
                self._interruptible_sleep(60, cycle_sig)
                return

        # 2. Pick eligible campaign(s) in queue order and find 1 live stream
        eligible_campaigns = self.get_eligible_campaigns()
        # Re-read config in case get_eligible_campaigns auto-pruned a completed game from the queue
        config = load_mining_config()
        queue_cfg = config.get("queue", [])
        selected_game_cfg = queue_cfg[0] if queue_cfg else ""
        cycle_sig = self._get_config_signature()
        self.last_config_sig = cycle_sig

        if not eligible_campaigns:
            msg = (
                f"Queued games ({', '.join(queue_cfg)}) have no active watch-time drops right now."
                if (mode_cfg == "selective" and queue_cfg)
                else (
                    "Selective mode active: Add games to your mining queue below to start mining."
                    if mode_cfg == "selective"
                    else "No eligible campaigns to mine at this moment."
                )
            )
            save_miner_status({
                "active": False,
                "selected_game": selected_game_cfg,
                "queue": queue_cfg,
                "mode": mode_cfg,
                "message": msg,
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            })
            logger.info("[MINER] %s Waiting (will wake immediately if queue changes)...", msg)
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

        excluded_channels = self._get_excluded_channels()

        # Try eligible campaigns in exact priority/queue order (if #1 in queue has no live stream online, falls back to #2 in queue!)
        for candidate_camp in eligible_campaigns:
            if self._has_config_changed(cycle_sig):
                logger.info("[MINER] Queue changed while searching for stream. Switching immediately...")
                return
            cand_game = extract_game_name(candidate_camp, "Unknown")
            cand_name = candidate_camp.get("name", cand_game)

            # Adopt pre-resolved stream from server.py if fresh (<60s), matches cand_game, and is not blacklisted
            pre_login = str(pre_switched_stat.get("channel_login") or "").lower().strip()
            if (
                pre_switched_stat.get("active")
                and pre_login
                and pre_login not in excluded_channels
                and str(pre_switched_stat.get("game") or "").lower() == cand_game.lower()
                and (time.time() - float(pre_switched_stat.get("switched_at") or 0)) < 60
            ):
                chosen_campaign = candidate_camp
                channel = {
                    "channel_id": str(pre_switched_stat.get("channel_id") or ""),
                    "channel_login": pre_switched_stat["channel_login"],
                    "channel_name": pre_switched_stat.get("channel_name") or pre_switched_stat["channel_login"],
                    "stream_id": str(pre_switched_stat.get("stream_id") or ""),
                    "stream_title": str(pre_switched_stat.get("stream_title") or ""),
                    "viewers": int(pre_switched_stat.get("viewers") or 0),
                    "game_name": cand_game,
                    "game_id": str(pre_switched_stat.get("game_id") or ""),
                    "verified_drops": bool(pre_switched_stat.get("verified_drops", True)),
                    "has_drops_in_title": bool(pre_switched_stat.get("has_drops_in_title", True)),
                }
                game_name = cand_game
                camp_name = cand_name
                logger.info("[MINER] Adopted instant-switched stream %s (@%s) for '%s'", channel["channel_name"], channel["channel_login"], game_name)
                break

            logger.info("[MINER] Checking live streams with drops enabled for '%s' (%s)...", cand_game, cand_name)
            found_ch = self.client.find_eligible_channel(
                cand_game,
                fast_first=True,
                excluded_channels=excluded_channels,
            )
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
            logger.warning("[MINER] No live streams with drops enabled for '%s'. Retrying in 3 minutes (or immediately on queue change)...", game_name)
            save_miner_status({
                "active": False,
                "game": game_name,
                "campaign": camp_name,
                "selected_game": selected_game_cfg or game_name,
                "queue": queue_cfg,
                "mode": mode_cfg,
                "message": f"No live streams currently online with drops enabled for {game_name}.",
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            })
            self._interruptible_sleep(180, cycle_sig)
            return

        stream_url = f"https://www.twitch.tv/{channel['channel_login']}"
        logger.info(
            "[MINER] Watching 1 stream: %s (@%s) for '%s' with %d viewers | Queue size: %d | URL: %s",
            channel["channel_name"], channel["channel_login"], game_name, channel["viewers"],
            len(queue_cfg), stream_url
        )

        # Immediately write active stream status BEFORE fetching playback info or sending heartbeat so UI updates with 0 lag
        initial_drop_info = self._get_live_drop_progress(game_name, camp_name, fallback_camp=chosen_campaign)
        baseline_watched = self._get_campaign_total_watched(
            game_name, camp_name, fallback_camp=chosen_campaign, live_drop_info=initial_drop_info
        )
        last_progress_minute = 0
        progress_verified = False

        immediate_status = {
            "active": True,
            "switching": False,
            "paused": False,
            "user_active": False,
            "game": game_name,
            "selected_game": selected_game_cfg or game_name,
            "queue": queue_cfg,
            "mode": mode_cfg,
            "campaign": camp_name,
            "channel_id": channel.get("channel_id", ""),
            "channel_name": channel["channel_name"],
            "channel_login": channel["channel_login"],
            "stream_id": channel.get("stream_id", ""),
            "stream_title": channel.get("stream_title", ""),
            "verified_drops": bool(channel.get("verified_drops", True)),
            "has_drops_in_title": bool(channel.get("has_drops_in_title", False)),
            "progress_verified": False,
            "minutes_without_progress": 0,
            "progress_check_minutes": self.progress_check_minutes,
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
        # Strictly watches ONLY this single stream, verifies progress every 2-5 mins, and immediately swaps when completed or stalled!
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
                    "queue": queue_cfg,
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
            claimed_now = self.claim_pending_drops()
            live_drop_info = self._get_live_drop_progress(game_name, camp_name, fallback_camp=chosen_campaign)

            # Check if all drops for this campaign/game are now completed!
            if not live_drop_info or not live_drop_info.get("current_drop_required"):
                logger.info(
                    "[MINER] All watch-time drops for '%s' (%s) are now completed! Advancing queue and swapping immediately...",
                    game_name, camp_name
                )
                updated_cfg = remove_from_mining_queue(game_name)
                next_q = updated_cfg.get("queue", [])
                next_game = next_q[0] if next_q else "Auto-Mine"
                self.last_config_sig = self._get_config_signature()
                save_miner_status({
                    "active": False,
                    "switching": True,
                    "game": next_q[0] if next_q else "",
                    "selected_game": next_q[0] if next_q else "",
                    "queue": next_q,
                    "mode": updated_cfg.get("mode", mode_cfg),
                    "message": f"Completed '{game_name}'! Swapping immediately to next in queue: {next_game}...",
                    "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                })
                return

            # 2-5 Minute Progress Watchdog: verify drop progress is actually increasing on this channel
            current_watched = self._get_campaign_total_watched(
                game_name, camp_name, fallback_camp=chosen_campaign, live_drop_info=live_drop_info
            )
            if current_watched > baseline_watched or claimed_now > 0:
                gained = max(1, current_watched - baseline_watched)
                baseline_watched = current_watched
                last_progress_minute = minute + 1
                progress_verified = True
                logger.info(
                    "[MINER] Drop progress verified on %s (@%s) for '%s' (+%dm watched).",
                    channel["channel_name"], channel["channel_login"], game_name, gained
                )
            else:
                mins_stalled = (minute + 1) - last_progress_minute
                if mins_stalled >= self.progress_check_minutes:
                    logger.warning(
                        "[MINER] No drop progress increase after %d minutes on %s (@%s) for '%s' (watched remained at %dm). "
                        "Marking channel unproductive and switching to another Drops channel immediately!",
                        mins_stalled, channel["channel_name"], channel["channel_login"], game_name, current_watched
                    )
                    self._mark_channel_unproductive(channel["channel_login"])
                    save_miner_status({
                        "active": False,
                        "switching": True,
                        "game": game_name,
                        "selected_game": selected_game_cfg or game_name,
                        "queue": queue_cfg,
                        "mode": mode_cfg,
                        "message": (
                            f"No drop progress increase after {mins_stalled} mins on {channel['channel_name']} (@{channel['channel_login']}). "
                            f"Switching immediately to a verified Drops channel..."
                        ),
                        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    })
                    return

            # Re-verify config hasn't changed while claiming/fetching inventory
            if self._has_config_changed(cycle_sig):
                self._handle_mid_cycle_swap(channel["channel_name"], game_name)
                return

            mins_without_prog = (minute + 1) - last_progress_minute
            current_status = {
                "active": True,
                "switching": False,
                "paused": False,
                "user_active": False,
                "game": game_name,
                "selected_game": selected_game_cfg or game_name,
                "queue": queue_cfg,
                "mode": mode_cfg,
                "campaign": camp_name,
                "channel_id": channel.get("channel_id", ""),
                "channel_name": channel["channel_name"],
                "channel_login": channel["channel_login"],
                "stream_id": broadcast_id,
                "stream_title": channel.get("stream_title", ""),
                "verified_drops": bool(channel.get("verified_drops", True)),
                "has_drops_in_title": bool(channel.get("has_drops_in_title", False)),
                "progress_verified": progress_verified,
                "minutes_without_progress": mins_without_prog,
                "progress_check_minutes": self.progress_check_minutes,
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

            # Interruptible ~60s sleep: wakes within 0.2s if user changes queue, selected game, or mode!
            if self._interruptible_sleep(random.uniform(58.0, 62.0), cycle_sig):
                self._handle_mid_cycle_swap(channel["channel_name"], game_name)
                return

        # Reset active status when switching streams at end of 15-min cycle
        save_miner_status({"active": False, "queue": queue_cfg, "message": "Refreshing stream & checking queue."})

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
            "Twitch session valid! Mining as: %s (ID: %s) [Single viewing & Queue enforced]",
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
