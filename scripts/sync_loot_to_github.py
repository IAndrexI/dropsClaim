"""
Sync and export claimed loot records to GitHub markdown file.
Collects claimed Epic Games, Amazon Prime Gaming, GOG giveaways, and Twitch drops,
and formats them into a clean, human-readable CLAIMED_LOOT.md document in the repository.
Optionally commits and pushes updates directly to GitHub origin/main.

ZERO EMOJIS compliant. Python standard library only.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from typing import Dict, List, Any

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
CLAIMED_FILE = os.path.join(DATA_DIR, "claimed_games.json")
INVENTORY_FILE = os.path.join(DATA_DIR, "twitch_inventory.json")
TARGET_MD = os.path.join(BASE_DIR, "CLAIMED_LOOT.md")


def load_claimed_stores() -> Dict[str, List[str]]:
    if not os.path.exists(CLAIMED_FILE):
        return {"epic": [], "amazon": [], "gog": []}
    try:
        with open(CLAIMED_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return {
                "epic": sorted(list(set(data.get("epic", [])))),
                "amazon": sorted(list(set(data.get("amazon", [])))),
                "gog": sorted(list(set(data.get("gog", [])))),
            }
    except Exception:
        return {"epic": [], "amazon": [], "gog": []}


def load_claimed_drops() -> List[Dict[str, str]]:
    if not os.path.exists(INVENTORY_FILE):
        return []
    try:
        with open(INVENTORY_FILE, "r", encoding="utf-8") as f:
            inv = json.load(f)
        drops = []
        for d in inv.get("claimed_drops", []):
            drops.append({
                "game": d.get("game_name", "Unknown"),
                "name": d.get("name", "Unknown Drop"),
                "date": d.get("last_awarded_at", ""),
            })
        return drops
    except Exception:
        return []


def generate_markdown() -> str:
    stores = load_claimed_stores()
    drops = load_claimed_drops()
    now_utc = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())

    lines = []
    lines.append("# Claimed Loot & Rewards Record")
    lines.append("")
    lines.append(f"> Last synchronized: **{now_utc}**")
    lines.append("")
    lines.append("This document tracks all free games, codes, and Twitch drops automatically or manually confirmed for this account.")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Summary Statistics")
    lines.append("")
    lines.append(f"- **Epic Games Store Freebies**: {len(stores['epic'])}")
    lines.append(f"- **Amazon Prime Gaming Loot**: {len(stores['amazon'])}")
    lines.append(f"- **GOG DRM-Free Giveaways**: {len(stores['gog'])}")
    lines.append(f"- **Twitch Drops Claimed**: {len(drops)}")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Epic Games
    lines.append(f"## Epic Games Store ({len(stores['epic'])})")
    lines.append("")
    if stores["epic"]:
        lines.append("| Status | Title | Storefront Link |")
        lines.append("| :--- | :--- | :--- |")
        for title in stores["epic"]:
            lines.append(f"| [CLAIMED] | **{title}** | [Epic Games Store](https://store.epicgames.com/) |")
    else:
        lines.append("*No Epic Games claimed yet.*")
    lines.append("")

    # Amazon Prime Gaming
    lines.append(f"## Amazon Prime Gaming ({len(stores['amazon'])})")
    lines.append("")
    if stores["amazon"]:
        lines.append("| Status | Title / Code | Storefront Link |")
        lines.append("| :--- | :--- | :--- |")
        for title in stores["amazon"]:
            lines.append(f"| [CLAIMED] | **{title}** | [Prime Gaming](https://gaming.amazon.com/) |")
    else:
        lines.append("*No Amazon Prime Gaming items claimed yet.*")
    lines.append("")

    # GOG
    lines.append(f"## GOG DRM-Free Giveaways ({len(stores['gog'])})")
    lines.append("")
    if stores["gog"]:
        lines.append("| Status | Title | Storefront Link |")
        lines.append("| :--- | :--- | :--- |")
        for title in stores["gog"]:
            lines.append(f"| [CLAIMED] | **{title}** | [GOG.com](https://www.gog.com/) |")
    else:
        lines.append("*No GOG giveaways claimed yet.*")
    lines.append("")

    # Twitch Drops
    lines.append(f"## Twitch Drops Inventory ({len(drops)})")
    lines.append("")
    if drops:
        lines.append("| Status | Game | Reward Name | Date Claimed |")
        lines.append("| :--- | :--- | :--- | :--- |")
        for drop in drops:
            lines.append(f"| [CLAIMED] | {drop['game']} | **{drop['name']}** | {drop['date']} |")
    else:
        lines.append("*No Twitch drops in inventory history yet.*")
    lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Export and synchronize claimed loot to GitHub markdown file")
    parser.add_argument("--push", action="store_true", help="Automatically commit and push CLAIMED_LOOT.md to GitHub origin/main")
    args = parser.parse_args()

    content = generate_markdown()
    with open(TARGET_MD, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"[OK] Generated claimed loot markdown at: {TARGET_MD}")

    if args.push:
        if not os.path.exists(os.path.join(BASE_DIR, ".git")):
            print("[WARN] Not a git repository. Skipping push.")
            return
        try:
            print("[GIT] Staging CLAIMED_LOOT.md...")
            subprocess.run(["git", "add", "CLAIMED_LOOT.md"], cwd=BASE_DIR, check=True)
            status = subprocess.check_output(["git", "status", "--porcelain", "CLAIMED_LOOT.md"], cwd=BASE_DIR, text=True).strip()
            if not status:
                print("[OK] CLAIMED_LOOT.md is already up to date on GitHub. Nothing to commit.")
                return
            commit_msg = f"Update claimed loot records [{time.strftime('%Y-%m-%d')}]"
            subprocess.run(["git", "commit", "-m", commit_msg], cwd=BASE_DIR, check=True)
            print("[GIT] Pushing updates to origin main on GitHub...")
            subprocess.run(["git", "push", "origin", "main"], cwd=BASE_DIR, check=True)
            print("[SUCCESS] Successfully updated claimed loot on GitHub!")
        except Exception as err:
            print(f"[ERROR] Failed to push to GitHub: {err}")


if __name__ == "__main__":
    main()
