"""
Cloudflare WARP Auto-Claim Bridge for Windows & Desktop Browsers.
Automates turning Cloudflare WARP ON when clicking 'Claim Now' on Twitch,
then turns WARP back OFF automatically after the claim completes.

Usage:
  1. Background Server Mode (default):
     python scripts/warp_claim_helper.py
     (Listens on http://127.0.0.1:8765 for claim triggers from browser or dashboard)

  2. One-Shot Mode (CLI / Batch shortcut):
     python scripts/warp_claim_helper.py --claim --duration 8
"""

import argparse
import http.server
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from typing import Dict, Optional, Tuple

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("warp_helper")

DEFAULT_PORT = 8765
WARP_EXE_CANDIDATES = [
    r"C:\Program Files\Cloudflare\Cloudflare WARP\warp-cli.exe",
    r"C:\Program Files (x86)\Cloudflare\Cloudflare WARP\warp-cli.exe",
    "warp-cli.exe",
    "warp-cli",
]


def find_warp_cli() -> Optional[str]:
    """Locate the warp-cli binary on Windows or system PATH."""
    for cand in WARP_EXE_CANDIDATES:
        if os.path.isfile(cand):
            return cand
        resolved = shutil.which(cand)
        if resolved and os.path.isfile(resolved):
            return resolved
    return None


WARP_CLI = find_warp_cli()


def _run_warp_cmd(args: list) -> Tuple[int, str]:
    if not WARP_CLI:
        return 1, "warp-cli executable not found"
    try:
        proc = subprocess.run(
            [WARP_CLI] + args,
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return proc.returncode, (proc.stdout or proc.stderr).strip()
    except Exception as e:
        return 1, str(e)


def get_warp_status() -> Dict[str, str]:
    code, output = _run_warp_cmd(["status"])
    is_connected = "Status update: Connected" in output or "Connected" in output
    return {
        "connected": is_connected,
        "output": output,
        "cli_available": bool(WARP_CLI),
    }


def connect_warp(timeout: float = 6.0) -> bool:
    logger.info("[WARP] Connecting Cloudflare WARP...")
    _run_warp_cmd(["connect"])
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = get_warp_status()
        if st["connected"]:
            logger.info("[WARP] Successfully connected to Cloudflare WARP!")
            return True
        time.sleep(0.5)
    return False


def disconnect_warp() -> bool:
    logger.info("[WARP] Disconnecting Cloudflare WARP...")
    _run_warp_cmd(["disconnect"])
    time.sleep(0.5)
    st = get_warp_status()
    logger.info("[WARP] Disconnect complete (Connected=%s)", st["connected"])
    return not st["connected"]


_claim_lock = threading.Lock()


def execute_claim_session(duration_secs: float = 8.0) -> Dict[str, any]:
    """Turn WARP on, wait for the user/browser to execute claims, then disconnect."""
    with _claim_lock:
        logger.info("[WARP] Starting automated claim window (%s seconds)...", duration_secs)
        connected = connect_warp()
        if not connected:
            logger.warning("[WARP] Could not establish connection; proceeding with caution.")

        # Keep WARP active for the duration of the claim mutation
        time.sleep(duration_secs)

        # Disconnect WARP back to normal internet
        disconnected = disconnect_warp()
        return {
            "success": True,
            "connected_initially": connected,
            "disconnected_finally": disconnected,
            "duration": duration_secs,
            "message": f"Cloudflare WARP was active for {duration_secs}s and is now disconnected.",
        }


class WarpBridgeHandler(http.server.BaseHTTPRequestHandler):
    def _send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def do_OPTIONS(self):
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self):
        self._handle_requests()

    def do_POST(self):
        self._handle_requests()

    def _handle_requests(self):
        path = self.path.split("?")[0].rstrip("/")
        if path == "/status":
            st = get_warp_status()
            self._json_response(200, st)
            return

        elif path in ("/connect", "/on"):
            ok = connect_warp()
            self._json_response(200, {"success": ok, "connected": ok})
            return

        elif path in ("/disconnect", "/off"):
            ok = disconnect_warp()
            self._json_response(200, {"success": ok, "connected": not ok})
            return

        elif path in ("/claim", "/trigger"):
            # Query duration param
            duration = 8.0
            if "?" in self.path:
                try:
                    import urllib.parse
                    qs = urllib.parse.parse_qs(self.path.split("?", 1)[1])
                    if "duration" in qs:
                        duration = max(3.0, min(30.0, float(qs["duration"][0])))
                except Exception:
                    pass

            # Run in separate thread or synchronously
            result = execute_claim_session(duration_secs=duration)
            self._json_response(200, result)
            return

        self._json_response(404, {"error": "Not Found", "available_endpoints": ["/status", "/connect", "/disconnect", "/claim"]})

    def _json_response(self, code: int, data: dict):
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # Quiet regular access logs unless error
        if args and str(args[1]) != "200":
            super().log_message(format, *args)


def main():
    parser = argparse.ArgumentParser(description="Cloudflare WARP Auto-Claim Bridge")
    parser.add_argument("--claim", action="store_true", help="Connect WARP, wait duration, disconnect, and exit")
    parser.add_argument("--duration", type=float, default=8.0, help="Seconds to keep WARP connected for claim (default: 8.0)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"HTTP Server port (default: {DEFAULT_PORT})")
    args = parser.parse_args()

    if not WARP_CLI:
        print("[ERROR] Cloudflare warp-cli was not found on this machine!")
        print("Please ensure Cloudflare WARP is installed at C:\\Program Files\\Cloudflare\\Cloudflare WARP\\warp-cli.exe")
        sys.exit(1)

    if args.claim:
        print(f"[1/3] Connecting Cloudflare WARP for {args.duration}s claim window...")
        res = execute_claim_session(duration_secs=args.duration)
        print(f"[2/3] Claim window completed!")
        print(f"[3/3] Cloudflare WARP disconnected. Status: {res.get('message')}")
        return

    # Background Bridge Server Mode
    server_address = ("127.0.0.1", args.port)
    httpd = http.server.ThreadingHTTPServer(server_address, WarpBridgeHandler)
    logger.info("==========================================================")
    logger.info("   CLOUDFLARE WARP AUTO-CLAIM BRIDGE ACTIVE               ")
    logger.info("   Listening on http://127.0.0.1:%d                     ", args.port)
    logger.info("   Trigger URL: http://127.0.0.1:%d/claim                ", args.port)
    logger.info("==========================================================")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("\nShutting down bridge server...")
        disconnect_warp()
        sys.exit(0)


if __name__ == "__main__":
    main()
