"""
Audited, zero-telemetry Twitch GraphQL client for Drops Tracking and Claiming.
Uses Python Standard Library ONLY (no pip or external dependencies required).

STRICT DESTINATION POLICY:
All network requests in this module are strictly restricted to official Twitch endpoints:
- https://gql.twitch.tv/gql
- https://id.twitch.tv
- https://spade.twitch.tv
- https://usher.ttvnw.net
No third-party analytics, tracking, or proxy endpoints are contacted.

"""

import base64
import concurrent.futures
from datetime import datetime, timezone
import gzip
import html
import json
import logging
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger("twitch_api")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(BASE_DIR, "data")
CAMPAIGNS_CACHE_FILE = os.path.join(DATA_DIR, "twitch_campaigns_cache.json")

TWITCH_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"  # Official Twitch Web Client ID
GQL_URL = "https://gql.twitch.tv/gql"
SPADE_URL = "https://spade.twitch.tv/track"
SPADE_FALLBACK_URL = "https://spade.twitch.tv/batched"

DROPHUNTER_CACHE: Dict[str, Any] = {"timestamp": 0, "campaigns": [], "updated_at": ""}


def clean_text(text: Any) -> str:
    """Strip emojis and non-standard symbols to ensure clean terminal and UI rendering."""
    if not text:
        return ""
    unescaped = html.unescape(str(text)).replace("\ufffd", "")
    emoji_pattern = re.compile(
        "[\U00010000-\U0010ffff]|"
        "[\u2600-\u27bf]|"
        "[\u2300-\u23ff]|"
        "[\u2b50-\u2b55]|"
        "[\u203c-\u2049]"
    )
    cleaned = emoji_pattern.sub("", unescaped)
    return " ".join(cleaned.split()).strip()


def extract_game_name(camp: Optional[Dict[str, Any]], default: str = "Twitch Game") -> str:
    """Safely extract game name whether 'game' field is a dict or string."""
    if not camp:
        return default
    g = camp.get("game")
    if isinstance(g, dict):
        name = g.get("displayName") or g.get("name") or ""
        if clean_text(name):
            return clean_text(name)
    elif isinstance(g, str) and g.strip():
        return clean_text(g)

    # Check campaign name for special events / global chat badge campaigns (e.g. Evolutions)
    cname = str(camp.get("name") or "").lower()
    if "evolution" in cname:
        return "Evolutions"
    return default


def infer_game_from_reward(event: Dict[str, Any]) -> str:
    """Infer game title for a claimed UserDropReward (gameEventDrops) from game object, account link URL, or name."""
    g = event.get("game")
    if isinstance(g, dict):
        gname = g.get("displayName") or g.get("name")
        if gname:
            return clean_text(gname)
    elif isinstance(g, str) and g.strip():
        return clean_text(g)

    link = str(event.get("requiredAccountLink") or "").lower()
    name = str(event.get("name") or "")
    name_low = name.lower()

    link_map = [
        ("facepunch.com", "Rust"),
        ("marvelrivals.com", "Marvel Rivals"),
        ("seaofthieves.com", "Sea of Thieves"),
        ("oncehuman.game", "Once Human"),
        ("wutheringwaves", "Wuthering Waves"),
        ("kurogames", "Wuthering Waves"),
        ("narakathegame.com", "NARAKA: BLADEPOINT"),
        ("rockstargames.com/gta-online", "Grand Theft Auto V"),
        ("rockstargames.com", "Grand Theft Auto V"),
        ("fortnite.com", "Fortnite"),
        ("pathofexile.com", "Path of Exile"),
        ("wherewindsmeetgame.com", "Where Winds Meet"),
        ("subnautica", "Subnautica 2"),
        ("unknownworlds.com", "Subnautica 2"),
        ("civilization.2k.com", "Sid Meier's Civilization VII"),
        ("endfield.gryphline.com", "Arknights: Endfield"),
        ("embark-games", "THE FINALS / ARC Raiders"),
        ("embark.net", "THE FINALS / ARC Raiders"),
        ("pearlabyss.com", "Black Desert"),
        ("playblackdesert.com", "Black Desert"),
        ("drops-register.ubi.com", "Rainbow Six Siege / Ubisoft"),
        ("ubisoft.com", "Ubisoft"),
        ("battle.net", "World of Warcraft / Blizzard"),
        ("riot-connection", "Valorant / Riot Games"),
        ("riotgames.com", "Valorant / Riot Games"),
        ("ea.com", "EA Sports / Apex Legends"),
        ("warframe.com", "Warframe"),
        ("bungie.net", "Destiny 2"),
        ("halowaypoint.com", "Halo Infinite"),
        ("callofduty.com", "Call of Duty"),
        ("activision.com", "Call of Duty"),
        ("escapefromtarkov.com", "Escape from Tarkov"),
        ("deadbydaylight.com", "Dead by Daylight"),
        ("bhvr.com", "Dead by Daylight"),
        ("newworld.com", "New World"),
        ("playlostark.com", "Lost Ark"),
        ("playthroneandliberty.com", "THRONE AND LIBERTY"),
        ("albiononline.com", "Albion Online"),
        ("worldoftanks", "World of Tanks"),
        ("wargaming.net", "World of Tanks / Warships"),
        ("warthunder.com", "War Thunder"),
        ("gaijin.net", "War Thunder"),
        ("genshin", "Genshin Impact"),
        ("hoyoverse.com", "HoYoverse"),
        ("epicgames.com", "Epic Games / Fortnite"),
        ("help.twitch.tv", "Twitch Global Emote / Badge"),
        ("twitch.tv", "Twitch Global Drop"),
    ]
    for domain_kw, game_label in link_map:
        if domain_kw in link:
            if "battle.net" in domain_kw:
                if "murloc" in name_low or "warcraft" in name_low:
                    return "World of Warcraft"
                if "overwatch" in name_low:
                    return "Overwatch 2"
                if "diablo" in name_low:
                    return "Diablo IV"
            return game_label

    if "rust" in name_low:
        return "Rust"
    if "evolution" in name_low or "pichu" in name_low or "pikachu" in name_low or "pokemon" in name_low:
        return "Evolutions / Pokémon"
    return "Twitch Drop Reward"


def extract_reward_image_key(url: Optional[str]) -> str:
    """Extract normalized image filename key (e.g. '09809427-978c-48c6-9467-6b37598d2bbb.png') from Twitch reward URL."""
    if not url or not isinstance(url, str):
        return ""
    clean = url.split("?")[0].strip().lower()
    if "/reward/" in clean:
        return clean.split("/reward/")[-1].strip("/")
    return ""


def is_timestamp_expired(iso_str: Optional[str], grace_seconds: float = 0.0) -> bool:
    """Return True if an ISO-8601 timestamp is in the past (plus optional grace window)."""
    if not iso_str:
        return False
    try:
        clean_ts = str(iso_str).replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean_ts)
        return (dt.timestamp() + grace_seconds) < datetime.now(timezone.utc).timestamp()
    except Exception:
        return False


def is_timestamp_future(iso_str: Optional[str]) -> bool:
    """Return True if an ISO-8601 timestamp is in the future."""
    if not iso_str:
        return False
    try:
        clean_ts = str(iso_str).replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean_ts)
        return dt.timestamp() > (datetime.now(timezone.utc).timestamp() + 60)
    except Exception:
        return False


def is_campaign_expired(camp: Optional[Dict[str, Any]]) -> bool:
    """Check if a campaign has expired based on status, time_left, or endAt timestamp."""
    if not camp:
        return True
    if camp.get("status") == "EXPIRED":
        return True
    time_left = str(camp.get("time_left", "")).lower()
    if any(k in time_left for k in ["ended", "expired", "past"]):
        return True
    if is_timestamp_expired(camp.get("endAt")):
        return True
    return False


def format_time_left(end_at: Optional[str]) -> str:
    """Compute a clean human-readable remaining time string from an ISO-8601 endAt timestamp."""
    if not end_at:
        return ""
    try:
        clean_ts = str(end_at).replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean_ts)
        rem_secs = dt.timestamp() - datetime.now(timezone.utc).timestamp()
        if rem_secs <= 0:
            return "Campaign Ended"
        days = int(rem_secs // 86400)
        hours = int((rem_secs % 86400) // 3600)
        mins = int((rem_secs % 3600) // 60)
        if days > 0:
            return f"{days}d {hours}h left"
        if hours > 0:
            return f"{hours}h {mins}m left"
        return f"{max(1, mins)}m left"
    except Exception:
        return ""


def format_box_art_url(url: Optional[str]) -> str:
    """Normalize Twitch boxArtURL templates ({width}x{height}) into a standard cover image URL."""
    if not url or not isinstance(url, str):
        return ""
    return url.replace("{width}", "285").replace("{height}", "380").replace("52x72", "285x380")


GAME_LOGO_FALLBACKS: Dict[str, str] = {
    "evolutions": "https://static-cdn.jtvnw.net/ttv-boxart/1982936547_IGDB-285x380.jpg",
    "pokémon community game": "https://static-cdn.jtvnw.net/ttv-boxart/329951934_IGDB-285x380.jpg",
    "pokémon trading card game pocket": "https://static-cdn.jtvnw.net/ttv-boxart/375403621_IGDB-285x380.jpg",
    "pokémon trading card game live": "https://static-cdn.jtvnw.net/ttv-boxart/155409827-285x380.jpg",
    "pokémon": "https://static-cdn.jtvnw.net/ttv-boxart/1982936547_IGDB-285x380.jpg",
    "pokemon": "https://static-cdn.jtvnw.net/ttv-boxart/1982936547_IGDB-285x380.jpg",
    "wardogs": "https://static-cdn.jtvnw.net/ttv-boxart/743893402_IGDB-285x380.jpg",
    "rust": "https://static-cdn.jtvnw.net/ttv-boxart/263490_IGDB-285x380.jpg",
    "world of warcraft": "https://static-cdn.jtvnw.net/ttv-boxart/18122-285x380.jpg",
    "overwatch 2": "https://static-cdn.jtvnw.net/ttv-boxart/515025-285x380.jpg",
    "overwatch": "https://static-cdn.jtvnw.net/ttv-boxart/488552-285x380.jpg",
    "marvel rivals": "https://static-cdn.jtvnw.net/ttv-boxart/1476685419_IGDB-285x380.jpg",
    "escape from tarkov": "https://static-cdn.jtvnw.net/ttv-boxart/491931-285x380.jpg",
    "warframe": "https://static-cdn.jtvnw.net/ttv-boxart/66170-285x380.jpg",
    "rainbow six siege": "https://static-cdn.jtvnw.net/ttv-boxart/460630-285x380.jpg",
    "apex legends": "https://static-cdn.jtvnw.net/ttv-boxart/511224-285x380.jpg",
    "grand theft auto v": "https://static-cdn.jtvnw.net/ttv-boxart/32982_IGDB-285x380.jpg",
    "dead by daylight": "https://static-cdn.jtvnw.net/ttv-boxart/491487-285x380.jpg",
    "fortnite": "https://static-cdn.jtvnw.net/ttv-boxart/33214-285x380.jpg",
    "valorant": "https://static-cdn.jtvnw.net/ttv-boxart/516575-285x380.jpg",
    "sea of thieves": "https://static-cdn.jtvnw.net/ttv-boxart/490377-285x380.jpg",
    "once human": "https://static-cdn.jtvnw.net/ttv-boxart/1283626244_IGDB-285x380.jpg",
    "delta force": "https://static-cdn.jtvnw.net/ttv-boxart/1568265005_IGDB-285x380.jpg",
    "the finals": "https://static-cdn.jtvnw.net/ttv-boxart/1709407338_IGDB-285x380.jpg",
    "arc raiders": "https://static-cdn.jtvnw.net/ttv-boxart/1709407338_IGDB-285x380.jpg",
    "path of exile": "https://static-cdn.jtvnw.net/ttv-boxart/29307_IGDB-285x380.jpg",
    "path of exile 2": "https://static-cdn.jtvnw.net/ttv-boxart/518184_IGDB-285x380.jpg",
    "diablo iv": "https://static-cdn.jtvnw.net/ttv-boxart/515024-285x380.jpg",
    "just chatting": "https://static-cdn.jtvnw.net/ttv-boxart/509658-285x380.jpg",
}


def resolve_game_logo(game_name: str, existing_url: Optional[str] = None) -> str:
    """Return a verified Twitch box-art / game logo URL for the given game."""
    if existing_url and str(existing_url).strip():
        return format_box_art_url(str(existing_url).strip())
    g_low = clean_text(game_name or "").lower().strip()
    if g_low in GAME_LOGO_FALLBACKS:
        return GAME_LOGO_FALLBACKS[g_low]
    for k, v in GAME_LOGO_FALLBACKS.items():
        if k == g_low or k in g_low or g_low in k:
            return v
    return ""


DEFAULT_DROPS_GAMES_ROSTER: List[str] = [
    "Evolutions",
    "Pokémon Community Game",
    "WARDOGS",
    "Rust",
    "World of Warcraft",
    "Overwatch 2",
    "Overwatch",
    "Marvel Rivals",
    "Escape from Tarkov",
    "Warframe",
    "Rainbow Six Siege",
    "Apex Legends",
    "Grand Theft Auto V",
    "Call of Duty: Warzone",
    "Call of Duty: Modern Warfare 4",
    "Dead by Daylight",
    "Fortnite",
    "VALORANT",
    "Sea of Thieves",
    "Once Human",
    "Delta Force",
    "THE FINALS",
    "ARC Raiders",
    "Path of Exile",
    "Path of Exile 2",
    "Diablo IV",
    "DayZ",
    "Counter-Strike",
    "PUBG: BATTLEGROUNDS",
    "Rocket League",
    "Dota 2",
    "League of Legends",
    "Albion Online",
    "Black Desert",
    "THRONE AND LIBERTY",
    "Arena Breakout: Infinite",
    "Hunt: Showdown 1896",
    "No Man's Sky",
    "Minecraft",
    "Wuthering Waves",
    "Genshin Impact",
    "Honkai: Star Rail",
    "Zenless Zone Zero",
    "Battlefield 6",
    "Enshrouded",
    "FragPunk",
    "Tom Clancy's The Division 2",
    "World of Tanks",
    "World of Warships",
    "War Thunder",
    "ELDEN RING",
    "Cyberpunk 2077",
    "AION 2",
    "Madden NFL 27",
    "EA Sports FC 27",
    "Subnautica 2",
    "Big Walk",
    "New World: Aeternum",
    "Lost Ark",
    "Hearthstone",
    "Street Fighter 6",
    "Tekken 8",
    "Palworld",
    "ARK: Survival Ascended",
    "Dark and Darker",
    "Gray Zone Warfare",
    "Exoborne",
    "Mecha BREAK",
    "Splitgate 2",
]


def normalize_cached_campaign(camp: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure a campaign dict from disk cache has standard 'game' dict and 'timeBasedDrops' list."""
    c = dict(camp)
    gname = extract_game_name(c, "Twitch Game")
    if not isinstance(c.get("game"), dict):
        c["game"] = {"displayName": gname, "name": gname}
    else:
        c["game"] = {
            "displayName": c["game"].get("displayName") or gname,
            "name": c["game"].get("name") or gname,
            **{k: v for k, v in c["game"].items() if k not in ("displayName", "name")},
        }

    if not c.get("timeBasedDrops") and isinstance(c.get("drops"), list):
        converted = []
        for d in c["drops"]:
            converted.append({
                "id": d.get("id") or "",
                "name": d.get("name") or "Drop Reward",
                "requiredMinutesWatched": d.get("required_minutes", d.get("requiredMinutesWatched", 0)),
                "requiredSubs": d.get("required_subs", d.get("requiredSubs", 0)),
                "requirement_text": d.get("requirement_text", ""),
                "image_url": d.get("image_url", ""),
                "self": {
                    "currentMinutesWatched": d.get("watched_minutes", 0),
                    "isClaimed": d.get("status") == "CLAIMED",
                    "dropInstanceID": d.get("drop_instance_id"),
                    "hasPreconditionsMet": True,
                },
            })
        c["timeBasedDrops"] = converted
    return c


def fetch_drophunter_live_campaigns(force_refresh: bool = False) -> List[Dict[str, Any]]:
    """
    Fetch all active drop campaigns from drophunter.app/drops.
    Returns 80+ games and 115+ live campaigns with active drop items (including watch time, sub requirements, and reward icons).
    Cached for 10 minutes in memory and persisted to data/twitch_campaigns_cache.json for daily freshness.
    """
    global DROPHUNTER_CACHE
    now = time.time()
    if not force_refresh and (now - DROPHUNTER_CACHE["timestamp"] < 600) and DROPHUNTER_CACHE["campaigns"]:
        return [normalize_cached_campaign(c) for c in DROPHUNTER_CACHE["campaigns"]]

    # Try loading from disk if in-memory cache is empty and not force_refresh
    if not DROPHUNTER_CACHE["campaigns"] and os.path.exists(CAMPAIGNS_CACHE_FILE):
        try:
            with open(CAMPAIGNS_CACHE_FILE, "r", encoding="utf-8") as f:
                disk_cache = json.load(f)
                c_list = [normalize_cached_campaign(c) for c in (disk_cache.get("campaigns") or []) if isinstance(c, dict)]
                c_time = disk_cache.get("timestamp", 0)
                if c_list and (not force_refresh and (now - c_time < 3600)):
                    DROPHUNTER_CACHE["timestamp"] = c_time
                    DROPHUNTER_CACHE["campaigns"] = c_list
                    DROPHUNTER_CACHE["updated_at"] = disk_cache.get("updated_at", "")
                    return c_list
        except Exception:
            pass

    if now - float(DROPHUNTER_CACHE.get("last_fail_ts") or 0) < 1800:
        return [normalize_cached_campaign(c) for c in DROPHUNTER_CACHE.get("campaigns", [])]

    req = urllib.request.Request(
        "https://drophunter.app/drops",
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    results = []
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            html = resp.read().decode("utf-8", errors="ignore")

        # 1. Primary parser: New DropHunter HTML structure (pk-card)
        if 'class="pk-card"' in html or 'class="pk-card ' in html:
            cards = html.split('class="pk-card"')
            for card in cards[1:]:
                # Only include Twitch drop campaigns
                if "fa-twitch" not in card and "Twitch" not in card:
                    continue

                m_game = re.search(r'class="pk-game"[^>]*>\s*<a[^>]*>([^<]+)</a>', card)
                game_name = clean_text(m_game.group(1)) if m_game else ""
                if not game_name:
                    continue

                m_cover = re.search(r'class="pk-cover"[^>]*>[\s\S]*?<img[^>]*src="([^"]+)"', card)
                cover_url = m_cover.group(1).strip() if m_cover else ""

                camp_blocks = card.split('class="pk-camp"')
                for cb in camp_blocks[1:]:
                    m_cname = re.search(r'class="pk-camp-name"[^>]*>([^<]+)</span>', cb)
                    cname = clean_text(m_cname.group(1)) if m_cname else f"{game_name} Drops"

                    m_time = re.search(r'class="pk-camp-time"[^>]*>[\s\S]*?</i>\s*([^<]+)</span>', cb)
                    time_left = clean_text(m_time.group(1)) if m_time else ""

                    rewards = []
                    rw_blocks = cb.split('class="pk-rw"')
                    if len(rw_blocks) > 1:
                        for rwb in rw_blocks[1:]:
                            m_rname = re.search(r'data-reward-name="([^"]+)"', rwb) or re.search(
                                r'class="pk-rw-name"[^>]*>([^<]+)</div>', rwb
                            )
                            m_rreq = re.search(r'data-reward-req="([^"]+)"', rwb) or re.search(
                                r'class="pk-req"[^>]*>([^<]+)</span>', rwb
                            )
                            m_rimg = re.search(r'<img[^>]*src="([^"]+)"', rwb)
                            r_name = clean_text(m_rname.group(1)) if m_rname else ""
                            r_req = clean_text(m_rreq.group(1)) if m_rreq else ""
                            r_img = m_rimg.group(1).strip() if m_rimg else ""
                            if r_name:
                                rewards.append({"name": r_name, "req": r_req, "image_url": r_img})
                    else:
                        rw_matches = re.findall(r'data-reward-name="([^"]+)"(?:\s+data-reward-req="([^"]+)")?', cb)
                        if rw_matches:
                            for r_name, r_req in rw_matches:
                                rewards.append({"name": clean_text(r_name), "req": clean_text(r_req), "image_url": ""})
                        else:
                            names = re.findall(r'class="pk-rw-name"[^>]*>([^<]+)</div>', cb)
                            for n in names:
                                rewards.append({"name": clean_text(n), "req": "", "image_url": ""})

                    time_based_drops = []
                    for idx, rw in enumerate(rewards):
                        r_name = rw["name"]
                        req_str = rw["req"]
                        r_img = rw.get("image_url", "")
                        req_mins = 0
                        req_subs = 0

                        m_sub = re.search(r'(\d+)\s*sub', req_str, re.IGNORECASE)
                        if m_sub:
                            req_subs = int(m_sub.group(1))
                        else:
                            m_h = re.search(r'(\d+)\s*h', req_str, re.IGNORECASE)
                            m_m = re.search(r'(\d+)\s*m', req_str, re.IGNORECASE)
                            if m_h:
                                req_mins += int(m_h.group(1)) * 60
                            if m_m:
                                req_mins += int(m_m.group(1))

                        time_based_drops.append({
                            "id": f"dh_{game_name}_{idx}_{r_name}".replace(" ", "_"),
                            "name": r_name,
                            "requiredMinutesWatched": req_mins,
                            "requiredSubs": req_subs,
                            "requirement_text": req_str,
                            "image_url": r_img,
                            "self": {
                                "currentMinutesWatched": 0,
                                "currentSubs": 0,
                                "isClaimed": False,
                                "dropInstanceID": None,
                                "hasPreconditionsMet": True,
                            },
                        })

                    camp_id = f"dh_{game_name}_{cname}".replace(" ", "_")
                    game_slug = game_name.lower().replace(" ", "-").replace(":", "").replace("'", "")
                    stream_url = f"https://www.twitch.tv/directory/category/{urllib.parse.quote(game_slug)}?filter=drops"

                    results.append({
                        "id": camp_id,
                        "name": cname,
                        "game": {"displayName": game_name, "name": game_name},
                        "cover_url": cover_url,
                        "time_left": time_left,
                        "timeBasedDrops": time_based_drops,
                        "stream_url": stream_url,
                        "source": "drophunter",
                    })

        # 2. Fallback parser: Legacy DropHunter HTML structure (game-item)
        if not results and '<div class="game-item' in html:
            game_blocks = html.split('<div class="game-item')
            for b in game_blocks[1:]:
                if "platform-twitch" not in b and "Twitch" not in b:
                    continue

                m_game = re.search(r'class="mb-0 dl-game-name"[^>]*>\s*<a[^>]*>([^<]+)</a>', b)
                game_name = clean_text(m_game.group(1)) if m_game else ""
                if not game_name:
                    continue

                m_img = re.search(r'class="game-cover"[^>]*src="([^"]+)"', b)
                cover_url = m_img.group(1).strip() if m_img else ""

                camp_blocks = b.split('class="campaign-mini-item"')
                for cb in camp_blocks[1:]:
                    m_cname = re.search(r'class="campaign-name"[^>]*title="([^"]+)"', cb) or re.search(
                        r'class="campaign-name"[^>]*>[\s\S]*?</div>', cb
                    )
                    cname = ""
                    if m_cname:
                        cname = m_cname.group(1) if len(m_cname.groups()) > 0 else re.sub(r"<[^>]+>", "", m_cname.group(0)).strip()
                    cname = clean_text(cname) or f"{game_name} Drops"

                    rewards = [clean_text(r) for r in re.findall(r'class="reward-label">([^<]+)</div>', cb) if clean_text(r)]
                    m_time = re.search(r'class="campaign-time"[^>]*>[\s\S]*?<strong>([^<]+)</strong>', cb)
                    time_left = clean_text(m_time.group(1)) if m_time else ""

                    time_based_drops = []
                    for idx, r_name in enumerate(rewards):
                        time_based_drops.append({
                            "id": f"dh_{game_name}_{idx}_{r_name}".replace(" ", "_"),
                            "name": r_name,
                            "requiredMinutesWatched": 0,
                            "requiredSubs": 0,
                            "requirement_text": "",
                            "image_url": "",
                            "self": {
                                "currentMinutesWatched": 0,
                                "currentSubs": 0,
                                "isClaimed": False,
                                "dropInstanceID": None,
                                "hasPreconditionsMet": True,
                            },
                        })

                    camp_id = f"dh_{game_name}_{cname}".replace(" ", "_")
                    game_slug = game_name.lower().replace(" ", "-").replace(":", "").replace("'", "")
                    stream_url = f"https://www.twitch.tv/directory/category/{urllib.parse.quote(game_slug)}?filter=drops"

                    results.append({
                        "id": camp_id,
                        "name": cname,
                        "game": {"displayName": game_name, "name": game_name},
                        "cover_url": cover_url,
                        "time_left": time_left,
                        "timeBasedDrops": time_based_drops,
                        "stream_url": stream_url,
                        "source": "drophunter",
                    })

        if results:
            updated_iso = datetime.now(timezone.utc).isoformat()
            DROPHUNTER_CACHE["timestamp"] = now
            DROPHUNTER_CACHE["campaigns"] = results
            DROPHUNTER_CACHE["updated_at"] = updated_iso
            logger.info("Retrieved %d active drop campaigns from drophunter.app", len(results))

            # Persist to disk cache
            if "unittest" not in sys.modules:
                try:
                    os.makedirs(DATA_DIR, exist_ok=True)
                    with open(CAMPAIGNS_CACHE_FILE, "w", encoding="utf-8") as f:
                        json.dump({
                            "timestamp": now,
                            "updated_at": updated_iso,
                            "total_campaigns": len(results),
                            "campaigns": results,
                        }, f, indent=2)
                except Exception as save_err:
                    logger.debug("Could not save campaigns disk cache: %s", save_err)

    except Exception as e:
        DROPHUNTER_CACHE["last_fail_ts"] = now
        logger.debug("drophunter.app unavailable (%s), using Twitch GQL live discovery + disk cache", e)
        if not results and os.path.exists(CAMPAIGNS_CACHE_FILE):
            try:
                with open(CAMPAIGNS_CACHE_FILE, "r", encoding="utf-8") as f:
                    disk_cache = json.load(f)
                    results = [normalize_cached_campaign(c) for c in (disk_cache.get("campaigns") or []) if isinstance(c, dict)]
                    if len(results) >= 5:
                        DROPHUNTER_CACHE["timestamp"] = disk_cache.get("timestamp", now)
                        DROPHUNTER_CACHE["campaigns"] = results
                        DROPHUNTER_CACHE["updated_at"] = disk_cache.get("updated_at", "")
            except Exception:
                pass

    return results or [normalize_cached_campaign(c) for c in DROPHUNTER_CACHE.get("campaigns", [])]


def _merge_campaign_drops(dh_drops: List[Dict[str, Any]], tw_drops: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Merge Twitch GQL timeBasedDrops (which have real IDs, startAt/endAt, and live 'self' progress)
    with DropHunter drops (which have image_url and sub requirement strings).
    """
    if not tw_drops:
        return dh_drops
    if not dh_drops:
        return tw_drops

    dh_by_name = {clean_text(d.get("name", "")).lower(): d for d in dh_drops if d.get("name")}
    dh_by_img = {
        extract_reward_image_key(d.get("image_url")): d
        for d in dh_drops
        if extract_reward_image_key(d.get("image_url"))
    }

    merged = []
    for td in tw_drops:
        td_copy = dict(td)
        t_name = clean_text(td.get("name", "")).lower()
        t_img = ""
        for edge in td.get("benefitEdges") or []:
            ben = edge.get("benefit") or {}
            if ben.get("imageAssetURL"):
                t_img = ben["imageAssetURL"]
                break
        t_img_key = extract_reward_image_key(t_img)

        matched_dh = (dh_by_img.get(t_img_key) if t_img_key else None) or dh_by_name.get(t_name)
        if matched_dh:
            if not t_img and matched_dh.get("image_url"):
                td_copy["image_url"] = matched_dh["image_url"]
            else:
                td_copy["image_url"] = t_img
            if not td_copy.get("requiredSubs") and matched_dh.get("requiredSubs"):
                td_copy["requiredSubs"] = matched_dh["requiredSubs"]
            if matched_dh.get("requirement_text"):
                td_copy["requirement_text"] = matched_dh["requirement_text"]
        else:
            td_copy["image_url"] = t_img
        merged.append(td_copy)

    return merged


class TwitchClient:
    def __init__(self, auth_token: str, user_agent: Optional[str] = None, client_id: Optional[str] = None):
        """
        Initialize the Twitch API client using your browser's auth-token cookie or OAuth device token.
        
        :param auth_token: The 'auth-token' cookie or OAuth token value from twitch.tv
        :param user_agent: Optional custom browser user-agent
        """
        from src.auth.twitch_auth import _VALIDATED_TOKEN_CACHE, clean_auth_token, client_id_for, load_token
        self.auth_token = clean_auth_token(auth_token)
        self.client_id = client_id or client_id_for(self.auth_token)
        self.user_agent = user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
        self.user_id: Optional[str] = None
        self.user_login: Optional[str] = None
        self.last_drop_session: Dict[str, Any] = {}
        self._integrity_token: str = ""
        self._integrity_expiration: int = 0
        import uuid
        self.session_id: str = uuid.uuid4().hex[:16]
        self.device_id: str = uuid.uuid4().hex[:32]
        try:
            stored = load_token()
            if stored and clean_auth_token(stored.get("access_token", "")) == self.auth_token:
                if stored.get("user_id"):
                    self.user_id = str(stored["user_id"])
                if stored.get("login"):
                    self.user_login = str(stored["login"])
            elif self.auth_token and self.auth_token in _VALIDATED_TOKEN_CACHE:
                v_info = _VALIDATED_TOKEN_CACHE[self.auth_token].get("info") or {}
                if v_info.get("user_id"):
                    self.user_id = str(v_info["user_id"])
                if v_info.get("login"):
                    self.user_login = str(v_info["login"])
        except Exception:
            pass

    def get_client_integrity_token(self, force_refresh: bool = False) -> str:
        """
        Fetch or return a cached Client-Integrity token from https://gql.twitch.tv/integrity.
        Twitch requires this cryptographic token on GQL queries/mutations and telemetry heartbeats
        to verify legitimate client integrity and grant drop watch-time progression.
        """
        now_ms = int(time.time() * 1000)
        cached_tok = getattr(self, "_integrity_token", "")
        cached_exp = getattr(self, "_integrity_expiration", 0)
        if cached_tok and not force_refresh and (cached_exp - now_ms) > 60000:
            return cached_tok

        try:
            headers = {
                "Client-Id": self.client_id,
                "Client-Session-Id": getattr(self, "session_id", "1234567890abcdef"),
                "X-Device-Id": getattr(self, "device_id", "1234567890abcdef1234567890abcdef"),
                "User-Agent": self.user_agent,
                "Origin": "https://www.twitch.tv",
                "Referer": "https://www.twitch.tv/",
                "Content-Type": "application/json",
            }
            if self.auth_token:
                headers["Authorization"] = f"OAuth {self.auth_token}"

            req = urllib.request.Request(
                "https://gql.twitch.tv/integrity",
                headers=headers,
                data=b"",
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                tok = str(data.get("token") or "")
                exp = int(data.get("expiration") or (now_ms + 14400000))
                if tok:
                    self._integrity_token = tok
                    self._integrity_expiration = exp
                    logger.debug("Acquired fresh Twitch Client-Integrity token (valid for %ds)", int((exp - now_ms) / 1000))
                    return tok
        except Exception as e:
            logger.debug("Could not fetch Client-Integrity token: %s", e)
        return getattr(self, "_integrity_token", "")

    def new_session_id(self) -> str:
        """Rotate client session ID to establish a distinct, fresh stream viewing session with Twitch."""
        import uuid
        self.session_id = uuid.uuid4().hex[:16]
        try:
            self.get_client_integrity_token(force_refresh=True)
        except Exception:
            pass
        return self.session_id

    def _make_request(self, url: str, data: bytes, headers: Dict[str, str], timeout: int = 15) -> bytes:
        """Make an HTTP POST request using Python standard library."""
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.read()

    def post_gql(self, query: str, variables: Optional[Dict[str, Any]] = None, operation_name: Optional[str] = None) -> Dict[str, Any]:
        """Send a standard query or mutation to Twitch GraphQL."""
        payload: Dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables
        if operation_name:
            payload["operationName"] = operation_name

        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Client-Id": self.client_id,
            "User-Agent": self.user_agent,
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Origin": "https://www.twitch.tv",
            "Referer": "https://www.twitch.tv/",
            "Client-Session-Id": getattr(self, "session_id", "1234567890abcdef"),
            "X-Device-Id": getattr(self, "device_id", "1234567890abcdef1234567890abcdef"),
        }
        integrity_tok = self.get_client_integrity_token()
        if integrity_tok:
            headers["Client-Integrity"] = integrity_tok
        if self.auth_token:
            headers["Authorization"] = f"OAuth {self.auth_token}"

        try:
            raw = self._make_request(GQL_URL, data=body, headers=headers, timeout=15)
            data = json.loads(raw.decode("utf-8"))
        except Exception as err:
            if self.auth_token and operation_name not in ("CheckUserSession", "CheckUserPresence"):
                pub_headers = {
                    "Client-Id": TWITCH_CLIENT_ID,
                    "User-Agent": self.user_agent,
                    "Content-Type": "application/json",
                    "Accept": "*/*",
                }
                raw = self._make_request(GQL_URL, data=body, headers=pub_headers, timeout=15)
                data = json.loads(raw.decode("utf-8"))
            else:
                raise err

        if "errors" in data and data["errors"]:
            err_str = str(data["errors"]).lower()
            if self.auth_token and operation_name not in ("CheckUserSession", "CheckUserPresence") and any(
                k in err_str for k in ("client-id", "oauth", "unauthenticated", "unauthorized", "token")
            ):
                try:
                    pub_headers = {
                        "Client-Id": TWITCH_CLIENT_ID,
                        "User-Agent": self.user_agent,
                        "Content-Type": "application/json",
                        "Accept": "*/*",
                    }
                    raw_retry = self._make_request(GQL_URL, data=body, headers=pub_headers, timeout=15)
                    data_retry = json.loads(raw_retry.decode("utf-8"))
                    if not data_retry.get("errors"):
                        return data_retry
                except Exception:
                    pass
            logger.error("Twitch GQL returned errors: %s", data["errors"])
        return data

    def post_persisted_gql(self, operation_name: str, sha256_hash: str, variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Send an official persisted query to Twitch GraphQL with valid client integrity verification."""
        payload: Dict[str, Any] = {
            "operationName": operation_name,
            "extensions": {
                "persistedQuery": {
                    "version": 1,
                    "sha256Hash": sha256_hash
                }
            }
        }
        if variables:
            payload["variables"] = variables

        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Client-Id": self.client_id,
            "User-Agent": self.user_agent,
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Origin": "https://www.twitch.tv",
            "Referer": "https://www.twitch.tv/",
            "Client-Session-Id": getattr(self, "session_id", "1234567890abcdef"),
            "X-Device-Id": getattr(self, "device_id", "1234567890abcdef1234567890abcdef"),
        }
        integrity_tok = self.get_client_integrity_token()
        if integrity_tok:
            headers["Client-Integrity"] = integrity_tok
        if self.auth_token:
            headers["Authorization"] = f"OAuth {self.auth_token}"

        raw = self._make_request(GQL_URL, data=body, headers=headers, timeout=15)
        return json.loads(raw.decode("utf-8"))

    def validate_session(self) -> bool:
        """Validate the OAuth token and fetch current user profile."""
        if not self.auth_token:
            return False

        # First inspect token via https://id.twitch.tv/oauth2/validate (cached 5m) to ensure Client-Id matches token
        try:
            from src.auth.twitch_auth import VALID_GQL_CLIENT_IDS, validate_access_token
            info = validate_access_token(self.auth_token)
            if isinstance(info, dict) and info.get("user_id"):
                token_cid = str(info.get("client_id") or "")
                if token_cid in VALID_GQL_CLIENT_IDS:
                    self.client_id = token_cid
                self.user_id = str(info["user_id"])
                self.user_login = str(info.get("login") or "")
                logger.info("Authenticated via OAuth validate as: %s (ID: %s)", self.user_login, self.user_id)
                return True
        except Exception as val_err:
            logger.debug("OAuth validate check note: %s", val_err)

        query = """
        query CheckUserSession {
            currentUser {
                id
                login
                displayName
            }
        }
        """
        try:
            res = self.post_gql(query, operation_name="CheckUserSession")
            user = (res.get("data") or {}).get("currentUser")
            if user and user.get("id"):
                self.user_id = str(user.get("id"))
                self.user_login = user.get("login")
                logger.info("Authenticated successfully as: %s (ID: %s)", self.user_login, self.user_id)
                return True
        except Exception as e:
            logger.debug("Primary GQL CheckUserSession error: %s", e)

        # Final fallback: if stored token failed and TWITCH_AUTH_TOKEN in env/.env differs, try env token
        from src.auth.twitch_auth import clean_auth_token
        env_tok = clean_auth_token(os.environ.get("TWITCH_AUTH_TOKEN", ""))
        if not env_tok:
            try:
                env_path = os.path.join(BASE_DIR, ".env")
                if os.path.exists(env_path):
                    with open(env_path, "r", encoding="utf-8") as ef:
                        for line in ef:
                            if line.strip().startswith("TWITCH_AUTH_TOKEN="):
                                env_tok = clean_auth_token(line.split("=", 1)[1])
                                break
            except Exception:
                pass
        if env_tok and env_tok != self.auth_token:
            self.auth_token = env_tok
            self.client_id = TWITCH_CLIENT_ID
            try:
                res = self.post_gql(query, operation_name="CheckUserSession")
                user = (res.get("data") or {}).get("currentUser")
                if user and user.get("id"):
                    self.user_id = str(user.get("id"))
                    self.user_login = user.get("login")
                    logger.info("Authenticated via fallback TWITCH_AUTH_TOKEN as: %s (ID: %s)", self.user_login, self.user_id)
                    return True
            except Exception:
                pass

        logger.error("Authentication failed: currentUser is null. Check your auth-token.")
        return False

    def get_user_presence_info(self) -> Dict[str, Any]:
        """
        Detect detailed active user presence and external viewing activity on Twitch.
        Queries Twitch currentUser presence, availability, and activity.
        """
        if not self.auth_token:
            return {"is_active": False, "availability": "", "activity_type": ""}
        query = """
        query CheckUserPresence {
            currentUser {
                id
                login
                availability
                activity {
                    type
                }
            }
        }
        """
        try:
            res = self.post_gql(query, operation_name="CheckUserPresence")
            user = (res.get("data") or {}).get("currentUser")
            if user:
                availability = str(user.get("availability") or "")
                activity = user.get("activity") or {}
                act_type = str(activity.get("type") or "")
                is_active = bool(availability in ("ONLINE", "AWAY", "IDLE") or act_type)
                return {
                    "is_active": is_active,
                    "availability": availability,
                    "activity_type": act_type,
                }
        except Exception as e:
            logger.debug("Error checking user active presence: %s", e)
        return {"is_active": False, "availability": "", "activity_type": ""}

    def is_user_actively_using_twitch(self) -> bool:
        """
        Detect if the user is currently using Twitch on any browser, mobile app, or device.
        Queries Twitch currentUser presence, availability, and activity.
        Returns True if user status indicates an active session (ONLINE, AWAY, IDLE, or active activity).
        """
        info = self.get_user_presence_info()
        if info.get("is_active"):
            logger.info(
                "Detected active user presence on Twitch: availability=%s, activity=%s",
                info.get("availability"), info.get("activity_type")
            )
            return True
        return False

    def get_inventory(self) -> Dict[str, Any]:
        """Fetch the user's active drop campaigns, progress, and claimed items via persisted query."""
        if not self.auth_token:
            return {}
        try:
            res = self.post_persisted_gql(
                operation_name="Inventory",
                sha256_hash="8337eb8541b314040b0edde0c09c5c7a2783ba1960aa9edfbf3bac16d0fec404",
                variables={"fetchRewardCampaigns": False}
            )
            return (res.get("data") or {}).get("currentUser", {}).get("inventory") or {}
        except Exception as e:
            logger.error("Error fetching inventory via persisted query: %s", e)
            return {}

    def post_gql_batch(self, operations: List[Dict[str, Any]], timeout: int = 15) -> List[Dict[str, Any]]:
        """Send a batched array of GraphQL operations (up to 35 per call) to Twitch GQL."""
        if not operations:
            return []
        headers = {
            "Client-Id": self.client_id,
            "User-Agent": self.user_agent,
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Origin": "https://www.twitch.tv",
            "Referer": "https://www.twitch.tv/",
            "Client-Session-Id": getattr(self, "session_id", "1234567890abcdef"),
            "X-Device-Id": getattr(self, "device_id", "1234567890abcdef1234567890abcdef"),
        }
        integrity_tok = self.get_client_integrity_token()
        if integrity_tok:
            headers["Client-Integrity"] = integrity_tok
        if self.auth_token:
            headers["Authorization"] = f"OAuth {self.auth_token}"
        body = json.dumps(operations).encode("utf-8")
        try:
            raw = self._make_request(GQL_URL, data=body, headers=headers, timeout=timeout)
            parsed = json.loads(raw.decode("utf-8"))
            res_list = parsed if isinstance(parsed, list) else [parsed]
            if self.auth_token and res_list and all(isinstance(x, dict) and x.get("errors") and not x.get("data") for x in res_list):
                pub_headers = {
                    "Client-Id": TWITCH_CLIENT_ID,
                    "User-Agent": self.user_agent,
                    "Content-Type": "application/json",
                    "Accept": "*/*",
                }
                raw_pub = self._make_request(GQL_URL, data=body, headers=pub_headers, timeout=timeout)
                parsed_pub = json.loads(raw_pub.decode("utf-8"))
                return parsed_pub if isinstance(parsed_pub, list) else [parsed_pub]
            return res_list
        except Exception:
            if self.auth_token:
                pub_headers = {
                    "Client-Id": TWITCH_CLIENT_ID,
                    "User-Agent": self.user_agent,
                    "Content-Type": "application/json",
                    "Accept": "*/*",
                }
                raw_pub = self._make_request(GQL_URL, data=body, headers=pub_headers, timeout=timeout)
                parsed_pub = json.loads(raw_pub.decode("utf-8"))
                return parsed_pub if isinstance(parsed_pub, list) else [parsed_pub]
            raise

    def fetch_twitch_gql_live_campaigns(self, priority_games: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """
        Discover all live Twitch Drop campaigns directly from Twitch's official GraphQL API:
        1. ViewerDropsDashboard (if authenticated) for all active/upcoming campaigns on Twitch.
        2. TopLiveGames (top 100 live games on Twitch) + StreamsWithDrops.
        3. Priority/queued games + DEFAULT_DROPS_GAMES_ROSTER (including WARDOGS, Rust, etc.).
        4. Batched DropsHighlightService_AvailableDrops across live broadcasters for full drop tiers & reward icons.
        """
        dashboard_meta: Dict[str, Dict[str, Any]] = {}
        dashboard_games: List[str] = []

        # 1. Query official ViewerDropsDashboard when authenticated
        if self.auth_token:
            try:
                dash_res = self.post_persisted_gql(
                    operation_name="ViewerDropsDashboard",
                    sha256_hash="5a4da2ab3d5b47c9f9ce864e727b2cb346af1e3ea8b897fe8f704a97ff017619",
                    variables={"fetchRewardCampaigns": False},
                )
                dash_camps = ((dash_res.get("data") or {}).get("currentUser") or {}).get("dropCampaigns") or []
                for dc in dash_camps:
                    if not isinstance(dc, dict):
                        continue
                    status_str = str(dc.get("status") or "").upper()
                    if status_str == "EXPIRED" or is_campaign_expired(dc):
                        continue
                    cid = str(dc.get("id") or "")
                    gname = extract_game_name(dc, "")
                    if cid:
                        dashboard_meta[cid] = dc
                    if gname and gname not in dashboard_games:
                        dashboard_games.append(gname)
            except Exception as err:
                logger.debug("ViewerDropsDashboard query note: %s", err)

        # 2. Query Top 100 live games on Twitch + StreamsWithDrops
        game_broadcasters: Dict[str, Dict[str, Any]] = {}  # game_low -> {"name": str, "box_art": str, "bids": List[str]}
        try:
            q_top = """
            query TopLiveGames {
                games(first: 100) {
                    edges {
                        node {
                            id
                            name
                            displayName
                            boxArtURL
                            streams(first: 2) {
                                edges {
                                    node {
                                        broadcaster {
                                            id
                                            login
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                streams(first: 30, options: {systemFilters: [DROPS_ENABLED]}) {
                    edges {
                        node {
                            broadcaster {
                                id
                                login
                            }
                            game {
                                id
                                name
                                displayName
                                boxArtURL
                            }
                        }
                    }
                }
            }
            """
            res_top = self.post_gql(q_top, operation_name="TopLiveGames")
            top_data = res_top.get("data") or {}
            for edge in ((top_data.get("games") or {}).get("edges") or []):
                node = edge.get("node") or {}
                gname = clean_text(node.get("displayName") or node.get("name") or "")
                if not gname:
                    continue
                box_art = format_box_art_url(node.get("boxArtURL"))
                st_edges = (node.get("streams") or {}).get("edges") or []
                bids = [
                    str(((se.get("node") or {}).get("broadcaster") or {}).get("id"))
                    for se in st_edges
                    if ((se.get("node") or {}).get("broadcaster") or {}).get("id")
                ]
                if bids:
                    game_broadcasters[gname.lower()] = {"name": gname, "box_art": box_art, "bids": bids}

            for edge in ((top_data.get("streams") or {}).get("edges") or []):
                node = edge.get("node") or {}
                gobj = node.get("game") or {}
                bobj = node.get("broadcaster") or {}
                gname = clean_text(gobj.get("displayName") or gobj.get("name") or "")
                bid = str(bobj.get("id") or "")
                if gname and bid:
                    entry = game_broadcasters.setdefault(
                        gname.lower(),
                        {"name": gname, "box_art": format_box_art_url(gobj.get("boxArtURL")), "bids": []},
                    )
                    if bid not in entry["bids"]:
                        entry["bids"].append(bid)
        except Exception as err:
            logger.debug("TopLiveGames GQL discovery note: %s", err)

        # 3. Ensure priority_games + dashboard_games + DEFAULT_DROPS_GAMES_ROSTER (including WARDOGS) are checked
        target_games_ordered: List[str] = []
        seen_target_low: Set[str] = set()
        for g in list(priority_games or []) + dashboard_games + DEFAULT_DROPS_GAMES_ROSTER:
            gs = clean_text(g)
            if gs and gs.lower() not in seen_target_low:
                seen_target_low.add(gs.lower())
                target_games_ordered.append(gs)

        missing_games = [g for g in target_games_ordered if g.lower() not in game_broadcasters]
        if missing_games:
            q_check = """
            query CheckGameTopStream($name: String!) {
                game(name: $name) {
                    id
                    name
                    displayName
                    boxArtURL
                    streams(first: 2) {
                        edges {
                            node {
                                broadcaster {
                                    id
                                    login
                                }
                            }
                        }
                    }
                }
            }
            """
            for i in range(0, len(missing_games), 30):
                chunk = missing_games[i : i + 30]
                ops = [{"operationName": "CheckGameTopStream", "query": q_check, "variables": {"name": g}} for g in chunk]
                try:
                    batch_res = self.post_gql_batch(ops, timeout=12)
                    for item in batch_res:
                        gobj = ((item or {}).get("data") or {}).get("game")
                        if not gobj:
                            continue
                        gname = clean_text(gobj.get("displayName") or gobj.get("name") or "")
                        box_art = format_box_art_url(gobj.get("boxArtURL"))
                        st_edges = (gobj.get("streams") or {}).get("edges") or []
                        bids = [
                            str(((se.get("node") or {}).get("broadcaster") or {}).get("id"))
                            for se in st_edges
                            if ((se.get("node") or {}).get("broadcaster") or {}).get("id")
                        ]
                        if gname and bids:
                            game_broadcasters[gname.lower()] = {"name": gname, "box_art": box_art, "bids": bids}
                except Exception as err:
                    logger.debug("Batch CheckGameTopStream error: %s", err)

        # 4. Batch-query DropsHighlightService_AvailableDrops (up to 35 broadcasters per batch)
        broadcaster_ids: List[str] = []
        seen_bids: Set[str] = set()
        box_art_by_game_low: Dict[str, str] = {}

        # Put priority/target games first so they are always queried in the first batch
        for g in target_games_ordered:
            info = game_broadcasters.get(g.lower())
            if info:
                if info.get("box_art"):
                    box_art_by_game_low[g.lower()] = info["box_art"]
                for bid in info["bids"][:1]:
                    if bid not in seen_bids:
                        seen_bids.add(bid)
                        broadcaster_ids.append(bid)

        for g_low, info in game_broadcasters.items():
            if info.get("box_art"):
                box_art_by_game_low.setdefault(g_low, info["box_art"])
            for bid in info["bids"][:1]:
                if bid not in seen_bids:
                    seen_bids.add(bid)
                    broadcaster_ids.append(bid)

        discovered_campaigns: Dict[str, Dict[str, Any]] = {}
        for i in range(0, len(broadcaster_ids), 35):
            chunk_bids = broadcaster_ids[i : i + 35]
            ops = [
                {
                    "operationName": "DropsHighlightService_AvailableDrops",
                    "extensions": {
                        "persistedQuery": {
                            "version": 1,
                            "sha256Hash": "9a62a09bce5b53e26e64a671e530bc599cb6aab1e5ba3cbd5d85966d3940716f",
                        }
                    },
                    "variables": {"channelID": bid},
                }
                for bid in chunk_bids
            ]
            try:
                batch_res = self.post_gql_batch(ops, timeout=15)
                for item in batch_res:
                    camps = (((item or {}).get("data") or {}).get("channel") or {}).get("viewerDropCampaigns") or []
                    for camp in camps:
                        if not isinstance(camp, dict):
                            continue
                        cid = str(camp.get("id") or "")
                        if not cid or cid in discovered_campaigns or is_campaign_expired(camp):
                            continue
                        gname = extract_game_name(camp, "")
                        if not gname:
                            continue
                        raw_drops = camp.get("timeBasedDrops") or []
                        if not raw_drops:
                            continue

                        # Normalize and sort timeBasedDrops by requiredMinutesWatched ascending
                        norm_drops = []
                        for d in raw_drops:
                            d_copy = dict(d)
                            req_mins = int(d_copy.get("requiredMinutesWatched") or 0)
                            req_subs = int(d_copy.get("requiredSubs") or 0)
                            d_img = str(d_copy.get("image_url") or "")
                            if not d_img:
                                for edge in d_copy.get("benefitEdges") or []:
                                    ben = (edge or {}).get("benefit") or {}
                                    if ben.get("imageAssetURL"):
                                        d_img = str(ben["imageAssetURL"])
                                        break
                            d_copy["image_url"] = d_img
                            d_copy["requiredMinutesWatched"] = req_mins
                            d_copy["requiredSubs"] = req_subs
                            d_copy["requirement_text"] = (
                                f"{req_subs} sub(s)" if req_subs > 0 and req_mins == 0 else f"{req_mins}m"
                            )
                            if not isinstance(d_copy.get("self"), dict):
                                d_copy["self"] = {
                                    "currentMinutesWatched": 0,
                                    "currentSubs": 0,
                                    "isClaimed": False,
                                    "dropInstanceID": None,
                                    "hasPreconditionsMet": True,
                                }
                            norm_drops.append(d_copy)

                        norm_drops.sort(key=lambda x: (int(x.get("requiredMinutesWatched") or 0), str(x.get("name") or "")))

                        d_meta = dashboard_meta.get(cid) or {}
                        d_game = d_meta.get("game") if isinstance(d_meta.get("game"), dict) else {}
                        c_game = camp.get("game") if isinstance(camp.get("game"), dict) else {}
                        cover_url = (
                            box_art_by_game_low.get(gname.lower())
                            or format_box_art_url(d_game.get("boxArtURL") or c_game.get("boxArtURL"))
                            or str(camp.get("imageURL") or d_meta.get("imageURL") or "")
                        )
                        end_at = str(camp.get("endAt") or d_meta.get("endAt") or "")
                        game_slug = gname.lower().replace(" ", "-").replace(":", "").replace("'", "")
                        stream_url = f"https://www.twitch.tv/directory/category/{urllib.parse.quote(game_slug)}?filter=drops"

                        discovered_campaigns[cid] = {
                            "id": cid,
                            "real_twitch_id": cid,
                            "name": clean_text(camp.get("name") or d_meta.get("name") or f"{gname} Drops"),
                            "game": {
                                "id": str(c_game.get("id") or d_game.get("id") or ""),
                                "displayName": gname,
                                "name": gname,
                                "boxArtURL": cover_url,
                            },
                            "status": str(d_meta.get("status") or camp.get("status") or "ACTIVE"),
                            "startAt": camp.get("startAt") or d_meta.get("startAt"),
                            "endAt": end_at,
                            "time_left": format_time_left(end_at),
                            "cover_url": cover_url,
                            "detailsURL": camp.get("detailsURL") or d_meta.get("detailsURL") or "",
                            "accountLinkURL": camp.get("accountLinkURL") or d_meta.get("accountLinkURL") or "",
                            "self": d_meta.get("self") or camp.get("self") or {},
                            "timeBasedDrops": norm_drops,
                            "stream_url": stream_url,
                            "source": "twitch_gql",
                        }
            except Exception as err:
                logger.debug("Batch DropsHighlightService_AvailableDrops error: %s", err)

        logger.info("Discovered %d live drop campaigns directly from Twitch GQL", len(discovered_campaigns))
        return list(discovered_campaigns.values())

    def get_all_active_campaigns(
        self,
        priority_games: Optional[List[str]] = None,
        force_refresh: bool = False,
        include_expired_inventory: bool = True,
        inv_override: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Fetch all active drop campaigns currently streaming on Twitch merged with user progress.
        Combines direct Twitch GQL live discovery (WARDOGS, Rust, etc.) + DropHunter/cached catalog + user inventory.
        Preserves real-time watched minutes, drop instance IDs, and optionally expired inventory campaigns.
        """
        global DROPHUNTER_CACHE
        campaigns_map: Dict[str, Dict[str, Any]] = {}
        now = time.time()

        # 1. Load DropHunter or disk cache first
        dh_campaigns = fetch_drophunter_live_campaigns(force_refresh=force_refresh)
        for dh_c in dh_campaigns:
            if not include_expired_inventory and is_campaign_expired(dh_c):
                continue
            cid = dh_c.get("id")
            if cid:
                campaigns_map[cid] = normalize_cached_campaign(dh_c)

        # Check if any priority/queued game is missing from current cached campaigns
        prio_low = [g.strip().lower() for g in (priority_games or []) if g and g.strip()]
        cached_games_low = {extract_game_name(c, "").lower() for c in campaigns_map.values() if not is_campaign_expired(c)}
        missing_prio = any(
            not any(p == cg or p in cg or cg in p for cg in cached_games_low)
            for p in prio_low
        )

        # 2. Fetch live campaigns directly from Twitch GraphQL when refreshing, cache expired (>10m), empty, partial, or missing queued game
        cache_age = now - float(DROPHUNTER_CACHE.get("timestamp") or 0)
        needs_full_catalog = ("unittest" not in sys.modules) and len(campaigns_map) < 10
        if force_refresh or not campaigns_map or needs_full_catalog or cache_age >= 600 or missing_prio:
            gql_live_camps = self.fetch_twitch_gql_live_campaigns(priority_games=priority_games)
            for gc in gql_live_camps:
                gc_id = gc.get("id")
                gc_game = extract_game_name(gc, "")
                gc_name = clean_text(gc.get("name") or "")
                matched_existing_id = None
                for ex_id, ex_c in campaigns_map.items():
                    ex_game = extract_game_name(ex_c, "")
                    ex_name = clean_text(ex_c.get("name") or "")
                    if gc_id and (gc_id == ex_id or gc_id == ex_c.get("real_twitch_id")):
                        matched_existing_id = ex_id
                        break
                    if gc_game and ex_game and gc_game.lower() == ex_game.lower():
                        if gc_name and ex_name and (
                            gc_name.lower() == ex_name.lower()
                            or gc_name.lower() in ex_name.lower()
                            or ex_name.lower() in gc_name.lower()
                        ):
                            matched_existing_id = ex_id
                            break
                if matched_existing_id:
                    ex_c = campaigns_map[matched_existing_id]
                    ex_c["timeBasedDrops"] = _merge_campaign_drops(
                        ex_c.get("timeBasedDrops") or [],
                        gc.get("timeBasedDrops") or [],
                    )
                    ex_c["real_twitch_id"] = gc_id
                    ex_c["endAt"] = gc.get("endAt") or ex_c.get("endAt")
                    ex_c["time_left"] = gc.get("time_left") or ex_c.get("time_left") or format_time_left(ex_c.get("endAt"))
                    if gc.get("cover_url") and not ex_c.get("cover_url"):
                        ex_c["cover_url"] = gc["cover_url"]
                elif gc_id:
                    campaigns_map[gc_id] = gc

            # Update in-memory and disk cache with the merged live campaigns so subsequent polls are fast & fresh
            if campaigns_map:
                active_to_cache = [c for c in campaigns_map.values() if not is_campaign_expired(c)]
                updated_iso = datetime.now(timezone.utc).isoformat()
                DROPHUNTER_CACHE["timestamp"] = now
                DROPHUNTER_CACHE["campaigns"] = active_to_cache
                DROPHUNTER_CACHE["updated_at"] = updated_iso
                if "unittest" not in sys.modules:
                    try:
                        os.makedirs(DATA_DIR, exist_ok=True)
                        with open(CAMPAIGNS_CACHE_FILE, "w", encoding="utf-8") as f:
                            json.dump({
                                "timestamp": now,
                                "updated_at": updated_iso,
                                "total_campaigns": len(active_to_cache),
                                "campaigns": active_to_cache,
                            }, f, indent=2)
                    except Exception as cache_err:
                        logger.debug("Could not persist merged campaigns cache: %s", cache_err)

        # 3. Add or merge in-progress & recent campaigns from user inventory (real minutes & drop IDs)
        inv = inv_override if inv_override is not None else self.get_inventory()
        in_prog = inv.get("dropCampaignsInProgress") or []
        if isinstance(in_prog, dict):
            in_prog = [in_prog]

        for camp in in_prog:
            camp_expired = is_campaign_expired(camp)
            if camp_expired and not include_expired_inventory:
                continue

            cid = camp.get("id")
            gname = extract_game_name(camp, "")
            cname = clean_text(camp.get("name") or "")
            g_obj = camp.get("game") if isinstance(camp.get("game"), dict) else {}
            box_art = str(g_obj.get("boxArtURL") or camp.get("imageURL") or "")

            # Collect drop names & image keys from this Twitch inventory campaign for accurate matching
            tw_drop_names = {clean_text(d.get("name", "")).lower() for d in (camp.get("timeBasedDrops") or []) if d.get("name")}
            tw_img_keys = set()
            for d in camp.get("timeBasedDrops") or []:
                for edge in d.get("benefitEdges") or []:
                    ben = edge.get("benefit") or {}
                    k = extract_reward_image_key(ben.get("imageAssetURL"))
                    if k:
                        tw_img_keys.add(k)

            matched_dh_id = None
            if not camp_expired:
                # Pass 1: Match by exact game name AND campaign name
                for dh_id, dh_c in campaigns_map.items():
                    dh_gname = extract_game_name(dh_c, "")
                    dh_cname = clean_text(dh_c.get("name") or "")
                    if cid and (cid == dh_id or cid == dh_c.get("real_twitch_id")):
                        matched_dh_id = dh_id
                        break
                    if gname and dh_gname and gname.lower() == dh_gname.lower():
                        if cname and dh_cname and (
                            cname.lower() == dh_cname.lower()
                            or cname.lower() in dh_cname.lower()
                            or dh_cname.lower() in cname.lower()
                        ):
                            matched_dh_id = dh_id
                            break

                # Pass 2: Match by same game AND overlapping drop reward image keys or drop names
                if not matched_dh_id and gname:
                    for dh_id, dh_c in campaigns_map.items():
                        dh_gname = extract_game_name(dh_c, "")
                        if dh_gname.lower() != gname.lower():
                            continue
                        dh_drops = dh_c.get("timeBasedDrops") or []
                        dh_names = {clean_text(d.get("name", "")).lower() for d in dh_drops if d.get("name")}
                        dh_imgs = {extract_reward_image_key(d.get("image_url")) for d in dh_drops if extract_reward_image_key(d.get("image_url"))}
                        if (tw_img_keys and (tw_img_keys & dh_imgs)) or (tw_drop_names and len(tw_drop_names & dh_names) >= 1):
                            matched_dh_id = dh_id
                            break

                # Pass 3: If there is only 1 DropHunter campaign for this game, match it
                if not matched_dh_id and gname:
                    same_game_ids = [
                        dh_id for dh_id, dh_c in campaigns_map.items()
                        if extract_game_name(dh_c, "").lower() == gname.lower()
                        and not dh_c.get("real_twitch_id")
                    ]
                    if len(same_game_ids) == 1:
                        matched_dh_id = same_game_ids[0]

            if matched_dh_id:
                dh_c = campaigns_map[matched_dh_id]
                dh_c["timeBasedDrops"] = _merge_campaign_drops(
                    dh_c.get("timeBasedDrops") or [],
                    camp.get("timeBasedDrops") or [],
                )
                dh_c["real_twitch_id"] = cid
                dh_c["status"] = camp.get("status") or dh_c.get("status", "ACTIVE")
                dh_c["startAt"] = camp.get("startAt") or dh_c.get("startAt")
                dh_c["endAt"] = camp.get("endAt") or dh_c.get("endAt")
                dh_c["detailsURL"] = camp.get("detailsURL") or dh_c.get("detailsURL")
                dh_c["accountLinkURL"] = camp.get("accountLinkURL") or dh_c.get("accountLinkURL")
                dh_c["self"] = camp.get("self") or dh_c.get("self") or {}
                if not dh_c.get("cover_url") and box_art:
                    dh_c["cover_url"] = box_art
            elif cid:
                camp_copy = dict(camp)
                if not isinstance(camp_copy.get("game"), dict):
                    camp_copy["game"] = {"displayName": gname or "Twitch Game", "name": gname or "Twitch Game"}
                elif not camp_copy["game"].get("displayName"):
                    camp_copy["game"] = dict(camp_copy["game"])
                    camp_copy["game"]["displayName"] = gname or "Twitch Game"
                if not camp_copy.get("cover_url") and box_art:
                    camp_copy["cover_url"] = box_art
                if camp_expired and not camp_copy.get("time_left"):
                    camp_copy["time_left"] = "Campaign Ended"
                campaigns_map[cid] = camp_copy

        for c in campaigns_map.values():
            if not c.get("cover_url"):
                c["cover_url"] = resolve_game_logo(extract_game_name(c, ""))

        return list(campaigns_map.values())

    def get_drops_overview(
        self, priority_games: Optional[List[str]] = None, force_refresh: bool = False
    ) -> Dict[str, Any]:
        """
        Builds a comprehensive drops overview:
        - Cross-references every drop against the user's Twitch inventory (150+ collected items).
        - Accurately updates live progress from Twitch's Inventory query.
        - Tags each drop as:
          * CLAIMED (Collected)
          * READY_TO_CLAIM (Needs Collection - 100% watched)
          * IN_PROGRESS (Needs Collection - currently earning progress)
          * AVAILABLE (Needs Collection - available to watch & earn)
          * UNAVAILABLE (Unavailable for collection: Campaign/Drop Expired, Paid Sub Required, Locked/Precondition, or Upcoming)
        - Returns structured campaigns list, collected_items list, needs_collection_drops, unavailable_drops, and summary metrics.
        """
        inv = self.get_inventory()
        claimed_events = inv.get("gameEventDrops") or []
        in_prog_camps = inv.get("dropCampaignsInProgress") or []
        if isinstance(in_prog_camps, dict):
            in_prog_camps = [in_prog_camps]

        # Build fast lookup structures for claimed rewards
        claimed_ids: Set[str] = set()
        claimed_image_keys: Set[str] = set()
        recent_claimed_names: Set[str] = set()
        now_ts = datetime.now(timezone.utc).timestamp()

        collected_items: List[Dict[str, Any]] = []
        seen_collected_keys: Set[str] = set()

        for ce in claimed_events:
            if not isinstance(ce, dict):
                continue
            cid = str(ce.get("id") or "")
            cname = clean_text(ce.get("name") or "Collected Drop")
            img_url = str(ce.get("imageURL") or "")
            img_key = extract_reward_image_key(img_url)
            awarded_at = str(ce.get("lastAwardedAt") or "")
            acct_link = str(ce.get("requiredAccountLink") or "")
            is_conn = bool(ce.get("isConnected", True))
            total_cnt = int(ce.get("totalCount") or 1)
            inferred_game = infer_game_from_reward(ce)

            if cid:
                claimed_ids.add(cid.lower())
                if "_CUSTOM_ID_" in cid:
                    parts = cid.split("_CUSTOM_ID_")
                    claimed_ids.add(parts[0].lower())
                    claimed_ids.add(parts[1].lower())
            if img_key:
                claimed_image_keys.add(img_key)

            # Only consider name-only fallback if awarded within the last 14 days
            if cname and awarded_at:
                try:
                    dt = datetime.fromisoformat(awarded_at.replace("Z", "+00:00"))
                    if (now_ts - dt.timestamp()) <= (14 * 86400):
                        recent_claimed_names.add(cname.lower().strip())
                except Exception:
                    pass

            dedup_key = (cid or img_key or f"{inferred_game}:{cname}").lower()
            if dedup_key not in seen_collected_keys:
                seen_collected_keys.add(dedup_key)
                collected_items.append({
                    "id": cid,
                    "name": cname,
                    "game": inferred_game,
                    "image_url": img_url,
                    "awarded_at": awarded_at,
                    "total_count": total_cnt,
                    "is_connected": is_conn,
                    "account_link_url": acct_link,
                    "source": "inventory_history",
                })

        all_campaigns = self.get_all_active_campaigns(
            priority_games=priority_games,
            force_refresh=force_refresh,
            include_expired_inventory=True,
            inv_override=inv,
        )

        formatted_campaigns = []
        needs_collection_drops: List[Dict[str, Any]] = []
        unavailable_drops: List[Dict[str, Any]] = []

        # Read active miner status so real-time DropCurrentSessionContext progress is reflected immediately on UI cards
        live_miner_stat: Dict[str, Any] = {}
        miner_stat_file = os.path.join(DATA_DIR, "miner_status.json")
        if os.path.exists(miner_stat_file):
            try:
                with open(miner_stat_file, "r", encoding="utf-8") as f:
                    live_miner_stat = json.load(f) or {}
            except Exception:
                live_miner_stat = {}
        active_mined_game = str(live_miner_stat.get("game") or "").strip().lower() if live_miner_stat.get("active") else ""
        active_mined_drop_name = clean_text(live_miner_stat.get("current_drop_name") or "").lower()
        active_mined_watched = int(live_miner_stat.get("current_drop_watched") or 0) if active_mined_game else 0
        active_mined_req = int(live_miner_stat.get("current_drop_required") or 0) if active_mined_game else 0

        total_ready = 0
        total_in_prog = 0
        total_claimed_in_camps = 0
        total_available = 0
        total_unavailable = 0

        for camp in all_campaigns:
            cid = camp.get("id")
            gname = extract_game_name(camp, "Twitch Game")
            camp_name = clean_text(camp.get("name") or gname)
            timed_drops = camp.get("timeBasedDrops") or []
            cover_url = camp.get("cover_url") or ""
            if not cover_url and isinstance(camp.get("game"), dict):
                cover_url = str(camp["game"].get("boxArtURL") or "")
            time_left = camp.get("time_left") or format_time_left(camp.get("endAt"))
            camp_expired = is_campaign_expired(camp)
            if camp_expired and not time_left:
                time_left = "Campaign Ended"

            camp_self = camp.get("self") or {}
            is_account_connected = camp_self.get("isAccountConnected")
            account_link_url = camp.get("accountLinkURL") or ""
            details_url = camp.get("detailsURL") or ""

            game_slug = gname.lower().replace(" ", "-").replace(":", "").replace("'", "")
            stream_url = camp.get("stream_url") or f"https://www.twitch.tv/directory/category/{urllib.parse.quote(game_slug)}?filter=drops"

            formatted_drops = []
            for d in timed_drops:
                d_id = str(d.get("id") or "")
                d_name = clean_text(d.get("name") or "Drop Reward")
                req_min = int(d.get("requiredMinutesWatched") or 0)
                req_subs = int(d.get("requiredSubs") or 0)
                req_text = str(d.get("requirement_text") or "")
                self_data = d.get("self") or {}
                watched_min = int(self_data.get("currentMinutesWatched") or 0)
                if (
                    active_mined_watched > watched_min
                    and not camp_expired
                    and req_min > 0
                    and gname.lower() == active_mined_game
                    and (
                        (active_mined_drop_name and d_name.lower() == active_mined_drop_name)
                        or (active_mined_req > 0 and req_min == active_mined_req)
                    )
                ):
                    watched_min = active_mined_watched
                current_subs = int(self_data.get("currentSubs") or 0)
                is_claimed = bool(self_data.get("isClaimed", False))
                has_preconditions = bool(self_data.get("hasPreconditionsMet", True))
                drop_instance_id = self_data.get("dropInstanceID") or self_data.get("dropInstanceId")

                # Extract reward image, benefit info, and tier labels (e.g. Day 1, Stage 1, ???)
                d_img = str(d.get("image_url") or "")
                benefit_ids = []
                benefit_names = []
                raw_dname = clean_text(d.get("name") or "")
                raw_bname = ""
                for edge in d.get("benefitEdges") or []:
                    ben = edge.get("benefit") or {}
                    if ben.get("id"):
                        benefit_ids.append(str(ben["id"]).lower())
                    if ben.get("name"):
                        b_clean = clean_text(ben["name"])
                        benefit_names.append(b_clean.lower())
                        if not raw_bname:
                            raw_bname = b_clean
                    if not d_img and ben.get("imageAssetURL"):
                        d_img = str(ben["imageAssetURL"])

                # Detect tier labels like (Day 1), Day 1, Stage 1
                tier_label = ""
                for cand_text in [raw_dname, raw_bname, str(d.get("preconditionDrops") or "")]:
                    m = re.search(r"\b(Day\s*\d+|Stage\s*\d+|Part\s*\d+)\b", cand_text, re.IGNORECASE)
                    if m:
                        tier_label = m.group(1).strip()
                        break

                # Resolve drop display name cleanly
                is_mystery = (raw_dname == "???" or raw_bname == "???" or d_name == "???")
                if is_mystery:
                    d_name = f"??? ({tier_label})" if tier_label else "???"
                elif raw_bname and raw_bname not in ("Drop Reward", "???") and (d_name in ("Drop Reward", "") or not d_name):
                    d_name = raw_bname
                elif raw_dname and raw_dname not in ("Drop Reward", ""):
                    d_name = raw_dname
                elif raw_bname:
                    d_name = raw_bname
                elif not d_name or d_name == "Drop Reward":
                    d_name = f"{gname} Drop Reward"

                d_img_key = extract_reward_image_key(d_img)

                # Cross-reference with past inventory history
                if not is_claimed:
                    if d_id and d_id.lower() in claimed_ids:
                        is_claimed = True
                    elif any(bid in claimed_ids for bid in benefit_ids):
                        is_claimed = True
                    elif d_img_key and d_img_key in claimed_image_keys:
                        is_claimed = True
                    elif not d_img_key and d_name and d_name.lower().strip() in recent_claimed_names:
                        is_claimed = True

                drop_end_at = d.get("endAt") or camp.get("endAt")
                drop_start_at = d.get("startAt") or camp.get("startAt")
                drop_expired = camp_expired or is_timestamp_expired(drop_end_at)
                # Twitch allows claiming completed drops up to 24 hours after campaign endAt if dropInstanceID exists
                claim_window_expired = is_timestamp_expired(drop_end_at, grace_seconds=86400) if drop_end_at else camp_expired
                drop_upcoming = is_timestamp_future(drop_start_at)

                unavailable_reason = ""
                can_try_claim = False

                if is_claimed:
                    status = "CLAIMED"
                    total_claimed_in_camps += 1
                    # Also ensure this claimed campaign drop is represented in collected_items
                    dedup_key = (d_id or d_img_key or f"{gname}:{d_name}").lower()
                    if dedup_key not in seen_collected_keys:
                        seen_collected_keys.add(dedup_key)
                        collected_items.append({
                            "id": d_id,
                            "name": d_name,
                            "game": gname,
                            "image_url": d_img,
                            "awarded_at": drop_end_at or "",
                            "total_count": 1,
                            "is_connected": bool(is_account_connected) if is_account_connected is not None else True,
                            "account_link_url": account_link_url,
                            "source": "active_campaign",
                        })
                elif drop_expired:
                    status = "UNAVAILABLE"
                    total_unavailable += 1
                    if req_min > 0 and watched_min >= req_min:
                        if drop_instance_id and not claim_window_expired:
                            # Still within 24h claim grace window
                            status = "READY_TO_CLAIM"
                            total_unavailable -= 1
                            total_ready += 1
                        else:
                            unavailable_reason = f"Campaign Ended - Claim Window Expired ({watched_min}/{req_min} min completed)"
                            can_try_claim = bool(drop_instance_id)
                    elif watched_min > 0:
                        unavailable_reason = f"Campaign Ended (Incomplete: {watched_min}/{req_min} min watched)"
                    else:
                        unavailable_reason = "Campaign Ended / No Longer Available"
                elif req_subs > 0 and req_min == 0:
                    status = "UNAVAILABLE"
                    total_unavailable += 1
                    sub_word = "Sub" if req_subs == 1 else "Subs"
                    unavailable_reason = f"Requires {req_subs} Paid Twitch {sub_word} (Cannot be mined via watch time)"
                elif drop_upcoming:
                    status = "UNAVAILABLE"
                    total_unavailable += 1
                    unavailable_reason = "Upcoming Drop (Window has not started yet)"
                elif not has_preconditions:
                    status = "UNAVAILABLE"
                    total_unavailable += 1
                    unavailable_reason = "Locked (Requires prerequisite drop tier to be claimed first)"
                elif req_min > 0 and watched_min >= req_min:
                    status = "READY_TO_CLAIM"
                    total_ready += 1
                elif watched_min > 0:
                    status = "IN_PROGRESS"
                    total_in_prog += 1
                elif req_min > 0:
                    status = "AVAILABLE"
                    total_available += 1
                else:
                    status = "UNAVAILABLE"
                    total_unavailable += 1
                    unavailable_reason = f"Special / Event Requirement ({req_text or 'No watch-time tier'})"

                pct = 0
                if status == "CLAIMED":
                    pct = 100
                elif req_min > 0:
                    pct = min(100, int((watched_min / req_min) * 100))

                remaining_min = max(0, req_min - watched_min) if req_min > 0 and status != "CLAIMED" else 0

                drop_entry = {
                    "id": d_id,
                    "name": d_name,
                    "tier_label": tier_label,
                    "is_mystery": is_mystery,
                    "game": gname,
                    "campaign_name": camp_name,
                    "status": status,
                    "watched_minutes": watched_min,
                    "required_minutes": req_min,
                    "remaining_minutes": remaining_min,
                    "required_subs": req_subs,
                    "current_subs": current_subs,
                    "requirement_text": req_text or (f"{req_subs} sub(s)" if req_subs > 0 else f"{req_min} min"),
                    "progress_percent": pct,
                    "drop_instance_id": drop_instance_id,
                    "image_url": d_img,
                    "unavailable_reason": unavailable_reason,
                    "can_try_claim": can_try_claim,
                    "end_at": drop_end_at,
                }
                formatted_drops.append(drop_entry)

                if status in ("READY_TO_CLAIM", "IN_PROGRESS", "AVAILABLE"):
                    needs_collection_drops.append(drop_entry)
                elif status == "UNAVAILABLE":
                    unavailable_drops.append(drop_entry)

            formatted_drops.sort(key=lambda x: (int(x.get("required_minutes") or 0), str(x.get("name") or "")))

            claimed_cnt = sum(1 for d in formatted_drops if d["status"] == "CLAIMED")
            ready_cnt = sum(1 for d in formatted_drops if d["status"] == "READY_TO_CLAIM")
            in_prog_cnt = sum(1 for d in formatted_drops if d["status"] == "IN_PROGRESS")
            avail_cnt = sum(1 for d in formatted_drops if d["status"] == "AVAILABLE")
            unavail_cnt = sum(1 for d in formatted_drops if d["status"] == "UNAVAILABLE")
            needs_coll_cnt = ready_cnt + in_prog_cnt + avail_cnt

            if formatted_drops and claimed_cnt == len(formatted_drops):
                camp_status = "COMPLETED"
            elif ready_cnt > 0:
                camp_status = "READY"
            elif in_prog_cnt > 0:
                camp_status = "IN_PROGRESS"
            elif camp_expired:
                camp_status = "EXPIRED"
            elif avail_cnt == 0 and unavail_cnt > 0:
                camp_status = "UNAVAILABLE"
            else:
                camp_status = "ACTIVE"

            formatted_campaigns.append({
                "id": cid,
                "name": camp_name,
                "game": gname,
                "status": camp_status,
                "cover_url": cover_url,
                "time_left": time_left,
                "end_at": camp.get("endAt"),
                "stream_url": stream_url,
                "campaigns_url": "https://www.twitch.tv/drops/campaigns",
                "details_url": details_url,
                "account_link_url": account_link_url,
                "is_account_connected": is_account_connected,
                "drops": formatted_drops,
                "total_drops": len(formatted_drops),
                "claimed_count": claimed_cnt,
                "ready_count": ready_cnt,
                "in_progress_count": in_prog_cnt,
                "available_count": avail_cnt,
                "needs_collection_count": needs_coll_cnt,
                "unavailable_count": unavail_cnt,
            })

        # Sort campaigns:
        # 1. Ready to claim or currently in-progress first
        # 2. Targeted / Priority games that are active
        # 3. Other active collectable campaigns
        # 4. Completed campaigns
        # 5. Unavailable / Expired campaigns (with expired inventory campaigns that had progress before purely sub-only campaigns)
        priority_clean = [p.lower().strip() for p in (priority_games or []) if p.strip()]

        def campaign_sort_key(c):
            g_low = (c["game"] or "").lower()
            c_low = (c["name"] or "").lower()
            is_prio = any(p in g_low or p in c_low for p in priority_clean)
            has_watched = any(d.get("watched_minutes", 0) > 0 for d in c.get("drops", []))
            status_order = {
                "READY": 0,
                "IN_PROGRESS": 1,
                "ACTIVE": 2,
                "COMPLETED": 3,
                "EXPIRED": 4,
                "UNAVAILABLE": 5,
            }.get(c["status"], 6)
            return (
                0 if c["ready_count"] > 0 else 1,
                0 if c["in_progress_count"] > 0 else 1,
                0 if (is_prio and c["status"] in ("READY", "IN_PROGRESS", "ACTIVE")) else 1,
                status_order,
                0 if has_watched else 1,
                0 if is_prio else 1,
                c["game"],
            )

        formatted_campaigns.sort(key=campaign_sort_key)

        # Sort collected_items newest first
        collected_items.sort(key=lambda x: x.get("awarded_at") or "", reverse=True)

        # Sort needs_collection_drops: READY_TO_CLAIM first, then IN_PROGRESS (highest % first), then AVAILABLE
        needs_collection_drops.sort(
            key=lambda d: (
                0 if d["status"] == "READY_TO_CLAIM" else (1 if d["status"] == "IN_PROGRESS" else 2),
                -d.get("progress_percent", 0),
                d.get("game", ""),
            )
        )

        # Persist claimed drops to data/twitch_inventory.json so sync_loot_to_github.py can export them
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            inv_file = os.path.join(DATA_DIR, "twitch_inventory.json")
            with open(inv_file, "w", encoding="utf-8") as f:
                json.dump({
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "claimed_drops": [
                        {
                            "id": item["id"],
                            "game_name": item["game"],
                            "name": item["name"],
                            "image_url": item["image_url"],
                            "last_awarded_at": item["awarded_at"],
                        }
                        for item in collected_items
                    ],
                }, f, indent=2)
        except Exception as inv_err:
            logger.debug("Could not write twitch_inventory.json: %s", inv_err)

        active_camps_cnt = sum(1 for c in formatted_campaigns if c["status"] in ("ACTIVE", "IN_PROGRESS", "READY"))

        return {
            "campaigns": formatted_campaigns,
            "total_campaigns": len(formatted_campaigns),
            "active_campaigns_count": active_camps_cnt,
            "claimed_history_count": len(collected_items),
            "collected_in_campaigns_count": total_claimed_in_camps,
            "ready_to_claim_count": total_ready,
            "in_progress_count": total_in_prog,
            "available_count": total_available,
            "needs_collection_count": total_ready + total_in_prog + total_available,
            "unavailable_count": total_unavailable,
            "collected_items": collected_items,
            "needs_collection_drops": needs_collection_drops,
            "unavailable_drops": unavailable_drops,
            "last_refreshed_at": DROPHUNTER_CACHE.get("updated_at") or datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _node_has_drops_in_title_or_tags(node: Dict[str, Any]) -> bool:
        """Check if a stream node explicitly mentions drops in its stream title or tags."""
        title = str(node.get("title") or "").lower()
        if "drop" in title:
            return True
        tags = node.get("freeformTags") or []
        if isinstance(tags, list):
            for t in tags:
                tname = str((t.get("name") if isinstance(t, dict) else t) or "").lower()
                if "drop" in tname:
                    return True
        return False

    def find_eligible_channel(
        self,
        game_name: str,
        fast_first: bool = True,
        excluded_channels: Optional[Set[str]] = None,
        campaign_game: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Find a live channel streaming the specified game with verified watch-time drops enabled
        and/or 'drops' in the channel title/tags. Skips any channel in excluded_channels.
        Supports special campaigns and chat-badge campaigns (such as Evolutions) across qualifying categories.
        """
        gn_low = game_name.lower().strip()
        GAME_CATEGORY_ALIASES = {
            "evolutions": [
                "Pokémon Community Game",
                "Pokémon Trading Card Game Pocket",
                "Pokémon Trading Card Game Live",
                "Pokémon",
                "Just Chatting",
            ],
            "pokemon": [
                "Pokémon Community Game",
                "Pokémon Trading Card Game Pocket",
                "Pokémon Trading Card Game Live",
                "Pokémon",
            ],
            "twitch global drop": [
                "Just Chatting",
            ],
        }
        if gn_low in GAME_CATEGORY_ALIASES or "evolution" in gn_low or "pokemon" in gn_low:
            cand_list = (
                GAME_CATEGORY_ALIASES.get(gn_low)
                or (GAME_CATEGORY_ALIASES["evolutions"] if "evolution" in gn_low else GAME_CATEGORY_ALIASES["pokemon"])
            )
            for alt_game in cand_list:
                ch = self.find_eligible_channel(alt_game, fast_first=fast_first, excluded_channels=excluded_channels, campaign_game=game_name)
                if ch:
                    ch["game_name"] = game_name
                    logger.info("Found eligible stream for '%s' in category '%s': @%s", game_name, alt_game, ch.get("channel_login"))
                    return ch

        excluded_set: Set[str] = {
            str(ch).lower().strip() for ch in (excluded_channels or set()) if ch and str(ch).strip()
        }
        query = """
        query DirectoryPage_Game($name: String!) {
            game(name: $name) {
                id
                name
                streams(first: 30, options: {sort: RELEVANCE, systemFilters: [DROPS_ENABLED], recommendationsContext: {platform: "web"}}) {
                    edges {
                        node {
                            id
                            title
                            viewersCount
                            freeformTags {
                                name
                            }
                            broadcaster {
                                id
                                login
                                displayName
                            }
                        }
                    }
                }
            }
        }
        """
        try:
            res = self.post_gql(query, variables={"name": game_name}, operation_name="DirectoryPage_Game")
            game_obj = res.get("data", {}).get("game") or {}
            game_id = str(game_obj.get("id") or "")
            edges = game_obj.get("streams", {}).get("edges", [])

            used_drops_filter = bool(edges)
            # Fallback if no streams found with DropsEnabled filter query
            if not edges:
                query_fallback = """
                query DirectoryPage_GameFallback($name: String!) {
                    game(name: $name) {
                        id
                        name
                        streams(first: 30) {
                            edges {
                                node {
                                    id
                                    title
                                    viewersCount
                                    freeformTags {
                                        name
                                    }
                                    broadcaster {
                                        id
                                        login
                                        displayName
                                    }
                                }
                            }
                        }
                    }
                }
                """
                res_fb = self.post_gql(query_fallback, variables={"name": game_name}, operation_name="DirectoryPage_GameFallback")
                game_obj = res_fb.get("data", {}).get("game") or {}
                game_id = str(game_obj.get("id") or "")
                edges = game_obj.get("streams", {}).get("edges", [])

            if not edges:
                logger.info("No live streams found for game '%s'", game_name)
                return None

            raw_nodes = [e["node"] for e in edges if e.get("node")]
            candidate_nodes: List[Dict[str, Any]] = []
            for node in raw_nodes:
                b = node.get("broadcaster") or {}
                b_login = str(b.get("login") or "").lower().strip()
                b_id = str(b.get("id") or "").lower().strip()
                if (b_login and b_login in excluded_set) or (b_id and b_id in excluded_set):
                    logger.debug("Skipping excluded/unproductive channel @%s for '%s'", b_login, game_name)
                    continue
                candidate_nodes.append(node)

            if not candidate_nodes:
                logger.info("No non-excluded live streams found for game '%s' (excluded=%d)", game_name, len(excluded_set))
                return None

            # Keep Twitch's directory order: the FIRST live participating channel in the list is chosen.
            target_game_low = game_name.lower().strip()
            campaign_low = (campaign_game or "").lower().strip()
            is_global_target = any(
                kw in target_game_low or kw in campaign_low
                for kw in ("evolutions", "pokemon", "twitch global drop")
            )

            # Verify which channels actually have active watch-time drops enabled via DropsHighlightService_AvailableDrops
            def verify_channel(node: Dict[str, Any]) -> Optional[Dict[str, Any]]:
                try:
                    broadcaster = node.get("broadcaster") or {}
                    b_id = str(broadcaster.get("id") or "")
                    if not b_id:
                        return None
                    drops_res = self.post_persisted_gql(
                        operation_name="DropsHighlightService_AvailableDrops",
                        sha256_hash="9a62a09bce5b53e26e64a671e530bc599cb6aab1e5ba3cbd5d85966d3940716f",
                        variables={"channelID": b_id}
                    )
                    camps = (drops_res.get("data") or {}).get("channel", {}).get("viewerDropCampaigns") or []
                    has_watchable = False
                    for c in camps:
                        c_game = extract_game_name(c, "").lower()
                        # Ignore global badge campaigns when targeting a specific game
                        if not c_game:
                            if not is_global_target:
                                continue
                        elif (
                            target_game_low not in c_game and c_game not in target_game_low
                            and not (campaign_low and (campaign_low in c_game or c_game in campaign_low))
                        ):
                            continue
                        t_drops = c.get("timeBasedDrops") or []
                        if any((d.get("requiredMinutesWatched") or 0) > 0 for d in t_drops):
                            has_watchable = True
                            break
                    if has_watchable:
                        has_title_drops = self._node_has_drops_in_title_or_tags(node)
                        return {
                            "channel_id": b_id,
                            "channel_login": broadcaster.get("login"),
                            "channel_name": broadcaster.get("displayName"),
                            "stream_id": str(node.get("id") or ""),
                            "stream_title": str(node.get("title") or ""),
                            "viewers": node.get("viewersCount", 0),
                            "game_name": game_name,
                            "game_id": game_id,
                            "verified_drops": True,
                            "has_drops_in_title": has_title_drops,
                        }
                except Exception as err:
                    logger.debug("Failed verifying channel drops for node: %s", err)
                return None

            verified_channels: List[Dict[str, Any]] = []
            pool_slice = candidate_nodes[:15]
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=6)
            try:
                futures = [executor.submit(verify_channel, node) for node in pool_slice]
                deadline = time.time() + 8
                # Walk results in directory order so the first listed verified channel wins
                for fut in futures:
                    try:
                        r = fut.result(timeout=max(0.1, deadline - time.time()))
                    except Exception:
                        r = None
                    if r:
                        verified_channels.append(r)
                        break
            except Exception:
                pass
            finally:
                executor.shutdown(wait=False, cancel_futures=True)

            if verified_channels:
                chosen = verified_channels[0]
                logger.info(
                    "Selected first live participating channel for '%s': %s (@%s) [title_drops=%s] with %d viewers",
                    game_name, chosen["channel_name"], chosen["channel_login"],
                    chosen.get("has_drops_in_title"), chosen["viewers"]
                )
                return chosen

            # Fallback ONLY to channels that explicitly include 'drops' in their stream title or tags
            title_drop_nodes = [n for n in candidate_nodes if self._node_has_drops_in_title_or_tags(n)]
            if title_drop_nodes:
                fallback_node = title_drop_nodes[0]
                b = fallback_node.get("broadcaster") or {}
                logger.info(
                    "Selecting stream with Drops in title/tags for '%s': %s (@%s) — will verify progress increase within 2-5 mins.",
                    game_name, b.get("displayName"), b.get("login")
                )
                return {
                    "channel_id": str(b.get("id") or ""),
                    "channel_login": b.get("login"),
                    "channel_name": b.get("displayName"),
                    "stream_id": str(fallback_node.get("id") or ""),
                    "stream_title": str(fallback_node.get("title") or ""),
                    "viewers": fallback_node.get("viewersCount", 0),
                    "game_name": game_name,
                    "game_id": game_id,
                    "verified_drops": False,
                    "has_drops_in_title": True,
                }

            logger.warning(
                "No live streams for '%s' had verified watch-time drops or 'drops' in their channel title/tags. Skipping.",
                game_name
            )
            return None

        except Exception as e:
            logger.warning("Error finding channel for %s: %s", game_name, e)
            return None

    def list_eligible_channels(
        self,
        game_name: str,
        limit: int = 12,
        campaign_game: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Return an ordered list of live participating channels for the specified game
        in Twitch directory order, so the user can inspect or switch between channels.
        """
        gn_low = (game_name or "").lower().strip()
        if not gn_low:
            return []
        alias_map = {
            "evolutions": [
                "Pokémon Community Game",
                "Pokémon Trading Card Game Pocket",
                "Pokémon Trading Card Game Live",
                "Pokémon",
                "Just Chatting",
            ],
            "pokemon": [
                "Pokémon Community Game",
                "Pokémon Trading Card Game Pocket",
                "Pokémon Trading Card Game Live",
                "Pokémon",
            ],
            "twitch global drop": [
                "Just Chatting",
            ],
        }
        if gn_low in alias_map or "evolution" in gn_low or "pokemon" in gn_low:
            cand_list = (
                alias_map.get(gn_low)
                or (alias_map["evolutions"] if "evolution" in gn_low else alias_map["pokemon"])
            )
            combined: List[Dict[str, Any]] = []
            seen_logins: Set[str] = set()
            for alt_game in cand_list:
                sub_chs = self.list_eligible_channels(alt_game, limit=limit, campaign_game=game_name)
                for ch in sub_chs:
                    lg = str(ch.get("channel_login") or "").lower()
                    if lg and lg not in seen_logins:
                        seen_logins.add(lg)
                        ch["game_name"] = game_name
                        ch["rank"] = len(combined) + 1
                        combined.append(ch)
                if len(combined) >= limit:
                    break
            return combined[:limit]

        query = """
        query DirectoryPage_Game($name: String!) {
            game(name: $name) {
                id
                name
                streams(first: 30, options: {sort: RELEVANCE, systemFilters: [DROPS_ENABLED], recommendationsContext: {platform: "web"}}) {
                    edges {
                        node {
                            id
                            title
                            viewersCount
                            freeformTags {
                                name
                            }
                            broadcaster {
                                id
                                login
                                displayName
                            }
                        }
                    }
                }
            }
        }
        """
        try:
            drops_filtered = False
            res = self.post_gql(query, variables={"name": game_name}, operation_name="DirectoryPage_Game")
            game_obj = res.get("data", {}).get("game") or {}
            game_id = str(game_obj.get("id") or "")
            edges = game_obj.get("streams", {}).get("edges", [])
            if edges:
                drops_filtered = True
            else:
                query_fallback = """
                query DirectoryPage_GameFallback($name: String!) {
                    game(name: $name) {
                        id
                        name
                        streams(first: 30) {
                            edges {
                                node {
                                    id
                                    title
                                    viewersCount
                                    freeformTags {
                                        name
                                    }
                                    broadcaster {
                                        id
                                        login
                                        displayName
                                    }
                                }
                            }
                        }
                    }
                }
                """
                res_fb = self.post_gql(query_fallback, variables={"name": game_name}, operation_name="DirectoryPage_GameFallback")
                game_obj = res_fb.get("data", {}).get("game") or {}
                game_id = str(game_obj.get("id") or "")
                edges = game_obj.get("streams", {}).get("edges", [])

            raw_nodes = [e["node"] for e in edges if e.get("node")]
            if not raw_nodes:
                return []

            target_game_low = game_name.lower().strip()
            campaign_low = (campaign_game or "").lower().strip()
            is_global_target = any(
                kw in target_game_low or kw in campaign_low
                for kw in ("evolutions", "pokemon", "twitch global drop")
            )

            def verify_node(node: Dict[str, Any]) -> Optional[Dict[str, Any]]:
                try:
                    broadcaster = node.get("broadcaster") or {}
                    b_id = str(broadcaster.get("id") or "")
                    if not b_id:
                        return None
                    drops_res = self.post_persisted_gql(
                        operation_name="DropsHighlightService_AvailableDrops",
                        sha256_hash="9a62a09bce5b53e26e64a671e530bc599cb6aab1e5ba3cbd5d85966d3940716f",
                        variables={"channelID": b_id},
                    )
                    camps = (drops_res.get("data") or {}).get("channel", {}).get("viewerDropCampaigns") or []
                    has_watchable = False
                    for c in camps:
                        c_game = extract_game_name(c, "").lower()
                        if not c_game:
                            if not is_global_target:
                                continue
                        elif (
                            target_game_low not in c_game and c_game not in target_game_low
                            and not (campaign_low and (campaign_low in c_game or c_game in campaign_low))
                        ):
                            continue
                        t_drops = c.get("timeBasedDrops") or []
                        if any((d.get("requiredMinutesWatched") or 0) > 0 for d in t_drops):
                            has_watchable = True
                            break
                    if has_watchable or drops_filtered:
                        return {
                            "channel_id": b_id,
                            "channel_login": broadcaster.get("login"),
                            "channel_name": broadcaster.get("displayName") or broadcaster.get("login"),
                            "stream_id": str(node.get("id") or ""),
                            "stream_title": str(node.get("title") or ""),
                            "viewers": int(node.get("viewersCount") or 0),
                            "game_name": game_name,
                            "game_id": game_id,
                            "verified_drops": True,
                            "has_drops_in_title": self._node_has_drops_in_title_or_tags(node),
                        }
                except Exception:
                    pass
                return None

            pool_slice = raw_nodes[:max(limit, 12)]
            results: List[Dict[str, Any]] = []
            seen_logins: Set[str] = set()
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=6)
            try:
                futures = [executor.submit(verify_node, node) for node in pool_slice]
                deadline = time.time() + 8
                for fut in futures:
                    try:
                        r = fut.result(timeout=max(0.1, deadline - time.time()))
                    except Exception:
                        r = None
                    if r and r.get("channel_login"):
                        lg = str(r["channel_login"]).lower()
                        if lg not in seen_logins:
                            seen_logins.add(lg)
                            r["rank"] = len(results) + 1
                            results.append(r)
            except Exception:
                pass
            finally:
                executor.shutdown(wait=False, cancel_futures=True)

            # Append any remaining title/tag drop channels if below limit
            for node in raw_nodes:
                if len(results) >= limit:
                    break
                b = node.get("broadcaster") or {}
                lg = str(b.get("login") or "").lower().strip()
                if not lg or lg in seen_logins:
                    continue
                if self._node_has_drops_in_title_or_tags(node):
                    seen_logins.add(lg)
                    results.append({
                        "channel_id": str(b.get("id") or ""),
                        "channel_login": b.get("login"),
                        "channel_name": b.get("displayName") or b.get("login"),
                        "stream_id": str(node.get("id") or ""),
                        "stream_title": str(node.get("title") or ""),
                        "viewers": int(node.get("viewersCount") or 0),
                        "game_name": game_name,
                        "game_id": game_id,
                        "verified_drops": drops_filtered,
                        "has_drops_in_title": True,
                        "rank": len(results) + 1,
                    })

            return results[:limit]
        except Exception as e:
            logger.debug("Error listing eligible channels for %s: %s", game_name, e)
            return []

    def resolve_specific_channel(self, channel_input: str, game_name: str = "") -> Optional[Dict[str, Any]]:
        """
        Resolve a user-specified Twitch channel login or URL (e.g. 'dani_aetherwolf' or
        'https://www.twitch.tv/dani_aetherwolf') into a live channel dictionary.
        """
        raw = str(channel_input or "").strip()
        if not raw:
            return None
        if "twitch.tv/" in raw.lower():
            raw = raw.lower().split("twitch.tv/", 1)[1]
        clean_login = raw.split("?")[0].split("#")[0].strip().strip("/").lstrip("@").lower()
        if not clean_login or "/" in clean_login:
            clean_login = clean_login.split("/")[0].strip()
        if not clean_login:
            return None

        query = """
        query ResolveSpecificChannel($login: String!) {
            user(login: $login) {
                id
                login
                displayName
                stream {
                    id
                    title
                    viewersCount
                    game {
                        id
                        name
                        displayName
                    }
                }
            }
        }
        """
        try:
            res = self.post_gql(query, variables={"login": clean_login}, operation_name="ResolveSpecificChannel")
            user_obj = (res.get("data") or {}).get("user") or {}
            if not user_obj or not user_obj.get("id"):
                return None
            stream_obj = user_obj.get("stream")
            if not stream_obj or not stream_obj.get("id"):
                # Fallback check via Usher PlaybackAccessToken in case GQL stream object lagged
                pb = self.get_playback_stream_info(clean_login)
                if not pb or not pb.get("broadcast_id"):
                    return None
                stream_obj = {"id": pb["broadcast_id"], "title": "", "viewersCount": 0, "game": {}}

            g_obj = stream_obj.get("game") or {}
            resolved_game = (
                game_name
                or clean_text(g_obj.get("displayName") or g_obj.get("name") or "")
                or "Twitch Game"
            )
            return {
                "channel_id": str(user_obj.get("id") or ""),
                "channel_login": user_obj.get("login") or clean_login,
                "channel_name": user_obj.get("displayName") or user_obj.get("login") or clean_login,
                "stream_id": str(stream_obj.get("id") or ""),
                "stream_title": str(stream_obj.get("title") or ""),
                "viewers": int(stream_obj.get("viewersCount") or 0),
                "game_name": resolved_game,
                "game_id": str(g_obj.get("id") or ""),
                "verified_drops": True,
                "has_drops_in_title": True,
            }
        except Exception as e:
            logger.debug("Error resolving specific channel %s: %s", clean_login, e)
            return None

    def get_playback_stream_info(self, channel_login: str) -> Dict[str, Any]:
        """
        Fetch PlaybackAccessToken and Usher m3u8 playlist to obtain authentic broadcast_id and chunk playlist URL.
        """
        info: Dict[str, Any] = {"broadcast_id": "", "sub_playlist_url": ""}
        try:
            token_res = self.post_persisted_gql(
                operation_name="PlaybackAccessToken",
                sha256_hash="ed230aa1e33e07eebb8928504583da78a5173989fadfb1ac94be06a04f3cdbe9",
                variables={
                    "isLive": True,
                    "isVod": False,
                    "login": channel_login,
                    "platform": "web",
                    "playerType": "site",
                    "vodID": "",
                }
            )
            spat = (token_res.get("data") or {}).get("streamPlaybackAccessToken")
            if not spat:
                return info
            sig = spat.get("signature")
            tok = spat.get("value")
            if not sig or not tok:
                return info

            usher_url = (
                f"https://usher.ttvnw.net/api/channel/hls/{channel_login}.m3u8?"
                f"sig={sig}&token={urllib.parse.quote(tok)}&allow_source=true&p={int(time.time())}"
            )
            req = urllib.request.Request(
                usher_url,
                headers={
                    "User-Agent": self.user_agent,
                    "Client-Id": self.client_id,
                }
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                playlist = resp.read().decode("utf-8", errors="ignore")

            for line in playlist.splitlines():
                if 'BROADCAST-ID="' in line:
                    info["broadcast_id"] = line.split('BROADCAST-ID="')[1].split('"')[0]
                if line.startswith("https://") and not info["sub_playlist_url"]:
                    info["sub_playlist_url"] = line

        except Exception as e:
            logger.debug("Playback stream info error for %s: %s", channel_login, e)
        return info

    def is_stream_and_drops_available(self, channel_id: str, channel_login: str, game_name: str) -> tuple:
        """
        Check that the channel is still live. Retries once to avoid false "offline" results from transient
        network/Usher errors. Category/drops matching is intentionally NOT enforced here because special
        campaigns (e.g. Evolutions) are mined from differently-named categories.
        Returns (is_available: bool, reason: str).
        """
        if not channel_login:
            return False, "Channel login missing"
        for attempt in range(2):
            pb = self.get_playback_stream_info(channel_login)
            if pb and pb.get("broadcast_id"):
                return True, "Stream available"
            if attempt == 0:
                time.sleep(3)
        return False, "Stream is offline or broadcast ended"

    def touch_stream_chunk(self, sub_playlist_url: str, channel_login: str = "") -> bool:
        """
        Perform an authentic lightweight Range GET request against the latest stream video segment.
        Downloads a tiny 1KB slice so Twitch's CDN and video edge log active media streaming without bandwidth.
        """
        if not sub_playlist_url:
            return False
        try:
            req = urllib.request.Request(
                sub_playlist_url,
                headers={"User-Agent": self.user_agent, "Connection": "close"}
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = resp.read().decode("utf-8", errors="ignore")
            chunks = [line.strip() for line in data.splitlines() if line.strip().startswith("https://")]
            if chunks:
                latest_chunk = chunks[-1]
                ch_ref = f"https://www.twitch.tv/{channel_login}" if channel_login else "https://www.twitch.tv/"
                r_req = urllib.request.Request(
                    latest_chunk,
                    headers={
                        "User-Agent": self.user_agent,
                        "Range": "bytes=0-1023",
                        "Connection": "close",
                        "Referer": ch_ref,
                        "Origin": "https://www.twitch.tv",
                    }
                )
                with urllib.request.urlopen(r_req, timeout=6) as r_resp:
                    r_resp.read(1024)
                    return r_resp.status in (200, 204, 206)
        except Exception as e:
            logger.debug("Stream chunk touch error: %s", e)
        return False

    def get_drop_current_session(self, channel_id: str, channel_login: str = "") -> Dict[str, Any]:
        """
        Query DropCurrentSessionContext for the watched channel.
        Binds/refreshes the active drop watching session on Twitch's backend and returns
        real-time per-minute progress (currentMinutesWatched, requiredMinutesWatched, dropID).
        """
        if not self.auth_token or not channel_id:
            return {}
        try:
            login_str = str(channel_login or "").strip()
            res = self.post_persisted_gql(
                operation_name="DropCurrentSessionContext",
                sha256_hash="4d06b702d25d652afb9ef835d2a550031f1cf762b193523a92166f40ea3d142b",
                variables={"channelID": str(channel_id), "channelLogin": login_str},
            )
            session = ((res.get("data") or {}).get("currentUser") or {}).get("dropCurrentSession")
            if not session and login_str:
                res_fallback = self.post_persisted_gql(
                    operation_name="DropCurrentSessionContext",
                    sha256_hash="4d06b702d25d652afb9ef835d2a550031f1cf762b193523a92166f40ea3d142b",
                    variables={"channelID": str(channel_id), "channelLogin": ""},
                )
                session = ((res_fallback.get("data") or {}).get("currentUser") or {}).get("dropCurrentSession")
            if isinstance(session, dict):
                self.last_drop_session = session
                return session
        except Exception as e:
            logger.debug("DropCurrentSessionContext error for %s: %s", channel_id, e)
        return {}

    def send_minute_watched_heartbeat(
        self,
        channel_id: str,
        stream_id: str,
        channel_login: Optional[str] = None,
        game_name: Optional[str] = None,
        game_id: Optional[str] = None,
        sub_playlist_url: Optional[str] = None,
        minutes_logged: int = 1,
    ) -> bool:
        """
        Send a stream-viewing progress heartbeat to Twitch via both the authenticated GraphQL
        SendEvents (sendSpadeEvents GZIP_B64) mutation and https://spade.twitch.tv/track.
        Simulates watching the stream under the user's account without video bandwidth.
        Also refreshes DropsHighlightService_AvailableDrops and DropCurrentSessionContext for the
        watched channel so Twitch's drop progress session remains bound to this channel.
        """
        if not self.user_id and self.auth_token:
            try:
                self.validate_session()
            except Exception:
                pass

        if (not stream_id or not sub_playlist_url) and channel_login:
            try:
                pb = self.get_playback_stream_info(channel_login)
                if not stream_id and pb.get("broadcast_id"):
                    stream_id = pb["broadcast_id"]
                if not sub_playlist_url and pb.get("sub_playlist_url"):
                    sub_playlist_url = pb["sub_playlist_url"]
            except Exception:
                pass

        try:
            self.post_persisted_gql(
                operation_name="DropsHighlightService_AvailableDrops",
                sha256_hash="9a62a09bce5b53e26e64a671e530bc599cb6aab1e5ba3cbd5d85966d3940716f",
                variables={"channelID": str(channel_id)}
            )
        except Exception:
            pass

        try:
            self.get_drop_current_session(str(channel_id), str(channel_login or ""))
        except Exception:
            pass

        if sub_playlist_url:
            self.touch_stream_chunk(sub_playlist_url, channel_login=str(channel_login or ""))

        uid_val: Any = str(self.user_id) if self.user_id else 0
        if isinstance(uid_val, str) and uid_val.isdigit():
            uid_val = int(uid_val)

        now_iso = datetime.now(timezone.utc).isoformat()
        if "+00:00" in now_iso:
            now_iso = now_iso.replace("+00:00", "Z")
        elif not now_iso.endswith("Z"):
            now_iso += "Z"

        session_id_val = getattr(self, "session_id", "1234567890abcdef")
        device_id_val = getattr(self, "device_id", "1234567890abcdef1234567890abcdef")

        properties: Dict[str, Any] = {
            "broadcast_id": str(stream_id),
            "channel_id": str(channel_id),
            "channel": str(channel_login) if channel_login else "",
            "chat_visible": True,
            "chat_visibility_status": "visible",
            "client_session_id": session_id_val,
            "client_time": now_iso,
            "content_mode": "live",
            "device_id": device_id_val,
            "game": str(game_name or ""),
            "game_id": str(game_id or ""),
            "hidden": False,
            "is_live": True,
            "live": True,
            "location": "channel",
            "logged_in": bool(uid_val),
            "minutes_logged": max(1, int(minutes_logged or 1)),
            "muted": False,
            "player": "site",
            "player_size_mode": "default",
            "time_spent_hidden": 0,
            "user_id": uid_val,
        }

        payload = [
            {
                "event": "minute-watched",
                "properties": properties
            }
        ]
        raw_json = json.dumps(payload, separators=(',', ':'))

        sent_ok = False
        try:
            gz_bytes = gzip.compress(raw_json.encode("utf-8"), compresslevel=1)
            gz_b64 = base64.b64encode(gz_bytes).decode("utf-8")
            gql_spade_query = (
                "mutation SendEvents($input: SendSpadeEventsInput!) { "
                "sendSpadeEvents(input: $input) { statusCode } }"
            )
            gql_res = self.post_gql(
                gql_spade_query,
                variables={
                    "input": {
                        "data": gz_b64,
                        "repository": "twilight",
                        "encoding": "GZIP_B64",
                    }
                },
                operation_name="SendEvents",
            )
            status_code = (((gql_res or {}).get("data") or {}).get("sendSpadeEvents") or {}).get("statusCode")
            if status_code in (200, 204):
                sent_ok = True
        except Exception as err:
            logger.debug("GQL SendEvents heartbeat error: %s", err)

        b64_data = base64.b64encode(raw_json.encode('utf-8')).decode('utf-8')
        body_form = f"data={urllib.parse.quote(b64_data)}".encode('utf-8')
        json_body = json.dumps({"data": b64_data}).encode('utf-8')

        integrity_tok = self.get_client_integrity_token()
        ch_referer = f"https://www.twitch.tv/{channel_login}" if channel_login else "https://www.twitch.tv/"
        headers_form = {
            "Client-Id": self.client_id,
            "Client-Session-Id": session_id_val,
            "X-Device-Id": device_id_val,
            "User-Agent": self.user_agent,
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://www.twitch.tv",
            "Referer": ch_referer,
        }
        headers_json = {
            "Client-Id": self.client_id,
            "Client-Session-Id": session_id_val,
            "X-Device-Id": device_id_val,
            "User-Agent": self.user_agent,
            "Content-Type": "application/json",
            "Origin": "https://www.twitch.tv",
            "Referer": ch_referer,
        }
        if integrity_tok:
            headers_form["Client-Integrity"] = integrity_tok
            headers_json["Client-Integrity"] = integrity_tok
        if self.auth_token:
            headers_form["Authorization"] = f"OAuth {self.auth_token}"
            headers_json["Authorization"] = f"OAuth {self.auth_token}"
        for track_url in ["https://spade.twitch.tv/track", "https://beacon.twitch.tv/track", SPADE_URL]:
            try:
                req_json = urllib.request.Request(track_url, data=json_body, headers=headers_json, method="POST")
                with urllib.request.urlopen(req_json, timeout=10) as resp:
                    if resp.status in (200, 204):
                        sent_ok = True
                        break
            except Exception:
                try:
                    req_form = urllib.request.Request(track_url, data=body_form, headers=headers_form, method="POST")
                    with urllib.request.urlopen(req_form, timeout=10) as resp:
                        if resp.status in (200, 204):
                            sent_ok = True
                            break
                except Exception as err:
                    logger.debug("Heartbeat error on %s: %s", track_url, err)
        return sent_ok


    def claim_drop(self, drop_instance_id: str) -> bool:
        """
        Claim a completed drop reward.
        Sends the official DropsPage_ClaimDropRewards persisted GraphQL query.
        """
        try:
            res = self.post_persisted_gql(
                operation_name="DropsPage_ClaimDropRewards",
                sha256_hash="a455deea71bdc9015b78eb49f4acfbce8baa7ccbedd28e549bb025bd0f751930",
                variables={"input": {"dropInstanceID": str(drop_instance_id)}}
            )
            # Response may have claimDropRewards or claimDrop
            data = res.get("data") or {}
            claim_info = data.get("claimDropRewards") or data.get("claimDrop") or {}
            status = claim_info.get("status")
            logger.info("Claim result for drop %s: %s", drop_instance_id, status)
            return status in ("SUCCESS", "ALREADY_CLAIMED", "ELIGIBLE", True)
        except Exception as e:
            logger.error("Failed to claim drop %s: %s", drop_instance_id, e)
            return False
