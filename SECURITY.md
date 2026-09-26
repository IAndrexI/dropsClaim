# Security, Privacy & Network Transparency Audit

This document outlines the strict security posture, data flow architecture, and zero-telemetry guarantee of **Auto Loot Claimer**.

---

## 1. Zero-Telemetry & Anti-Exfiltration Guarantee

Many automated scripts circulating online include telemetry, third-party analytics (e.g. Sentry, Google Analytics), or obfuscated credential exfiltration. 

**This repository adheres to the following non-negotiable principles:**
1. **Zero External Telemetry**: No analytics libraries, crash reporters, or tracking beacons are present.
2. **Direct Official Endpoints Only**: All outbound network calls are made strictly and exclusively to the official servers of Twitch, Epic Games, Amazon, and GOG.
3. **Local Credential Storage**: Authentication tokens, session cookies, and credentials are saved locally in the `./data/` directory with restricted file permissions (`chmod 600 / 700`). No tokens are ever sent to any remote server other than the authentic authentication endpoint of that platform.
4. **Standard Library Purity**: The core Twitch Drops daemon is implemented using the standard Python library (`urllib.request`)—avoiding deep dependency trees or malicious npm/pip supply-chain risks.

---

## 2. Explicit Network Destination Matrix

Every domain contacted by this application is documented below and validated by `scripts/audit_network.py`:

| Service | Target Domain | Protocol & Port | Purpose |
| :--- | :--- | :--- | :--- |
| **Twitch** | `gql.twitch.tv` | HTTPS (443) | Drops inventory, campaign status, and claiming mutations |
| **Twitch** | `id.twitch.tv` | HTTPS (443) | OAuth profile validation |
| **Twitch** | `spade.twitch.tv` | HTTPS (443) | Watch time progress heartbeat (streamless watch simulation) |
| **Epic Games** | `store.epicgames.com` | HTTPS (443) | Free weekly store catalog & checkout |
| **Epic Games** | `graphql.epicgames.com` | HTTPS (443) | Promotional queries |
| **Epic Games** | `store-site-backend-static.ak.epicgames.com` | HTTPS (443) | Public promotion inspection (zero auth needed) |
| **Amazon** | `gaming.amazon.com` | HTTPS (443) | Prime Gaming monthly games and in-game loot drops |
| **Amazon** | `www.amazon.com` | HTTPS (443) | Amazon account session authentication |
| **GOG** | `www.gog.com` | HTTPS (443) | Periodic DRM-free giveaway claiming |
| **Webhook (User)**| `discord.com` / `api.telegram.org` | HTTPS (443) | *Optional* user-specified notification webhook |

---

## 3. How to Audit the Running Container / LXC

You can verify with 100% certainty that no unauthorized network connections are made:

### Step 1: Run the Automated Code Audit
```bash
python3 scripts/audit_network.py
```
This scans all code and configurations, ensuring no foreign URLs or IP addresses exist.

### Step 2: Live Network Packet Capture (tcpdump)
Inside your Debian LXC container or host, run:
```bash
# Monitor all outbound DNS queries in real-time
tcpdump -i any -n 'udp port 53'

# Monitor all outbound HTTPS connections and their remote IPs
tcpdump -i any -n 'tcp port 443'
```
You will observe that traffic flows exclusively to Twitch/Amazon/Epic IP ranges.

### Step 3: Enforce Strict Egress Firewall
To guarantee that no process in your container can ever connect to unauthorized ports or rogue destinations, execute:
```bash
bash scripts/firewall_lockdown.sh
```

---

## 4. Safe Credential Handling

- **Twitch Drops**: Uses your browser's existing `auth-token` cookie. You **never** need to supply your Twitch password.
- **Epic Games & Amazon**: We strongly recommend using dedicated secondary passwords or application-specific 2FA tokens. In containerized mode, you can log in once via the local browser interface (`http://<lxc-ip>:6080`), after which your session cookies are stored locally and credentials are no longer needed.
