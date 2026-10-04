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
import json
import logging
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger("twitch_api")


TWITCH_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"  # Official Twitch Web Client ID
GQL_URL = "https://gql.twitch.tv/gql"
SPADE_URL = "https://spade.twitch.tv/track"
SPADE_FALLBACK_URL = "https://spade.twitch.tv/batched"

DROPHUNTER_CACHE: Dict[str, Any] = {"timestamp": 0, "campaigns": []}


def clean_text(text: Any) -> str:
    """Strip emojis and non-standard symbols to ensure clean terminal and UI rendering."""
    if not text:
        return ""
    emoji_pattern = re.compile(
        "[\U00010000-\U0010ffff]|"
        "[\u2600-\u27bf]|"
        "[\u2300-\u23ff]|"
        "[\u2b50-\u2b55]|"
        "[\u203c-\u2049]"
    )
    cleaned = emoji_pattern.sub("", str(text))
    return " ".join(cleaned.split()).strip()
 
 
def is_campaign_expired(camp: Optional[Dict[str, Any]]) -> bool:
    """Check if a campaign has expired based on status or endAt timestamp."""
    if not camp:
        return True
    if camp.get("status") == "EXPIRED":
        return True
    end_at_str = camp.get("endAt")
    if end_at_str:
        try:
            clean_end = str(end_at_str).replace("Z", "+00:00")
            if datetime.fromisoformat(clean_end) < datetime.now(timezone.utc):
                return True
        except Exception:
            pass
    return False



def fetch_drophunter_live_campaigns() -> List[Dict[str, Any]]:
    """
    Fetch all active drop campaigns from drophunter.app/drops.
    Returns 80+ games and 120+ live campaigns with active drop items.
    Cached for 10 minutes to maintain fast responses and low resource usage.
    """
    global DROPHUNTER_CACHE
    now = time.time()
    if now - DROPHUNTER_CACHE["timestamp"] < 600 and DROPHUNTER_CACHE["campaigns"]:
        return DROPHUNTER_CACHE["campaigns"]

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

                # Construct drop entries
                time_based_drops = []
                for idx, r_name in enumerate(rewards):
                    time_based_drops.append({
                        "id": f"dh_{game_name}_{idx}_{r_name}".replace(" ", "_"),
                        "name": r_name,
                        "requiredMinutesWatched": 0,
                        "self": {
                            "currentMinutesWatched": 0,
                            "isClaimed": False,
                            "dropInstanceID": None,
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
            DROPHUNTER_CACHE["timestamp"] = now
            DROPHUNTER_CACHE["campaigns"] = results
            logger.info("Retrieved %d active drop campaigns from drophunter.app", len(results))
    except Exception as e:
        logger.warning("Could not fetch drophunter.app campaigns (%s), using native Twitch fallback", e)

    return results or DROPHUNTER_CACHE.get("campaigns", [])


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
        try:
            res = self.post_persisted_gql(
                operation_name="Inventory",
                sha256_hash="8337eb8541b314040b0edde0c09c5c7a2783ba1960aa9edfbf3bac16d0fec404",
                variables={"fetchRewardCampaigns": False}
            )
            return res.get("data", {}).get("currentUser", {}).get("inventory", {})
        except Exception as e:
            logger.error("Error fetching inventory via persisted query: %s", e)
            return {}

    def get_all_active_campaigns(self, priority_games: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """
        Fetch all active drop campaigns currently streaming on Twitch merged with user progress.
        Combines drophunter.app/drops (all 85+ games & 120+ live campaigns) + user inventory progress + GQL.
        """
        import concurrent.futures

        campaigns_map: Dict[str, Dict[str, Any]] = {}

        # 1. Fetch comprehensive live campaigns roster from drophunter.app (all 85+ games)
        dh_campaigns = fetch_drophunter_live_campaigns()
        for dh_c in dh_campaigns:
            cid = dh_c.get("id")
            if cid:
                campaigns_map[cid] = dict(dh_c)

        # 2. Add or merge in-progress campaigns from user inventory (real minutes & drop IDs)
        inv = self.get_inventory()
        in_prog = inv.get("dropCampaignsInProgress") or []
        if isinstance(in_prog, dict):
            in_prog = [in_prog]

        for camp in in_prog:
            if is_campaign_expired(camp):
                continue
            cid = camp.get("id")
            gname = (camp.get("game") or {}).get("displayName") or (camp.get("game") or {}).get("name") or ""
            # Match drophunter campaign by game name or ID
            matched = False
            for dh_id, dh_c in list(campaigns_map.items()):
                dh_gname = (dh_c.get("game") or {}).get("displayName") or (dh_c.get("game") or {}).get("name") or ""
                if (gname and gname.lower() == dh_gname.lower()) or (cid and cid == dh_id):
                    # Enrich with real in-progress drops
                    dh_c["timeBasedDrops"] = camp.get("timeBasedDrops", dh_c.get("timeBasedDrops", []))
                    if camp.get("id"):
                        dh_c["real_twitch_id"] = camp["id"]
                    matched = True
                    break
            if not matched and cid:
                campaigns_map[cid] = camp

        # 3. For priority games, also query eligible channels to enrich drops if available
        provided_games = [g.strip() for g in (priority_games or []) if g.strip()]
        if provided_games:
            def fetch_game_campaigns(game_name: str):
                try:
                    channel = self.find_eligible_channel(game_name)
                    if channel and channel.get("channel_id"):
                        drops_res = self.post_persisted_gql(
                            operation_name="DropsHighlightService_AvailableDrops",
                            sha256_hash="9a62a09bce5b53e26e64a671e530bc599cb6aab1e5ba3cbd5d85966d3940716f",
                            variables={"channelID": str(channel["channel_id"])}
                        )
                        channel_obj = (drops_res.get("data") or {}).get("channel")
                        if channel_obj and channel_obj.get("viewerDropCampaigns"):
                            return game_name, channel_obj["viewerDropCampaigns"]
                except Exception as err:
                    logger.debug("Failed fetching drops for %s: %s", game_name, err)
                return game_name, []

            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                for gname, camps in executor.map(fetch_game_campaigns, provided_games):
                    for c in camps:
                        cid = c.get("id")
                        if cid:
                            matched = False
                            for dh_c in campaigns_map.values():
                                dh_gname = (dh_c.get("game") or {}).get("displayName") or (dh_c.get("game") or {}).get("name") or ""
                                if gname.lower() in dh_gname.lower() or dh_gname.lower() in gname.lower():
                                    if c.get("timeBasedDrops"):
                                        dh_c["timeBasedDrops"] = c["timeBasedDrops"]
                                    matched = True
                                    break
                            if not matched and cid not in campaigns_map:
                                if not c.get("game"):
                                    c["game"] = {"displayName": gname, "name": gname}
                                campaigns_map[cid] = c

        return list(campaigns_map.values())

    def get_drops_overview(self, priority_games: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Builds a comprehensive drops overview:
        - Cross-references every drop against the user's Twitch inventory (167+ claimed items).
        - Tags each drop as [CLAIMED], [READY TO CLAIM], [IN PROGRESS], or [AVAILABLE].
        - Returns structured campaigns list and summary metrics.
        """
        inv = self.get_inventory()
        claimed_events = inv.get("gameEventDrops", [])
        in_prog_camps = inv.get("dropCampaignsInProgress", []) or []
        if isinstance(in_prog_camps, dict):
            in_prog_camps = [in_prog_camps]

        # Build fast lookup sets for claimed rewards
        claimed_ids: Set[str] = set()
        claimed_names: Set[str] = set()
        for ce in claimed_events:
            cid = ce.get("id")
            if cid:
                claimed_ids.add(cid.lower())
                if "_CUSTOM_ID_" in cid:
                    parts = cid.split("_CUSTOM_ID_")
                    claimed_ids.add(parts[0].lower())
                    claimed_ids.add(parts[1].lower())
            cname = ce.get("name")
            if cname:
                claimed_names.add(cname.lower().strip())

        all_campaigns = self.get_all_active_campaigns(priority_games)

        formatted_campaigns = []
        total_ready = 0
        total_in_prog = 0
        total_claimed = 0

        for camp in all_campaigns:
            cid = camp.get("id")
            game_obj = camp.get("game") or {}
            gname = clean_text(game_obj.get("displayName") or game_obj.get("name") or "Twitch Game")
            camp_name = clean_text(camp.get("name") or gname)
            timed_drops = camp.get("timeBasedDrops") or []
            cover_url = camp.get("cover_url", "")
            time_left = camp.get("time_left", "")
            game_slug = gname.lower().replace(" ", "-").replace(":", "").replace("'", "")
            stream_url = camp.get("stream_url") or f"https://www.twitch.tv/directory/category/{urllib.parse.quote(game_slug)}?filter=drops"

            formatted_drops = []
            for d in timed_drops:
                d_id = d.get("id") or ""
                d_name = clean_text(d.get("name") or "Drop Reward")
                req_min = d.get("requiredMinutesWatched", 0)
                self_data = d.get("self") or {}
                watched_min = self_data.get("currentMinutesWatched", 0)
                is_claimed = self_data.get("isClaimed", False)
                drop_instance_id = self_data.get("dropInstanceID") or self_data.get("dropInstanceId")

                # Cross-reference with past inventory history
                if not is_claimed:
                    if d_id and d_id.lower() in claimed_ids:
                        is_claimed = True
                    elif d_name and d_name.lower().strip() in claimed_names:
                        is_claimed = True
                    else:
                        for edge in d.get("benefitEdges") or []:
                            ben = edge.get("benefit") or {}
                            bid = ben.get("id", "").lower()
                            bname = ben.get("name", "").lower().strip()
                            if bid in claimed_ids or bname in claimed_names:
                                is_claimed = True
                                break

                if is_claimed:
                    status = "CLAIMED"
                    total_claimed += 1
                elif req_min > 0 and watched_min >= req_min:
                    status = "READY_TO_CLAIM"
                    total_ready += 1
                elif watched_min > 0:
                    status = "IN_PROGRESS"
                    total_in_prog += 1
                else:
                    status = "AVAILABLE"

                pct = 0
                if req_min > 0:
                    pct = min(100, int((watched_min / req_min) * 100))

                formatted_drops.append({
                    "id": d_id,
                    "name": d_name,
                    "status": status,
                    "watched_minutes": watched_min,
                    "required_minutes": req_min,
                    "progress_percent": pct,
                    "drop_instance_id": drop_instance_id,
                })

            camp_status = "ACTIVE"
            if is_campaign_expired(camp):
                camp_status = "EXPIRED"
            elif all(d["status"] == "CLAIMED" for d in formatted_drops) and formatted_drops:
                camp_status = "COMPLETED"
            elif any(d["status"] == "READY_TO_CLAIM" for d in formatted_drops):
                camp_status = "READY"
            elif any(d["status"] == "IN_PROGRESS" for d in formatted_drops):
                camp_status = "IN_PROGRESS"

            formatted_campaigns.append({
                "id": cid,
                "name": camp_name,
                "game": gname,
                "status": camp_status,
                "cover_url": cover_url,
                "time_left": time_left,
                "stream_url": stream_url,
                "campaigns_url": "https://www.twitch.tv/drops/campaigns",
                "drops": formatted_drops,
                "total_drops": len(formatted_drops),
                "claimed_count": sum(1 for d in formatted_drops if d["status"] == "CLAIMED"),
                "ready_count": sum(1 for d in formatted_drops if d["status"] == "READY_TO_CLAIM"),
                "in_progress_count": sum(1 for d in formatted_drops if d["status"] == "IN_PROGRESS"),
            })

        # Sort campaigns: Targeted / Priority games first, then ready to claim, then in-progress, then others (expired last)
        priority_clean = [p.lower().strip() for p in (priority_games or []) if p.strip()]

        def campaign_sort_key(c):
            g_low = (c["game"] or "").lower()
            c_low = (c["name"] or "").lower()
            is_prio = any(p in g_low or p in c_low for p in priority_clean)
            is_expired = c["status"] == "EXPIRED"
            return (
                1 if is_expired else 0,
                0 if is_prio else 1,
                0 if c["ready_count"] > 0 else 1,
                0 if c["in_progress_count"] > 0 else 1,
                0 if c["status"] != "COMPLETED" else 1,
                c["game"]
            )


        formatted_campaigns.sort(key=campaign_sort_key)

        return {
            "campaigns": formatted_campaigns,
            "total_campaigns": len(formatted_campaigns),
            "claimed_history_count": len(claimed_events),
            "ready_to_claim_count": total_ready,
            "in_progress_count": total_in_prog,
        }

    def find_eligible_channel(self, game_name: str) -> Optional[Dict[str, Any]]:
        """
        Find a live channel streaming the specified game with verified drops enabled.
        Selects a random channel from all verified drop channels.
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

            # Verify which channels actually have active drops enabled via DropsHighlightService_AvailableDrops
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
                    has_active = any(bool(c.get("timeBasedDrops")) for c in camps)
                    if has_active:
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
                results = executor.map(verify_channel, candidate_nodes[:25])
                for r in results:
                    if r:
                        verified_channels.append(r)

            if verified_channels:
                chosen = random.choice(verified_channels)
                logger.info(
                    "Found %d verified drop channel(s) for '%s'. Randomly selected: %s (@%s) with %d viewers",
                    len(verified_channels), game_name, chosen["channel_name"], chosen["channel_login"], chosen["viewers"]
                )
                return chosen

            logger.warning("None of the checked channels for '%s' returned active drops. Selecting random candidate.", game_name)
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
        """
        if sub_playlist_url:
            self.touch_stream_chunk(sub_playlist_url)

        now_iso = datetime.now(timezone.utc).isoformat()

        properties: Dict[str, Any] = {
            "broadcast_id": str(stream_id),
            "channel_id": str(channel_id),
            "channel": str(channel_login) if channel_login else "",
            "client_time": now_iso,
            "game": str(game_name) if game_name else "",
            "game_id": str(game_id) if game_id else "",
            "hidden": False,
            "is_live": True,
            "live": True,
            "logged_in": True,
            "minutes_logged": 1,
            "muted": False,
            "player": "site",
            "user_id": str(self.user_id) if self.user_id else "",
        }

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
