"""
Twitch OAuth device-code login + persistent token store (stdlib only).

Flow (no password, no copy-pasting browser cookies):
  1. start_device_login()  -> returns a short user code + activation URL
  2. user approves at https://www.twitch.tv/activate
  3. poll_device_login()   -> exchanges the device code for an access token
  4. token is saved (0600) in data/twitch_token.json and reused / refreshed

The token is bound to the Client-Id it was issued for, so the store keeps both.
"""

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("twitch_auth")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(BASE_DIR, "data")
TOKEN_FILE = os.path.join(DATA_DIR, "twitch_token.json")

# Twitch no longer accepts its first-party app client IDs for the device-code
# grant, so login uses an application YOU register at https://dev.twitch.tv/console
# (Client Type: "Public"). Put its Client ID in TWITCH_CLIENT_ID (.env or env var).
# Tokens are bound to that Client-Id, which the store records.
DEVICE_USER_AGENT = "dropsClaim/2.0"


def device_client_id() -> str:
    cid = os.environ.get("TWITCH_CLIENT_ID", "").strip()
    if cid:
        return cid
    try:
        with open(os.path.join(BASE_DIR, ".env"), "r", encoding="utf-8") as f:
            for line in f:
                if line.strip().startswith("TWITCH_CLIENT_ID="):
                    return line.split("=", 1)[1].strip().strip("'\"")
    except OSError:
        pass
    return ""

WEB_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"

DEVICE_URL = "https://id.twitch.tv/oauth2/device"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"
VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"

# In-memory pending logins keyed by device_code
_PENDING: Dict[str, Dict[str, Any]] = {}


def _post_form(url: str, fields: Dict[str, str], timeout: int = 15) -> Tuple[int, Dict[str, Any]]:
    req = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(fields).encode(),
        headers={"User-Agent": DEVICE_USER_AGENT, "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode() or "{}")
        except Exception:
            body = {}
        return e.code, body


# ---------------------------------------------------------------- token store
def load_token() -> Optional[Dict[str, Any]]:
    try:
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if data.get("access_token") else None
    except (OSError, ValueError):
        return None


def save_token(data: Dict[str, Any]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = TOKEN_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, TOKEN_FILE)


def clear_token() -> None:
    try:
        os.remove(TOKEN_FILE)
    except OSError:
        pass


def validate_access_token(access_token: str) -> Optional[Dict[str, Any]]:
    """Returns id.twitch.tv validation info (login, user_id, expires_in) or None if invalid."""
    req = urllib.request.Request(
        VALIDATE_URL, headers={"Authorization": f"OAuth {access_token}", "User-Agent": DEVICE_USER_AGENT}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return None
        raise
    except Exception as e:  # network trouble: do not treat as invalid
        logger.warning("Token validation unreachable: %s", e)
        return {"unreachable": True}


def refresh_token() -> bool:
    """Refresh the stored token using its refresh_token, if one was issued."""
    tok = load_token()
    if not tok or not tok.get("refresh_token"):
        return False
    status, body = _post_form(
        TOKEN_URL,
        {
            "client_id": tok.get("client_id") or device_client_id(),
            "grant_type": "refresh_token",
            "refresh_token": tok["refresh_token"],
        },
    )
    if status != 200 or "access_token" not in body:
        logger.warning("Token refresh failed (%s): %s", status, body.get("message"))
        return False
    tok.update(
        access_token=body["access_token"],
        refresh_token=body.get("refresh_token", tok["refresh_token"]),
        obtained_at=int(time.time()),
    )
    save_token(tok)
    return True


def get_credentials(env_token: str = "") -> Tuple[str, str]:
    """
    Resolve (access_token, client_id): stored device-login token first,
    legacy TWITCH_AUTH_TOKEN browser cookie as fallback.
    """
    tok = load_token()
    if tok:
        info = validate_access_token(tok["access_token"])
        if info is None and refresh_token():
            tok = load_token()
        elif info is None:
            logger.error("Stored Twitch token is invalid; run login again (python -m src.twitch_drops.main --login)")
            tok = None
        if tok:
            return tok["access_token"], tok.get("client_id") or device_client_id()
    return (env_token or "").strip(), WEB_CLIENT_ID


def resolve_token(env_token: str = "") -> str:
    return get_credentials(env_token)[0]


def client_id_for(token: str) -> str:
    tok = load_token()
    if tok and tok.get("access_token") == token:
        return tok.get("client_id") or device_client_id()
    return WEB_CLIENT_ID


# ----------------------------------------------------------------- device flow
def start_device_login() -> Dict[str, Any]:
    cid = device_client_id()
    if not cid:
        raise RuntimeError(
            "TWITCH_CLIENT_ID is not set. Register a Public app at https://dev.twitch.tv/console "
            "and put its Client ID in .env as TWITCH_CLIENT_ID (see README)."
        )
    status, body = _post_form(DEVICE_URL, {"client_id": cid, "scopes": "user:read:email"})
    if status != 200 or "device_code" not in body:
        raise RuntimeError(f"Twitch device authorization failed ({status}): {body}")
    _PENDING[body["device_code"]] = {
        "expires_at": time.time() + int(body.get("expires_in", 1800)),
        "interval": int(body.get("interval", 5)),
    }
    return {
        "device_code": body["device_code"],
        "user_code": body["user_code"],
        "verification_uri": body.get("verification_uri", "https://www.twitch.tv/activate"),
        "expires_in": int(body.get("expires_in", 1800)),
        "interval": int(body.get("interval", 5)),
    }


def poll_device_login(device_code: str) -> Dict[str, Any]:
    """One poll. Returns {"status": "pending"|"ok"|"expired"|"error", ...}."""
    pend = _PENDING.get(device_code)
    if pend and time.time() > pend["expires_at"]:
        _PENDING.pop(device_code, None)
        return {"status": "expired"}
    status, body = _post_form(
        TOKEN_URL,
        {
            "client_id": device_client_id(),
            "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        },
    )
    if status == 200 and "access_token" in body:
        info = validate_access_token(body["access_token"]) or {}
        save_token(
            {
                "access_token": body["access_token"],
                "refresh_token": body.get("refresh_token"),
                "client_id": device_client_id(),
                "login": info.get("login"),
                "user_id": info.get("user_id"),
                "obtained_at": int(time.time()),
            }
        )
        _PENDING.pop(device_code, None)
        return {"status": "ok", "login": info.get("login")}
    msg = str(body.get("message", "")).lower()
    if "pending" in msg:
        return {"status": "pending"}
    if "expired" in msg or "invalid device" in msg:
        _PENDING.pop(device_code, None)
        return {"status": "expired"}
    return {"status": "error", "message": body.get("message", f"HTTP {status}")}


def interactive_login(print_fn=print) -> bool:
    """Blocking CLI login."""
    d = start_device_login()
    print_fn(f"\nOpen  {d['verification_uri']}\nand enter code:  {d['user_code']}\n(waiting for approval...)")
    deadline = time.time() + d["expires_in"]
    while time.time() < deadline:
        time.sleep(d["interval"])
        r = poll_device_login(d["device_code"])
        if r["status"] == "ok":
            print_fn(f"[SUCCESS] Logged in as {r.get('login')}. Token saved to {TOKEN_FILE}")
            return True
        if r["status"] in ("expired", "error"):
            print_fn(f"[FAIL] {r}")
            return False
    print_fn("[FAIL] Login timed out.")
    return False
