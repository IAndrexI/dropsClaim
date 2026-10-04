"""
CLI Entrypoint for Twitch Drops Miner (Pure Python Standard Library).
"""

import argparse
import os
import sys

from .miner import DropsMiner
from .process_guard import PID_FILE, get_running_bot_processes, kill_pid
from .twitch_api import TwitchClient


def load_env_file(filepath=".env"):
    """Load key-value pairs from .env without external python-dotenv package."""
    if not os.path.exists(filepath):
        return
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = val


def main():
    load_env_file()

    parser = argparse.ArgumentParser(description="Zero-Telemetry Twitch Drops Miner & Auto-Claimer")
    parser.add_argument("--daemon", action="store_true", help="Run continuously in background daemon mode")
    parser.add_argument("--test", action="store_true", help="Validate authentication token and connection")
    parser.add_argument("--inventory", action="store_true", help="Print active drops progress and claimed loot")
    parser.add_argument("--campaigns", action="store_true", help="List all currently active Twitch drop campaigns")
    parser.add_argument("--claim-now", action="store_true", help="Immediately claim any drops at 100% and exit")
    parser.add_argument("--check-bots", action="store_true", help="Check for running Twitch bot processes and active viewing")
    parser.add_argument("--kill-bots", action="store_true", help="Terminate conflicting bot processes to ensure single viewing")
    parser.add_argument("--token", type=str, default=None, help="Twitch auth-token cookie (or set TWITCH_AUTH_TOKEN in env)")
    args = parser.parse_args()

    if args.check_bots:
        bots = get_running_bot_processes()
        print(f"=== Active Twitch Bot Processes Detected: {len(bots)} ===")
        if not bots:
            print("[OK] No conflicting background Twitch bots detected on this system.")
        for idx, bot in enumerate(bots, 1):
            print(f" [{idx}] PID: {bot['pid']} | Command: {bot['cmdline']}")
        return

    if args.kill_bots:
        bots = get_running_bot_processes()
        print(f"=== Terminating {len(bots)} Twitch Bot Process(es) ===")
        for bot in bots:
            p = int(bot['pid'])
            if kill_pid(p):
                print(f"  [OK] Terminated PID {p}")
            else:
                print(f"  [FAIL] Could not terminate PID {p}")
        if os.path.exists(PID_FILE):
            try:
                os.remove(PID_FILE)
            except Exception:
                pass
        return

    auth_token = args.token or os.environ.get("TWITCH_AUTH_TOKEN", "").strip()
    if not auth_token:
        print("[ERROR] No Twitch auth-token provided! Set TWITCH_AUTH_TOKEN in .env or pass --token <token>")
        print("\nHow to get your Twitch auth-token:")
        print("  1. Open https://www.twitch.tv in your desktop browser and make sure you are logged in.")
        print("  2. Open Developer Tools (F12 or Ctrl+Shift+I).")
        print("  3. Go to the 'Application' tab -> 'Cookies' -> 'https://www.twitch.tv'.")
        print("  4. Look for the cookie named 'auth-token' and copy its value.")
        print("  5. Paste it in your .env file as: TWITCH_AUTH_TOKEN=your_token_here")
        sys.exit(1)

    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip() or None
    priority_env = os.environ.get("TWITCH_PRIORITY_GAMES", "")
    priority_games = [g.strip() for g in priority_env.split(",") if g.strip()]

    if args.test:
        client = TwitchClient(auth_token)
        if client.validate_session():
            print(f"[SUCCESS] Connected to Twitch! Logged in as: {client.user_login} (User ID: {client.user_id})")
            user_active = client.is_user_actively_using_twitch()
            print(f"User actively using Twitch right now: {user_active}")
        else:
            print("[FAIL] Could not validate token. Please verify your auth-token cookie.")
        return


    if args.inventory:
        client = TwitchClient(auth_token)
        if not client.validate_session():
            sys.exit(1)
        inv = client.get_inventory()
        campaigns = inv.get("dropCampaignInProgress") or []
        if isinstance(campaigns, dict):
            campaigns = [campaigns]
        print("\n=== Current Drop Progress ===")
        if not campaigns:
            print("No campaigns currently in progress.")
        for camp in campaigns:
            gname = camp.get("game", {}).get("displayName", "Unknown")
            print(f"\nGame: {gname} ({camp.get('name')})")
            for drop in camp.get("timeBasedDrops", []):
                sdata = drop.get("self", {})
                w = sdata.get("currentMinutesWatched", 0)
                req = drop.get("requiredMinutesWatched", 0)
                pct = int((w / req) * 100) if req else 0
                claimed = " [CLAIMED]" if sdata.get("isClaimed") else (" [READY TO CLAIM!]" if w >= req else "")
                print(f"  - {drop.get('name')}: {w}/{req} min ({pct}%){claimed}")
        return

    if args.campaigns:
        client = TwitchClient(auth_token)
        if not client.validate_session():
            sys.exit(1)
        camps = client.get_all_active_campaigns()
        print(f"\n=== Active Twitch Campaigns ({len(camps)} Total) ===")
        for c in camps:
            gname = c.get("game", {}).get("displayName", "Unknown")
            print(f"- {gname}: {c.get('name')} (Ends: {c.get('endAt', 'N/A')})")
        return

    if args.claim_now:
        miner = DropsMiner(auth_token, priority_games=priority_games, webhook_url=webhook_url)
        if not miner.client.validate_session():
            sys.exit(1)
        count = miner.claim_pending_drops()
        print(f"[DONE] Claimed {count} completed drops.")
        return

    # Default or --daemon
    miner = DropsMiner(auth_token, priority_games=priority_games, webhook_url=webhook_url)
    miner.start()


if __name__ == "__main__":
    main()
