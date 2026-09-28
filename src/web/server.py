"""
Ultra-lightweight Web Dashboard Server for Auto Loot Claimer.
Built using Python Standard Library ONLY (zero external pip packages).
Features secure session authentication, login page, and in-browser account management.
"""

import argparse
import json
import os
import secrets
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
from src.twitch_drops.miner import DropsMiner

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

    priority_str = env.get("TWITCH_PRIORITY_GAMES", "").strip()
    priority_list = [g.strip() for g in priority_str.split(",") if g.strip()]

    # 1. Twitch Status & Personal Inventory
    twitch_info = {
        "username": None,
        "user_id": None,
        "connected": False,
        "campaigns": [],
        "drops_in_progress": [],
        "priority_games": priority_list,
        "active_campaigns_count": 0,
        "claimed_history_count": 0,
        "ready_to_claim_count": 0,
        "in_progress_count": 0,
    }

    if twitch_token:
        try:
            client = TwitchClient(twitch_token)
            if client.validate_session():
                twitch_info["username"] = client.user_login
                twitch_info["user_id"] = client.user_id
                twitch_info["connected"] = True

                overview = client.get_drops_overview()
                twitch_info["campaigns"] = overview.get("campaigns", [])
                twitch_info["active_campaigns_count"] = overview.get("total_campaigns", 0)
                twitch_info["claimed_history_count"] = overview.get("claimed_history_count", 0)
                twitch_info["ready_to_claim_count"] = overview.get("ready_to_claim_count", 0)
                twitch_info["in_progress_count"] = overview.get("in_progress_count", 0)
                
                # Maintain legacy drops_in_progress flat list for backwards compatibility
                for camp in overview.get("campaigns", []):
                    for d in camp.get("drops", []):
                        if d.get("status") in ("IN_PROGRESS", "READY_TO_CLAIM"):
                            twitch_info["drops_in_progress"].append({
                                "id": d.get("id"),
                                "name": d.get("name"),
                                "campaign_name": camp.get("name"),
                                "game": camp.get("game"),
                                "required_minutes": d.get("required_minutes", 0),
                                "watched_minutes": d.get("watched_minutes", 0),
                                "is_claimed": d.get("status") == "CLAIMED",
                                "drop_instance_id": d.get("drop_instance_id"),
                                "status": d.get("status"),
                            })
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
        # 1. Login Page
        if self.path == "/login":
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
            if self.path.startswith("/api/"):
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

        elif self.path == "/api/accounts":
            env = load_env_file()
            data = {
                "dashboard_username": env.get("DASHBOARD_USERNAME", "andrex"),
                "twitch_auth_token": env.get("TWITCH_AUTH_TOKEN", ""),
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

        elif self.path == "/api/logs":
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
            expected_pass = env.get("DASHBOARD_PASSWORD", "10140523Andy!").strip()

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
            if "twitch_auth_token" in payload:
                updates["TWITCH_AUTH_TOKEN"] = payload["twitch_auth_token"].strip()
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
                token = payload.get("token") or env.get("TWITCH_AUTH_TOKEN", "")
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
            token = env.get("TWITCH_AUTH_TOKEN", "").strip()
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

        elif self.path == "/api/trigger-games":
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
