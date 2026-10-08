#!/usr/bin/env python3
"""
Desktop App Launcher for Auto Loot Claimer.
Launches the Web Dashboard in dedicated standalone application window mode
(using Chromium/Edge/Brave app mode) or falls back to system default browser.

Provides a 100% native desktop application experience synchronized with the server.
Python standard library only (zero external pip packages).
"""

import argparse
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import webbrowser

APP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def load_env_file(path=None):
    if not path:
        path = os.path.join(APP_DIR, ".env")
    env = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip("'\"")
        except Exception:
            pass
    return env


def find_chromium_browser():
    """Detects available Chromium-based browser supporting --app mode."""
    candidates = []

    if sys.platform == "win32":
        # Common Windows paths for Edge, Chrome, Brave
        program_files = os.environ.get("ProgramFiles", "C:\\Program Files")
        program_files_x86 = os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")
        local_app_data = os.environ.get("LocalAppData", "")

        candidates.extend([
            os.path.join(program_files_x86, "Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join(program_files, "Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join(program_files, "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(program_files_x86, "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(local_app_data, "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(program_files, "BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
            os.path.join(local_app_data, "BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
        ])
        for name in ["msedge.exe", "chrome.exe", "brave.exe"]:
            found = shutil.which(name)
            if found:
                candidates.append(found)

    elif sys.platform == "darwin":
        # macOS paths
        candidates.extend([
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ])

    else:
        # Linux / BSD
        for name in [
            "google-chrome",
            "google-chrome-stable",
            "chromium",
            "chromium-browser",
            "microsoft-edge",
            "microsoft-edge-stable",
            "brave-browser",
        ]:
            found = shutil.which(name)
            if found:
                candidates.append(found)

    for path in candidates:
        if path and os.path.exists(path) and (os.access(path, os.X_OK) or sys.platform == "win32"):
            return path

    return None


def check_server_status(url, timeout=1.5):
    """Checks whether the dashboard server is responding."""
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "AutoLootClaimerDesktopLauncher/1.0"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status in (200, 301, 302, 307, 308)
    except Exception:
        return False


def launch_app(url, custom_browser=None, force_browser=False):
    """Launches the app in windowed mode or default browser."""
    print("===========================================================")
    print("  Auto Loot Claimer - Desktop Application Launcher")
    print("===========================================================")
    print(f"Target URL: {url}")

    is_online = check_server_status(url)
    if is_online:
        print("Server status: [ONLINE] Dashboard server is reachable.")
    else:
        print("Server status: [STANDBY] Server not answering on port yet.")
        print("Tip: If dashboard is not started, start it via:")
        print("     python src/web/server.py")
        print("     or ensure cloudflared tunnel is running.")

    browser_bin = custom_browser or (None if force_browser else find_chromium_browser())

    if browser_bin and not force_browser:
        browser_name = os.path.basename(browser_bin)
        print(f"Window mode: Standalone Window (via {browser_name})")
        print("Starting standalone app window...")

        cmd = [
            browser_bin,
            f"--app={url}",
            "--window-size=1280,840",
            "--window-position=100,80",
        ]

        try:
            if sys.platform == "win32":
                subprocess.Popen(cmd, creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
            else:
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            print("[SUCCESS] Application window launched.")
            print("Note: Website and App instances are synced in real time.")
            return True
        except Exception as e:
            print(f"[WARNING] Failed to launch in app mode: {e}. Falling back to default browser.")

    print("Window mode: Default Web Browser Tab")
    try:
        webbrowser.open(url)
        print("[SUCCESS] Opened dashboard in default browser.")
        return True
    except Exception as e:
        print(f"[ERROR] Could not open browser: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Auto Loot Claimer Desktop App Launcher")
    parser.add_argument("--url", type=str, default=None, help="Dashboard URL (default: from .env or http://localhost:8080)")
    parser.add_argument("--port", type=int, default=None, help="Port if targeting local server (default: 8080)")
    parser.add_argument("--browser", type=str, default=None, help="Path to specific Chromium browser executable")
    parser.add_argument("--no-app", action="store_true", help="Launch in standard browser tab instead of standalone app window")
    args = parser.parse_args()

    env = load_env_file()
    url = args.url

    if not url:
        configured_url = env.get("DASHBOARD_URL", "").strip()
        if configured_url:
            url = configured_url
        else:
            port = args.port or int(env.get("PORT", "8080"))
            url = f"http://localhost:{port}"

    launch_app(url, custom_browser=args.browser, force_browser=args.no_app)


if __name__ == "__main__":
    main()
