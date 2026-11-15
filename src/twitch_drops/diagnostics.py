"""
Comprehensive Twitch Account & Network Diagnostic Suite (Pure Python Standard Library).
Tests:
1. Valid currentUser session resolution from https://gql.twitch.tv/gql
2. ViewerDropsDashboard campaign retrieval and IntegrityCheckFailed detection
3. Container IP/network reachability, WAF/CloudFront blocks, and Kasada integrity status
"""

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("twitch_diagnostics")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GQL_URL = "https://gql.twitch.tv/gql"
INTEGRITY_URL = "https://gql.twitch.tv/integrity"
TWITCH_WEB_URL = "https://www.twitch.tv"
OAUTH_VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
)
WEB_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
VIEWER_DROPS_DASHBOARD_HASH = "5a4da2ab3d5b47c9f9ce864e727b2cb346af1e3ea8b897fe8f704a97ff017619"


def _make_http_request(
    url: str,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    data: Optional[bytes] = None,
    timeout: int = 10,
) -> Tuple[int, bytes, Dict[str, str]]:
    """Safe standard-library HTTP request helper returning (status_code, body_bytes, headers_dict)."""
    req_headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "*/*",
    }
    if headers:
        req_headers.update(headers)

    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 200)
            body = resp.read()
            resp_headers = dict(resp.headers)
            return status, body, resp_headers
    except urllib.error.HTTPError as err:
        try:
            body = err.read()
        except Exception:
            body = b""
        resp_headers = dict(err.headers) if hasattr(err, "headers") else {}
        return err.code, body, resp_headers


def check_network_and_ip_restrictions(timeout: int = 10) -> Dict[str, Any]:
    """
    Test 3: Check whether container's IP/network is blocked or restricted by Twitch.
    Probes:
      1) https://www.twitch.tv (Web edge: tests AWS WAF / CloudFront 403 blocks)
      2) https://gql.twitch.tv/integrity (Kasada anti-bot token acquisition & ASN challenges)
      3) https://gql.twitch.tv/gql (GQL gateway connectivity & rate limits)
    """
    probes: Dict[str, Dict[str, Any]] = {}
    ip_blocked = False
    rate_limited = False
    integrity_ok = False
    details_messages: List[str] = []

    # 1. Probe Web Edge (www.twitch.tv)
    try:
        status, body, _ = _make_http_request(TWITCH_WEB_URL, method="GET", timeout=timeout)
        probes["twitch_web"] = {"status": status, "reachable": True}
        if status == 403:
            ip_blocked = True
            details_messages.append("www.twitch.tv returned HTTP 403 Forbidden (CloudFront/WAF IP block).")
        elif status == 429:
            rate_limited = True
            details_messages.append("www.twitch.tv returned HTTP 429 Too Many Requests (edge rate limit).")
        elif status != 200:
            details_messages.append(f"www.twitch.tv returned unexpected status HTTP {status}.")
    except Exception as e:
        probes["twitch_web"] = {"status": 0, "reachable": False, "error": str(e)}
        details_messages.append(f"www.twitch.tv connection failed: {e}")

    # 2. Probe Integrity Endpoint (gql.twitch.tv/integrity)
    integrity_token = ""
    try:
        int_headers = {
            "Client-Id": WEB_CLIENT_ID,
            "Client-Session-Id": "1234567890abcdef",
            "X-Device-Id": "1234567890abcdef1234567890abcdef",
            "Origin": "https://www.twitch.tv",
            "Referer": "https://www.twitch.tv/",
            "Content-Type": "application/json",
        }
        status, body, _ = _make_http_request(INTEGRITY_URL, method="POST", headers=int_headers, data=b"", timeout=timeout)
        probes["twitch_integrity"] = {"status": status, "reachable": True}
        if status == 200:
            try:
                int_data = json.loads(body.decode("utf-8"))
                integrity_token = str(int_data.get("token") or "")
                if integrity_token:
                    integrity_ok = True
                else:
                    details_messages.append("gql.twitch.tv/integrity succeeded but token was empty.")
            except Exception:
                details_messages.append("gql.twitch.tv/integrity returned unparseable JSON.")
        elif status == 403:
            ip_blocked = True
            details_messages.append("gql.twitch.tv/integrity returned HTTP 403 (Kasada IP blacklist / Datacenter ASN block).")
        elif status == 429:
            rate_limited = True
            details_messages.append("gql.twitch.tv/integrity returned HTTP 429 (rate-limited by Kasada/Twitch edge).")
        else:
            details_messages.append(f"gql.twitch.tv/integrity returned HTTP {status}.")
    except Exception as e:
        probes["twitch_integrity"] = {"status": 0, "reachable": False, "error": str(e)}
        details_messages.append(f"gql.twitch.tv/integrity connection failed: {e}")

    # 3. Probe GQL Gateway (gql.twitch.tv/gql)
    try:
        gql_probe_payload = json.dumps({"query": "query ProbeGateway { __typename }"}).encode("utf-8")
        gql_headers = {
            "Client-Id": WEB_CLIENT_ID,
            "Content-Type": "application/json",
            "Origin": "https://www.twitch.tv",
            "Referer": "https://www.twitch.tv/",
        }
        status, body, _ = _make_http_request(GQL_URL, method="POST", headers=gql_headers, data=gql_probe_payload, timeout=timeout)
        probes["twitch_gql"] = {"status": status, "reachable": True}
        if status == 403:
            ip_blocked = True
            details_messages.append("gql.twitch.tv/gql returned HTTP 403 Forbidden.")
        elif status == 429:
            rate_limited = True
            details_messages.append("gql.twitch.tv/gql returned HTTP 429 Too Many Requests.")
    except Exception as e:
        probes["twitch_gql"] = {"status": 0, "reachable": False, "error": str(e)}
        details_messages.append(f"gql.twitch.tv/gql connection failed: {e}")

    all_reachable = all(p.get("reachable") for p in probes.values())

    if ip_blocked:
        status_code = "BLOCKED"
        summary_msg = "Twitch IP Block Detected (HTTP 403). CloudFront, AWS WAF, or Kasada is restricting this container's IP/ASN."
        recommendation = (
            "Your IP address or datacenter ASN is blocked by Twitch anti-bot edge. "
            "Remedies: 1) Switch network/VPN or use residential proxy. 2) If running in a cloud/VPS container, "
            "run the miner on your local home network where Twitch traffic is permitted."
        )
    elif rate_limited:
        status_code = "RATE_LIMITED"
        summary_msg = "Twitch Rate Limit Active (HTTP 429). The edge gateway is temporarily throttling requests from this IP."
        recommendation = "Wait 15-30 minutes for edge rate-limit counters to reset. Avoid high-frequency polling."
    elif not all_reachable:
        status_code = "UNREACHABLE"
        summary_msg = "Twitch Network Unreachable. One or more core endpoints failed to connect."
        recommendation = "Check container DNS, firewall rules (outbound HTTPS port 443), and internet connection."
    elif integrity_ok:
        status_code = "OK"
        summary_msg = "Twitch network connection and Kasada Client-Integrity are fully functional. Container IP is clean and unrestricted."
        recommendation = ""
    else:
        status_code = "DEGRADED"
        summary_msg = "Twitch network is reachable, but Client-Integrity token acquisition did not complete cleanly."
        recommendation = "Check if adblocking/firewall software is modifying HTTPS headers."

    return {
        "status": status_code,
        "is_blocked": ip_blocked,
        "is_rate_limited": rate_limited,
        "all_reachable": all_reachable,
        "integrity_token_acquired": integrity_ok,
        "integrity_token_preview": (integrity_token[:20] + "...") if integrity_token else "",
        "message": summary_msg,
        "details": details_messages,
        "recommendation": recommendation,
        "probes": probes,
    }


def check_current_user(token: str, client_id: Optional[str] = None, timeout: int = 10) -> Dict[str, Any]:
    """
    Test 1: Does the user's stored Twitch auth-token return a valid currentUser from gql.twitch.tv?
    """
    if not token or not str(token).strip():
        return {
            "status": "FAIL",
            "valid": False,
            "error": "missing_token",
            "message": "Twitch auth-token is missing. No token configured in .env or data/twitch_token.json.",
            "recommendation": "Run 'python -m src.twitch_drops.main --login' or set TWITCH_AUTH_TOKEN in .env.",
        }

    clean_tok = token.strip().strip("'\"")
    if clean_tok.lower().startswith("oauth "):
        clean_tok = clean_tok[6:].strip()

    from src.auth.twitch_auth import client_id_for
    cid = client_id or client_id_for(clean_tok) or WEB_CLIENT_ID

    headers = {
        "Client-Id": cid,
        "Authorization": f"OAuth {clean_tok}",
        "Content-Type": "application/json",
        "Origin": "https://www.twitch.tv",
        "Referer": "https://www.twitch.tv/",
    }
    query_body = json.dumps({"query": "query CheckUserSession { currentUser { id login displayName email } }"}).encode("utf-8")

    status, body, _ = _make_http_request(GQL_URL, method="POST", headers=headers, data=query_body, timeout=timeout)

    if status == 401:
        return {
            "status": "FAIL",
            "valid": False,
            "error": "http_401_unauthorized",
            "client_id": cid,
            "message": "Twitch rejected authorization (HTTP 401 Unauthorized). The stored auth-token is invalid or expired.",
            "recommendation": "Re-authenticate using 'python -m src.twitch_drops.main --login' or refresh your browser 'auth-token' cookie.",
        }

    if status != 200:
        return {
            "status": "ERROR",
            "valid": False,
            "error": f"http_{status}",
            "client_id": cid,
            "message": f"Twitch GQL returned HTTP {status} during CheckUserSession.",
            "recommendation": "Check network connectivity and ensure Twitch GQL is accessible.",
        }

    try:
        res_json = json.loads(body.decode("utf-8"))
    except Exception as e:
        return {
            "status": "ERROR",
            "valid": False,
            "error": "invalid_json",
            "message": f"Failed to parse GQL response JSON: {e}",
            "recommendation": "Retry request.",
        }

    data = res_json.get("data") or {}
    user = data.get("currentUser")

    if user and user.get("id"):
        return {
            "status": "OK",
            "valid": True,
            "user_id": str(user.get("id")),
            "login": str(user.get("login") or ""),
            "display_name": str(user.get("displayName") or ""),
            "email": str(user.get("email") or ""),
            "client_id": cid,
            "message": f"Valid currentUser verified: {user.get('displayName')} (Login: {user.get('login')}, ID: {user.get('id')})",
            "recommendation": "",
        }
    else:
        errs = res_json.get("errors") or []
        err_msg = str(errs[0].get("message") if errs and isinstance(errs[0], dict) else errs) if errs else "currentUser is null"
        return {
            "status": "FAIL",
            "valid": False,
            "error": "currentUser_null",
            "client_id": cid,
            "raw_errors": errs,
            "message": f"Twitch returned currentUser=null ({err_msg}). The auth-token is no longer associated with an active session.",
            "recommendation": "Your Twitch token has been invalidated or logged out. Run 'python -m src.twitch_drops.main --login' to re-link.",
        }


def check_viewer_drops_dashboard(token: str, client_id: Optional[str] = None, timeout: int = 10) -> Dict[str, Any]:
    """
    Test 2: Does ViewerDropsDashboard return campaigns or an IntegrityCheckFailed error?
    """
    clean_tok = (token or "").strip().strip("'\"")
    if clean_tok.lower().startswith("oauth "):
        clean_tok = clean_tok[6:].strip()

    from src.auth.twitch_auth import client_id_for
    cid = client_id or client_id_for(clean_tok) or WEB_CLIENT_ID

    # Acquire fresh Client-Integrity token to test both with and without integrity
    integrity_token = ""
    try:
        int_headers = {
            "Client-Id": cid,
            "Client-Session-Id": "1234567890abcdef",
            "X-Device-Id": "1234567890abcdef1234567890abcdef",
            "Origin": "https://www.twitch.tv",
            "Referer": "https://www.twitch.tv/",
            "Content-Type": "application/json",
        }
        if clean_tok:
            int_headers["Authorization"] = f"OAuth {clean_tok}"
        st, b_int, _ = _make_http_request(INTEGRITY_URL, method="POST", headers=int_headers, data=b"", timeout=timeout)
        if st == 200:
            int_data = json.loads(b_int.decode("utf-8"))
            integrity_token = str(int_data.get("token") or "")
    except Exception:
        pass

    headers = {
        "Client-Id": cid,
        "Content-Type": "application/json",
        "Origin": "https://www.twitch.tv",
        "Referer": "https://www.twitch.tv/",
        "Client-Session-Id": "1234567890abcdef",
        "X-Device-Id": "1234567890abcdef1234567890abcdef",
    }
    if clean_tok:
        headers["Authorization"] = f"OAuth {clean_tok}"
    if integrity_token:
        headers["Client-Integrity"] = integrity_token

    payload = {
        "operationName": "ViewerDropsDashboard",
        "extensions": {
            "persistedQuery": {
                "version": 1,
                "sha256Hash": VIEWER_DROPS_DASHBOARD_HASH,
            }
        },
        "variables": {"fetchRewardCampaigns": False},
    }

    status, body, _ = _make_http_request(GQL_URL, method="POST", headers=headers, data=json.dumps(payload).encode("utf-8"), timeout=timeout)

    if status == 401:
        return {
            "status": "FAIL",
            "integrity_failed": False,
            "campaigns_count": 0,
            "error": "http_401",
            "message": "ViewerDropsDashboard returned HTTP 401 Unauthorized (token invalid).",
            "recommendation": "Update Twitch auth-token via --login.",
        }

    try:
        res_json = json.loads(body.decode("utf-8"))
    except Exception as e:
        return {
            "status": "ERROR",
            "integrity_failed": False,
            "campaigns_count": 0,
            "error": "invalid_json",
            "message": f"ViewerDropsDashboard returned unparseable JSON: {e}",
            "recommendation": "Check GQL service status.",
        }

    errors = res_json.get("errors") or []
    err_str = json.dumps(errors).lower()
    has_integrity_failure = any("integrity" in err_str for _ in [1]) or any(
        "failed integrity check" in str(e).lower() or "integritycheckfailed" in str(e).lower() for e in errors
    )

    if has_integrity_failure:
        return {
            "status": "INTEGRITY_FAILED",
            "integrity_failed": True,
            "campaigns_count": 0,
            "raw_errors": errors,
            "message": "ViewerDropsDashboard failed with IntegrityCheckFailed! Twitch's Kasada anti-bot challenged this client session.",
            "recommendation": (
                "Twitch requires authentic client integrity. In our codebase, DropsMiner handles this via headless Chromium "
                "(which passes Kasada telemetry natively) and automatic Client-Integrity token acquisition at /integrity."
            ),
        }

    if errors and not res_json.get("data"):
        return {
            "status": "ERROR",
            "integrity_failed": False,
            "campaigns_count": 0,
            "raw_errors": errors,
            "message": f"ViewerDropsDashboard returned GraphQL errors: {errors[0].get('message') if isinstance(errors[0], dict) else errors}",
            "recommendation": "Inspect raw errors for details.",
        }

    user_data = (res_json.get("data") or {}).get("currentUser")
    if user_data is None:
        if not clean_tok:
            return {
                "status": "UNAUTHENTICATED",
                "integrity_failed": False,
                "campaigns_count": 0,
                "message": "ViewerDropsDashboard returned currentUser=null (no auth-token supplied; user must be logged in to view personal campaigns).",
                "recommendation": "Log in to view active campaigns via 'python -m src.twitch_drops.main --login'.",
            }
        return {
            "status": "FAIL",
            "integrity_failed": False,
            "campaigns_count": 0,
            "message": "ViewerDropsDashboard returned currentUser=null despite an auth-token being provided.",
            "recommendation": "Token is invalid or expired. Please re-authenticate.",
        }

    campaigns = user_data.get("dropCampaigns") or []
    c_names = []
    for c in campaigns[:8]:
        if isinstance(c, dict):
            c_name = c.get("name") or (c.get("game") or {}).get("displayName") or "Unknown"
            c_names.append(c_name)

    return {
        "status": "OK",
        "integrity_failed": False,
        "campaigns_count": len(campaigns),
        "campaign_samples": c_names,
        "message": f"ViewerDropsDashboard succeeded! Returned {len(campaigns)} active drop campaigns (integrity check PASSED).",
        "recommendation": "",
    }


def run_twitch_diagnostics(token: Optional[str] = None, verbose: bool = True) -> Dict[str, Any]:
    """
    Run full 3-part diagnostic suite:
      1) Token & currentUser verification
      2) ViewerDropsDashboard & IntegrityCheckFailed test
      3) Container IP / Network restriction probe
    """
    from src.auth.twitch_auth import get_credentials, resolve_token

    env_tok = os.environ.get("TWITCH_AUTH_TOKEN", "").strip()
    resolved_tok, resolved_cid = get_credentials(token or env_tok)
    active_tok = token or resolved_tok

    if verbose:
        print("==========================================================")
        print("      TWITCH ACCOUNT & NETWORK COMPREHENSIVE DIAGNOSTICS   ")
        print("==========================================================")

    # 1. IP & Network restriction probe
    net_res = check_network_and_ip_restrictions()
    if verbose:
        print("\n[CHECK 1/3] Container IP & Network Restrictions:")
        status_tag = f"[{net_res['status']}]"
        print(f"  {status_tag} {net_res['message']}")
        for detail in net_res.get("details", []):
            print(f"    - {detail}")
        if net_res.get("recommendation"):
            print(f"    -> RECOMMENDATION: {net_res['recommendation']}")

    # 2. Token & currentUser check
    user_res = check_current_user(active_tok, client_id=resolved_cid)
    if verbose:
        print("\n[CHECK 2/3] Stored Twitch Auth-Token & currentUser Profile:")
        status_tag = f"[{user_res['status']}]"
        print(f"  {status_tag} {user_res['message']}")
        if user_res.get("recommendation"):
            print(f"    -> RECOMMENDATION: {user_res['recommendation']}")

    # 3. ViewerDropsDashboard & Integrity check
    dash_res = check_viewer_drops_dashboard(active_tok, client_id=resolved_cid)
    if verbose:
        print("\n[CHECK 3/3] ViewerDropsDashboard & Kasada Integrity Check:")
        status_tag = f"[{dash_res['status']}]"
        print(f"  {status_tag} {dash_res['message']}")
        if dash_res.get("campaign_samples"):
            print(f"    Active campaign samples: {', '.join(dash_res['campaign_samples'][:5])}")
        if dash_res.get("recommendation"):
            print(f"    -> RECOMMENDATION: {dash_res['recommendation']}")

    overall_healthy = (
        net_res.get("status") == "OK"
        and user_res.get("status") == "OK"
        and dash_res.get("status") == "OK"
    )

    if verbose:
        print("\n----------------------------------------------------------")
        if overall_healthy:
            print("[SUMMARY] HEALTHY: All 3 Twitch diagnostic checks passed cleanly.")
        else:
            issues = []
            if net_res.get("status") != "OK":
                issues.append(f"Network/IP ({net_res['status']})")
            if user_res.get("status") != "OK":
                issues.append(f"Auth Token ({user_res['status']})")
            if dash_res.get("status") != "OK":
                issues.append(f"Drops Dashboard ({dash_res['status']})")
            print(f"[SUMMARY] ATTENTION REQUIRED: Issues detected in: {', '.join(issues)}")
        print("==========================================================\n")

    return {
        "healthy": overall_healthy,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "network": net_res,
        "user_session": user_res,
        "drops_dashboard": dash_res,
    }
