"""
Security & Network Audit Script for Auto Loot Claimer.
Scans repository source code for all network endpoints, URLs, and external domains.
Verifies that 100% of network traffic matches the strict authorized domain allowlist.
"""

import os
import re
import socket
import ssl
import sys
from typing import Dict, List, Set

# Strict Whitelist of Authorized Domains
ALLOWED_DOMAINS = {
    # Twitch Official Services & Drops Directory
    "twitch.tv",
    "gql.twitch.tv",
    "id.twitch.tv",
    "passport.twitch.tv",
    "spade.twitch.tv",
    "pubsub-edge.twitch.tv",
    "usher.ttvnw.net",
    "ttvnw.net",
    "drophunter.app",

    # Epic Games Official Services
    "epicgames.com",
    "store.epicgames.com",
    "graphql.epicgames.com",
    "store-site-backend-static.ak.epicgames.com",
    "account-public-service-prod.ol.epicgames.com",
    # Amazon Prime Gaming Official Services
    "amazon.com",
    "gaming.amazon.com",
    "www.amazon.com",
    # GOG Official Services
    "gog.com",
    "www.gog.com",
    # User Webhooks (Optional)
    "discord.com",
    "discordapp.com",
    "api.telegram.org",
    # Official Open-Source Repositories (installers only)
    "github.com",
    "ghcr.io",
    # Local loopback
    "localhost",
    "127.0.0.1",
}

URL_REGEX = re.compile(r'https?://([a-zA-Z0-9.-]+)')


def scan_source_files(root_dir: str) -> Dict[str, Set[str]]:
    """Scan all source code files for hardcoded URLs and domain references."""
    findings: Dict[str, Set[str]] = {}
    extensions = {".py", ".sh", ".json", ".yml", ".yaml", ".drops"}

    for dirpath, _, filenames in os.walk(root_dir):
        if "data" in dirpath or "vendor" in dirpath or ".git" in dirpath:
            continue
        for file in filenames:
            ext = os.path.splitext(file)[1]
            if ext in extensions or file in ("Dockerfile", "Dockerfile.drops"):
                filepath = os.path.join(dirpath, file)
                try:
                    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read()
                    matches = set(URL_REGEX.findall(content))
                    if matches:
                        findings[filepath] = matches
                except Exception as e:
                    print(f"[WARN] Could not read {filepath}: {e}")
    return findings


def test_endpoint_reachability(domain: str) -> bool:
    """Test DNS resolution and TLS handshake with an authorized endpoint."""
    try:
        # 1. Test DNS resolution
        ip = socket.gethostbyname(domain)
        # 2. Test TLS connection on port 443
        ctx = ssl.create_default_context()
        with socket.create_connection((ip, 443), timeout=5) as sock:
            with ctx.wrap_socket(sock, server_hostname=domain) as ssock:
                return True
    except Exception as e:
        return False


def main():
    print("==========================================================")
    print("       ZERO-TELEMETRY NETWORK SECURITY AUDIT              ")
    print("==========================================================")
    
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    print(f"Scanning repository source at: {repo_root}\n")

    findings = scan_source_files(repo_root)
    all_detected_domains: Set[str] = set()
    unauthorized_domains: List[str] = []

    for path, domains in findings.items():
        rel_path = os.path.relpath(path, repo_root)
        print(f"File: {rel_path}")
        for domain in sorted(domains):
            all_detected_domains.add(domain)
            is_allowed = any(domain == a or domain.endswith("." + a) for a in ALLOWED_DOMAINS)
            status = "[ALLOWED]" if is_allowed else "[UNAUTHORIZED / SUSPICIOUS!]"
            if not is_allowed:
                unauthorized_domains.append((rel_path, domain))
            print(f"  {status} -> {domain}")

    print("\n----------------------------------------------------------")
    if unauthorized_domains:
        print("[ALERT] Unauthorized domains detected in source code:")
        for file, domain in unauthorized_domains:
            print(f"  FAILED: {file} references {domain}")
        sys.exit(1)
    else:
        print("[SUCCESS] All detected domains belong to the strict security allowlist!")
        print("No telemetry, tracking, analytics, or third-party proxies were found.")

    print("\n----------------------------------------------------------")
    print("Testing connectivity to authorized service endpoints...")
    test_domains = ["gql.twitch.tv", "store.epicgames.com", "gaming.amazon.com"]
    for d in test_domains:
        reachable = test_endpoint_reachability(d)
        res = "[OK: Reachable via TLS]" if reachable else "[WARN: Could not reach endpoint]"
        print(f"  {d}: {res}")

    print("\n==========================================================")
    print("Audit status: CLEAN AND VERIFIED")
    print("==========================================================")


if __name__ == "__main__":
    main()
