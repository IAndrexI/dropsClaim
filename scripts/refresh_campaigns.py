"""
Twitch Drops Daily Campaign Refresh Utility.
Periodically or daily fetches live Twitch drop campaigns from DropHunter and Twitch GQL,
filters expired events, caches them to data/twitch_campaigns_cache.json,
and cross-references with user inventory if credentials exist.

ZERO EMOJIS compliant. Standard library only.
"""

import os
import sys
import json
import logging
from datetime import datetime, timezone

# Ensure project root is in sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from src.twitch_drops.twitch_api import (
    TwitchClient,
    fetch_drophunter_live_campaigns,
    is_campaign_expired,
    CAMPAIGNS_CACHE_FILE,
    DATA_DIR,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] (CampaignRefresh) %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("campaign_refresh")


def load_env(path: str = os.path.join(BASE_DIR, ".env")) -> dict:
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


def refresh_campaigns(force: bool = True) -> dict:
    logger.info("Initiating daily Twitch drop campaigns refresh...")
    env = load_env()
    token = env.get("TWITCH_AUTH_TOKEN", "").strip()
    priority_str = env.get("TWITCH_PRIORITY_GAMES", "")
    priority_games = [g.strip() for g in priority_str.split(",") if g.strip()]

    # 1. Fetch live campaigns from DropHunter
    raw_campaigns = fetch_drophunter_live_campaigns(force_refresh=force)
    active_campaigns = [c for c in raw_campaigns if not is_campaign_expired(c)]
    logger.info("Retrieved %d active campaigns across games from DropHunter.", len(active_campaigns))

    # 2. Enrich with Twitch user inventory if authenticated
    overview = {}
    client = TwitchClient(auth_token=token)
    if token and client.validate_session():
        logger.info("Authenticated as Twitch user: %s (ID: %s)", client.user_login, client.user_id)
        overview = client.get_drops_overview(priority_games=priority_games, force_refresh=force)
        camps = overview.get("campaigns", [])
        total = overview.get("total_campaigns", 0)
        claimed_hist = overview.get("claimed_history_count", 0)
        ready_cnt = overview.get("ready_to_claim_count", 0)
        in_prog_cnt = overview.get("in_progress_count", 0)
        logger.info(
            "Overview: %d total campaigns, %d in progress, %d ready to claim, %d claimed in history.",
            total, in_prog_cnt, ready_cnt, claimed_hist
        )
    else:
        logger.info("No Twitch session token configured or session inactive. Using raw active campaigns roster.")
        overview = client.get_drops_overview(priority_games=priority_games, force_refresh=force)

    # 3. Save cache file with explicit metadata
    os.makedirs(DATA_DIR, exist_ok=True)
    cache_payload = {
        "timestamp": datetime.now(timezone.utc).timestamp(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "total_campaigns": overview.get("total_campaigns", len(active_campaigns)),
        "campaigns": overview.get("campaigns", active_campaigns),
    }
    with open(CAMPAIGNS_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache_payload, f, indent=2)

    logger.info("Successfully updated campaigns cache file: %s", CAMPAIGNS_CACHE_FILE)
    return cache_payload


def main():
    try:
        data = refresh_campaigns(force=True)
        print("==========================================================")
        print("  TWITCH DROPS DAILY CAMPAIGN REFRESH SUCCESSFUL")
        print("==========================================================")
        print(f"  Total Campaigns : {data.get('total_campaigns', 0)}")
        print(f"  Refreshed At    : {data.get('updated_at', '')}")
        print(f"  Cache File      : {CAMPAIGNS_CACHE_FILE}")
        print("==========================================================")
        sys.exit(0)
    except Exception as err:
        logger.error("Failed to refresh campaigns: %s", err, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
