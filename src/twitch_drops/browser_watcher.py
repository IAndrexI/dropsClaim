"""
Lightweight Headless Chromium Watcher for Twitch Drops (Pure Python Standard Library + CDP).

When Chromium is installed on the host (e.g., Alpine/Debian LXC containers), this module
launches a single muted, 160p headless Chromium session via Chrome DevTools Protocol (CDP),
injects the user's Twitch session cookies/localStorage, opens https://www.twitch.tv/<channel>,
clicks through any mature/content-classification gates, and monitors live <video> playback.
Runs alongside or reinforces API heartbeats so Twitch sees an authentic open browser tab.
"""

import base64
import hashlib
import json
import logging
import os
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from .activity_logger import log_event

logger = logging.getLogger("browser_watcher")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(BASE_DIR, "data")
PROFILE_DIR = os.path.join(DATA_DIR, "twitch_browser_profile")


def find_chromium_binary() -> Optional[str]:
    """Locate a Chromium / Chrome executable on the host, or return None if unavailable."""
    env_override = os.environ.get("TWITCH_HEADLESS_BROWSER", "").strip().lower()
    if env_override in ("0", "false", "no", "off", "disabled"):
        return None
    if "unittest" in sys.modules and env_override not in ("1", "true", "force"):
        return None

    candidates = [
        os.environ.get("CHROMIUM_PATH", "").strip(),
        os.environ.get("PUPPETEER_EXECUTABLE_PATH", "").strip(),
        shutil.which("chromium-browser"),
        shutil.which("chromium"),
        shutil.which("google-chrome"),
        shutil.which("google-chrome-stable"),
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
        "/usr/bin/google-chrome",
    ]
    for path in candidates:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    if os.name == "nt":
        for win_path in (
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ):
            if os.path.isfile(win_path):
                return win_path
    return None


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _CDPWebSocket:
    """Minimal RFC 6455 stdlib WebSocket client for Chrome DevTools Protocol on 127.0.0.1."""

    def __init__(self, ws_url: str, timeout: float = 8.0):
        parsed = urllib.parse.urlparse(ws_url)
        host = parsed.hostname or "127.0.0.1"
        port = int(parsed.port or 80)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self._buf = bytearray()
        self._next_id = 1

        sec_key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {sec_key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        self.sock.sendall(req)

        while b"\r\n\r\n" not in self._buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("CDP WebSocket handshake closed")
            self._buf.extend(chunk)

        hdr_end = self._buf.index(b"\r\n\r\n") + 4
        hdr_bytes = bytes(self._buf[:hdr_end])
        del self._buf[:hdr_end]
        if b"101 " not in hdr_bytes.split(b"\r\n", 1)[0]:
            raise ConnectionError(f"Unexpected CDP handshake response: {hdr_bytes[:120]!r}")

        expected_accept = base64.b64encode(
            hashlib.sha1((sec_key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
        ).decode("ascii")
        if expected_accept.encode("ascii") not in hdr_bytes:
            logger.debug("CDP Sec-WebSocket-Accept header check skipped")

    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(max(4096, n - len(self._buf)))
            if not chunk:
                raise ConnectionError("CDP WebSocket closed")
            self._buf.extend(chunk)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        length = len(payload)
        header = bytearray([0x80 | (opcode & 0x0F)])
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def _recv_message(self) -> Optional[str]:
        fragments: List[bytes] = []
        while True:
            b1, b2 = self._read_exact(2)
            fin = (b1 & 0x80) != 0
            opcode = b1 & 0x0F
            masked = (b2 & 0x80) != 0
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8))[0]
            mask_key = self._read_exact(4) if masked else b""
            data = self._read_exact(length) if length else b""
            if masked:
                data = bytes(b ^ mask_key[i % 4] for i, b in enumerate(data))

            if opcode == 0x8:  # close
                return None
            if opcode == 0x9:  # ping -> pong
                self._send_frame(0xA, data)
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode in (0x1, 0x0):
                fragments.append(data)
                if fin:
                    return b"".join(fragments).decode("utf-8", errors="ignore")

    def call(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: float = 8.0) -> Dict[str, Any]:
        req_id = self._next_id
        self._next_id += 1
        self.sock.settimeout(timeout)
        msg = json.dumps({"id": req_id, "method": method, "params": params or {}})
        self._send_frame(0x1, msg.encode("utf-8"))

        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = self._recv_message()
            if raw is None:
                raise ConnectionError("CDP closed waiting for reply")
            try:
                resp = json.loads(raw)
            except Exception:
                continue
            if resp.get("id") == req_id:
                if "error" in resp:
                    raise RuntimeError(f"CDP {method} error: {resp['error']}")
                return resp.get("result") or {}
        raise TimeoutError(f"CDP timeout waiting for {method}")

    def close(self) -> None:
        try:
            self._send_frame(0x8, b"")
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


_PLAYER_MAINTAIN_JS = r"""
(() => {
  const res = {
    href: String(location.href || ""),
    videoFound: false,
    paused: null,
    readyState: 0,
    currentTime: 0,
    muted: null,
    width: 0,
    height: 0,
    gateClicked: [],
    loggedInUser: ""
  };
  try {
    const m = document.cookie.match(/(?:^|;\s*)twilight-user=([^;]+)/);
    if (m && m[1]) {
      const parsed = JSON.parse(decodeURIComponent(m[1]));
      if (parsed && parsed.login) res.loggedInUser = String(parsed.login);
    }
  } catch (e) {}

  try {
    if (document.hidden) {
      Object.defineProperty(document, 'hidden', { get: () => false, configurable: true });
      Object.defineProperty(document, 'visibilityState', { get: () => 'visible', configurable: true });
      window.dispatchEvent(new Event('visibilitychange'));
    }
    localStorage.setItem("mature", "true");
    localStorage.setItem("video-muted", JSON.stringify({ default: true }));
    localStorage.setItem("video-quality", JSON.stringify({ default: "160p30" }));
    localStorage.setItem("volume", "0");
    localStorage.setItem("muted", "true");
    localStorage.setItem("low-latency", "false");
  } catch (e) {}

  const selectors = [
    '#onetrust-accept-btn-handler',
    '#accept-recommended-btn-handler',
    '#onetrust-banner-sdk button',
    '.onetrust-close-btn-handler',
    '[data-a-target="content-classification-gate-overlay-start-watching-button"]',
    '[data-test-selector="content-classification-gate-overlay-start-watching-button"]',
    '[data-a-target="player-overlay-mature-accept"]',
    'button[data-a-target="consent-banner-accept"]',
    '#consent-banner button',
    '.consent-banner button',
    '[data-a-target="player-overlay-content-gate"] button',
    '[data-test-selector="cookie-banner"] button',
    'button[data-testid="accept-cookies"]',
    '[data-test-selector="community-points-summary-claim-button"]',
    'button[aria-label="Claim Bonus"]',
    '[data-test-selector="DropsCampaignInProgressDescription-claim-button"]'
  ];
  for (const sel of selectors) {
    const btn = document.querySelector(sel);
    if (btn && typeof btn.click === "function") {
      try {
        btn.click();
        res.gateClicked.push(sel);
      } catch (e) {}
    }
  }
  if (!res.gateClicked.length) {
    for (const btn of document.querySelectorAll("button, div[role=\"button\"]")) {
      const txt = (btn.innerText || btn.textContent || "").trim().toLowerCase();
      if (txt === "start watching" || txt === "i am 18+" || txt === "accept" || txt === "agree" || txt === "accept cookies" || txt === "allow all" || txt === "claim" || txt === "claim now") {
        try {
          btn.click();
          res.gateClicked.push("button:" + txt);
        } catch (e) {}
      }
    }
  }

  const videos = Array.from(document.querySelectorAll("video"));
  const video = videos.find(v => (v.src || v.srcObject || v.readyState > 0)) || videos[0];
  if (video) {
    res.videoFound = true;
    try { video.muted = true; } catch (e) {}
    try { video.volume = 0; } catch (e) {}
    try {
      if (video.paused) {
        const p = video.play();
        if (p && typeof p.catch === "function") p.catch(() => {});
      }
    } catch (e) {}
    const playBtn = document.querySelector('[data-a-target="player-play-pause-button"]');
    if (playBtn && video.paused) {
      try { playBtn.click(); } catch (e) {}
    }
    res.paused = Boolean(video.paused);
    res.readyState = Number(video.readyState || 0);
    res.currentTime = Math.round(Number(video.currentTime || 0) * 10) / 10;
    res.muted = Boolean(video.muted);
    res.width = Number(video.videoWidth || 0);
    res.height = Number(video.videoHeight || 0);
    if (video.error) {
      res.videoError = { code: Number(video.error.code || 0), message: String(video.error.message || "") };
    }
  } else {
    res.videoError = null;
    const iframes = Array.from(document.querySelectorAll("iframe"));
    if (iframes.length) res.iframeCount = iframes.length;
  }
  return res;
})()
"""


class HeadlessBrowserWatcher:
    """
    Manages a single Chromium headless tab watching the miner's active Twitch channel.
    Falls back cleanly to no-op if Chromium is not installed.
    """

    def __init__(self, profile_dir: Optional[str] = None):
        self.profile_dir = profile_dir or PROFILE_DIR
        self.chromium_bin: Optional[str] = find_chromium_binary()
        self.proc: Optional[subprocess.Popen] = None
        self.port: Optional[int] = None
        self.cdp: Optional[_CDPWebSocket] = None
        self.current_channel: str = ""
        self.last_state: Dict[str, Any] = {"available": bool(self.chromium_bin), "running": False}
        self._init_script_added: bool = False
        self._consecutive_tick_errors: int = 0

    def is_available(self) -> bool:
        self.chromium_bin = find_chromium_binary()
        return bool(self.chromium_bin)

    def _is_proc_alive(self) -> bool:
        return bool(self.proc is not None and self.proc.poll() is None)

    def _ensure_browser_started(self, auth_token: str = "", device_id: str = "") -> bool:
        if not self.is_available():
            self.last_state = {"available": False, "running": False, "reason": "chromium_not_found"}
            return False

        if self._is_proc_alive() and self.cdp is not None:
            return True

        self.stop()
        os.makedirs(self.profile_dir, exist_ok=True)
        lock_file = os.path.join(self.profile_dir, "SingletonLock")
        if os.path.exists(lock_file):
            try:
                os.remove(lock_file)
            except OSError:
                pass

        self.port = _pick_free_port()
        user_agent_str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
        cmd = [
            self.chromium_bin,
            "--headless=new",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--mute-audio",
            "--autoplay-policy=no-user-gesture-required",
            "--disable-blink-features=AutomationControlled",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--window-size=1280,720",
            f"--user-agent={user_agent_str}",
            f"--remote-debugging-port={self.port}",
            "--remote-allow-origins=*",
            f"--user-data-dir={self.profile_dir}",
            "about:blank",
        ]
        try:
            self.proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                start_new_session=(os.name != "nt"),
            )
            log_event(
                "BROWSER_WATCHER",
                "chromium_spawned",
                {"pid": self.proc.pid, "port": self.port, "binary": os.path.basename(self.chromium_bin)},
            )
        except Exception as err:
            logger.warning("[BROWSER] Failed to launch Chromium: %s", err)
            log_event("BROWSER_WATCHER", "chromium_spawn_failed", {"error": str(err)}, level="WARNING")
            self.last_state = {"available": True, "running": False, "error": str(err)}
            self.proc = None
            return False

        # Wait up to 10s for CDP HTTP /json target endpoint
        ws_url = ""
        deadline = time.time() + 10.0
        while time.time() < deadline:
            if not self._is_proc_alive():
                stderr_tail = ""
                try:
                    if self.proc and self.proc.stderr:
                        stderr_tail = self.proc.stderr.read().decode("utf-8", errors="ignore")[-300:]
                except Exception:
                    pass
                log_event("BROWSER_WATCHER", "chromium_exited_early", {"stderr": stderr_tail}, level="WARNING")
                self.proc = None
                self.last_state = {"available": True, "running": False, "error": stderr_tail or "exited_early"}
                return False
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{self.port}/json")
                with urllib.request.urlopen(req, timeout=1.5) as resp:
                    targets = json.loads(resp.read().decode("utf-8"))
                for t in targets:
                    if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                        ws_url = t["webSocketDebuggerUrl"]
                        break
                if ws_url:
                    break
            except Exception:
                time.sleep(0.25)

        if not ws_url:
            log_event("BROWSER_WATCHER", "cdp_connect_timeout", {"port": self.port}, level="WARNING")
            self.stop()
            return False

        try:
            self.cdp = _CDPWebSocket(ws_url, timeout=8.0)
            self.cdp.call("Network.enable")
            self.cdp.call("Page.enable")
            try:
                self.cdp.call("Emulation.setFocusEmulationEnabled", {"enabled": True})
            except Exception:
                pass
            try:
                self.cdp.call("Page.setWebLifecycleState", {"state": "active"})
            except Exception:
                pass
            self._configure_session(auth_token=auth_token, device_id=device_id)
            return True
        except Exception as err:
            log_event("BROWSER_WATCHER", "cdp_init_error", {"error": str(err)}, level="WARNING")
            self.stop()
            return False

    def _configure_session(
        self,
        auth_token: str = "",
        device_id: str = "",
        user_login: str = "",
        user_id: str = "",
    ) -> None:
        if not self.cdp:
            return
        cookies_to_set: List[Dict[str, Any]] = [
            {
                "name": "mature",
                "value": "true",
                "domain": ".twitch.tv",
                "path": "/",
                "secure": True,
                "url": "https://www.twitch.tv",
            },
            {
                "name": "twitch.lohp.countryCode",
                "value": "US",
                "domain": ".twitch.tv",
                "path": "/",
                "secure": True,
                "url": "https://www.twitch.tv",
            },
        ]
        if device_id:
            cookies_to_set.append({
                "name": "unique_id",
                "value": str(device_id),
                "domain": ".twitch.tv",
                "path": "/",
                "secure": True,
                "url": "https://www.twitch.tv",
            })
        if auth_token:
            cookies_to_set.append({
                "name": "auth-token",
                "value": str(auth_token),
                "domain": ".twitch.tv",
                "path": "/",
                "secure": True,
                "httpOnly": False,
                "sameSite": "Lax",
                "url": "https://www.twitch.tv",
            })
        if user_login:
            cookies_to_set.append({
                "name": "login",
                "value": str(user_login),
                "domain": ".twitch.tv",
                "path": "/",
                "secure": True,
                "url": "https://www.twitch.tv",
            })
            cookies_to_set.append({
                "name": "name",
                "value": str(user_login),
                "domain": ".twitch.tv",
                "path": "/",
                "secure": True,
                "url": "https://www.twitch.tv",
            })
        if user_id:
            cookies_to_set.append({
                "name": "user_id",
                "value": str(user_id),
                "domain": ".twitch.tv",
                "path": "/",
                "secure": True,
                "url": "https://www.twitch.tv",
            })
        if user_id and user_login:
            twilight_user_val = urllib.parse.quote(json.dumps({
                "id": str(user_id),
                "login": str(user_login),
                "displayName": str(user_login),
            }))
            cookies_to_set.append({
                "name": "twilight-user",
                "value": twilight_user_val,
                "domain": ".twitch.tv",
                "path": "/",
                "secure": True,
                "url": "https://www.twitch.tv",
            })

        set_ok = 0
        for ck in cookies_to_set:
            try:
                res = self.cdp.call("Network.setCookie", ck)
                if res.get("success"):
                    set_ok += 1
            except Exception as err:
                logger.debug("Network.setCookie failed for %s: %s", ck["name"], err)

        if not self._init_script_added:
            init_js = """
            try {
              Object.defineProperty(navigator, 'webdriver', { get: () => undefined, configurable: true });
              if (window.navigator) {
                try { delete Object.getPrototypeOf(navigator).webdriver; } catch (e) {}
              }
              Object.defineProperty(document, 'visibilityState', { get: () => 'visible', configurable: true });
              Object.defineProperty(document, 'hidden', { get: () => false, configurable: true });
              localStorage.setItem("mature", "true");
              localStorage.setItem("video-muted", JSON.stringify({ default: true }));
              localStorage.setItem("video-quality", JSON.stringify({ default: "160p30" }));
              localStorage.setItem("volume", "0");
              localStorage.setItem("muted", "true");
              localStorage.setItem("low-latency", "false");
            } catch (e) {}
            """
            try:
                self.cdp.call("Page.addScriptToEvaluateOnNewDocument", {"source": init_js})
                self._init_script_added = True
            except Exception:
                pass

        log_event(
            "BROWSER_WATCHER",
            "session_cookies_configured",
            {"cookies_applied": set_ok, "has_auth_token": bool(auth_token), "logged_in_user": user_login},
        )

    def watch_channel(
        self,
        channel_login: str,
        auth_token: str = "",
        device_id: str = "",
        user_login: str = "",
        user_id: str = "",
    ) -> Dict[str, Any]:
        """Open or navigate the headless browser tab to https://www.twitch.tv/<channel_login>."""
        clean_login = str(channel_login or "").strip().lower()
        if not clean_login:
            return {"available": self.is_available(), "running": False}

        if not self._ensure_browser_started(auth_token=auth_token, device_id=device_id):
            return dict(self.last_state)

        target_url = f"https://www.twitch.tv/{clean_login}"
        try:
            self._configure_session(
                auth_token=auth_token,
                device_id=device_id,
                user_login=user_login,
                user_id=user_id,
            )
            nav_res = self.cdp.call("Page.navigate", {"url": target_url}, timeout=15.0)
            self.current_channel = clean_login
            # Poll every 2s (up to 30s) for Twitch SPA DOM to render and <video> to mount
            state = {}
            for _w in range(15):
                time.sleep(2.0)
                state = self.tick(
                    clean_login,
                    auth_token=auth_token,
                    device_id=device_id,
                    user_login=user_login,
                    user_id=user_id,
                )
                if state.get("video_found") and not state.get("paused"):
                    break

            state["frame_id"] = nav_res.get("frameId", "")
            logger.info(
                "[BROWSER] Headless Chromium opened %s (pid=%s, logged_in=%s, video=%s, time=%.1fs)",
                target_url,
                self.proc.pid if self.proc else "?",
                state.get("logged_in_user") or user_login or "cookie-pending",
                state.get("video_found", False),
                state.get("current_time", 0.0),
            )
            log_event(
                "BROWSER_WATCHER",
                "navigate_channel",
                {
                    "channel": clean_login,
                    "url": target_url,
                    "pid": self.proc.pid if self.proc else 0,
                    "video_found": state.get("video_found", False),
                    "logged_in_user": state.get("logged_in_user", "") or user_login,
                },
            )
            return state
        except Exception as err:
            log_event("BROWSER_WATCHER", "navigate_error", {"channel": clean_login, "error": str(err)}, level="WARNING")
            self.stop()
            self.last_state = {"available": True, "running": False, "error": str(err)}
            return dict(self.last_state)

    def tick(
        self,
        channel_login: str = "",
        auth_token: str = "",
        device_id: str = "",
        user_login: str = "",
        user_id: str = "",
    ) -> Dict[str, Any]:
        """Verify headless browser is open on the target channel, dismiss gates, and inspect <video> playback."""
        clean_login = str(channel_login or self.current_channel or "").strip().lower()
        if not self.is_available():
            self.last_state = {"available": False, "running": False}
            return dict(self.last_state)

        if not self._is_proc_alive() or self.cdp is None:
            if clean_login:
                return self.watch_channel(
                    clean_login,
                    auth_token=auth_token,
                    device_id=device_id,
                    user_login=user_login,
                    user_id=user_id,
                )
            return {"available": True, "running": False}

        try:
            eval_res = self.cdp.call(
                "Runtime.evaluate",
                {"expression": _PLAYER_MAINTAIN_JS, "returnByValue": True},
                timeout=15.0,
            )
            self._consecutive_tick_errors = 0
            val = ((eval_res.get("result") or {}).get("value")) or {}
            href = str(val.get("href") or "")
            if clean_login and (not href or clean_login not in href.lower() or "about:blank" in href.lower()):
                self.cdp.call("Page.navigate", {"url": f"https://www.twitch.tv/{clean_login}"}, timeout=12.0)
                self.current_channel = clean_login

            logged_in = str(val.get("loggedInUser") or "") or user_login
            state = {
                "available": True,
                "running": True,
                "pid": self.proc.pid if self.proc else 0,
                "channel_login": clean_login,
                "href": href or f"https://www.twitch.tv/{clean_login}",
                "video_found": bool(val.get("videoFound")),
                "paused": val.get("paused"),
                "ready_state": int(val.get("readyState") or 0),
                "current_time": float(val.get("currentTime") or 0.0),
                "muted": val.get("muted"),
                "resolution": f"{val.get('width', 0)}x{val.get('height', 0)}",
                "gate_clicked": val.get("gateClicked") or [],
                "logged_in_user": logged_in,
                "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            self.last_state = state
            log_event("BROWSER_WATCHER", "player_tick", state)
            return state
        except Exception as err:
            self._consecutive_tick_errors += 1
            log_event(
                "BROWSER_WATCHER",
                "player_tick_error",
                {"error": str(err), "consecutive_errors": self._consecutive_tick_errors},
                level="WARNING",
            )
            if self._consecutive_tick_errors >= 3:
                self.stop()
                self.last_state = {"available": True, "running": False, "error": str(err)}
            else:
                if isinstance(self.last_state, dict):
                    self.last_state["transient_error"] = str(err)
            return dict(self.last_state)

    def is_watching(self, channel_login: str = "") -> bool:
        if not self._is_proc_alive():
            return False
        if not channel_login:
            return True
        return self.current_channel.lower() == channel_login.strip().lower()

    def execute_gql_in_browser(
        self,
        operation_name: str,
        query_or_hash: str,
        variables: Optional[Dict[str, Any]] = None,
        is_persisted: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """
        Execute a GraphQL query/mutation directly inside the authenticated Chromium browser tab.
        Leverages the browser's live Twitch session, device context, and anti-bot verification.
        """
        if not self.is_available() or not self._is_proc_alive() or self.cdp is None:
            return None

        js_code = f"""
        (async () => {{
            try {{
                const opName = {json.dumps(operation_name)};
                const qOrHash = {json.dumps(query_or_hash)};
                const vars = {json.dumps(variables or {})};
                const isPersisted = {json.dumps(bool(is_persisted))};
                const body = isPersisted ? {{
                    operationName: opName,
                    extensions: {{ persistedQuery: {{ version: 1, sha256Hash: qOrHash }} }},
                    variables: vars
                }} : {{
                    operationName: opName,
                    query: qOrHash,
                    variables: vars
                }};
                const resp = await fetch('https://gql.twitch.tv/gql', {{
                    method: 'POST',
                    headers: {{
                        'Client-Id': 'kimne78kx3ncx6brgo4mv6wki5h1ko',
                        'Content-Type': 'application/json'
                    }},
                    body: JSON.stringify(body)
                }});
                return await resp.json();
            }} catch (err) {{
                return {{ error: String(err) }};
            }}
        }})()
        """
        try:
            eval_res = self.cdp.call(
                "Runtime.evaluate",
                {"expression": js_code, "awaitPromise": True, "returnByValue": True},
                timeout=12.0,
            )
            val = ((eval_res.get("result") or {}).get("value"))
            if isinstance(val, dict):
                return val
        except Exception as err:
            logger.debug("execute_gql_in_browser exception: %s", err)
        return None

    def stop(self) -> None:
        if self.cdp is not None:
            self.cdp.close()
            self.cdp = None
        if self.proc is not None:
            pid = self.proc.pid
            try:
                self.proc.terminate()
                self.proc.wait(timeout=3)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            log_event("BROWSER_WATCHER", "chromium_stopped", {"pid": pid})
            self.proc = None
        self.current_channel = ""
        self._init_script_added = False
        self.last_state = {"available": self.is_available(), "running": False}
