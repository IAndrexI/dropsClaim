"""
Check and display currently available free games across Epic Games and others.
Public API check - uses Python standard library ONLY (no external dependencies).
"""

import json
from datetime import datetime
import urllib.request
import urllib.error

EPIC_FREE_GAMES_URL = (
    "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions"
    "?locale=en-US&country=US&allowCountries=US"
)


def fetch_epic_freebies():
    """Fetch and parse current and upcoming Epic Games Store free games using stdlib."""
    req = urllib.request.Request(
        EPIC_FREE_GAMES_URL,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception as err:
        print(f"[ERROR] Failed to fetch Epic free games: {err}")
        return [], []

    elements = data.get("data", {}).get("Catalog", {}).get("searchStore", {}).get("elements", [])
    active_freebies = []
    upcoming_freebies = []

    for item in elements:
        title = item.get("title")
        promotions = item.get("promotions") or {}
        promotional_offers = promotions.get("promotionalOffers", [])
        upcoming_offers = promotions.get("upcomingPromotionalOffers", [])

        # Check currently active 100% discount offers
        if promotional_offers:
            offers = promotional_offers[0].get("promotionalOffers", [])
            for offer in offers:
                discount = offer.get("discountSetting", {}).get("discountPercentage", 0)
                if discount == 0:  # 0 cost / 100% off
                    start = offer.get("startDate")
                    end = offer.get("endDate")
                    active_freebies.append({"title": title, "start": start, "end": end})

        # Check upcoming offers
        elif upcoming_offers:
            offers = upcoming_offers[0].get("promotionalOffers", [])
            for offer in offers:
                start = offer.get("startDate")
                end = offer.get("endDate")
                upcoming_freebies.append({"title": title, "start": start, "end": end})

    return active_freebies, upcoming_freebies


def main():
    print("==================================================")
    print("      CURRENT FREE PROMOTIONS CHECKER             ")
    print("==================================================")
    print("\n--- Epic Games Store ---")
    active, upcoming = fetch_epic_freebies()

    if active:
        print("\n[Available to Claim Right Now (FREE)]:")
        for g in active:
            print(f"  * {g['title']} (Ends: {g['end']})")
    else:
        print("No free games currently flagged as 100% off.")

    if upcoming:
        print("\n[Upcoming Next Week]:")
        for g in upcoming:
            print(f"  * {g['title']} (Starts: {g['start']})")


if __name__ == "__main__":
    main()
