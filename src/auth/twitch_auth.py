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

# Twitch's official first-party Client IDs:
# - DEVICE_CLIENT_ID ("ue6666qo983tsx6so1t0vnawi233wa", Twitch TV/Console client) supports
#   BOTH the OAuth device-code flow (https://id.twitch.tv/oauth2/device) AND Twitch's internal
#   GraphQL API (https://gql.twitch.tv/gql). Third-party dev.twitch.tv Client IDs are rejected
#   by https://gql.twitch.tv/gql with HTTP 400 ("The Client-ID header is invalid").
# - WEB_CLIENT_ID ("kimne78kx3ncx6brgo4mv6wki5h1ko") is used for browser auth-token cookies.
DEVICE_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
DEVICE_CLIENT_ID = "ue6666qo983tsx6so1t0vnawi233wa"
WEB_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
VALID_GQL_CLIENT_IDS = {
    WEB_CLIENT_ID,
    DEVICE_CLIENT_ID,
    "kd1unb4b3q4t58fwlpcbzcbnm76a8fp",
    "85lcqzx34057526980268080982680",
}


def device_client_id() -> str:
    cid = os.environ.get("TWITCH_CLIENT_ID", "").strip()
    if cid in VALID_GQL_CLIENT_IDS:
        return cid
    try:
        with open(os.path.join(BASE_DIR, ".env"), "r", encoding="utf-8") as f:
            for line in f:
                if line.strip().startswith("TWITCH_CLIENT_ID="):
                    val = line.split("=", 1)[1].strip().strip("'\"")
                    if val in VALID_GQL_CLIENT_IDS:
                        return val
    except OSError:
        pass
    return DEVICE_CLIENT_ID

DEVICE_URL = "https://id.twitch.tv/oauth2/device"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"
VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"

# In-memory pending logins keyed by device_code
_PENDING: Dict[str, Dict[str, Any]] = {}
_VALIDATED_TOKEN_CACHE: Dict[str, Dict[str, Any]] = {}


def clean_auth_token(raw_token: str) -> str:
    """Normalize a user-provided or stored Twitch auth token (strip OAuth/Bearer/cookie prefixes & quotes)."""
    tok = (raw_token or "").strip().strip("'\"")
    if tok.lower().startswith("oauth "):
        tok = tok[6:].strip()
    elif tok.lower().startswith("bearer "):
        tok = tok[7:].strip()
    if "auth-token=" in tok:
        for part in tok.split(";"):
            part = part.strip()
            if part.startswith("auth-token="):
                tok = part.split("=", 1)[1].strip().strip("'\"")
                break
    return tok.strip()


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
    _VALIDATED_TOKEN_CACHE.clear()
    try:
        os.remove(TOKEN_FILE)
    except OSError:
        pass


def validate_access_token(access_token: str) -> Optional[Dict[str, Any]]:
    """Returns id.twitch.tv validation info (login, user_id, expires_in) or None if invalid."""
    clean_tok = clean_auth_token(access_token)
    if not clean_tok:
        return None
    cached = _VALIDATED_TOKEN_CACHE.get(clean_tok)
    now = time.time()
    if cached and (now - float(cached.get("_cached_at", 0))) < 300:
        return dict(cached["info"])
    req = urllib.request.Request(
        VALIDATE_URL, headers={"Authorization": f"OAuth {clean_tok}", "User-Agent": DEVICE_USER_AGENT}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            info = json.loads(r.read().decode())
            if isinstance(info, dict) and info.get("user_id"):
                _VALIDATED_TOKEN_CACHE[clean_tok] = {"_cached_at": now, "info": dict(info)}
            return info
    except urllib.error.HTTPError as e:
        if e.code == 401:
            _VALIDATED_TOKEN_CACHE.pop(clean_tok, None)
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
    Automatically rejects any stored token bound to a non-GQL third-party client_id.
    """
    tok = load_token()
    if tok:
        saved_cid = str(tok.get("client_id") or "")
        # Third-party 30-char dev.twitch.tv client IDs are rejected by https://gql.twitch.tv/gql
        if len(saved_cid) >= 20 and saved_cid not in VALID_GQL_CLIENT_IDS:
            logger.warning(
                "Stored token uses third-party client_id (%s) rejected by Twitch GQL; falling back to TWITCH_AUTH_TOKEN or re-login.",
                saved_cid,
            )
            tok = None
        else:
            info = validate_access_token(tok["access_token"])
            if info is None and refresh_token():
                tok = load_token()
                info = validate_access_token(tok["access_token"]) if tok else None
            if info is None:
                logger.error("Stored Twitch token is invalid; run login again (python -m src.twitch_drops.main --login)")
                tok = None
            elif isinstance(info, dict):
                val_cid = str(info.get("client_id") or "")
                if len(val_cid) >= 20 and val_cid not in VALID_GQL_CLIENT_IDS:
                    logger.warning("Validated token belongs to third-party client_id (%s); ignoring for GQL.", val_cid)
                    tok = None
                elif tok is not None:
                    updated = False
                    if val_cid in VALID_GQL_CLIENT_IDS and tok.get("client_id") != val_cid:
                        tok["client_id"] = val_cid
                        updated = True
                    if info.get("user_id") and tok.get("user_id") != str(info["user_id"]):
                        tok["user_id"] = str(info["user_id"])
                        updated = True
                    if info.get("login") and tok.get("login") != info["login"]:
                        tok["login"] = info["login"]
                        updated = True
                    if updated:
                        try:
                            save_token(tok)
                        except Exception:
                            pass
        if tok:
            return clean_auth_token(tok["access_token"]), tok.get("client_id") or device_client_id()
    clean_env = clean_auth_token(env_token)
    if clean_env:
        return clean_env, client_id_for(clean_env)
    return "", WEB_CLIENT_ID


def resolve_token(env_token: str = "") -> str:
    return get_credentials(env_token)[0]


def client_id_for(token: str) -> str:
    clean_tok = clean_auth_token(token)
    if not clean_tok:
        return WEB_CLIENT_ID
    tok = load_token()
    if tok and clean_auth_token(tok.get("access_token", "")) == clean_tok:
        cid = str(tok.get("client_id") or "")
        if len(cid) >= 20 and cid not in VALID_GQL_CLIENT_IDS:
            return WEB_CLIENT_ID
        return cid or device_client_id()
    cached = _VALIDATED_TOKEN_CACHE.get(clean_tok)
    if cached and isinstance(cached.get("info"), dict):
        val_cid = str(cached["info"].get("client_id") or "")
        if val_cid in VALID_GQL_CLIENT_IDS:
            return val_cid
    return WEB_CLIENT_ID


# ----------------------------------------------------------------- device flow
def start_device_login() -> Dict[str, Any]:
    cid = device_client_id()
    status, body = _post_form(DEVICE_URL, {"client_id": cid, "scopes": ""})
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
