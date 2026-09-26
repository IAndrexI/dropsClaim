"""
Ultra-lightweight Web Dashboard Server for Auto Loot Claimer.
Built using Python Standard Library ONLY (zero external pip packages).
Runs on port 8080 by default.
"""

import argparse
import json
import os
import subprocess
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

# Path resolution
APP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, APP_DIR)

from src.free_games.preview_promotions import fetch_epic_freebies
from src.twitch_drops.twitch_api import TwitchClient
from src.twitch_drops.miner import DropsMiner


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


def get_claimed_epic_titles(data_dir):
    """Scan local logs and data to find titles already claimed."""
    claimed = set()
    if not os.path.exists(data_dir):
        return list(claimed)
    for root, _, files in os.walk(data_dir):
        for file in files:
            if file.endswith(".json") or file.endswith(".log"):
                filepath = os.path.join(root, file)
                try:
                    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                        text = f.read()
                        # Astrea, Mechabellum, etc.
                        if "claimed" in text.lower():
                            for line in text.splitlines():
                                if "claimed" in line.lower():
                                    claimed.add(line.strip())
                except Exception:
                    pass
    return list(claimed)


def build_status_payload():
    env = load_env_file()
    data_dir = os.path.join(APP_DIR, "data", "fgc")
    twitch_token = env.get("TWITCH_AUTH_TOKEN", "").strip()

    # 1. Twitch Status & Personal Inventory
    twitch_info = {
        "username": None,
        "user_id": None,
        "connected": False,
        "drops_in_progress": [],
        "active_campaigns_count": 0,
    }

    if twitch_token:
        try:
            client = TwitchClient(twitch_token)
            if client.validate_session():
                twitch_info["username"] = client.user_login
                twitch_info["user_id"] = client.user_id
                twitch_info["connected"] = True

                inv = client.get_inventory()
                campaigns = inv.get("dropCampaignInProgress") or []
                if isinstance(campaigns, dict):
                    campaigns = [campaigns]

                for camp in campaigns:
                    camp_name = camp.get("name", "Drop Campaign")
                    gname = camp.get("game", {}).get("displayName", "Twitch Game")
                    for drop in camp.get("timeBasedDrops", []):
                        sdata = drop.get("self", {})
                        twitch_info["drops_in_progress"].append({
                            "id": drop.get("id"),
                            "name": drop.get("name"),
                            "campaign_name": camp_name,
                            "game": gname,
                            "required_minutes": drop.get("requiredMinutesWatched", 0),
                            "watched_minutes": sdata.get("currentMinutesWatched", 0),
                            "is_claimed": sdata.get("isClaimed", False),
                            "drop_instance_id": sdata.get("dropInstanceId"),
                        })

                # Global campaigns count
                camps = client.get_all_active_campaigns()
                twitch_info["active_campaigns_count"] = len(camps)
        except Exception as err:
            twitch_info["error"] = str(err)

    # 2. Epic Games Status & Free Promotions
    active_free, upcoming_free = fetch_epic_freebies()
    claimed_titles = get_claimed_epic_titles(data_dir)

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
        "claimed_titles": claimed_titles,
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
    }

    gog_info = {
        "email": env.get("GOG_EMAIL"),
        "has_saved_session": has_gog_cookies,
    }

    # 4. System / Daemon status
    pid_files = ["/run/twitch-drops.pid", "/var/run/twitch-drops.pid"]
    miner_running = False
    for p in pid_files:
        if os.path.exists(p):
            try:
                with open(p, "r") as f:
                    pid = f.read().strip()
                if pid and os.path.exists(f"/proc/{pid}"):
                    miner_running = True
                    break
            except Exception:
                pass

    return {
        "twitch": twitch_info,
        "epic": epic_info,
        "amazon": amazon_info,
        "gog": gog_info,
        "system": {
            "miner_running": miner_running,
        }
    }


class DashboardHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
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

        elif self.path == "/api/status":
            data = build_status_payload()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode("utf-8"))
            return

        elif self.path == "/api/logs":
            log_paths = ["/var/log/twitch-drops.log", "/var/log/free-games.log"]
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

        self.send_error(404, "Not Found")

    def do_POST(self):
        if self.path == "/api/claim-drops":
            env = load_env_file()
            token = env.get("TWITCH_AUTH_TOKEN", "").strip()
            claimed = 0
            if token:
                try:
                    miner = DropsMiner(token)
                    claimed = miner.claim_pending_drops()
                except Exception as e:
                    print("Error claiming drops:", e)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"success": True, "claimed": claimed}).encode("utf-8"))
            return

        elif self.path == "/api/trigger-games":
            # Run claim script asynchronously
            script_path = os.path.join(APP_DIR, "scripts", "run_games_claimer.sh")
            if os.path.exists(script_path):
                subprocess.Popen(["bash", script_path], cwd=APP_DIR)
                msg = "Triggered game storefronts claim in the background!"
            else:
                msg = "Claimer script not found."

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"success": True, "message": msg}).encode("utf-8"))
            return

        self.send_error(404, "Not Found")


def run_server(port=8080):
    server_address = ("0.0.0.0", port)
    httpd = ThreadingHTTPServer(server_address, DashboardHandler)
    print("===========================================================")
    print("  Auto Loot Claimer Web Dashboard running!")
    print(f"  Access in your browser: http://<your-server-ip>:{port}")
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
