"""
Sync and record claimed games from Free Games Claimer output/logs
into persistent storage (data/claimed_games.json).
Standard library only. Zero external dependencies.
"""

import json
import os
import re
from typing import Dict, Iterable, List, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(BASE_DIR, "data")
CLAIMED_FILE = os.path.join(DATA_DIR, "claimed_games.json")

IGNORED_FILENAMES = {
    "store_claimer_status.json",
    "pending_2fa.json",
    "config.json",
    "mining_config.json",
    "miner_status.json",
    "twitch_inventory.json",
    "twitch_campaigns_cache.json",
    "twitch_inventory_cache.json",
    "twitch_integrity_cache.json",
    "twitch_token.json",
    "claimed_games.json",
}

SKIP_DIR_NAMES = {
    "chromium_profile",
    "browser",
    "screenshots",
    "record",
    "twitch_browser_profile",
}

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")
CURRENT_GAME_RE = re.compile(r"current free game:\s*(.+)$", re.IGNORECASE)
INLINE_CLAIM_PATTERNS = [
    re.compile(r"\[(?:epic|amazon|prime|gog)\]\s*(?:successfully\s+)?claimed[:\s]+['\"]?([^'\"\r\n]+)['\"]?", re.IGNORECASE),
    re.compile(r"successfully\s+claimed[:\s]+['\"]?([^'\"\r\n]+)['\"]?", re.IGNORECASE),
    re.compile(r"already\s+claimed[:\s]+['\"]?([^'\"\r\n]+)['\"]?", re.IGNORECASE),
    re.compile(r"already\s+in\s+library[:\s]+['\"]?([^'\"\r\n]+)['\"]?", re.IGNORECASE),
    re.compile(r"^claimed:\s+['\"]?([^'\"\r\n]+)['\"]?", re.IGNORECASE),
]

GENERIC_PHRASES = (
    "game",
    "games",
    "successfully",
    "nothing to claim",
    "none",
    "n/a",
)


def _infer_store_from_path(filepath: str) -> str:
    low_path = filepath.lower()
    if "prime" in low_path or "amazon" in low_path:
        return "amazon"
    if "gog" in low_path:
        return "gog"
    return "epic"


def _is_claimed_entry(entry: dict) -> bool:
    status = str(entry.get("status") or "").strip().lower()
    if status.startswith("failed") or "error" in status:
        return False
    if entry.get("claimed") is True:
        return True
    if status in ("claimed", "existed", "collected"):
        return True
    if status.startswith("claimed") or status.startswith("already"):
        return True
    return False


def _clean_title(raw: str) -> str:
    title = ANSI_ESCAPE_RE.sub("", str(raw or "")).strip()
    if " - http" in title:
        title = title.split(" - http", 1)[0].strip()
    title = title.strip(" -:[]*\"'")
    return title


def _is_valid_title(title: str) -> bool:
    if not title or len(title) <= 1 or len(title) >= 100:
        return False
    low = title.lower()
    if low in GENERIC_PHRASES or low.startswith("game "):
        return False
    return True


def load_claimed_records(base_dir: str = BASE_DIR) -> Dict[str, List[str]]:
    claimed_file = os.path.join(base_dir, "data", "claimed_games.json")
    if os.path.exists(claimed_file):
        try:
            with open(claimed_file, "r", encoding="utf-8") as f:
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


def save_claimed_records(records: Dict[str, List[str]], base_dir: str = BASE_DIR) -> None:
    data_dir = os.path.join(base_dir, "data")
    claimed_file = os.path.join(data_dir, "claimed_games.json")
    os.makedirs(data_dir, exist_ok=True)
    tmp_file = f"{claimed_file}.tmp.{os.getpid()}"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2)
    os.replace(tmp_file, claimed_file)


def _parse_json_claims(jdata, store: str, sets: Dict[str, set]) -> None:
    if isinstance(jdata, list):
        for item in jdata:
            if isinstance(item, dict):
                title = item.get("title") or item.get("name")
                if title and _is_claimed_entry(item):
                    cleaned = _clean_title(str(title))
                    if _is_valid_title(cleaned):
                        sets[store].add(cleaned)
            elif isinstance(item, str):
                cleaned = _clean_title(item)
                if _is_valid_title(cleaned):
                    sets[store].add(cleaned)
    elif isinstance(jdata, dict):
        for k, v in jdata.items():
            if not isinstance(v, dict):
                continue
            # 1-level dict: { "Game Title": { "status": "claimed", ... } }
            if "status" in v or "claimed" in v or "title" in v or "name" in v:
                title = v.get("title") or v.get("name") or k
                if title and _is_claimed_entry(v):
                    cleaned = _clean_title(str(title))
                    if _is_valid_title(cleaned):
                        sets[store].add(cleaned)
            else:
                # 2-level lowdb format: { "<user>": { "<title>": { "title": "...", "status": "claimed" } } }
                for sub_k, sub_v in v.items():
                    if isinstance(sub_v, dict):
                        title = sub_v.get("title") or sub_v.get("name") or sub_k
                        if title and _is_claimed_entry(sub_v):
                            cleaned = _clean_title(str(title))
                            if _is_valid_title(cleaned):
                                sets[store].add(cleaned)


def _parse_log_claims(content: str, default_store: str, sets: Dict[str, set]) -> None:
    current_store = default_store
    last_game_title: Optional[str] = None

    for raw_line in content.splitlines():
        line = ANSI_ESCAPE_RE.sub("", raw_line).strip()
        if not line:
            continue
        low = line.lower()

        if "[epic]" in low or "started checking epic-games" in low:
            current_store = "epic"
            last_game_title = None
        elif "[amazon]" in low or "[prime]" in low or "started checking prime-gaming" in low:
            current_store = "amazon"
            last_game_title = None
        elif "[gog]" in low or "started checking gog" in low:
            current_store = "gog"
            last_game_title = None

        m_curr = CURRENT_GAME_RE.search(line)
        if m_curr:
            candidate = _clean_title(m_curr.group(1))
            if _is_valid_title(candidate):
                last_game_title = candidate
            continue

        matched_inline = False
        for pat in INLINE_CLAIM_PATTERNS:
            m = pat.search(line)
            if m:
                title = _clean_title(m.group(1))
                if _is_valid_title(title):
                    sets[current_store].add(title)
                    matched_inline = True
                    last_game_title = None
                    break
        if matched_inline:
            continue

        if last_game_title and (
            "claimed successfully" in low
            or "already in library" in low
            or "already claimed" in low
        ):
            sets[current_store].add(last_game_title)
            last_game_title = None


def collect_claimed_records(
    base_dir: str = BASE_DIR,
    extra_log_paths: Optional[Iterable[str]] = None,
) -> Dict[str, List[str]]:
    records = load_claimed_records(base_dir=base_dir)
    sets = {k: set(v) for k, v in records.items()}

    search_paths: List[str] = [
        "/var/log/free-games.log",
        os.path.join(base_dir, "data", "free-games.log"),
        os.path.join(base_dir, "data", "fgc"),
        os.path.join(base_dir, "vendor", "free-games-claimer", "data"),
    ]
    if extra_log_paths:
        search_paths.extend(list(extra_log_paths))

    target_files: List[str] = []
    seen_files = set()
    for sp in search_paths:
        if not sp:
            continue
        if os.path.isfile(sp):
            if sp not in seen_files:
                seen_files.add(sp)
                target_files.append(sp)
        elif os.path.isdir(sp):
            for root, dirs, files in os.walk(sp):
                dirs[:] = [d for d in dirs if d.lower() not in SKIP_DIR_NAMES]
                for f in files:
                    if f.lower() in IGNORED_FILENAMES:
                        continue
                    if f.endswith((".json", ".log", ".txt")):
                        full_path = os.path.join(root, f)
                        if full_path not in seen_files:
                            seen_files.add(full_path)
                            target_files.append(full_path)

    for tf in target_files:
        store = _infer_store_from_path(tf)
        try:
            with open(tf, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()

            if tf.endswith(".json"):
                try:
                    jdata = json.loads(content)
                    _parse_json_claims(jdata, store, sets)
                except Exception:
                    pass
            else:
                _parse_log_claims(content, store, sets)
        except Exception:
            pass

    return {k: sorted(list(v)) for k, v in sets.items()}


def sync_from_sources(
    base_dir: str = BASE_DIR,
    extra_log_paths: Optional[Iterable[str]] = None,
) -> Dict[str, List[str]]:
    updated = collect_claimed_records(base_dir=base_dir, extra_log_paths=extra_log_paths)
    save_claimed_records(updated, base_dir=base_dir)
    return updated


if __name__ == "__main__":
    res = sync_from_sources()
    print("[CLAIM_SYNC] Sync complete:")
    print(f"  Epic Games Claimed: {len(res['epic'])}")
    print(f"  Amazon Prime Gaming Claimed: {len(res['amazon'])}")
    print(f"  GOG Games Claimed: {len(res['gog'])}")
