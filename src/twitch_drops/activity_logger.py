"""
Unified, Secret-Redacted Activity & Action Logger for Auto Loot Claimer.
Records granular UI button presses, API actions, miner heartbeats, GQL calls,
Spade watch pings, HLS chunk requests, and headless browser player telemetry to
data/activity.log for troubleshooting and 1-click diagnostic export.
"""

import json
import os
import re
import sys
import threading
import time
from typing import Any, Dict, List, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(BASE_DIR, "data")
ACTIVITY_LOG_FILE = os.path.join(DATA_DIR, "activity.log")
_DEFAULT_ACTIVITY_LOG_FILE = ACTIVITY_LOG_FILE
MAX_LOG_BYTES = 2 * 1024 * 1024  # 2 MB rotate threshold

_LOG_LOCK = threading.Lock()

_SENSITIVE_KEYS = (
    "password",
    "pass",
    "token",
    "auth",
    "secret",
    "otp",
    "cookie",
    "session_id",
    "webhook",
    "authorization",
    "device_code_secret",
)

_TOKEN_PATTERNS = [
    (re.compile(r"(OAuth\s+|Bearer\s+)([A-Za-z0-9._\-]{6,})", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(auth-token\s*=\s*)([^;\s'\"]+)", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(session_id\s*=\s*)([^;\s'\"]+)", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(discord(?:app)?\.com/api/webhooks/\d+/)([^\s'\"]+)", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(api\.telegram\.org/bot)([^/\s'\"]+)", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(TWITCH_AUTH_TOKEN|DASHBOARD_PASSWORD|EG_PASSWORD|EG_OTP_SECRET|PG_PASSWORD|PG_OTP_SECRET|GOG_PASSWORD)\s*=\s*\S+", re.IGNORECASE), r"\1=[REDACTED]"),
]


def sanitize_text(text: Any) -> str:
    """Redact credentials, tokens, cookies, OTP secrets, and webhooks from text."""
    s = str(text or "")
    for pat, repl in _TOKEN_PATTERNS:
        s = pat.sub(repl, s)
    return s


def sanitize_value(key: str, val: Any) -> Any:
    """Redact sensitive dictionary values based on key name and string patterns."""
    k_low = str(key or "").lower()
    if any(sk in k_low for sk in _SENSITIVE_KEYS):
        if isinstance(val, bool):
            return val
        return "[REDACTED]" if val else ""
    if isinstance(val, dict):
        return sanitize_dict(val)
    if isinstance(val, list):
        return [sanitize_value(key, item) for item in val]
    if isinstance(val, str):
        return sanitize_text(val)
    return val


def sanitize_dict(data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Return a deep copy of a dictionary with all secret values redacted."""
    if not isinstance(data, dict):
        return {}
    out: Dict[str, Any] = {}
    for k, v in data.items():
        out[str(k)] = sanitize_value(str(k), v)
    return out


def _rotate_if_needed(log_path: str) -> None:
    try:
        if os.path.exists(log_path) and os.path.getsize(log_path) >= MAX_LOG_BYTES:
            bak_path = log_path + ".1"
            try:
                if os.path.exists(bak_path):
                    os.remove(bak_path)
            except OSError:
                pass
            os.replace(log_path, bak_path)
    except OSError:
        pass


def log_event(
    category: str,
    action: str,
    details: Optional[Dict[str, Any]] = None,
    level: str = "INFO",
) -> str:
    """
    Append a structured, secret-redacted event line to data/activity.log.
    Returns the formatted line written (or empty string on write failure).
    """
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    cat = sanitize_text(category).strip().upper() or "SYSTEM"
    act = sanitize_text(action).strip() or "event"
    lvl = str(level or "INFO").strip().upper()

    detail_str = ""
    if details:
        clean = sanitize_dict(details)
        parts: List[str] = []
        for k, v in clean.items():
            if isinstance(v, (dict, list)):
                v_str = json.dumps(v, separators=(",", ":"), ensure_ascii=False)
            else:
                v_str = str(v)
            if len(v_str) > 260:
                v_str = v_str[:257] + "..."
            parts.append(f"{k}={v_str}")
        if parts:
            detail_str = " | " + " ".join(parts)

    line = f"{ts} [{lvl}] [{cat}] {act}{detail_str}\n"
    if "unittest" in sys.modules and ACTIVITY_LOG_FILE == _DEFAULT_ACTIVITY_LOG_FILE:
        return line

    try:
        with _LOG_LOCK:
            os.makedirs(os.path.dirname(ACTIVITY_LOG_FILE), exist_ok=True)
            _rotate_if_needed(ACTIVITY_LOG_FILE)
            with open(ACTIVITY_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line)
    except OSError:
        return ""
    return line


def read_activity_log(max_lines: int = 200, filter_category: Optional[str] = None) -> List[str]:
    """Read recent redacted lines from data/activity.log."""
    if not os.path.exists(ACTIVITY_LOG_FILE):
        return []
    try:
        with _LOG_LOCK:
            with open(ACTIVITY_LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
    except OSError:
        return []

    if filter_category:
        tag = f"[{filter_category.strip().upper()}]"
        lines = [ln for ln in lines if tag in ln]

    if max_lines > 0:
        lines = lines[-max_lines:]
    return [sanitize_text(ln.rstrip("\r\n")) for ln in lines]
