"""
Comprehensive status and connection diagnostic checker for all services:
Twitch Drops, Epic Games, Amazon Prime Gaming, and GOG.
Uses safe ASCII logging to guarantee cross-terminal compatibility.
"""

import os
import sys
import json
import urllib.request
import urllib.error

# Ensure repository root is on sys.path
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)


def load_env(path=".env"):
    if not os.path.exists(path):
        return {}
    env = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip("'\"")
    return env


def check_twitch(token=None, env=None):
    print("\n[1] --- TWITCH DROPS & ACCOUNT DIAGNOSTICS ---")
    try:
        from src.auth.twitch_auth import get_credentials, resolve_token
        from src.twitch_drops.diagnostics import run_twitch_diagnostics

        raw_env_tok = (env or {}).get("TWITCH_AUTH_TOKEN", "")
        active_tok = resolve_token(token or raw_env_tok)

        diag = run_twitch_diagnostics(token=active_tok, verbose=True)
        return diag.get("healthy", False)
    except Exception as err:
        print(f"  [WARN] Advanced diagnostic suite note ({err}); running direct GQL verification...")

    # Fallback to direct GQL query
    if not token and env:
        token = env.get("TWITCH_AUTH_TOKEN")
    if not token:
        print("  [FAIL] TWITCH_AUTH_TOKEN is missing in .env and data/twitch_token.json")
        return False

    url = "https://gql.twitch.tv/gql"
    headers = {
        "Client-Id": "kimne78kx3ncx6brgo4mv6wki5h1ko",
        "Authorization": f"OAuth {token}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Content-Type": "application/json",
    }
    query = {"query": "query CheckUserSession { currentUser { id login displayName email } }"}
    try:
        req = urllib.request.Request(url, data=json.dumps(query).encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        user = data.get("data", {}).get("currentUser")
        if user:
            print(f"  [OK] Connected as Twitch user: {user.get('displayName')} (Login: {user.get('login')}, ID: {user.get('id')})")
            return True
        else:
            print("  [FAIL] Token invalid: Twitch did not return a user profile. Check your auth-token cookie.")
            return False
    except Exception as e:
        print(f"  [FAIL] Error connecting to Twitch: {e}")
        return False


def check_epic(env, data_dir):
    print("\n[2] --- EPIC GAMES STORE ---")
    email = env.get("EG_EMAIL")
    password = env.get("EG_PASSWORD")
    if not email or not password:
        print("  [WARN] EG_EMAIL or EG_PASSWORD not configured in .env")
        return

    print(f"  Configured account: {email}")

    cookies_found = False
    if os.path.exists(data_dir):
        for root, _, files in os.walk(data_dir):
            for file in files:
                if "epic" in file.lower() or "cookie" in file.lower():
                    cookies_found = True
                    break

    if cookies_found:
        print("  [OK] Saved Epic Games session cookies found in data directory! (Auto-login active)")
    else:
        print("  [PENDING] No saved session cookies yet.")
        print("            Run 'bash scripts/run_games_claimer.sh' to perform initial 2FA login.")

    promo_url = "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions?locale=en-US&country=US&allowCountries=US"
    try:
        req = urllib.request.Request(promo_url, headers={"User-Agent": "Mozilla/5.0"}, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"  [OK] Epic Games Store API reachable (HTTP {resp.status})")
    except Exception as e:
        print(f"  [FAIL] Could not connect to Epic Games Store API: {e}")


def check_amazon(env, data_dir):
    print("\n[3] --- AMAZON PRIME GAMING ---")
    email = env.get("PG_EMAIL")
    if not email:
        print("  [WARN] PG_EMAIL not configured in .env")
        return

    print(f"  Configured account: {email}")
    cookies_found = False
    if os.path.exists(data_dir):
        for root, _, files in os.walk(data_dir):
            for file in files:
                if "amazon" in file.lower() or "prime" in file.lower():
                    cookies_found = True
                    break

    if cookies_found:
        print("  [OK] Saved Amazon Prime Gaming session cookies found in data directory!")
    else:
        print("  [PENDING] No saved session cookies yet.")
        print("            Run 'bash scripts/run_games_claimer.sh' to perform initial login.")


def check_gog(env, data_dir):
    print("\n[4] --- GOG GIVEAWAYS ---")
    email = env.get("GOG_EMAIL")
    if not email:
        print("  [WARN] GOG_EMAIL not configured in .env")
        return

    print(f"  Configured account: {email}")
    cookies_found = False
    if os.path.exists(data_dir):
        for root, _, files in os.walk(data_dir):
            for file in files:
                if "gog" in file.lower():
                    cookies_found = True
                    break

    if cookies_found:
        print("  [OK] Saved GOG session cookies found in data directory!")
    else:
        print("  [PENDING] No saved session cookies yet.")
        print("            Run 'bash scripts/run_games_claimer.sh' to perform initial login.")


def check_process():
    print("\n[5] --- BACKGROUND TWITCH DROPS SERVICE ---")
    pid_files = ["/run/twitch-drops.pid", "/var/run/twitch-drops.pid"]
    pid = None
    for p in pid_files:
        if os.path.exists(p):
            try:
                with open(p, "r") as f:
                    pid = f.read().strip()
                break
            except Exception:
                pass

    if pid and os.path.exists(f"/proc/{pid}"):
        print(f"  [OK] Twitch Drops Miner daemon is RUNNING (PID: {pid})")
    else:
        print("  [INFO] Twitch Drops service is currently NOT running.")
        print("         To start it: rc-service twitch-drops start")


def main():
    print("==========================================================")
    print("        ALL SERVICES & CONNECTION HEALTH CHECK            ")
    print("==========================================================")
    app_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    env_file = os.path.join(app_dir, ".env")
    data_dir = os.path.join(app_dir, "data", "fgc")

    env = {}
    if os.path.exists(env_file):
        env = load_env(env_file)
    else:
        print(f"  [INFO] .env file not found at: {env_file}")
        print("         Checking stored session credentials & network connectivity...")

    check_twitch(env.get("TWITCH_AUTH_TOKEN"), env=env)
    check_epic(env, data_dir)
    check_amazon(env, data_dir)
    check_gog(env, data_dir)

    status_file = os.path.join(app_dir, "data", "store_claimer_status.json")
    if os.path.exists(status_file):
        try:
            with open(status_file, "r", encoding="utf-8") as sf:
                st = json.load(sf) or {}
            print("\n[4b] --- STORE AUTO-CLAIMER STATUS ---")
            print(f"  Engine: {st.get('engine', 'native-chromium-cdp')} | Last Run: {st.get('last_run_at') or 'Never'}")
            for store_k in ("epic", "amazon", "gog"):
                s_info = (st.get("stores") or {}).get(store_k) or {}
                print(f"  {store_k.upper():6s}: {s_info.get('status', 'idle')} - {s_info.get('message', '')}")
        except Exception:
            pass

    check_process()

    print("\n==========================================================")
    print("Diagnostic complete.")
    print("==========================================================")


if __name__ == "__main__":
    main()
