"""
Native Pure-Python Headless Chromium CDP Auto-Claimer for Epic Games,
Amazon Prime Gaming, and GOG Giveaways.
Standard library only. Zero external dependencies.
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from src.free_games.preview_promotions import fetch_epic_freebies
from src.free_games.sync_claimed import collect_claimed_records, sync_from_sources
from src.twitch_drops.browser_watcher import (
    _CDPWebSocket,
    _pick_free_port,
    find_chromium_binary,
    get_platform_user_agent,
)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(BASE_DIR, "data")
STATUS_FILE = os.path.join(DATA_DIR, "store_claimer_status.json")
PENDING_2FA_FILE = os.path.join(DATA_DIR, "fgc", "pending_2fa.json")


def _utc_iso(ts: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts if ts is not None else time.time()))


def _normalize_store(store: str) -> str:
    s = str(store or "").strip().lower()
    if s in ("prime", "prime-gaming", "amazon"):
        return "amazon"
    if s in ("epic", "epic-games"):
        return "epic"
    if s == "gog":
        return "gog"
    return "all"


def generate_totp(
    secret: str,
    timestamp: Optional[float] = None,
    interval: int = 30,
    digits: int = 6,
) -> str:
    """Compute RFC 6238 TOTP code using pure Python stdlib."""
    clean = re.sub(r"[\s-]+", "", str(secret or "")).upper()
    if not clean:
        raise ValueError("Empty TOTP secret")
    pad_len = (-len(clean)) % 8
    if pad_len:
        clean += "=" * pad_len
    key = base64.b32decode(clean, casefold=True)
    counter = int((timestamp if timestamp is not None else time.time()) // interval)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code_int = (
        ((digest[offset] & 0x7F) << 24)
        | ((digest[offset + 1] & 0xFF) << 16)
        | ((digest[offset + 2] & 0xFF) << 8)
        | (digest[offset + 3] & 0xFF)
    )
    return str(code_int % (10 ** digits)).zfill(digits)


def _atomic_write_json(filepath: str, data: Any, mode: Optional[int] = None) -> None:
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    tmp_path = f"{filepath}.tmp.{os.getpid()}"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    if mode is not None and os.name != "nt":
        try:
            os.chmod(tmp_path, mode)
        except Exception:
            pass
    os.replace(tmp_path, filepath)


def _default_store_status() -> Dict[str, Any]:
    return {
        "running": False,
        "engine": "native-chromium-cdp",
        "last_run_at": None,
        "next_scheduled_at": None,
        "stores": {
            "epic": {"status": "idle", "last_run_at": None, "last_success_at": None, "claimed": [], "message": ""},
            "amazon": {"status": "idle", "last_run_at": None, "last_success_at": None, "claimed": [], "message": ""},
            "gog": {"status": "idle", "last_run_at": None, "last_success_at": None, "claimed": [], "message": ""},
        },
    }


def load_store_claimer_status(base_dir: str = BASE_DIR) -> Dict[str, Any]:
    status_file = os.path.join(base_dir, "data", "store_claimer_status.json")
    base = _default_store_status()
    if os.path.exists(status_file):
        try:
            with open(status_file, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                base["running"] = bool(loaded.get("running", False))
                base["engine"] = loaded.get("engine") or base["engine"]
                base["last_run_at"] = loaded.get("last_run_at")
                base["next_scheduled_at"] = loaded.get("next_scheduled_at")
                stores = loaded.get("stores")
                if isinstance(stores, dict):
                    for k in ("epic", "amazon", "gog"):
                        if isinstance(stores.get(k), dict):
                            base["stores"][k].update(stores[k])
        except Exception:
            pass
    return base


def save_store_claimer_status(data: Dict[str, Any], base_dir: str = BASE_DIR) -> None:
    status_file = os.path.join(base_dir, "data", "store_claimer_status.json")
    _atomic_write_json(status_file, data)


def save_pending_2fa(
    store: str,
    code: str,
    ttl_seconds: int = 600,
    base_dir: str = BASE_DIR,
) -> None:
    norm_store = _normalize_store(store)
    if norm_store == "all":
        raise ValueError("Store must be epic, amazon, or gog")
    clean_code = re.sub(r"\s+", "", str(code or ""))
    if not clean_code:
        raise ValueError("2FA code cannot be empty")
    pending_file = os.path.join(base_dir, "data", "fgc", "pending_2fa.json")
    data: Dict[str, Any] = {}
    if os.path.exists(pending_file):
        try:
            with open(pending_file, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            data = {}
    data[norm_store] = {
        "code": clean_code,
        "expires_at": time.time() + max(30, int(ttl_seconds)),
        "saved_at": _utc_iso(),
    }
    _atomic_write_json(pending_file, data, mode=0o600)


def consume_pending_2fa(store: str, base_dir: str = BASE_DIR) -> Optional[str]:
    norm_store = _normalize_store(store)
    pending_file = os.path.join(base_dir, "data", "fgc", "pending_2fa.json")
    if not os.path.exists(pending_file):
        return None
    try:
        with open(pending_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        entry = data.pop(norm_store, None)
        _atomic_write_json(pending_file, data, mode=0o600)
        if isinstance(entry, dict):
            if float(entry.get("expires_at") or 0) >= time.time():
                code = str(entry.get("code") or "").strip()
                return code or None
    except Exception:
        pass
    return None


def record_fgc_db_entry(
    store: str,
    user: str,
    title: str,
    status: str,
    url: str = "",
    base_dir: str = BASE_DIR,
) -> None:
    norm_store = _normalize_store(store)
    db_map = {
        "epic": "epic-games.json",
        "amazon": "prime-gaming.json",
        "gog": "gog.json",
    }
    fname = db_map.get(norm_store)
    if not fname or not title:
        return
    db_path = os.path.join(base_dir, "data", "fgc", fname)
    data: Dict[str, Any] = {}
    if os.path.exists(db_path):
        try:
            with open(db_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            data = {}
    user_key = str(user or "default").strip() or "default"
    if not isinstance(data.get(user_key), dict):
        data[user_key] = {}
    data[user_key][title] = {
        "title": title,
        "time": _utc_iso(),
        "url": url,
        "status": status,
    }
    _atomic_write_json(db_path, data)


def send_discord_claim_alert(webhook_url: str, store_label: str, titles: List[str]) -> None:
    url = str(webhook_url or "").strip()
    if not url or not titles:
        return
    try:
        content = f"[{store_label}] Claimed {len(titles)} free item(s): " + ", ".join(titles[:10])
        req = urllib.request.Request(
            url,
            data=json.dumps({"content": content}).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "AutoLoot/1.0"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=8):
            pass
    except Exception:
        pass


class _ChromiumCDPSession:
    """Minimal pure-stdlib Chrome DevTools Protocol browser session."""

    def __init__(self, chromium_bin: str, profile_dir: str, timeout: float = 20.0):
        self.chromium_bin = chromium_bin
        self.profile_dir = profile_dir
        self.timeout = timeout
        self.proc: Optional[subprocess.Popen] = None
        self.ws: Optional[_CDPWebSocket] = None
        self.port = _pick_free_port()

    def __enter__(self) -> "_ChromiumCDPSession":
        os.makedirs(self.profile_dir, exist_ok=True)
        for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            lock_path = os.path.join(self.profile_dir, lock_name)
            if os.path.lexists(lock_path):
                try:
                    os.remove(lock_path)
                except Exception:
                    pass

        ua = get_platform_user_agent()
        cmd = [
            self.chromium_bin,
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--window-size=1920,1080",
            "--lang=en-US",
            "--disable-blink-features=AutomationControlled",
            f"--user-data-dir={self.profile_dir}",
            f"--remote-debugging-port={self.port}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-agent={ua}",
            "about:blank",
        ]
        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        ws_url = None
        deadline = time.time() + 15.0
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("Chromium exited unexpectedly during startup")
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/json/list", timeout=2.0
                ) as resp:
                    targets = json.loads(resp.read().decode("utf-8"))
                for t in targets:
                    if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                        ws_url = t["webSocketDebuggerUrl"]
                        break
                if ws_url:
                    break
            except Exception:
                time.sleep(0.35)

        if not ws_url:
            raise RuntimeError("Timed out waiting for Chromium CDP WebSocket")

        self.ws = _CDPWebSocket(ws_url, timeout=self.timeout)
        self.cdp("Page.enable")
        self.cdp("Runtime.enable")
        self.cdp("Network.enable")
        self.cdp(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"},
        )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=4)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self.proc = None

    def cdp(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if not self.ws:
            raise RuntimeError("CDP session is not open")
        return self.ws.send_command(method, params or {})

    def navigate(self, url: str, wait: float = 3.0) -> None:
        self.cdp("Page.navigate", {"url": url})
        if wait > 0:
            time.sleep(wait)

    def eval_js(self, expr: str, await_promise: bool = False) -> Any:
        res = self.cdp(
            "Runtime.evaluate",
            {
                "expression": expr,
                "returnByValue": True,
                "awaitPromise": await_promise,
            },
        )
        return res.get("result", {}).get("result", {}).get("value")

    def wait_for(self, js_predicate: str, timeout: float = 15.0, interval: float = 0.5) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.eval_js(js_predicate):
                    return True
            except Exception:
                pass
            time.sleep(interval)
        return False

    def current_url(self) -> str:
        return str(self.eval_js("window.location.href") or "")

    def page_text(self) -> str:
        return str(self.eval_js("document.body ? document.body.innerText : ''") or "")

    def set_cookie(self, name: str, value: str, domain: str, path: str = "/") -> None:
        self.cdp(
            "Network.setCookie",
            {"name": name, "value": value, "domain": domain, "path": path},
        )


def claim_gog(session: _ChromiumCDPSession, env: Dict[str, str], base_dir: str = BASE_DIR) -> Dict[str, Any]:
    email = (env.get("GOG_EMAIL") or "").strip()
    password = (env.get("GOG_PASSWORD") or "").strip()
    if not email or not password:
        return {
            "status": "unconfigured",
            "claimed": [],
            "already_claimed": [],
            "message": "Add GOG_EMAIL and GOG_PASSWORD in Accounts tab to enable auto-claiming.",
            "user": "",
            "engine": "native-chromium-cdp",
        }

    session.set_cookie(
        "CookieConsent",
        "{stamp:%274oR8MJL+bxVlG6g+kl2we5+suMJ+Tv7I4C5d4k+YY4vrnhCD+P23RQ==%27,necessary:true,preferences:true,statistics:true,marketing:true,method:%27explicit%27,ver:1,utc:1672331618201,region:%27de%27}",
        "www.gog.com",
    )
    session.navigate("https://www.gog.com/en", wait=4.0)

    user = str(
        session.eval_js(
            "(() => { const el = document.querySelector('#menuUsername, [hook-test=\"menuAccountButton\"]'); return el ? el.textContent.trim() : ''; })()"
        )
        or ""
    ).strip()

    if not user:
        # Attempt login
        session.eval_js(
            "(() => { const b = document.querySelector('a[hook-test=\"menuLogin\"], .menu-anonymous-header__btn, a[href*=\"login\"]'); if (b) b.click(); })()"
        )
        time.sleep(2.5)
        login_url = session.eval_js(
            "(() => { const f = document.querySelector('iframe[src*=\"login.gog.com\"]'); return f ? f.src : ''; })()"
        )
        if login_url:
            session.navigate(str(login_url), wait=3.0)

        filled = session.eval_js(
            f"""(() => {{
                const u = document.querySelector('#login_username');
                const p = document.querySelector('#login_password');
                const btn = document.querySelector('#login_login');
                if (!u || !p || !btn) return false;
                u.value = {json.dumps(email)};
                u.dispatchEvent(new Event('input', {{ bubbles: true }}));
                p.value = {json.dumps(password)};
                p.dispatchEvent(new Event('input', {{ bubbles: true }}));
                btn.click();
                return true;
            }})()"""
        )
        if filled:
            time.sleep(4.0)

        txt_low = session.page_text().lower()
        if "captcha" in txt_low or session.eval_js("Boolean(document.querySelector('iframe[src*=\"recaptcha\"], iframe[src*=\"hcaptcha\"]'))"):
            return {
                "status": "needs_captcha",
                "claimed": [],
                "already_claimed": [],
                "message": "GOG requested a CAPTCHA during login. Please wait or sign in manually.",
                "user": email,
                "engine": "native-chromium-cdp",
            }

        needs_2fa = bool(
            session.eval_js("Boolean(document.querySelector('#second_step_authentication_token_letter_1'))")
        ) or ("security code" in txt_low or "two-step" in txt_low)
        if needs_2fa:
            code = consume_pending_2fa("gog", base_dir=base_dir)
            if not code:
                return {
                    "status": "needs_2fa",
                    "claimed": [],
                    "already_claimed": [],
                    "message": "GOG emailed a 4-digit security code — enter it in the Prime & GOG tab and click Verify & Claim.",
                    "user": email,
                    "engine": "native-chromium-cdp",
                }
            session.eval_js(
                f"""(() => {{
                    const digits = {json.dumps(list(code[:4]))};
                    for (let i = 0; i < digits.length; i++) {{
                        const inp = document.querySelector('#second_step_authentication_token_letter_' + (i + 1));
                        if (inp) {{
                            inp.value = digits[i];
                            inp.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        }}
                    }}
                    const btn = document.querySelector('#second_step_authentication_send, button[type="submit"]');
                    if (btn) btn.click();
                }})()"""
            )
            time.sleep(4.0)

        session.navigate("https://www.gog.com/en", wait=3.5)
        user = str(
            session.eval_js(
                "(() => { const el = document.querySelector('#menuUsername, [hook-test=\"menuAccountButton\"]'); return el ? el.textContent.trim() : ''; })()"
            )
            or ""
        ).strip()

    if not user:
        return {
            "status": "needs_login",
            "claimed": [],
            "already_claimed": [],
            "message": "Could not verify GOG session. Check credentials or retry.",
            "user": email,
            "engine": "native-chromium-cdp",
        }

    banner_info = session.eval_js(
        """(() => {
            const b = document.querySelector('#giveaway');
            if (!b) return null;
            const hdr = b.querySelector('.giveaway__content-header');
            const text = hdr ? hdr.innerText : b.innerText;
            const a = b.querySelector('a');
            return { text: text || '', url: a ? a.href : '' };
        })()"""
    )

    if not banner_info:
        return {
            "status": "ok",
            "claimed": [],
            "already_claimed": [],
            "message": "No active GOG giveaway right now.",
            "user": user,
            "engine": "native-chromium-cdp",
        }

    raw_text = str(banner_info.get("text") or "")
    m = re.search(r"Claim (.*) and don't miss the|Success! (.*) was added to", raw_text)
    title = (m.group(1) or m.group(2)).strip() if m else raw_text.splitlines()[0].strip()[:80]
    giveaway_url = str(banner_info.get("url") or "https://www.gog.com/en")

    claim_resp = session.eval_js(
        "fetch('https://www.gog.com/giveaway/claim', { credentials: 'include' }).then(r => r.text()).catch(() => '')",
        await_promise=True,
    )
    resp_text = str(claim_resp or "").strip()
    if resp_text == "{}":
        print(f"[GOG] Claimed: {title}")
        record_fgc_db_entry("gog", user, title, "claimed", giveaway_url, base_dir=base_dir)
        if env.get("GOG_NEWSLETTER", "0") != "1":
            try:
                session.navigate("https://www.gog.com/en/account/settings/subscriptions", wait=2.5)
                session.eval_js(
                    "document.querySelectorAll('input[type=\"checkbox\"]:checked').forEach(c => { if (c.closest('li') && /promotions|marketing/i.test(c.closest('li').innerText)) c.click(); })"
                )
            except Exception:
                pass
        return {
            "status": "claimed",
            "claimed": [title],
            "already_claimed": [],
            "message": f"Claimed '{title}' on GOG!",
            "user": user,
            "engine": "native-chromium-cdp",
        }

    try:
        parsed = json.loads(resp_text) if resp_text else {}
    except Exception:
        parsed = {}
    msg = str(parsed.get("message") or resp_text)
    if msg == "Already claimed":
        print(f"[GOG] Already claimed: {title}")
        record_fgc_db_entry("gog", user, title, "existed", giveaway_url, base_dir=base_dir)
        return {
            "status": "ok",
            "claimed": [],
            "already_claimed": [title],
            "message": f"'{title}' was already in your GOG library.",
            "user": user,
            "engine": "native-chromium-cdp",
        }
    if msg == "Unauthorized":
        return {
            "status": "needs_login",
            "claimed": [],
            "already_claimed": [],
            "message": "GOG session expired.",
            "user": user,
            "engine": "native-chromium-cdp",
        }
    return {
        "status": "ok",
        "claimed": [],
        "already_claimed": [],
        "message": msg or "No active GOG giveaway right now.",
        "user": user,
        "engine": "native-chromium-cdp",
    }


def claim_prime_gaming(session: _ChromiumCDPSession, env: Dict[str, str], base_dir: str = BASE_DIR) -> Dict[str, Any]:
    email = (env.get("PG_EMAIL") or "").strip()
    password = (env.get("PG_PASSWORD") or "").strip()
    otp_secret = (env.get("PG_OTP_SECRET") or env.get("PG_OTPKEY") or "").strip()
    if not email or not password:
        return {
            "status": "unconfigured",
            "claimed": [],
            "already_claimed": [],
            "message": "Add PG_EMAIL and PG_PASSWORD in Accounts tab to enable auto-claiming.",
            "user": "",
            "engine": "native-chromium-cdp",
        }

    session.navigate("https://gaming.amazon.com/home", wait=4.0)

    def _is_signed_in() -> bool:
        return bool(
            session.eval_js(
                "Boolean(document.querySelector('[data-a-target=\"user-dropdown-first-name-text\"]')) || !Array.from(document.querySelectorAll('button')).some(b => /sign in/i.test(b.innerText))"
            )
        )

    if not _is_signed_in():
        session.eval_js(
            "(() => { const b = Array.from(document.querySelectorAll('button, a')).find(el => /sign in/i.test(el.innerText)); if (b) b.click(); })()"
        )
        time.sleep(3.0)
        session.eval_js(
            f"""(() => {{
                const e = document.querySelector('[name="email"]');
                const s = document.querySelector('input[type="submit"], #continue');
                if (e) {{
                    e.value = {json.dumps(email)};
                    e.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    if (s) s.click();
                }}
            }})()"""
        )
        time.sleep(2.5)
        session.eval_js(
            f"""(() => {{
                const p = document.querySelector('[name="password"]');
                const s = document.querySelector('input[type="submit"], #signInSubmit');
                if (p) {{
                    p.value = {json.dumps(password)};
                    p.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    if (s) s.click();
                }}
            }})()"""
        )
        time.sleep(4.0)

        cur_url = session.current_url().lower()
        txt_low = session.page_text().lower()
        if "/ap/cvf/" in cur_url or "enter the characters" in txt_low or "puzzle" in txt_low:
            return {
                "status": "needs_captcha",
                "claimed": [],
                "already_claimed": [],
                "message": "Amazon requested a CAPTCHA/puzzle during login.",
                "user": email,
                "engine": "native-chromium-cdp",
            }
        if "approve the notification" in txt_low:
            return {
                "status": "needs_2fa",
                "claimed": [],
                "already_claimed": [],
                "message": "Amazon sent a sign-in approval notification — approve it on your phone, then retry.",
                "user": email,
                "engine": "native-chromium-cdp",
            }
        if "/ap/mfa" in cur_url or session.eval_js("Boolean(document.querySelector('input[name=\"otpCode\"]'))"):
            code = None
            if otp_secret:
                try:
                    code = generate_totp(otp_secret)
                except Exception:
                    code = None
            if not code:
                code = consume_pending_2fa("amazon", base_dir=base_dir)
            if not code:
                return {
                    "status": "needs_2fa",
                    "claimed": [],
                    "already_claimed": [],
                    "message": "Amazon Prime requested a 2FA code — set PG_OTP_SECRET in Accounts or enter the one-time code in Prime & GOG tab.",
                    "user": email,
                    "engine": "native-chromium-cdp",
                }
            session.eval_js(
                f"""(() => {{
                    const rem = document.querySelector('[name="rememberDevice"]');
                    if (rem && !rem.checked) rem.click();
                    const inp = document.querySelector('input[name="otpCode"]');
                    const btn = document.querySelector('input[type="submit"], #auth-signin-button');
                    if (inp) {{
                        inp.value = {json.dumps(code)};
                        inp.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        if (btn) btn.click();
                    }}
                }})()"""
            )
            time.sleep(4.0)

    user = str(
        session.eval_js(
            "(() => { const el = document.querySelector('[data-a-target=\"user-dropdown-first-name-text\"]'); return el ? el.innerText.trim() : ''; })()"
        )
        or email
    ).strip()

    if session.eval_js("Array.from(document.querySelectorAll('button')).some(b => /try prime/i.test(b.innerText))"):
        return {
            "status": "error",
            "claimed": [],
            "already_claimed": [],
            "message": "Account does not appear to have an active Amazon Prime membership.",
            "user": user,
            "engine": "native-chromium-cdp",
        }

    session.eval_js(
        "(() => { const b = document.querySelector('button[data-type=\"Game\"]') || Array.from(document.querySelectorAll('button, a')).find(el => /^games$/i.test(el.innerText.trim())); if (b) b.click(); })()"
    )
    time.sleep(2.5)
    for _ in range(4):
        session.eval_js("window.scrollBy(0, window.innerHeight);")
        time.sleep(0.8)

    cards = session.eval_js(
        """(() => {
            const out = [];
            document.querySelectorAll('[data-a-target="offer-list-FGWP_FULL"] .item-card, .item-card').forEach(c => {
                const tEl = c.querySelector('.item-card-details__body__primary, h3');
                const title = tEl ? tEl.innerText.trim() : '';
                if (!title) return;
                const collected = /collected|claimed/i.test(c.innerText);
                const btn = c.querySelector('button, a');
                const a = c.querySelector('a');
                out.push({
                    title,
                    collected,
                    href: a ? a.href : '',
                    btnText: btn ? btn.innerText.trim() : ''
                });
            });
            return out.slice(0, 25);
        })()"""
    ) or []

    claimed: List[str] = []
    already_claimed: List[str] = []
    for card in cards:
        title = str(card.get("title") or "").strip()
        if not title:
            continue
        if card.get("collected"):
            already_claimed.append(title)
            record_fgc_db_entry("amazon", user, title, "existed", str(card.get("href") or ""), base_dir=base_dir)
            continue
        href = str(card.get("href") or "")
        if href:
            session.navigate(href, wait=2.5)
            did_claim = bool(
                session.eval_js(
                    """(() => {
                        const needLink = /link game account|link account/i.test(document.body.innerText || '');
                        if (needLink) return false;
                        const b = Array.from(document.querySelectorAll('button')).find(el => /get game|claim game|complete claim|^claim$/i.test(el.innerText.trim()));
                        if (b) { b.click(); return true; }
                        return false;
                    })()"""
                )
            )
            if did_claim:
                time.sleep(2.0)
                print(f"[AMAZON] Claimed: {title}")
                claimed.append(title)
                record_fgc_db_entry("amazon", user, title, "claimed", href, base_dir=base_dir)

    return {
        "status": "claimed" if claimed else "ok",
        "claimed": claimed,
        "already_claimed": already_claimed,
        "message": f"Claimed {len(claimed)} Prime Gaming item(s)." if claimed else "Checked Prime Gaming; no new unclaimed games.",
        "user": user,
        "engine": "native-chromium-cdp",
    }


def claim_epic(
    session: _ChromiumCDPSession,
    env: Dict[str, str],
    base_dir: str = BASE_DIR,
    force: bool = False,
) -> Dict[str, Any]:
    email = (env.get("EG_EMAIL") or "").strip()
    password = (env.get("EG_PASSWORD") or "").strip()
    if not email or not password:
        return {
            "status": "unconfigured",
            "claimed": [],
            "already_claimed": [],
            "message": "Add EG_EMAIL and EG_PASSWORD in Accounts tab to enable auto-claiming.",
            "user": "",
            "engine": "native-chromium-cdp",
        }

    active_free, _ = fetch_epic_freebies()
    already_set = {t.lower().strip() for t in collect_claimed_records(base_dir=base_dir).get("epic", [])}
    unclaimed = [g for g in active_free if str(g.get("title") or "").lower().strip() not in already_set]
    if not unclaimed and not force:
        return {
            "status": "ok",
            "claimed": [],
            "already_claimed": [g.get("title") for g in active_free if g.get("title")],
            "message": "All active Epic Games freebies are already claimed.",
            "user": email,
            "engine": "native-chromium-cdp",
        }

    session.set_cookie("OptanonAlertBoxClosed", _utc_iso(), ".epicgames.com")
    session.set_cookie("HasAcceptedAgeGates", "USK:9007199254740991,general:18,EPIC SUGGESTED RATING:18", "store.epicgames.com")
    session.navigate("https://store.epicgames.com/en-US/free-games", wait=4.0)

    logged_in = bool(
        session.eval_js("document.querySelector('egs-navigation')?.getAttribute('isloggedin') === 'true'")
    )
    if not logged_in:
        session.navigate("https://www.epicgames.com/id/login/epic", wait=3.5)
        session.eval_js(
            f"""(() => {{
                const e = document.querySelector('#email');
                const p = document.querySelector('#password');
                const b = document.querySelector('#sign-in');
                if (e && p) {{
                    e.value = {json.dumps(email)};
                    e.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    p.value = {json.dumps(password)};
                    p.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    if (b) b.click();
                }}
            }})()"""
        )
        time.sleep(4.0)
        txt_low = session.page_text().lower()
        if "captcha" in txt_low or session.eval_js("Boolean(document.querySelector('iframe[src*=\"hcaptcha\"], iframe[src*=\"turnstile\"], iframe[src*=\"arkose\"]'))"):
            return {
                "status": "needs_captcha",
                "claimed": [],
                "already_claimed": [],
                "message": "Epic Games requested a CAPTCHA during login.",
                "user": email,
                "engine": "native-chromium-cdp",
            }
        if "security code" in txt_low or session.eval_js("Boolean(document.querySelector('input[name=\"code\"]'))"):
            otp_secret = (env.get("EG_OTP_SECRET") or env.get("EG_OTPKEY") or "").strip()
            code = generate_totp(otp_secret) if otp_secret else consume_pending_2fa("epic", base_dir=base_dir)
            if not code:
                return {
                    "status": "needs_2fa",
                    "claimed": [],
                    "already_claimed": [],
                    "message": "Epic Games requested a 2FA security code.",
                    "user": email,
                    "engine": "native-chromium-cdp",
                }
            session.eval_js(
                f"""(() => {{
                    const inp = document.querySelector('input[name="code"]');
                    const btn = document.querySelector('#continue, button[type="submit"]');
                    if (inp) {{
                        inp.value = {json.dumps(code)};
                        inp.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        if (btn) btn.click();
                    }}
                }})()"""
            )
            time.sleep(4.0)

    claimed: List[str] = []
    already_claimed: List[str] = []
    for game in (active_free if force else unclaimed):
        title = str(game.get("title") or "").strip()
        if not title:
            continue
        search_url = f"https://store.epicgames.com/en-US/browse?q={urllib.request.quote(title)}"
        session.navigate(search_url, wait=3.0)
        in_lib = bool(session.eval_js("/in library|owned/i.test(document.body.innerText || '')"))
        if in_lib:
            already_claimed.append(title)
            record_fgc_db_entry("epic", email, title, "existed", search_url, base_dir=base_dir)

    return {
        "status": "claimed" if claimed else "ok",
        "claimed": claimed,
        "already_claimed": already_claimed,
        "message": f"Claimed {len(claimed)} Epic freebie(s)." if claimed else "Checked Epic Games Store.",
        "user": email,
        "engine": "native-chromium-cdp",
    }


def run_store_claims(
    target_store: str = "all",
    env: Optional[Dict[str, str]] = None,
    force: bool = False,
    base_dir: str = BASE_DIR,
) -> Dict[str, Any]:
    env_map = dict(os.environ)
    if env:
        env_map.update(env)

    norm = _normalize_store(target_store)
    stores_to_run = ["epic", "amazon", "gog"] if norm == "all" else [norm]

    status_doc = load_store_claimer_status(base_dir=base_dir)
    status_doc["running"] = True
    status_doc["engine"] = "native-chromium-cdp"
    save_store_claimer_status(status_doc, base_dir=base_dir)

    chromium_bin = find_chromium_binary()
    if not chromium_bin:
        now_iso = _utc_iso()
        status_doc["running"] = False
        status_doc["last_run_at"] = now_iso
        for s in stores_to_run:
            status_doc["stores"][s].update(
                {
                    "status": "error",
                    "last_run_at": now_iso,
                    "message": "Chromium binary not found on host.",
                }
            )
        save_store_claimer_status(status_doc, base_dir=base_dir)
        return status_doc

    profile_dir = os.path.join(base_dir, "data", "fgc", "chromium_profile")
    webhook_url = (env_map.get("DISCORD_WEBHOOK_URL") or env_map.get("NOTIFY") or "").strip()

    try:
        with _ChromiumCDPSession(chromium_bin, profile_dir) as session:
            for s in stores_to_run:
                now_iso = _utc_iso()
                try:
                    if s == "epic":
                        res = claim_epic(session, env_map, base_dir=base_dir, force=force)
                    elif s == "amazon":
                        res = claim_prime_gaming(session, env_map, base_dir=base_dir)
                    else:
                        res = claim_gog(session, env_map, base_dir=base_dir)
                except Exception as err:
                    res = {
                        "status": "error",
                        "claimed": [],
                        "already_claimed": [],
                        "message": str(err),
                        "user": "",
                        "engine": "native-chromium-cdp",
                    }
                status_doc["stores"][s]["status"] = res.get("status", "ok")
                status_doc["stores"][s]["last_run_at"] = now_iso
                status_doc["stores"][s]["claimed"] = res.get("claimed", [])
                status_doc["stores"][s]["message"] = res.get("message", "")
                if res.get("status") in ("ok", "claimed"):
                    status_doc["stores"][s]["last_success_at"] = now_iso
                if res.get("claimed"):
                    send_discord_claim_alert(webhook_url, s.upper(), res["claimed"])
                save_store_claimer_status(status_doc, base_dir=base_dir)
    finally:
        now_iso = _utc_iso()
        interval_hours = max(1, min(48, int(env_map.get("FREE_GAMES_INTERVAL_HOURS") or 6)))
        status_doc["running"] = False
        status_doc["last_run_at"] = now_iso
        status_doc["next_scheduled_at"] = _utc_iso(time.time() + interval_hours * 3600)
        save_store_claimer_status(status_doc, base_dir=base_dir)
        try:
            sync_from_sources(base_dir=base_dir)
        except Exception:
            pass

    return status_doc


def main() -> int:
    parser = argparse.ArgumentParser(description="Native Headless Chromium CDP Store Auto-Claimer")
    parser.add_argument("--store", default="all", help="Store to check: all, epic, amazon/prime, gog")
    parser.add_argument("--force", action="store_true", help="Force full store check even if cached")
    args = parser.parse_args()
    run_store_claims(target_store=args.store, force=args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
