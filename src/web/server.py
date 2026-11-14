"""
Ultra-lightweight Web Dashboard Server for Auto Loot Claimer.
Built using Python Standard Library ONLY (zero external pip packages).
Features secure session authentication, login page, and in-browser account management.
"""

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

# Path resolution
APP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, APP_DIR)

from src.free_games.preview_promotions import fetch_epic_freebies
from src.twitch_drops.twitch_api import TwitchClient
from src.auth.twitch_auth import (
    clear_token as clear_stored_token,
    load_token as load_stored_token,
    poll_device_login,
    resolve_token,
    start_device_login,
)
from src.twitch_drops.miner import DropsMiner
from src.twitch_drops.process_guard import get_running_bot_processes, kill_pid, PID_FILE, is_pid_alive

# In-memory session store: token -> expiration timestamp
ACTIVE_SESSIONS = {}


def load_env_file(path=None):
    if not path:
        path = os.path.join(APP_DIR, ".env")
    env = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip("'\"")
    return env


def save_env_file(updates, path=None):
    """Safely updates or appends key-value pairs in the .env file."""
    if not path:
        path = os.path.join(APP_DIR, ".env")
    lines = []
    existing_keys = set()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()

    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k, _ = stripped.split("=", 1)
            k = k.strip()
            if k in updates:
                val = updates[k]
                new_lines.append(f"{k}={val}\n")
                existing_keys.add(k)
                continue
        new_lines.append(line)

    for k, v in updates.items():
        if k not in existing_keys:
            new_lines.append(f"{k}={v}\n")

    with open(path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)


def parse_cookies(cookie_header):
    if not cookie_header:
        return {}
    cookies = {}
    for part in cookie_header.split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            cookies[k.strip()] = v.strip()
    return cookies


def is_authenticated(headers):
    cookies = parse_cookies(headers.get("Cookie", ""))
    session_id = cookies.get("session_id")
    if not session_id:
        return False
    expiry = ACTIVE_SESSIONS.get(session_id)
    if not expiry or expiry < time.time():
        if session_id in ACTIVE_SESSIONS:
            del ACTIVE_SESSIONS[session_id]
        return False
    return True


def get_claimed_store_titles():
    """
    Returns a dictionary of claimed titles per store:
    {"epic": [...], "amazon": [...], "gog": [...]}
    Reads persistent data/claimed_games.json and scans log files.
    """
    data_file = os.path.join(APP_DIR, "data", "claimed_games.json")
    results = {"epic": set(), "amazon": set(), "gog": set()}

    # 1. Read persistent JSON
    if os.path.exists(data_file):
        try:
            with open(data_file, "r", encoding="utf-8") as f:
                stored = json.load(f)
                if isinstance(stored, dict):
                    for k, titles in stored.items():
                        if k in results and isinstance(titles, list):
                            for t in titles:
                                if t and isinstance(t, str):
                                    results[k].add(t.strip())
        except Exception:
            pass

    # 2. Scan log files and vendor data for claimed keywords
    search_paths = [
        "/var/log/free-games.log",
        os.path.join(APP_DIR, "data", "fgc"),
        os.path.join(APP_DIR, "vendor", "free-games-claimer", "data"),
    ]
    for sp in search_paths:
        if os.path.isfile(sp):
            targets = [sp]
        elif os.path.isdir(sp):
            targets = [os.path.join(root, f) for root, _, fs in os.walk(sp) for f in fs if f.endswith((".log", ".json", ".txt"))]
        else:
            continue

        for tf in targets:
            try:
                with open(tf, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        line_low = line.lower()
                        if any(kw in line_low for kw in ["claimed", "already claimed", "already in library", "successfully claimed"]):
                            cleaned = line.strip()
                            for prefix in ["claimed:", "already claimed:", "successfully claimed:"]:
                                if prefix in line_low:
                                    idx = line_low.find(prefix) + len(prefix)
                                    cleaned = line[idx:].strip(" -:[]*\"'")
                            if cleaned and len(cleaned) < 80:
                                if "prime" in tf.lower() or "amazon" in tf.lower():
                                    results["amazon"].add(cleaned)
                                elif "gog" in tf.lower():
                                    results["gog"].add(cleaned)
                                else:
                                    results["epic"].add(cleaned)
            except Exception:
                pass

    return {k: sorted(list(v)) for k, v in results.items()}


def save_claimed_store_title(store: str, title: str, is_claimed: bool = True):
    """Saves or removes a claimed title in data/claimed_games.json."""
    data_dir = os.path.join(APP_DIR, "data")
    os.makedirs(data_dir, exist_ok=True)
    data_file = os.path.join(data_dir, "claimed_games.json")
    
    current = {"epic": [], "amazon": [], "gog": []}
    if os.path.exists(data_file):
        try:
            with open(data_file, "r", encoding="utf-8") as f:
                current = json.load(f)
        except Exception:
            pass

    store_key = store.lower().strip()
    if store_key not in current:
        current[store_key] = []

    title_clean = title.strip()
    if is_claimed:
        if title_clean and title_clean not in current[store_key]:
            current[store_key].append(title_clean)
    else:
        current[store_key] = [t for t in current[store_key] if t.lower() != title_clean.lower()]

    with open(data_file, "w", encoding="utf-8") as f:
        json.dump(current, f, indent=2)


def get_mining_config():
    config_file = os.path.join(APP_DIR, "data", "mining_config.json")
    if os.path.exists(config_file):
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    raw_q = data.get("queue")
                    if not isinstance(raw_q, list):
                        raw_q = data.get("activated_games", [])
                    seen = set()
                    clean_queue = []
                    for g in raw_q:
                        if isinstance(g, str) and g.strip():
                            gs = g.strip()
                            if gs.lower() not in seen:
                                seen.add(gs.lower())
                                clean_queue.append(gs)
                    raw_excl = data.get("excluded_channels") or []
                    clean_excl = [
                        str(ch).lower().strip()
                        for ch in (raw_excl if isinstance(raw_excl, list) else [])
                        if ch and str(ch).strip()
                    ]
                    pref_ch = str(data.get("preferred_channel") or "").lower().strip()
                    return {
                        "mode": data.get("mode", "default"),
                        "queue": clean_queue,
                        "activated_games": clean_queue,
                        "selected_game": clean_queue[0] if clean_queue else "",
                        "excluded_channels": clean_excl,
                        "preferred_channel": pref_ch,
                        "updated_at": data.get("updated_at", 0),
                    }
        except Exception:
            pass
    env = load_env_file()
    seen = set()
    default_priority = []
    for g in env.get("TWITCH_PRIORITY_GAMES", "").split(","):
        gs = g.strip()
        if gs and gs.lower() not in seen:
            seen.add(gs.lower())
            default_priority.append(gs)
    return {
        "mode": "default",
        "queue": default_priority,
        "activated_games": default_priority,
        "selected_game": default_priority[0] if default_priority else "",
        "excluded_channels": [],
        "preferred_channel": "",
        "updated_at": 0,
    }


_LAST_DAEMON_SPAWN_TS = 0.0


def is_miner_daemon_alive() -> bool:
    """Check if the background DropsMiner daemon process is currently alive."""
    for pf in [PID_FILE, "/run/twitch-drops.pid", "/var/run/twitch-drops.pid"]:
        if pf and os.path.exists(pf):
            try:
                with open(pf, "r", encoding="utf-8") as f:
                    raw_pid = f.read().strip()
                if raw_pid.isdigit() and is_pid_alive(int(raw_pid)):
                    return True
            except Exception:
                pass
    try:
        bots = get_running_bot_processes()
        if bots:
            return True
    except Exception:
        pass
    return False


def ensure_miner_daemon_running(token: str = "") -> bool:
    """Ensure the background DropsMiner daemon is running when a Twitch token is configured."""
    global _LAST_DAEMON_SPAWN_TS
    if is_miner_daemon_alive():
        return True
    if not token or "unittest" in sys.modules:
        return False
    now = time.time()
    if (now - _LAST_DAEMON_SPAWN_TS) < 15.0:
        return False
    _LAST_DAEMON_SPAWN_TS = now
    try:
        log_path = (
            "/var/log/twitch-drops.log"
            if (os.name != "nt" and os.path.isdir("/var/log"))
            else os.path.join(APP_DIR, "data", "miner.log")
        )
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        log_f = open(log_path, "a", encoding="utf-8")
        subprocess.Popen(
            [sys.executable, "-u", "-m", "src.twitch_drops.main", "--daemon"],
            cwd=APP_DIR,
            stdout=log_f,
            stderr=log_f,
            start_new_session=(os.name != "nt"),
        )
        return True
    except Exception:
        return False


def _send_async_initial_heartbeat(token: str, channel: dict, game_name: str):
    """Send immediate first minute-watched heartbeat in background upon stream swap."""
    try:
        client = TwitchClient(token)
        if not client.validate_session():
            return
        playback_info = client.get_playback_stream_info(channel["channel_login"])
        broadcast_id = playback_info.get("broadcast_id") or channel.get("stream_id", "")
        sub_playlist_url = playback_info.get("sub_playlist_url")
        client.send_minute_watched_heartbeat(
            channel_id=channel["channel_id"],
            stream_id=broadcast_id,
            channel_login=channel["channel_login"],
            game_name=game_name,
            game_id=channel.get("game_id"),
            sub_playlist_url=sub_playlist_url,
        )
    except Exception:
        pass


def save_mining_config(mode: str, activated_games: list, resolve_stream: bool = True):
    data_dir = os.path.join(APP_DIR, "data")
    os.makedirs(data_dir, exist_ok=True)
    config_file = os.path.join(data_dir, "mining_config.json")
    if mode in ("stopped", "pause", "stop"):
        clean_mode = "stopped"
    elif mode == "selective":
        clean_mode = "selective"
    else:
        clean_mode = "default"
    # Preserve ordered queue while deduplicating case-insensitively (strictly 1 mined at a time: queue[0])
    seen = set()
    clean_queue = []
    for g in (activated_games or []):
        if isinstance(g, str) and g.strip():
            gs = g.strip()
            if gs.lower() not in seen:
                seen.add(gs.lower())
                clean_queue.append(gs)

    selected_game = clean_queue[0] if clean_queue else ""
    now_ts = time.time()

    cfg = {
        "mode": clean_mode,
        "queue": clean_queue,
        "activated_games": clean_queue,
        "selected_game": selected_game,
        "updated_at": now_ts,
    }
    with open(config_file, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    save_env_file({"TWITCH_PRIORITY_GAMES": ", ".join(clean_queue)})

    status_file = os.path.join(data_dir, "miner_status.json")
    prev_status = {}
    if os.path.exists(status_file):
        try:
            with open(status_file, "r", encoding="utf-8") as f:
                prev_status = json.load(f) or {}
        except Exception:
            prev_status = {}

    prev_game = str(prev_status.get("game") or "").strip()
    prev_channel = str(prev_status.get("channel_name") or prev_status.get("channel_login") or "").strip()
    was_active = bool(prev_status.get("active"))

    # If the currently watched game is still #1 in the queue and resolve_stream is False, just update queue metadata
    if not resolve_stream and was_active and selected_game and prev_game.lower() == selected_game.lower():
        prev_status["queue"] = clean_queue
        prev_status["selected_game"] = selected_game
        prev_status["mode"] = clean_mode
        try:
            with open(status_file, "w", encoding="utf-8") as f:
                json.dump(prev_status, f, indent=2)
        except Exception:
            pass
        cfg["miner_status"] = prev_status
        return cfg

    env = load_env_file()
    token = resolve_token(env.get("TWITCH_AUTH_TOKEN", "").strip())
    if clean_mode != "stopped":
        ensure_miner_daemon_running(token)
    target_game = selected_game if clean_mode != "stopped" else ""

    # If in default mode with an empty queue, pick the first eligible campaign from cache
    if not target_game and clean_mode == "default":
        cache_file = os.path.join(data_dir, "twitch_campaigns_cache.json")
        if os.path.exists(cache_file):
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    cached = json.load(f) or {}
                for c in cached.get("campaigns", []):
                    gObj = c.get("game") or {}
                    gName = (gObj.get("displayName") or gObj.get("name") or "") if isinstance(gObj, dict) else str(gObj or "")
                    gName = gName.strip()
                    if gName and gName.lower() != prev_game.lower():
                        target_game = gName
                        break
            except Exception:
                pass

    new_status = None
    if clean_mode == "stopped":
        new_status = {
            "active": False,
            "switching": False,
            "paused": True,
            "user_active": False,
            "game": "",
            "selected_game": "",
            "channel_name": "",
            "channel_login": "",
            "channel_id": "",
            "stream_id": "",
            "stream_url": "",
            "viewers": 0,
            "queue": clean_queue,
            "mode": "stopped",
            "message": "Miner is completely stopped. No streams will be watched.",
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    # Try resolving a live channel for queue items in order
    candidates_to_try = list(clean_queue) if clean_queue else ([target_game] if target_game else [])
    if candidates_to_try and token:
        try:
            client = TwitchClient(token)
            for cand in candidates_to_try:
                channel = client.find_eligible_channel(cand, fast_first=True)
                if channel and channel.get("channel_login"):
                    stream_url = f"https://www.twitch.tv/{channel['channel_login']}"
                    new_status = {
                        "active": True,
                        "switching": False,
                        "paused": False,
                        "user_active": False,
                        "game": cand,
                        "selected_game": selected_game or cand,
                        "queue": clean_queue,
                        "mode": clean_mode,
                        "campaign": cand,
                        "channel_id": channel.get("channel_id", ""),
                        "channel_name": channel["channel_name"],
                        "channel_login": channel["channel_login"],
                        "stream_id": channel.get("stream_id", ""),
                        "stream_title": channel.get("stream_title", ""),
                        "verified_drops": bool(channel.get("verified_drops", True)),
                        "has_drops_in_title": bool(channel.get("has_drops_in_title", False)),
                        "progress_verified": False,
                        "minutes_without_progress": 0,
                        "progress_check_minutes": 5,
                        "game_id": channel.get("game_id", ""),
                        "stream_url": stream_url,
                        "viewers": channel.get("viewers", 0),
                        "minutes_watched": 1,
                        "user_login": prev_status.get("user_login") or "",
                        "user_id": prev_status.get("user_id") or "",
                        "switched_at": now_ts,
                        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    }
                    threading.Thread(
                        target=_send_async_initial_heartbeat,
                        args=(token, channel, cand),
                        daemon=True,
                    ).start()
                    break
            if not new_status and target_game:
                new_status = {
                    "active": False,
                    "switching": False,
                    "paused": False,
                    "user_active": False,
                    "game": target_game,
                    "selected_game": selected_game or target_game,
                    "queue": clean_queue,
                    "mode": clean_mode,
                    "message": f"No live streams currently online with drops enabled for {target_game}.",
                    "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
        except Exception:
            pass

    if not new_status:
        if selected_game:
            cancel_prefix = f"Cancelled {prev_channel} ({prev_game}). " if (was_active and prev_game.lower() != selected_game.lower()) else ""
            msg = f"{cancel_prefix}Mining '#1 {selected_game}' in queue ({len(clean_queue)} queued). Connecting to live stream..."
            is_switching = True
        elif clean_mode == "selective":
            cancel_prefix = f"Cancelled {prev_channel} ({prev_game}). " if was_active else ""
            msg = f"{cancel_prefix}Selective mode: Queue is empty. Add games to the queue below to start mining."
            is_switching = False
        else:
            cancel_prefix = f"Cancelled {prev_channel} ({prev_game}). " if was_active else ""
            msg = f"{cancel_prefix}Default mode: Auto-selecting 1 eligible drop stream to mine..."
            is_switching = True

        new_status = {
            "active": False,
            "switching": is_switching,
            "paused": False,
            "user_active": False,
            "game": selected_game,
            "selected_game": selected_game,
            "channel_name": "",
            "channel_login": "",
            "channel_id": "",
            "stream_id": "",
            "stream_url": "",
            "viewers": 0,
            "queue": clean_queue,
            "mode": clean_mode,
            "progress_verified": False,
            "minutes_without_progress": 0,
            "progress_check_minutes": 5,
            "message": msg,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    try:
        with open(status_file, "w", encoding="utf-8") as f:
            json.dump(new_status, f, indent=2)
    except Exception:
        pass

    cfg["miner_status"] = new_status
    return cfg


def get_miner_status():
    status_file = os.path.join(APP_DIR, "data", "miner_status.json")
    if not os.path.exists(status_file) and os.path.exists(os.path.join(APP_DIR, "miner_status.json")):
        status_file = os.path.join(APP_DIR, "miner_status.json")
    if os.path.exists(status_file):
        try:
            with open(status_file, "r", encoding="utf-8") as f:
                stat = json.load(f) or {}
                if stat.get("game") and not stat.get("game_cover_url"):
                    try:
                        from src.twitch_drops.twitch_api import resolve_game_logo
                        stat["game_cover_url"] = resolve_game_logo(stat["game"])
                    except Exception:
                        pass
                return stat
        except Exception:
            pass
    return {"active": False}


def restart_mining(target_game: Optional[str] = None) -> Dict[str, Any]:
    """
    Fully restart mining for the selected game.
    Clears stall counters, forces stream re-resolution, and ensures the miner daemon is actively running.
    """
    env = load_env_file()
    token = resolve_token(env.get("TWITCH_AUTH_TOKEN", "").strip())
    current_cfg = get_mining_config()
    current_stat = get_miner_status()
    current_queue = list(current_cfg.get("queue") or current_cfg.get("activated_games") or [])

    req_game = (target_game or "").strip()
    selected_game = req_game or (current_queue[0] if current_queue else str(current_stat.get("game") or "").strip())

    mode = current_cfg.get("mode", "default")
    if mode == "stopped":
        mode = "selective" if current_queue else "default"

    if selected_game:
        new_queue = [selected_game] + [g for g in current_queue if g.lower() != selected_game.lower()]
    else:
        new_queue = current_queue

    status_file = os.path.join(APP_DIR, "data", "miner_status.json")
    if not os.path.isdir(os.path.join(APP_DIR, "data")) and os.path.exists(os.path.join(APP_DIR, "miner_status.json")):
        status_file = os.path.join(APP_DIR, "miner_status.json")
    os.makedirs(os.path.dirname(status_file), exist_ok=True)
    try:
        with open(status_file, "w", encoding="utf-8") as f:
            json.dump({
                "active": False,
                "switching": True,
                "game": selected_game,
                "selected_game": selected_game,
                "channel_name": "",
                "channel_login": "",
                "channel_id": "",
                "stream_id": "",
                "stream_url": "",
                "viewers": 0,
                "queue": new_queue,
                "mode": mode,
                "message": f"Manual restart triggered for '{selected_game or 'active game'}'. Connecting to fresh stream and resetting watchdog...",
                "progress_verified": False,
                "minutes_without_progress": 0,
                "progress_check_minutes": 5,
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }, f, indent=2)
    except Exception:
        pass

    cfg = save_mining_config(mode=mode, activated_games=new_queue, resolve_stream=True)
    if token:
        ensure_miner_daemon_running(token)
    mstat = get_miner_status()
    game_lbl = selected_game or "selected game"
    return {
        "success": True,
        "game": selected_game,
        "mode": mode,
        "queue": new_queue,
        "mining_config": cfg,
        "miner_status": mstat,
        "message": f"Successfully restarted mining for '{game_lbl}'! Reconnecting to live stream...",
    }


def switch_watched_channel(
    target_game: Optional[str] = None,
    channel_login: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Switch the currently watched Twitch channel for the active/selected game.
    - If channel_login is provided, switches directly to that streamer.
    - If channel_login is omitted, skips the currently watched channel and rotates to the next
      live Drops-enabled channel in Twitch's directory order (wrapping around if all channels were visited).
    """
    env = load_env_file()
    token = resolve_token(env.get("TWITCH_AUTH_TOKEN", "").strip())
    current_cfg = get_mining_config()
    current_stat = get_miner_status()
    current_queue = list(current_cfg.get("queue") or current_cfg.get("activated_games") or [])

    req_game = (target_game or "").strip()
    selected_game = (
        req_game
        or str(current_stat.get("game") or "").strip()
        or (current_queue[0] if current_queue else "")
        or str(current_stat.get("selected_game") or "").strip()
    )
    mode = current_cfg.get("mode", "default")
    if mode == "stopped":
        mode = "selective" if current_queue else "default"

    if selected_game and current_queue:
        new_queue = [selected_game] + [g for g in current_queue if g.lower() != selected_game.lower()]
    else:
        new_queue = current_queue

    prev_login = str(current_stat.get("channel_login") or "").lower().strip()
    prev_name = str(current_stat.get("channel_name") or prev_login or "").strip()
    excluded_list: List[str] = [
        str(ch).lower().strip()
        for ch in (current_cfg.get("excluded_channels") or [])
        if ch and str(ch).strip()
    ]

    requested_login = str(channel_login or "").strip()
    if "twitch.tv/" in requested_login.lower():
        requested_login = requested_login.lower().split("twitch.tv/", 1)[1]
    requested_login = requested_login.split("?")[0].split("#")[0].strip().strip("/").lstrip("@").lower()

    client = TwitchClient(token) if token else TwitchClient("")
    chosen_channel: Optional[Dict[str, Any]] = None
    preferred_channel = ""

    if requested_login:
        preferred_channel = requested_login
        excluded_list = [ch for ch in excluded_list if ch != requested_login]
        chosen_channel = client.resolve_specific_channel(requested_login, selected_game)
        if not chosen_channel and selected_game:
            # Check directory list in case resolve_specific_channel couldn't find stream metadata
            for ch in client.list_eligible_channels(selected_game, limit=20):
                if str(ch.get("channel_login") or "").lower() == requested_login:
                    chosen_channel = ch
                    break
        if not chosen_channel:
            return {
                "success": False,
                "error": f"Channel '@{requested_login}' is not currently live on Twitch or could not be found.",
                "game": selected_game,
            }
        if not selected_game and chosen_channel.get("game_name"):
            selected_game = chosen_channel["game_name"]
    else:
        if prev_login and prev_login not in excluded_list:
            excluded_list.append(prev_login)
        if selected_game:
            chosen_channel = client.find_eligible_channel(
                selected_game,
                fast_first=True,
                excluded_channels=set(excluded_list),
            )
            if not chosen_channel and excluded_list:
                # Wrapped through all available channels: reset exclusions (still trying to skip prev_login first)
                only_prev = {prev_login} if prev_login else set()
                chosen_channel = client.find_eligible_channel(
                    selected_game,
                    fast_first=True,
                    excluded_channels=only_prev,
                )
                if chosen_channel:
                    excluded_list = [prev_login] if prev_login else []
                else:
                    # Only 1 live channel exists for this game
                    excluded_list = []
                    chosen_channel = client.find_eligible_channel(
                        selected_game,
                        fast_first=True,
                        excluded_channels=None,
                    )

    now_ts = time.time()
    data_dir = os.path.join(APP_DIR, "data")
    os.makedirs(data_dir, exist_ok=True)
    config_file = os.path.join(data_dir, "mining_config.json")
    status_file = os.path.join(data_dir, "miner_status.json")

    cfg = {
        "mode": mode,
        "queue": new_queue,
        "activated_games": new_queue,
        "selected_game": new_queue[0] if new_queue else selected_game,
        "excluded_channels": excluded_list,
        "preferred_channel": preferred_channel,
        "updated_at": now_ts,
    }
    try:
        with open(config_file, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass

    if not chosen_channel or not chosen_channel.get("channel_login"):
        return {
            "success": False,
            "error": f"No alternative live Drops-enabled channels found for '{selected_game or 'active game'}' right now.",
            "game": selected_game,
            "mining_config": cfg,
            "miner_status": current_stat,
        }

    stream_url = f"https://www.twitch.tv/{chosen_channel['channel_login']}"
    new_status = {
        "active": True,
        "switching": False,
        "paused": False,
        "user_active": False,
        "game": selected_game or chosen_channel.get("game_name", ""),
        "game_cover_url": current_stat.get("game_cover_url") or "",
        "selected_game": (new_queue[0] if new_queue else selected_game) or chosen_channel.get("game_name", ""),
        "queue": new_queue,
        "mode": mode,
        "campaign": current_stat.get("campaign") or selected_game,
        "channel_id": str(chosen_channel.get("channel_id") or ""),
        "channel_name": chosen_channel.get("channel_name") or chosen_channel["channel_login"],
        "channel_login": chosen_channel["channel_login"],
        "stream_id": str(chosen_channel.get("stream_id") or ""),
        "stream_title": str(chosen_channel.get("stream_title") or ""),
        "verified_drops": bool(chosen_channel.get("verified_drops", True)),
        "has_drops_in_title": bool(chosen_channel.get("has_drops_in_title", False)),
        "progress_verified": False,
        "minutes_without_progress": 0,
        "progress_check_minutes": 5,
        "game_id": str(chosen_channel.get("game_id") or ""),
        "stream_url": stream_url,
        "viewers": int(chosen_channel.get("viewers") or 0),
        "minutes_watched": 1,
        "cycle_minutes": int(current_stat.get("cycle_minutes") or 60),
        "channel_rotation_minutes": int(current_stat.get("channel_rotation_minutes") or 60),
        "availability_status": "Stream & drops active",
        "user_login": current_stat.get("user_login") or "",
        "user_id": current_stat.get("user_id") or "",
        "switched_at": now_ts,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    for k in ("current_drop_name", "current_drop_watched", "current_drop_required", "current_drop_percent", "current_drop_remaining"):
        if k in current_stat:
            new_status[k] = current_stat[k]

    try:
        with open(status_file, "w", encoding="utf-8") as f:
            json.dump(new_status, f, indent=2)
    except Exception:
        pass

    if token:
        threading.Thread(
            target=_send_async_initial_heartbeat,
            args=(token, chosen_channel, selected_game or chosen_channel.get("game_name", "")),
            daemon=True,
        ).start()
        ensure_miner_daemon_running(token)

    new_ch_display = chosen_channel.get("channel_name") or chosen_channel["channel_login"]
    if prev_login and prev_login == str(chosen_channel["channel_login"]).lower() and not requested_login:
        msg = f"Only 1 live Drops channel (@{chosen_channel['channel_login']}) is currently online for '{selected_game}'."
    elif prev_name and prev_login != str(chosen_channel["channel_login"]).lower():
        msg = f"Switched channel from {prev_name} to {new_ch_display} (@{chosen_channel['channel_login']}) for '{selected_game}'!"
    else:
        msg = f"Now watching {new_ch_display} (@{chosen_channel['channel_login']}) for '{selected_game}'!"

    cfg["miner_status"] = new_status
    return {
        "success": True,
        "game": selected_game,
        "channel": chosen_channel,
        "mode": mode,
        "queue": new_queue,
        "mining_config": cfg,
        "miner_status": new_status,
        "message": msg,
    }
 
 
_GIT_STATUS_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None}
_AUTOSTART_STATUS_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None}


def get_git_status(check_remote: bool = False):
    now = time.time()
    if not check_remote and _GIT_STATUS_CACHE["data"] and (now - _GIT_STATUS_CACHE["ts"]) < 300.0:
        return dict(_GIT_STATUS_CACHE["data"])

    status = {
        "is_git": False,
        "commit_hash": "",
        "commit_short": "",
        "commit_date": "",
        "commit_message": "",
        "branch": "main",
        "remote_url": "https://github.com/IAndrexI/dropsClaim",
        "updates_available": False,
        "remote_commit": "",
        "error": None,
    }
    if not os.path.exists(os.path.join(APP_DIR, ".git")):
        return status
    status["is_git"] = True
    try:
        status["commit_short"] = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=APP_DIR, text=True, stderr=subprocess.DEVNULL
        ).strip()
        status["commit_hash"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=APP_DIR, text=True, stderr=subprocess.DEVNULL
        ).strip()
        status["commit_message"] = subprocess.check_output(
            ["git", "log", "-1", "--pretty=%B"], cwd=APP_DIR, text=True, stderr=subprocess.DEVNULL
        ).strip().split("\n")[0]
        status["commit_date"] = subprocess.check_output(
            ["git", "log", "-1", "--pretty=%cd", "--date=iso"], cwd=APP_DIR, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception as e:
        status["error"] = str(e)

    try:
        r = subprocess.check_output(
            ["git", "remote", "get-url", "origin"], cwd=APP_DIR, text=True, stderr=subprocess.DEVNULL
        ).strip()
        if r:
            status["remote_url"] = r
    except Exception:
        pass

    if check_remote:
        try:
            remote_ls = subprocess.check_output(
                ["git", "ls-remote", "origin", "refs/heads/main"],
                cwd=APP_DIR, text=True, stderr=subprocess.DEVNULL, timeout=8
            ).strip()
            if remote_ls:
                remote_hash = remote_ls.split()[0]
                status["remote_commit"] = remote_hash[:7]
                if remote_hash and remote_hash != status["commit_hash"]:
                    status["updates_available"] = True
        except Exception:
            pass
    elif _GIT_STATUS_CACHE["data"]:
        status["remote_commit"] = _GIT_STATUS_CACHE["data"].get("remote_commit", "")
        status["updates_available"] = bool(_GIT_STATUS_CACHE["data"].get("updates_available", False))

    _GIT_STATUS_CACHE["ts"] = now
    _GIT_STATUS_CACHE["data"] = dict(status)
    return status


def perform_git_update():
    if os.path.exists("/opt/auto-loot-claimer/update.sh"):
        update_script = "/opt/auto-loot-claimer/update.sh"
        target_dir = "/opt/auto-loot-claimer"
    elif os.path.exists(os.path.join(APP_DIR, "update.sh")):
        update_script = os.path.join(APP_DIR, "update.sh")
        target_dir = APP_DIR
    else:
        update_script = os.path.join(APP_DIR, "scripts", "update_from_github.sh")
        target_dir = APP_DIR

    if os.path.exists(update_script) and os.name != "nt":
        run_env = os.environ.copy()
        run_env["SKIP_WEB_RESTART"] = "1"
        res = subprocess.run(["bash", update_script], cwd=target_dir, env=run_env, capture_output=True, text=True, timeout=120)
        if res.returncode == 0:
            try:
                subprocess.Popen(
                    [
                        "sh",
                        "-c",
                        "sleep 1.5; if command -v rc-service >/dev/null 2>&1; then rc-service auto-loot-web restart >/dev/null 2>&1; elif command -v systemctl >/dev/null 2>&1; then systemctl restart auto-loot-web.service >/dev/null 2>&1; fi",
                    ],
                    cwd=target_dir,
                    start_new_session=True,
                )
            except Exception:
                pass
        return {"success": res.returncode == 0, "output": (res.stdout or "") + "\n" + (res.stderr or "")}
    else:
        # Fallback for Windows or systems without bash
        try:
            subprocess.run(["git", "fetch", "origin", "main"], cwd=APP_DIR, capture_output=True, text=True, timeout=60)
            res = subprocess.run(["git", "reset", "--hard", "origin/main"], cwd=APP_DIR, capture_output=True, text=True, timeout=60)
            return {"success": res.returncode == 0, "output": (res.stdout or "") + "\n" + (res.stderr or "")}
        except Exception as e:
            return {"success": False, "output": f"Update failed: {e}"}


def get_autostart_status(use_cache: bool = False):
    now = time.time()
    if use_cache and _AUTOSTART_STATUS_CACHE["data"] and (now - _AUTOSTART_STATUS_CACHE["ts"]) < 60.0:
        return dict(_AUTOSTART_STATUS_CACHE["data"])
    status = {
        "init_system": "unknown",
        "enabled": False,
        "services": {},
        "error": None,
    }
    # OpenRC (Alpine Linux)
    if shutil.which("rc-update") or os.path.exists("/sbin/rc-update") or os.path.exists("/usr/sbin/rc-update"):
        status["init_system"] = "openrc"
        try:
            rc_cmd = shutil.which("rc-update") or "/sbin/rc-update"
            out = subprocess.check_output([rc_cmd, "show", "default"], text=True, stderr=subprocess.DEVNULL)
            status["services"]["twitch-drops"] = "twitch-drops" in out
            status["services"]["auto-loot-web"] = "auto-loot-web" in out
            status["services"]["crond"] = "crond" in out
            status["enabled"] = bool(status["services"]["twitch-drops"] and status["services"]["auto-loot-web"])
        except Exception as e:
            status["error"] = str(e)
    # systemd (Debian / Ubuntu)
    elif shutil.which("systemctl") or os.path.exists("/bin/systemctl") or os.path.exists("/usr/bin/systemctl"):
        status["init_system"] = "systemd"
        try:
            sys_cmd = shutil.which("systemctl") or "/bin/systemctl"
            td = subprocess.run([sys_cmd, "is-enabled", "twitch-drops.service"], capture_output=True, text=True)
            web = subprocess.run([sys_cmd, "is-enabled", "auto-loot-web.service"], capture_output=True, text=True)
            fg = subprocess.run([sys_cmd, "is-enabled", "free-games.timer"], capture_output=True, text=True)
            status["services"]["twitch-drops"] = (td.stdout.strip() == "enabled")
            status["services"]["auto-loot-web"] = (web.stdout.strip() == "enabled")
            status["services"]["free-games.timer"] = (fg.stdout.strip() == "enabled")
            status["enabled"] = bool(status["services"]["twitch-drops"] and status["services"]["auto-loot-web"])
        except Exception as e:
            status["error"] = str(e)
    _AUTOSTART_STATUS_CACHE["ts"] = now
    _AUTOSTART_STATUS_CACHE["data"] = dict(status)
    return status


def get_dashboard_password(env):
    """Dashboard password from .env, else a random one generated once and stored in data/ (0600)."""
    pw = (env.get("DASHBOARD_PASSWORD") or "").strip()
    if pw:
        return pw
    path = os.path.join(APP_DIR, "data", "dashboard_password")
    try:
        with open(path, "r", encoding="utf-8") as f:
            pw = f.read().strip()
            if pw:
                return pw
    except OSError:
        pass
    pw = secrets.token_urlsafe(16)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(pw)
    print(f"[SECURITY] DASHBOARD_PASSWORD not set. Generated one-time password saved to {path}", flush=True)
    return pw


def build_status_payload():
    env = load_env_file()
    data_dir = os.path.join(APP_DIR, "data", "fgc")
    twitch_token = resolve_token(env.get("TWITCH_AUTH_TOKEN", "").strip())
    mining_cfg = get_mining_config()
    miner_stat = get_miner_status()
    if mining_cfg.get("mode") != "stopped":
        ensure_miner_daemon_running(twitch_token)
    queue_list = list(mining_cfg.get("queue") or mining_cfg.get("activated_games") or [])
    watched_game_str = str(miner_stat.get("game") or "").strip() if miner_stat.get("active") else ""
    active_target_game = queue_list[0] if queue_list else watched_game_str

    def _mark_queued_and_active_campaigns(camps_list, target_game_name, current_queue):
        target_low = (target_game_name or "").strip().lower()
        queue_low = [q.strip().lower() for q in current_queue if q.strip()]
        already_marked_active = False

        for c in camps_list:
            c_game = str(c.get("game") or "").strip()
            c_game_low = c_game.lower()
            c_name_low = str(c.get("name") or "").strip().lower()
            q_pos = None
            for idx, ql in enumerate(queue_low):
                if ql == c_game_low or ql in c_game_low or ql in c_name_low:
                    q_pos = idx + 1
                    break

            c["queue_position"] = q_pos
            c["is_queued"] = q_pos is not None

            if (
                not already_marked_active
                and target_low
                and not c.get("is_expired")
                and c.get("status") not in ("UNAVAILABLE", "COMPLETED")
                and (c_game_low == target_low or target_low in c_game_low or target_low in c_name_low)
            ):
                c["is_activated"] = True
                c["is_watching"] = bool(miner_stat.get("active") and watched_game_str.lower() == c_game_low)
                already_marked_active = True
            else:
                c["is_activated"] = False
                c["is_watching"] = False

    # 1. Twitch Status & Personal Inventory
    twitch_info = {
        "username": None,
        "user_id": None,
        "connected": False,
        "campaigns": [],
        "collected_items": [],
        "needs_collection_drops": [],
        "unavailable_drops": [],
        "drops_in_progress": [],
        "priority_games": queue_list,
        "queue": queue_list,
        "mining_config": mining_cfg,
        "miner_status": miner_stat,
        "inventory_url": "https://www.twitch.tv/drops/inventory",
        "campaigns_url": "https://www.twitch.tv/drops/campaigns",
        "total_campaigns": 0,
        "active_campaigns_count": 0,
        "claimed_history_count": 0,
        "collected_in_campaigns_count": 0,
        "ready_to_claim_count": 0,
        "in_progress_count": 0,
        "available_count": 0,
        "needs_collection_count": 0,
        "unavailable_count": 0,
        "token_configured": bool(twitch_token or env.get("TWITCH_AUTH_TOKEN", "").strip() or load_stored_token()),
        "token_valid": False,
        "token_error": "",
    }

    if twitch_token:
        try:
            client = TwitchClient(twitch_token)
            if client.validate_session():
                twitch_info["username"] = client.user_login
                twitch_info["user_id"] = client.user_id
                twitch_info["connected"] = True
                twitch_info["token_valid"] = True

                overview = client.get_drops_overview(priority_games=queue_list)
                camps = overview.get("campaigns", [])
                _mark_queued_and_active_campaigns(camps, active_target_game, queue_list)
                twitch_info["priority_games"] = queue_list
                twitch_info["queue"] = queue_list
                twitch_info["mining_config"] = mining_cfg
                twitch_info["miner_status"] = miner_stat

                twitch_info["campaigns"] = camps
                twitch_info["collected_items"] = overview.get("collected_items", [])
                twitch_info["needs_collection_drops"] = overview.get("needs_collection_drops", [])
                twitch_info["unavailable_drops"] = overview.get("unavailable_drops", [])
                twitch_info["total_campaigns"] = overview.get("total_campaigns", len(camps))
                twitch_info["active_campaigns_count"] = overview.get("active_campaigns_count", overview.get("total_campaigns", 0))
                twitch_info["claimed_history_count"] = overview.get("claimed_history_count", 0)
                twitch_info["collected_in_campaigns_count"] = overview.get("collected_in_campaigns_count", 0)
                twitch_info["ready_to_claim_count"] = overview.get("ready_to_claim_count", 0)
                twitch_info["in_progress_count"] = overview.get("in_progress_count", 0)
                twitch_info["available_count"] = overview.get("available_count", 0)
                twitch_info["needs_collection_count"] = overview.get("needs_collection_count", 0)
                twitch_info["unavailable_count"] = overview.get("unavailable_count", 0)
                twitch_info["last_refreshed_at"] = overview.get("last_refreshed_at")

                # Enrich miner_status with live drop progress & elapsed session minutes if currently watching a game
                if miner_stat.get("active") and miner_stat.get("game"):
                    if miner_stat.get("switched_at"):
                        try:
                            sw_val = miner_stat["switched_at"]
                            if isinstance(sw_val, (int, float)):
                                sw_ts = float(sw_val)
                            elif isinstance(sw_val, str) and sw_val.strip().replace(".", "", 1).isdigit():
                                sw_ts = float(sw_val.strip())
                            else:
                                from datetime import datetime
                                dt = datetime.fromisoformat(str(sw_val).replace("Z", "+00:00"))
                                sw_ts = dt.timestamp()
                            elapsed_m = max(1, int((time.time() - sw_ts) // 60) + 1)
                            cyc_m = int(miner_stat.get("cycle_minutes") or 60)
                            miner_stat["minutes_watched"] = min(cyc_m, max(int(miner_stat.get("minutes_watched") or 1), elapsed_m))
                        except Exception:
                            pass

                    live_sess_watched = 0
                    if miner_stat.get("channel_id"):
                        try:
                            sess_ctx = client.get_drop_current_session(
                                str(miner_stat["channel_id"]),
                                str(miner_stat.get("channel_login") or ""),
                            )
                            if isinstance(sess_ctx, dict):
                                live_sess_watched = int(sess_ctx.get("currentMinutesWatched") or 0)
                        except Exception:
                            pass

                    watched_game_low = str(miner_stat.get("game") or "").lower()
                    stat_watched = max(int(miner_stat.get("current_drop_watched") or 0), live_sess_watched)
                    found_active_drop = False
                    for c in camps:
                        if c.get("is_expired"):
                            continue
                        if str(c.get("game") or "").lower() == watched_game_low:
                            for d in c.get("drops", []):
                                if d.get("status") in ("IN_PROGRESS", "AVAILABLE") and d.get("required_minutes", 0) > 0:
                                    req_m = int(d.get("required_minutes") or 0)
                                    best_w = max(int(d.get("watched_minutes") or 0), stat_watched if stat_watched < req_m else 0)
                                    pct_m = min(100, int((best_w / req_m) * 100)) if req_m > 0 else 0
                                    rem_m = max(0, req_m - best_w)
                                    d["watched_minutes"] = best_w
                                    d["progress_percent"] = pct_m
                                    d["remaining_minutes"] = rem_m
                                    if best_w > 0 and d.get("status") == "AVAILABLE":
                                        d["status"] = "IN_PROGRESS"
                                    miner_stat["current_drop_name"] = d.get("name")
                                    miner_stat["current_drop_watched"] = best_w
                                    miner_stat["current_drop_required"] = req_m
                                    miner_stat["current_drop_percent"] = pct_m
                                    miner_stat["current_drop_remaining"] = rem_m
                                    found_active_drop = True
                                    if d.get("status") == "IN_PROGRESS":
                                        break
                            if found_active_drop:
                                break

                # Maintain legacy drops_in_progress flat list for backwards compatibility
                for camp in camps:
                    for d in camp.get("drops", []):
                        if d.get("status") in ("IN_PROGRESS", "READY_TO_CLAIM"):
                            twitch_info["drops_in_progress"].append({
                                "id": d.get("id"),
                                "name": d.get("name"),
                                "campaign_name": camp.get("name"),
                                "game": camp.get("game"),
                                "required_minutes": d.get("required_minutes", 0),
                                "watched_minutes": d.get("watched_minutes", 0),
                                "progress_percent": d.get("progress_percent", 0),
                                "is_claimed": d.get("status") == "CLAIMED",
                                "drop_instance_id": d.get("drop_instance_id"),
                                "status": d.get("status"),
                            })
            else:
                twitch_info["token_error"] = "Twitch token is invalid or expired. Please re-link in Accounts & Logins."
        except Exception as err:
            twitch_info["error"] = str(err)
    elif twitch_info["token_configured"]:
        twitch_info["token_error"] = "Stored Twitch token expired or invalid. Please re-link in Accounts & Logins."

    if not twitch_info["campaigns"]:
        try:
            fallback_client = TwitchClient("")
            overview = fallback_client.get_drops_overview(priority_games=queue_list)
            camps = overview.get("campaigns", [])
            _mark_queued_and_active_campaigns(camps, active_target_game, queue_list)
            twitch_info["campaigns"] = camps
            twitch_info["collected_items"] = overview.get("collected_items", [])
            twitch_info["needs_collection_drops"] = overview.get("needs_collection_drops", [])
            twitch_info["unavailable_drops"] = overview.get("unavailable_drops", [])
            twitch_info["total_campaigns"] = overview.get("total_campaigns", len(camps))
            twitch_info["active_campaigns_count"] = overview.get("active_campaigns_count", overview.get("total_campaigns", 0))
            twitch_info["needs_collection_count"] = overview.get("needs_collection_count", 0)
            twitch_info["unavailable_count"] = overview.get("unavailable_count", 0)
            twitch_info["last_refreshed_at"] = overview.get("last_refreshed_at")
        except Exception as fb_err:
            pass

    # 2. Epic Games Status & Free Promotions
    active_free, upcoming_free = fetch_epic_freebies()
    claimed_store_map = get_claimed_store_titles()

    for game in active_free:
        game["store_url"] = f"https://store.epicgames.com/browse?q={urllib.parse.quote(game.get('title', ''))}"

    has_epic_cookies = False
    if os.path.exists(data_dir):
        for root, _, files in os.walk(data_dir):
            if any("epic" in f.lower() or "cookie" in f.lower() for f in files):
                has_epic_cookies = True
                break

    epic_info = {
        "email": env.get("EG_EMAIL"),
        "has_saved_session": has_epic_cookies,
        "active_freebies": active_free,
        "upcoming_freebies": upcoming_free,
        "claimed_titles": claimed_store_map.get("epic", []),
        "store_url": "https://store.epicgames.com/free-games",
        "library_url": "https://www.epicgames.com/account/transactions",
    }

    # 3. Amazon Prime Gaming & GOG
    has_amazon_cookies = False
    has_gog_cookies = False
    if os.path.exists(data_dir):
        for root, _, files in os.walk(data_dir):
            if any("amazon" in f.lower() or "prime" in f.lower() for f in files):
                has_amazon_cookies = True
            if any("gog" in f.lower() for f in files):
                has_gog_cookies = True

    amazon_info = {
        "email": env.get("PG_EMAIL"),
        "has_saved_session": has_amazon_cookies,
        "claimed_items": claimed_store_map.get("amazon", []),
        "claim_url": "https://gaming.amazon.com/home",
        "loot_url": "https://gaming.amazon.com/loot",
    }

    gog_info = {
        "email": env.get("GOG_EMAIL"),
        "has_saved_session": has_gog_cookies,
        "claimed_items": claimed_store_map.get("gog", []),
        "store_url": "https://www.gog.com",
        "library_url": "https://www.gog.com/account",
    }

    # 4. System / Daemon status
    miner_running = is_miner_daemon_alive()
    if not miner_running and miner_stat.get("active"):
        miner_running = ensure_miner_daemon_running(twitch_token) or True

    git_info = get_git_status(check_remote=False)
    running_bots = get_running_bot_processes()

    return {
        "twitch": twitch_info,
        "epic": epic_info,
        "amazon": amazon_info,
        "gog": gog_info,
        "system": {
            "miner_running": miner_running,
            "git": git_info,
            "bots": running_bots,
            "bot_count": len(running_bots),
            "autostart": get_autostart_status(use_cache=True),
        }
    }



class DashboardHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        route_path = self.path.split("?", 1)[0]
        # 1. Public PWA & Static Asset Handlers (Do not require authentication)
        if route_path == "/manifest.json":
            manifest_path = os.path.join(os.path.dirname(__file__), "static", "manifest.json")
            if os.path.exists(manifest_path):
                self.send_response(200)
                self.send_header("Content-Type", "application/manifest+json; charset=utf-8")
                self.send_header("Cache-Control", "public, max-age=3600")
                self.end_headers()
                with open(manifest_path, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.send_error(404, "Manifest not found")
            return

        if route_path == "/sw.js":
            sw_path = os.path.join(os.path.dirname(__file__), "static", "sw.js")
            if os.path.exists(sw_path):
                self.send_response(200)
                self.send_header("Content-Type", "application/javascript; charset=utf-8")
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.send_header("Service-Worker-Allowed", "/")
                self.end_headers()
                with open(sw_path, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.send_error(404, "Service Worker not found")
            return

        if route_path.startswith("/static/"):
            clean_path = route_path.lstrip("/")
            full_path = os.path.join(os.path.dirname(__file__), clean_path)
            if os.path.exists(full_path) and os.path.isfile(full_path):
                ext = os.path.splitext(full_path)[1].lower()
                mime_map = {
                    ".svg": "image/svg+xml",
                    ".png": "image/png",
                    ".ico": "image/x-icon",
                    ".js": "application/javascript",
                    ".json": "application/json",
                    ".css": "text/css",
                }
                ctype = mime_map.get(ext, "application/octet-stream")
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                with open(full_path, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.send_error(404, "Static asset not found")
            return

        if route_path == "/favicon.ico":
            ico_path = os.path.join(os.path.dirname(__file__), "static", "icons", "favicon.ico")
            if os.path.exists(ico_path):
                self.send_response(200)
                self.send_header("Content-Type", "image/x-icon")
                self.end_headers()
                with open(ico_path, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.send_error(404, "Favicon not found")
            return

        # 2. Login Page
        if route_path == "/login":
            if is_authenticated(self.headers):
                self.send_response(302)
                self.send_header("Location", "/")
                self.end_headers()
                return

            login_path = os.path.join(os.path.dirname(__file__), "templates", "login.html")
            if os.path.exists(login_path):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                with open(login_path, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.send_error(404, "Login template not found")
            return

        # 2. Enforce Authentication on All Other Routes
        if not is_authenticated(self.headers):
            if route_path.startswith("/api/"):
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Unauthorized"}).encode("utf-8"))
            else:
                self.send_response(302)
                self.send_header("Location", "/login")
                self.end_headers()
            return

        # 3. Main Dashboard
        if route_path == "/" or route_path == "/index.html":
            template_path = os.path.join(os.path.dirname(__file__), "templates", "index.html")
            if os.path.exists(template_path):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                with open(template_path, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.send_error(404, "Template not found")
            return

        elif route_path == "/api/status":
            data = build_status_payload()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode("utf-8"))
            return

        elif route_path == "/api/accounts":
            env = load_env_file()
            data = {
                "dashboard_username": env.get("DASHBOARD_USERNAME", "andrex"),
                "twitch_auth_token": "",
                "has_twitch_auth_token": bool(env.get("TWITCH_AUTH_TOKEN")),
                "twitch_login": (load_stored_token() or {}).get("login"),
                "twitch_priority_games": env.get("TWITCH_PRIORITY_GAMES", ""),
                "eg_email": env.get("EG_EMAIL", ""),
                "has_eg_password": bool(env.get("EG_PASSWORD")),
                "eg_otp_secret": env.get("EG_OTP_SECRET", ""),
                "pg_email": env.get("PG_EMAIL", ""),
                "has_pg_password": bool(env.get("PG_PASSWORD")),
                "pg_otp_secret": env.get("PG_OTP_SECRET", ""),
                "gog_email": env.get("GOG_EMAIL", ""),
                "has_gog_password": bool(env.get("GOG_PASSWORD")),
                "discord_webhook_url": env.get("DISCORD_WEBHOOK_URL", ""),
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode("utf-8"))
            return

        elif route_path == "/api/logs":
            log_paths = ["/var/log/twitch-drops.log", "/var/log/free-games.log", "/var/log/auto-loot-web.log"]
            combined = ""
            for lp in log_paths:
                if os.path.exists(lp):
                    try:
                        with open(lp, "r", encoding="utf-8", errors="ignore") as f:
                            lines = f.readlines()[-60:]
                            combined += f"=== Log: {os.path.basename(lp)} ===\n" + "".join(lines) + "\n\n"
                    except Exception:
                        pass
            if not combined:
                combined = "No log files found yet. Once services run, output will appear here."

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"logs": combined}).encode("utf-8"))
            return

        elif route_path == "/api/system/git-status":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(get_git_status(check_remote=True)).encode("utf-8"))
            return

        elif route_path == "/api/system/bots":
            bots = get_running_bot_processes()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"bots": bots, "count": len(bots)}).encode("utf-8"))
            return

        elif route_path == "/api/system/autostart":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(get_autostart_status()).encode("utf-8"))
            return

        elif route_path.startswith("/api/twitch/channels"):
            parsed_url = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed_url.query)
            req_game = (qs.get("game", [""])[0] or "").strip()
            current_stat = get_miner_status()
            current_cfg = get_mining_config()
            q_list = current_cfg.get("queue") or []
            target_game = (
                req_game
                or str(current_stat.get("game") or "").strip()
                or (q_list[0] if q_list else "")
                or str(current_stat.get("selected_game") or "").strip()
            )
            env = load_env_file()
            token = resolve_token(env.get("TWITCH_AUTH_TOKEN", "").strip())
            channels = []
            if target_game:
                try:
                    client = TwitchClient(token)
                    channels = client.list_eligible_channels(target_game, limit=15)
                except Exception:
                    channels = []
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "success": True,
                "game": target_game,
                "current_channel": current_stat.get("channel_login") or "",
                "current_channel_name": current_stat.get("channel_name") or "",
                "channels": channels,
            }).encode("utf-8"))
            return

        self.send_error(404, "Not Found")

    def do_POST(self):
        # 1. Login Endpoint
        if self.path == "/api/login":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                payload = {}

            username = payload.get("username", "").strip()
            password = payload.get("password", "").strip()

            env = load_env_file()
            expected_user = env.get("DASHBOARD_USERNAME", "andrex").strip()
            expected_pass = get_dashboard_password(env)

            user_matches = secrets.compare_digest(username, expected_user)
            pass_matches = secrets.compare_digest(password, expected_pass)

            if user_matches and pass_matches:
                token = secrets.token_hex(32)
                # Session valid for 7 days
                ACTIVE_SESSIONS[token] = time.time() + (86400 * 7)

                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Set-Cookie", f"session_id={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age=604800")
                self.end_headers()
                self.wfile.write(json.dumps({"success": True}).encode("utf-8"))
            else:
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Invalid username or password"}).encode("utf-8"))
            return

        # 2. Logout Endpoint
        if self.path == "/api/logout":
            cookies = parse_cookies(self.headers.get("Cookie", ""))
            session_id = cookies.get("session_id")
            if session_id and session_id in ACTIVE_SESSIONS:
                del ACTIVE_SESSIONS[session_id]

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", "session_id=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0")
            self.end_headers()
            self.wfile.write(json.dumps({"success": True}).encode("utf-8"))
            return

        # 3. Protected POST Endpoints
        if not is_authenticated(self.headers):
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Unauthorized"}).encode("utf-8"))
            return

        # 4a. Twitch device-code login
        if self.path in ("/api/twitch/login/start", "/api/twitch/login/poll", "/api/twitch/logout"):
            content_length = int(self.headers.get("Content-Length", 0))
            try:
                payload = json.loads(self.rfile.read(content_length).decode("utf-8") or "{}")
            except Exception:
                payload = {}
            try:
                if self.path.endswith("/start"):
                    result = start_device_login()
                    result.pop("device_code_secret", None)
                elif self.path.endswith("/poll"):
                    result = poll_device_login(str(payload.get("device_code", "")))
                    if result.get("status") == "ok":
                        ensure_miner_daemon_running(resolve_token())
                else:
                    clear_stored_token()
                    result = {"status": "ok"}
                code = 200
            except Exception as e:
                result, code = {"status": "error", "message": str(e)}, 502
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(result).encode("utf-8"))
            return

        # 4. Save Account Credentials to .env from Web UI
        if self.path == "/api/accounts":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                payload = {}

            updates = {}
            if "dashboard_username" in payload and payload["dashboard_username"].strip():
                updates["DASHBOARD_USERNAME"] = payload["dashboard_username"].strip()
            if "dashboard_password" in payload and payload["dashboard_password"].strip():
                updates["DASHBOARD_PASSWORD"] = payload["dashboard_password"].strip()
            if payload.get("twitch_auth_token", "").strip():
                updates["TWITCH_AUTH_TOKEN"] = payload["twitch_auth_token"].strip()
                clear_stored_token()
            if "twitch_priority_games" in payload:
                updates["TWITCH_PRIORITY_GAMES"] = payload["twitch_priority_games"].strip()
            if "eg_email" in payload:
                updates["EG_EMAIL"] = payload["eg_email"].strip()
            if "eg_password" in payload and payload["eg_password"].strip():
                updates["EG_PASSWORD"] = payload["eg_password"].strip()
            if "eg_otp_secret" in payload:
                updates["EG_OTP_SECRET"] = payload["eg_otp_secret"].strip()
            if "pg_email" in payload:
                updates["PG_EMAIL"] = payload["pg_email"].strip()
            if "pg_password" in payload and payload["pg_password"].strip():
                updates["PG_PASSWORD"] = payload["pg_password"].strip()
            if "pg_otp_secret" in payload:
                updates["PG_OTP_SECRET"] = payload["pg_otp_secret"].strip()
            if "gog_email" in payload:
                updates["GOG_EMAIL"] = payload["gog_email"].strip()
            if "gog_password" in payload and payload["gog_password"].strip():
                updates["GOG_PASSWORD"] = payload["gog_password"].strip()
            if "discord_webhook_url" in payload:
                updates["DISCORD_WEBHOOK_URL"] = payload["discord_webhook_url"].strip()

            save_env_file(updates)
            if updates.get("TWITCH_AUTH_TOKEN"):
                ensure_miner_daemon_running(updates["TWITCH_AUTH_TOKEN"])

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"success": True, "message": "Account credentials saved successfully!"}).encode("utf-8"))
            return

        # 5. Test specific service connection
        if self.path == "/api/test-service":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                payload = {}

            service = payload.get("service")
            env = load_env_file()

            if service == "twitch":
                token = payload.get("token") or resolve_token(env.get("TWITCH_AUTH_TOKEN", "").strip())
                if not token:
                    res = {"success": False, "message": "No Twitch auth-token configured."}
                else:
                    client = TwitchClient(token)
                    if client.validate_session():
                        res = {"success": True, "message": f"Connected as {client.user_login} (User ID: {client.user_id})"}
                    else:
                        res = {"success": False, "message": "Twitch token invalid or expired."}

            elif service == "discord":
                url = payload.get("url") or env.get("DISCORD_WEBHOOK_URL", "")
                if not url:
                    res = {"success": False, "message": "No Discord webhook URL configured."}
                else:
                    try:
                        req_data = json.dumps({"content": "[Auto Loot Claimer]: Test webhook alert connected successfully!"}).encode("utf-8")
                        req = urllib.request.Request(url, data=req_data, headers={"Content-Type": "application/json", "User-Agent": "AutoLoot/1.0"}, method="POST")
                        with urllib.request.urlopen(req, timeout=8):
                            res = {"success": True, "message": "Test ping sent to Discord successfully!"}
                    except Exception as e:
                        res = {"success": False, "message": f"Webhook error: {e}"}

            elif service == "epic":
                promo_url = "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions?locale=en-US&country=US&allowCountries=US"
                try:
                    req = urllib.request.Request(promo_url, headers={"User-Agent": "Mozilla/5.0"}, method="GET")
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        res = {"success": True, "message": "Epic Games Store API reachable (HTTP 200). Ready for automated claims."}
                except Exception as e:
                    res = {"success": False, "message": f"Could not reach Epic API: {e}"}

            else:
                res = {"success": True, "message": f"Configuration saved for {service}."}

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(res).encode("utf-8"))
            return

        if self.path == "/api/claim-drops":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length) if content_length > 0 else b"{}"
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                payload = {}

            drop_instance_id = payload.get("drop_instance_id")

            env = load_env_file()
            token = resolve_token(env.get("TWITCH_AUTH_TOKEN", "").strip())
            claimed = 0
            message = "No drops were claimed."
            if token:
                try:
                    if drop_instance_id:
                        client = TwitchClient(token)
                        if client.claim_drop(drop_instance_id):
                            claimed = 1
                            message = "Successfully claimed drop reward!"
                        else:
                            message = "Could not claim drop reward (it may have already been claimed)."
                    else:
                        miner = DropsMiner(token)
                        claimed = miner.claim_pending_drops()
                        message = f"Claimed {claimed} ready drop rewards!"
                except Exception as e:
                    message = f"Error claiming drops: {e}"

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"success": True, "claimed": claimed, "message": message}).encode("utf-8"))
            return

        elif self.path == "/api/twitch/priority":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                payload = {}

            games = payload.get("games", [])
            if isinstance(games, list):
                priority_val = ", ".join([g.strip() for g in games if g.strip()])
            else:
                priority_val = str(games).strip()

            save_env_file({"TWITCH_PRIORITY_GAMES": priority_val})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "success": True,
                "priority_games": [g.strip() for g in priority_val.split(",") if g.strip()],
                "message": "Twitch drop priorities updated successfully!"
            }).encode("utf-8"))
            return

        elif self.path == "/api/twitch/mining-config":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                payload = {}

            mode = payload.get("mode", "default")
            games = payload.get("queue") if "queue" in payload else payload.get("activated_games", [])
            if not isinstance(games, list):
                games = [g.strip() for g in str(games).split(",") if g.strip()]

            cfg = save_mining_config(mode=mode, activated_games=games, resolve_stream=True)
            mstat = cfg.get("miner_status") or {}
            sel = cfg.get("selected_game") or mstat.get("game")
            q_len = len(cfg.get("queue") or [])
            if cfg.get("mode") == "stopped":
                msg = "Disconnected from Twitch mining. Zero streams will be watched until toggled back on."
            elif mstat.get("active") and mstat.get("channel_name"):
                msg = f"Instantly switched stream! Now watching {mstat['channel_name']} for '{mstat.get('game')}' ({q_len} in queue)."
            elif sel:
                msg = f"Now targeting '#1 {sel}' ({q_len} in queue, {cfg['mode']} mode)."
            else:
                msg = f"Queue cleared. Switched to '{cfg['mode']}' mode."
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "success": True,
                "mode": cfg.get("mode", "default"),
                "queue": cfg.get("queue", []),
                "activated_games": cfg.get("queue", []),
                "mining_config": cfg,
                "miner_status": mstat,
                "message": msg
            }).encode("utf-8"))
            return

        elif self.path == "/api/twitch/restart-mining":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length) if content_length > 0 else b"{}"
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except Exception:
                payload = {}
            target_g = str(payload.get("game") or "").strip()
            res_data = restart_mining(target_g)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(res_data).encode("utf-8"))
            return

        elif self.path == "/api/twitch/switch-channel":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length) if content_length > 0 else b"{}"
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except Exception:
                payload = {}
            target_g = str(payload.get("game") or "").strip()
            target_ch = str(payload.get("channel_login") or payload.get("channel") or "").strip()
            res_data = switch_watched_channel(target_g, target_ch)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(res_data).encode("utf-8"))
            return

        elif self.path in ("/api/twitch/toggle-activation", "/api/twitch/queue"):
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                payload = {}

            game = payload.get("game", "").strip()
            action = str(payload.get("action") or "").strip().lower()
            activated = bool(payload.get("activated", True))

            if action in ("restart", "restart_mining", "restart_miner"):
                res_data = restart_mining(game)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(res_data).encode("utf-8"))
                return

            current_cfg = get_mining_config()
            current_queue = list(current_cfg.get("queue") or current_cfg.get("activated_games") or [])
            prev_top = current_queue[0].lower() if current_queue else ""

            if not action:
                action = "mine_now" if activated else "remove"

            def _remove_game(q_list, g_target):
                gt_low = g_target.lower()
                return [g for g in q_list if g.lower() != gt_low]

            if action == "clear":
                new_queue = []
            elif action == "reorder" and isinstance(payload.get("queue"), list):
                new_queue = [str(g).strip() for g in payload.get("queue", []) if str(g).strip()]
            elif not game:
                new_queue = current_queue
            elif action in ("mine_now", "front", "select"):
                # Put selected game at #1 in the queue (active immediately) and keep existing queued games after it!
                rest = _remove_game(current_queue, game)
                new_queue = [game] + rest
            elif action in ("enqueue", "add", "back"):
                # Append to the end of the queue (if queue was empty, it becomes #1 and starts mining)
                if any(g.lower() == game.lower() for g in current_queue):
                    new_queue = current_queue
                else:
                    new_queue = current_queue + [game]
            elif action == "move_up":
                new_queue = list(current_queue)
                for idx, g in enumerate(new_queue):
                    if g.lower() == game.lower() and idx > 0:
                        new_queue[idx - 1], new_queue[idx] = new_queue[idx], new_queue[idx - 1]
                        break
            elif action == "move_down":
                new_queue = list(current_queue)
                for idx, g in enumerate(new_queue):
                    if g.lower() == game.lower() and idx < len(new_queue) - 1:
                        new_queue[idx + 1], new_queue[idx] = new_queue[idx], new_queue[idx + 1]
                        break
            else:  # "remove" / deactivate
                new_queue = _remove_game(current_queue, game)

            new_top = new_queue[0].lower() if new_queue else ""
            top_changed = (new_top != prev_top) or (not get_miner_status().get("active") and bool(new_top))

            cfg = save_mining_config(
                mode=current_cfg.get("mode", "default"),
                activated_games=new_queue,
                resolve_stream=top_changed,
            )
            mstat = cfg.get("miner_status") or {}
            q_len = len(new_queue)

            if action in ("mine_now", "front", "select") and game:
                if mstat.get("active") and mstat.get("channel_name"):
                    msg = f"Switched active stream! Now mining #1 '{game}' on {mstat['channel_name']} ({q_len} in queue)."
                else:
                    msg = mstat.get("message") or f"Set '{game}' as #1 active target ({q_len} in queue)."
            elif action in ("enqueue", "add", "back") and game:
                pos = next((i + 1 for i, g in enumerate(new_queue) if g.lower() == game.lower()), q_len)
                if pos == 1 and mstat.get("active") and mstat.get("channel_name"):
                    msg = f"Added '{game}' as #1 in queue! Now watching {mstat['channel_name']}."
                else:
                    msg = f"Added '{game}' to mining queue at position #{pos} (1 mined at a time)."
            elif action in ("move_up", "move_down") and game:
                pos = next((i + 1 for i, g in enumerate(new_queue) if g.lower() == game.lower()), 1)
                msg = f"Moved '{game}' to queue position #{pos}."
            elif action == "clear":
                msg = "Cleared mining queue."
            else:
                if new_queue and mstat.get("active") and mstat.get("channel_name"):
                    msg = f"Removed '{game}' from queue. Now mining #1 '{new_queue[0]}' on {mstat['channel_name']}."
                elif new_queue:
                    msg = f"Removed '{game}'. Next up in queue: '{new_queue[0]}'."
                else:
                    msg = f"Removed '{game}' from mining queue."

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "success": True,
                "game": game,
                "action": action,
                "activated": bool(new_queue and any(g.lower() == game.lower() for g in new_queue)),
                "mode": cfg.get("mode", "default"),
                "queue": new_queue,
                "activated_games": new_queue,
                "mining_config": cfg,
                "miner_status": mstat,
                "message": msg
            }).encode("utf-8"))
            return

        elif self.path == "/api/trigger-games":
            content_length = int(self.headers.get("Content-Length", 0))
            payload = {}
            if content_length > 0:
                try:
                    payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
                except Exception:
                    pass
            target_store = payload.get("store", "").strip().lower()

            script_path = os.path.join(APP_DIR, "scripts", "run_games_claimer.sh")
            if os.path.exists(script_path):
                cmd = ["bash", script_path]
                if target_store:
                    cmd.append(target_store)
                subprocess.Popen(cmd, cwd=APP_DIR)
                if target_store:
                    msg = f"Triggered {target_store.upper()} claim check in the background!"
                else:
                    msg = "Triggered game storefronts check in the background!"
            else:
                msg = "Claimer script not found."

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"success": True, "message": msg}).encode("utf-8"))
            return

        elif self.path == "/api/claim-item":
            content_length = int(self.headers.get("Content-Length", 0))
            payload = {}
            if content_length > 0:
                try:
                    payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
                except Exception:
                    pass

            store = payload.get("store", "epic").strip().lower()
            title = payload.get("title", "").strip()
            claimed = bool(payload.get("claimed", True))

            if title:
                save_claimed_store_title(store, title, claimed)

            all_claimed = get_claimed_store_titles()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "success": True,
                "store": store,
                "title": title,
                "claimed": claimed,
                "claimed_titles": all_claimed.get(store, []),
                "message": f"Updated '{title}' as {'[CLAIMED]' if claimed else '[UNCLAIMED]'} for {store.upper()}."
            }).encode("utf-8"))
            return

        # 9. System: Update from GitHub
        if self.path == "/api/system/git-update":
            result = perform_git_update()
            self.send_response(200 if result.get("success") else 500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(result).encode("utf-8"))
            return

        # 10. System: Export & Sync Claimed Loot to GitHub
        if self.path == "/api/system/sync-github":
            sync_script = os.path.join(APP_DIR, "scripts", "sync_loot_to_github.py")
            res = subprocess.run([sys.executable, sync_script, "--push"], cwd=APP_DIR, capture_output=True, text=True, timeout=60)
            self.send_response(200 if res.returncode == 0 else 500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "success": res.returncode == 0,
                "output": (res.stdout or "") + "\n" + (res.stderr or "")
            }).encode("utf-8"))
            return

        # 11. System: Terminate Conflicting Bots
        if self.path == "/api/system/kill-bots":
            bots = get_running_bot_processes()
            killed = 0
            for b in bots:
                p = int(b.get("pid", 0))
                if p > 0 and kill_pid(p):
                    killed += 1
            if os.path.exists(PID_FILE):
                try:
                    os.remove(PID_FILE)
                except Exception:
                    pass
            # Clear previous stream info in miner_status.json
            status_file = os.path.join(APP_DIR, "data", "miner_status.json")
            try:
                if os.path.exists(status_file):
                    with open(status_file, "r", encoding="utf-8") as f:
                        cur_stat = json.load(f) or {}
                else:
                    cur_stat = {}
                cur_stat.update({
                    "active": False,
                    "switching": False,
                    "channel_name": "",
                    "channel_login": "",
                    "channel_id": "",
                    "stream_id": "",
                    "stream_url": "",
                    "viewers": 0,
                    "message": "Terminated bot processes and cleared stream.",
                    "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                })
                with open(status_file, "w", encoding="utf-8") as f:
                    json.dump(cur_stat, f, indent=2)
            except Exception:
                pass
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "success": True,
                "killed": killed,
                "message": f"Terminated {killed} bot process(es) and released PID lock."
            }).encode("utf-8"))
            return

        # 12. System: Enable Autostart on Reboot
        if self.path == "/api/system/autostart":
            script_path = os.path.join(APP_DIR, "scripts", "enable_autostart.sh")
            res = subprocess.run(["bash", script_path], cwd=APP_DIR, capture_output=True, text=True, timeout=60)
            status_after = get_autostart_status()
            self.send_response(200 if res.returncode == 0 else 500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "success": res.returncode == 0,
                "output": (res.stdout or "") + "\n" + (res.stderr or ""),
                "status": status_after
            }).encode("utf-8"))
            return

        # 13. Twitch: Daily / Manual Campaign Refresh
        if self.path == "/api/campaigns/refresh":
            env = load_env_file()
            token = resolve_token(env.get("TWITCH_AUTH_TOKEN", "").strip())
            mining_cfg = get_mining_config()
            queue_list = list(mining_cfg.get("queue") or mining_cfg.get("activated_games") or [])
            priority_str = env.get("TWITCH_PRIORITY_GAMES", "")
            env_prio = [g.strip() for g in priority_str.split(",") if g.strip()]
            priority_games = list(dict.fromkeys(queue_list + env_prio))

            client = TwitchClient(token)
            if token:
                client.validate_session()
            overview = client.get_drops_overview(priority_games=priority_games, force_refresh=True)
            active_cnt = overview.get("active_campaigns_count", overview.get("total_campaigns", 0))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "success": True,
                "message": f"Refreshed live Twitch Drops directly from Twitch API! Found {active_cnt} active campaigns ({overview.get('total_campaigns', 0)} total).",
                "total_campaigns": overview.get("total_campaigns", 0),
                "active_campaigns_count": active_cnt,
                "last_refreshed_at": overview.get("last_refreshed_at", ""),
                "campaigns": overview.get("campaigns", []),
            }).encode("utf-8"))
            return

        self.send_error(404, "Not Found")


def run_server(port=8080):
    server_address = ("0.0.0.0", port)
    httpd = ThreadingHTTPServer(server_address, DashboardHandler)
    env = load_env_file()
    user = env.get("DASHBOARD_USERNAME", "andrex").strip()
    print("===========================================================")
    print("  Auto Loot Claimer Web Dashboard running!")
    print(f"  Access in your browser: http://<your-server-ip>:{port}")
    print(f"  Authentication: Protected (User: {user})")
    print("===========================================================")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard server...")
        httpd.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Auto Loot Claimer Web Dashboard")
    parser.add_argument("--port", type=int, default=8080, help="Port to listen on (default 8080)")
    args = parser.parse_args()
    run_server(args.port)
