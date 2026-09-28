"""
Sync and record claimed games from Free Games Claimer output/logs
into persistent storage (data/claimed_games.json).
Standard library only. Zero external dependencies.
"""

import json
import os
import re
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(BASE_DIR, "data")
CLAIMED_FILE = os.path.join(DATA_DIR, "claimed_games.json")


def load_claimed_records():
    if os.path.exists(CLAIMED_FILE):
        try:
            with open(CLAIMED_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return {
                        "epic": [str(x).strip() for x in data.get("epic", []) if x],
                        "amazon": [str(x).strip() for x in data.get("amazon", []) if x],
                        "gog": [str(x).strip() for x in data.get("gog", []) if x],
                    }
        except Exception:
            pass
    return {"epic": [], "amazon": [], "gog": []}


def save_claimed_records(records):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CLAIMED_FILE, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2)


def sync_from_sources():
    records = load_claimed_records()
    sets = {k: set(v) for k, v in records.items()}

    # Scan directories and logs for claim entries
    search_paths = [
        "/var/log/free-games.log",
        os.path.join(BASE_DIR, "data", "fgc"),
        os.path.join(BASE_DIR, "vendor", "free-games-claimer", "data"),
    ]

    target_files = []
    for sp in search_paths:
        if os.path.isfile(sp):
            target_files.append(sp)
        elif os.path.isdir(sp):
            for root, _, files in os.walk(sp):
                for f in files:
                    if f.endswith((".json", ".log", ".txt")):
                        target_files.append(os.path.join(root, f))

    claim_patterns = [
        re.compile(r"claimed[:\s]+['\"]?([^'\"\r\n]+)['\"]?", re.IGNORECASE),
        re.compile(r"successfully claimed[:\s]+['\"]?([^'\"\r\n]+)['\"]?", re.IGNORECASE),
        re.compile(r"already in library[:\s]+['\"]?([^'\"\r\n]+)['\"]?", re.IGNORECASE),
        re.compile(r"already claimed[:\s]+['\"]?([^'\"\r\n]+)['\"]?", re.IGNORECASE),
    ]

    for tf in target_files:
        store = "epic"
        low_path = tf.lower()
        if "prime" in low_path or "amazon" in low_path:
            store = "amazon"
        elif "gog" in low_path:
            store = "gog"

        try:
            with open(tf, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()

            # If it is a JSON file from FGC
            if tf.endswith(".json"):
                try:
                    jdata = json.loads(content)
                    # Check for list of games or history object
                    if isinstance(jdata, list):
                        for item in jdata:
                            if isinstance(item, dict):
                                title = item.get("title") or item.get("name")
                                if title and (item.get("claimed") or item.get("status") == "claimed"):
                                    sets[store].add(str(title).strip())
                            elif isinstance(item, str):
                                sets[store].add(item.strip())
                    elif isinstance(jdata, dict):
                        for k, v in jdata.items():
                            if isinstance(v, dict):
                                title = v.get("title") or v.get("name") or k
                                if v.get("claimed") or v.get("status") == "claimed":
                                    sets[store].add(str(title).strip())
                except Exception:
                    pass

            # Also check text regex
            for line in content.splitlines():
                for pat in claim_patterns:
                    m = pat.search(line)
                    if m:
                        title = m.group(1).strip()
                        if 1 < len(title) < 90 and not title.lower().startswith("game"):
                            sets[store].add(title)
        except Exception:
            pass

    updated = {k: sorted(list(v)) for k, v in sets.items()}
    save_claimed_records(updated)
    return updated


if __name__ == "__main__":
    res = sync_from_sources()
    print("[CLAIM_SYNC] Sync complete:")
    print(f"  Epic Games Claimed: {len(res['epic'])}")
    print(f"  Amazon Prime Gaming Claimed: {len(res['amazon'])}")
    print(f"  GOG Games Claimed: {len(res['gog'])}")
