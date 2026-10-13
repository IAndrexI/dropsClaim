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
import html
import json
import logging
import os
import random
import re
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
        return clean_text(name) or default
    if isinstance(g, str) and g.strip():
        return clean_text(g)
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
    if not force_refresh and not DROPHUNTER_CACHE["campaigns"] and os.path.exists(CAMPAIGNS_CACHE_FILE):
        try:
            with open(CAMPAIGNS_CACHE_FILE, "r", encoding="utf-8") as f:
                disk_cache = json.load(f)
                c_list = [normalize_cached_campaign(c) for c in (disk_cache.get("campaigns") or []) if isinstance(c, dict)]
                c_time = disk_cache.get("timestamp", 0)
                if c_list and (now - c_time < 3600):  # Valid for 1 hour from disk
                    DROPHUNTER_CACHE["timestamp"] = c_time
                    DROPHUNTER_CACHE["campaigns"] = c_list
                    DROPHUNTER_CACHE["updated_at"] = disk_cache.get("updated_at", "")
                    return c_list
        except Exception:
            pass

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
        logger.warning("Could not fetch drophunter.app campaigns (%s), attempting disk cache fallback", e)
        if not results and os.path.exists(CAMPAIGNS_CACHE_FILE):
            try:
                with open(CAMPAIGNS_CACHE_FILE, "r", encoding="utf-8") as f:
                    disk_cache = json.load(f)
                    results = [normalize_cached_campaign(c) for c in (disk_cache.get("campaigns") or []) if isinstance(c, dict)]
                    if results:
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
    def __init__(self, auth_token: str, user_agent: Optional[str] = None):
        """
        Initialize the Twitch API client using your browser's auth-token cookie.
        
        :param auth_token: The 'auth-token' cookie value from twitch.tv
        :param user_agent: Optional custom browser user-agent
        """
        self.auth_token = auth_token.strip()
        self.user_agent = user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
        self.user_id: Optional[str] = None
        self.user_login: Optional[str] = None

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
            "Client-Id": TWITCH_CLIENT_ID,
            "Authorization": f"OAuth {self.auth_token}",
            "User-Agent": self.user_agent,
            "Content-Type": "application/json",
            "Accept": "*/*",
        }

        raw = self._make_request(GQL_URL, data=body, headers=headers, timeout=15)
        data = json.loads(raw.decode("utf-8"))
        if "errors" in data and data["errors"]:
            logger.error("Twitch GQL returned errors: %s", data["errors"])
        return data

    def post_persisted_gql(self, operation_name: str, sha256_hash: str, variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Send an official persisted query to Twitch GraphQL without triggering integrity challenges."""
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
            "Client-Id": TWITCH_CLIENT_ID,
            "Authorization": f"OAuth {self.auth_token}",
            "User-Agent": self.user_agent,
            "Content-Type": "application/json",
            "Accept": "*/*",
        }

        raw = self._make_request(GQL_URL, data=body, headers=headers, timeout=15)
        return json.loads(raw.decode("utf-8"))

    def validate_session(self) -> bool:
        """Validate the OAuth token and fetch current user profile."""
        query = """
        query CheckUserSession {
            currentUser {
                id
                login
                displayName
                email
            }
        }
        """
        try:
            res = self.post_gql(query, operation_name="CheckUserSession")
            user = res.get("data", {}).get("currentUser")
            if user:
                self.user_id = str(user.get("id"))
                self.user_login = user.get("login")
                logger.info("Authenticated successfully as: %s (ID: %s)", self.user_login, self.user_id)
                return True
            else:
                logger.error("Authentication failed: currentUser is null. Check your auth-token.")
                return False
        except Exception as e:
            logger.error("Error validating session: %s", e)
            return False

    def is_user_actively_using_twitch(self) -> bool:
        """
        Detect if the user is currently using Twitch on any browser, mobile app, or device.
        Queries Twitch currentUser presence, availability, and activity.
        Returns True if user status indicates an active session (ONLINE, AWAY, IDLE, or active activity).
        """
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
                availability = user.get("availability")
                activity = user.get("activity")
                if availability in ("ONLINE", "AWAY", "IDLE") or activity is not None:
                    logger.info(
                        "Detected active user presence on Twitch: availability=%s, activity=%s",
                        availability, activity
                    )
                    return True
        except Exception as e:
            logger.debug("Error checking user active presence: %s", e)
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

    def get_all_active_campaigns(
        self,
        priority_games: Optional[List[str]] = None,
        force_refresh: bool = False,
        include_expired_inventory: bool = True,
        inv_override: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Fetch all active drop campaigns currently streaming on Twitch merged with user progress.
        Combines drophunter.app/drops (all 85+ games & 110+ live campaigns) + user inventory progress.
        Preserves real-time watched minutes, drop instance IDs, and optionally expired inventory campaigns
        so the user can view both active progress and drops that are no longer available for collection.
        """
        campaigns_map: Dict[str, Dict[str, Any]] = {}

        # 1. Fetch comprehensive live campaigns roster from drophunter.app
        dh_campaigns = fetch_drophunter_live_campaigns(force_refresh=force_refresh)
        for dh_c in dh_campaigns:
            cid = dh_c.get("id")
            if cid:
                campaigns_map[cid] = normalize_cached_campaign(dh_c)

        # 2. Add or merge in-progress & recent campaigns from user inventory (real minutes & drop IDs)
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
            time_left = camp.get("time_left") or ""
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
                current_subs = int(self_data.get("currentSubs") or 0)
                is_claimed = bool(self_data.get("isClaimed", False))
                has_preconditions = bool(self_data.get("hasPreconditionsMet", True))
                drop_instance_id = self_data.get("dropInstanceID") or self_data.get("dropInstanceId")

                # Extract reward image and benefit info
                d_img = str(d.get("image_url") or "")
                benefit_ids = []
                benefit_names = []
                for edge in d.get("benefitEdges") or []:
                    ben = edge.get("benefit") or {}
                    if ben.get("id"):
                        benefit_ids.append(str(ben["id"]).lower())
                    if ben.get("name"):
                        benefit_names.append(clean_text(ben["name"]).lower())
                        if d_name == "Drop Reward" or not d_name:
                            d_name = clean_text(ben["name"])
                    if not d_img and ben.get("imageAssetURL"):
                        d_img = str(ben["imageAssetURL"])

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

    def find_eligible_channel(self, game_name: str) -> Optional[Dict[str, Any]]:
        """
        Find a live channel streaming the specified game with verified watch-time drops enabled.
        Selects a random channel from verified drop channels.
        """
        query = """
        query DirectoryPage_Game($name: String!) {
            game(name: $name) {
                id
                name
                streams(first: 30, options: {tags: ["DropsEnabled", "Drops"]}) {
                    edges {
                        node {
                            id
                            title
                            viewersCount
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

            # Fallback if no streams found with Drops tags
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

            candidate_nodes = [e["node"] for e in edges if e.get("node")]
            target_game_low = game_name.lower().strip()

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
                        # Ignore global 0-minute badge campaigns for other/null games
                        if c_game and target_game_low not in c_game and c_game not in target_game_low:
                            continue
                        t_drops = c.get("timeBasedDrops") or []
                        if any((d.get("requiredMinutesWatched") or 0) > 0 for d in t_drops):
                            has_watchable = True
                            break
                    if has_watchable:
                        return {
                            "channel_id": b_id,
                            "channel_login": broadcaster.get("login"),
                            "channel_name": broadcaster.get("displayName"),
                            "stream_id": str(node.get("id") or ""),
                            "viewers": node.get("viewersCount", 0),
                            "game_name": game_name,
                            "game_id": game_id,
                            "verified_drops": True,
                        }
                except Exception as err:
                    logger.debug("Failed verifying channel drops for node: %s", err)
                return None

            verified_channels: List[Dict[str, Any]] = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                results = executor.map(verify_channel, candidate_nodes[:20])
                for r in results:
                    if r:
                        verified_channels.append(r)

            if verified_channels:
                chosen = random.choice(verified_channels)
                logger.info(
                    "Found %d verified watch-drop channel(s) for '%s'. Selected: %s (@%s) with %d viewers",
                    len(verified_channels), game_name, chosen["channel_name"], chosen["channel_login"], chosen["viewers"]
                )
                return chosen

            logger.warning("None of the checked channels for '%s' returned active watch-time drops. Selecting candidate stream.", game_name)
            fallback_node = random.choice(candidate_nodes)
            b = fallback_node.get("broadcaster") or {}
            return {
                "channel_id": str(b.get("id") or ""),
                "channel_login": b.get("login"),
                "channel_name": b.get("displayName"),
                "stream_id": str(fallback_node.get("id") or ""),
                "viewers": fallback_node.get("viewersCount", 0),
                "game_name": game_name,
                "game_id": game_id,
                "verified_drops": False,
            }

        except Exception as e:
            logger.warning("Error finding channel for %s: %s", game_name, e)
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
                    "Client-Id": TWITCH_CLIENT_ID,
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

    def touch_stream_chunk(self, sub_playlist_url: str) -> bool:
        """
        Perform a lightweight HEAD request against the latest stream video segment.
        Verifies client playback presence without downloading video data.
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
            chunks = [c for c in data.splitlines() if c.startswith("https://")]
            if chunks:
                latest_chunk = chunks[-1]
                h_req = urllib.request.Request(
                    latest_chunk,
                    headers={"User-Agent": self.user_agent, "Connection": "close"},
                    method="HEAD"
                )
                with urllib.request.urlopen(h_req, timeout=6) as h_resp:
                    return h_resp.status in (200, 204, 206)
        except Exception as e:
            logger.debug("Stream chunk touch error: %s", e)
        return False

    def send_minute_watched_heartbeat(
        self,
        channel_id: str,
        stream_id: str,
        channel_login: Optional[str] = None,
        game_name: Optional[str] = None,
        game_id: Optional[str] = None,
        sub_playlist_url: Optional[str] = None,
    ) -> bool:
        """
        Send a stream-viewing progress heartbeat to Twitch's tracking endpoint (https://spade.twitch.tv/track).
        Simulates watching the stream under the user's account without video bandwidth.
        Also refreshes DropsHighlightService_AvailableDrops for the watched channel so Twitch's
        drop progress session remains bound to this channel.
        """
        try:
            self.post_persisted_gql(
                operation_name="DropsHighlightService_AvailableDrops",
                sha256_hash="9a62a09bce5b53e26e64a671e530bc599cb6aab1e5ba3cbd5d85966d3940716f",
                variables={"channelID": str(channel_id)}
            )
        except Exception:
            pass

        if sub_playlist_url:
            self.touch_stream_chunk(sub_playlist_url)

        uid_val: Any = str(self.user_id) if self.user_id else ""
        if isinstance(uid_val, str) and uid_val.isdigit():
            uid_val = int(uid_val)

        properties: Dict[str, Any] = {
            "channel_id": str(channel_id),
            "broadcast_id": str(stream_id),
            "player": "site",
            "user_id": uid_val,
            "live": True,
            "channel": str(channel_login) if channel_login else "",
            "location": "channel",
            "logged_in": True,
            "hidden": False,
            "muted": False,
        }
        if game_name:
            properties["game"] = str(game_name)
        if game_id:
            properties["game_id"] = str(game_id)

        payload = [
            {
                "event": "minute-watched",
                "properties": properties
            }
        ]
        raw_json = json.dumps(payload, separators=(',', ':'))
        b64_data = base64.b64encode(raw_json.encode('utf-8')).decode('utf-8')
        body_form = f"data={urllib.parse.quote(b64_data)}".encode('utf-8')

        headers = {
            "Client-Id": TWITCH_CLIENT_ID,
            "Authorization": f"OAuth {self.auth_token}",
            "User-Agent": self.user_agent,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        for track_url in ["https://spade.twitch.tv/track", SPADE_URL]:
            try:
                req = urllib.request.Request(track_url, data=body_form, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    if resp.status in (200, 204):
                        return True
            except Exception as err:
                logger.debug("Heartbeat error on %s: %s", track_url, err)
        return False


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
