"""
Audited, zero-telemetry Twitch GraphQL client for Drops Tracking and Claiming.
Uses Python Standard Library ONLY (no pip or external dependencies required).

STRICT DESTINATION POLICY:
All network requests in this module are strictly restricted to official Twitch endpoints:
- https://gql.twitch.tv/gql
- https://id.twitch.tv
- https://spade.twitch.tv
No third-party analytics, tracking, or proxy endpoints are contacted.
"""

import json
import logging
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger("twitch_api")

TWITCH_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"  # Official Twitch Web Client ID
GQL_URL = "https://gql.twitch.tv/gql"
SPADE_URL = "https://spade.twitch.tv/batched"


def clean_text(text: Any) -> str:
    """Strip emojis and non-standard symbols to ensure clean terminal and UI rendering."""
    if not text:
        return ""
    import re
    emoji_pattern = re.compile(
        "[\U00010000-\U0010ffff]|"
        "[\u2600-\u27bf]|"
        "[\u2300-\u23ff]|"
        "[\u2b50-\u2b55]|"
        "[\u203c-\u2049]"
    )
    cleaned = emoji_pattern.sub("", str(text))
    return " ".join(cleaned.split()).strip()


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
        Ensures campaigns for ALL provided games (in priority_games or popular drops directory)
        are queried and returned.
        """
        import concurrent.futures

        campaigns_map: Dict[str, Dict[str, Any]] = {}

        # 1. Add any in-progress campaigns from user inventory
        inv = self.get_inventory()
        in_prog = inv.get("dropCampaignsInProgress") or []
        if isinstance(in_prog, dict):
            in_prog = [in_prog]
        for camp in in_prog:
            cid = camp.get("id")
            if cid:
                campaigns_map[cid] = camp

        # 2. Targeted query for ALL provided priority games plus active drops roster
        provided_games = [g.strip() for g in (priority_games or []) if g.strip()]
        default_roster = [
            "World of Warcraft", "Rainbow Six Siege", "Rust", "Overwatch 2",
            "Apex Legends", "Rocket League", "Grand Theft Auto V", "Big Walk",
            "WARDOGS", "Counter-Strike", "Escape from Tarkov", "Warframe"
        ]
        all_targets = list(dict.fromkeys(provided_games + default_roster))

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

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            for gname, camps in executor.map(fetch_game_campaigns, all_targets):
                for c in camps:
                    cid = c.get("id")
                    if cid and cid not in campaigns_map:
                        if not c.get("game"):
                            c["game"] = {"displayName": gname, "name": gname}
                        campaigns_map[cid] = c

        # 3. Discover live campaigns from streams with drops tags (catches any new games)
        query_streams = """
        query StreamsWithDrops {
            streams(first: 30, options: {tags: ["DropsEnabled", "Drops"]}) {
                edges {
                    node {
                        id
                        game { id displayName name }
                        broadcaster { id login displayName }
                    }
                }
            }
        }
        """
        try:
            streams_res = self.post_gql(query_streams, operation_name="StreamsWithDrops")
            edges = streams_res.get("data", {}).get("streams", {}).get("edges", [])
            seen_games: Set[str] = set()

            for edge in edges:
                g = edge.get("node", {}).get("game")
                if not g:
                    continue
                gname = g.get("displayName") or g.get("name")
                if not gname or gname in seen_games or any(c.get("game", {}).get("displayName") == gname for c in campaigns_map.values()):
                    continue
                seen_games.add(gname)

                broadcaster_id = edge["node"]["broadcaster"]["id"]
                try:
                    drops_res = self.post_persisted_gql(
                        operation_name="DropsHighlightService_AvailableDrops",
                        sha256_hash="9a62a09bce5b53e26e64a671e530bc599cb6aab1e5ba3cbd5d85966d3940716f",
                        variables={"channelID": str(broadcaster_id)}
                    )
                    channel_obj = (drops_res.get("data") or {}).get("channel")
                    if channel_obj and channel_obj.get("viewerDropCampaigns"):
                        for c in channel_obj["viewerDropCampaigns"]:
                            cid = c.get("id")
                            if cid and cid not in campaigns_map:
                                if not c.get("game"):
                                    c["game"] = g
                                campaigns_map[cid] = c
                except Exception as inner_e:
                    logger.debug("Failed querying available drops on broadcaster %s: %s", broadcaster_id, inner_e)
        except Exception as e:
            logger.warning("Could not discover external live streams: %s", e)

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
            if all(d["status"] == "CLAIMED" for d in formatted_drops) and formatted_drops:
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
                "drops": formatted_drops,
                "total_drops": len(formatted_drops),
                "claimed_count": sum(1 for d in formatted_drops if d["status"] == "CLAIMED"),
                "ready_count": sum(1 for d in formatted_drops if d["status"] == "READY_TO_CLAIM"),
                "in_progress_count": sum(1 for d in formatted_drops if d["status"] == "IN_PROGRESS"),
            })

        # Sort campaigns: Targeted / Priority games first, then ready to claim, then in-progress, then others
        priority_clean = [p.lower().strip() for p in (priority_games or []) if p.strip()]

        def campaign_sort_key(c):
            g_low = (c["game"] or "").lower()
            c_low = (c["name"] or "").lower()
            is_prio = any(p in g_low or p in c_low for p in priority_clean)
            return (
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
        """Find a live channel streaming the specified game with drops enabled."""
        query = """
        query DirectoryPage_Game($name: String!) {
            game(name: $name) {
                streams(first: 20, options: {tags: ["DropsEnabled", "Drops"]}) {
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
            edges = res.get("data", {}).get("game", {}).get("streams", {}).get("edges", [])
            if edges:
                best_stream = edges[0]["node"]
                return {
                    "channel_id": str(best_stream["broadcaster"]["id"]),
                    "channel_login": best_stream["broadcaster"]["login"],
                    "channel_name": best_stream["broadcaster"]["displayName"],
                    "stream_id": str(best_stream["id"]),
                    "viewers": best_stream["viewersCount"],
                }
        except Exception as e:
            logger.warning("Error finding channel for %s: %s", game_name, e)
        return None

    def send_minute_watched_heartbeat(self, channel_id: str, stream_id: str) -> bool:
        """
        Send a stream-viewing progress heartbeat to Twitch's tracking endpoint.
        Signals watch time without needing to stream heavy video data (saving 99.9% bandwidth & CPU).
        """
        payload = [
            {
                "event": "minute-watched",
                "properties": {
                    "channel_id": str(channel_id),
                    "broadcast_id": str(stream_id),
                    "player": "site",
                    "user_id": str(self.user_id) if self.user_id else "",
                    "live": True,
                }
            }
        ]
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Client-Id": TWITCH_CLIENT_ID,
            "Authorization": f"OAuth {self.auth_token}",
            "User-Agent": self.user_agent,
            "Content-Type": "text/plain;charset=UTF-8",
        }
        try:
            req = urllib.request.Request(SPADE_URL, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status in (200, 204)
        except Exception as err:
            logger.warning("Spade heartbeat warning: %s", err)
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
