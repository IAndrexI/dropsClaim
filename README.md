# Auto Loot & Drops Claimer

An ultra-lightweight, security-audited, zero-telemetry application designed to automatically claim:
- **Epic Games Store**: Weekly free games and promotional drops
- **Amazon Prime Gaming**: Monthly free PC games, codes, and in-game loot
- **Twitch Drops**: Streamless background mining and automated reward claiming
- **GOG**: Promotional DRM-free giveaways

Engineered specifically for **LXC containers** (Proxmox, LXD) running **Plain Debian**, **Alpine Linux**, or **Docker**, with an extreme focus on minimal CPU, RAM (<20 MB idle on Alpine), and storage usage.

---

## Security & Zero-Telemetry Guarantee

Before running any tool handling game accounts, security and privacy are paramount.
- **Zero Third-Party Telemetry**: No Google Analytics, Mixpanel, Sentry, or external tracking beacons.
- **Strict Network Whitelist**: All outbound HTTPS connections are strictly locked to official endpoints (`twitch.tv`, `epicgames.com`, `amazon.com`, `gog.com`) and your optional webhook.
- **No Password Needed for Twitch**: Twitch Drops mining uses your existing browser `auth-token` cookie. You never provide your Twitch password.
- **Audited Source Code**: Includes an automated network audit script ([`scripts/audit_network.py`](file:///scripts/audit_network.py)) and optional strict egress firewall ([`scripts/firewall_lockdown.sh`](file:///scripts/firewall_lockdown.sh)). See [SECURITY.md](file:///SECURITY.md) for the full audit.

---

## Deployment Options (Alpine vs Debian vs Docker)

| Metric | Option A: Native Alpine LXC (Lowest) | Option B: Native Debian LXC | Option C: Docker in LXC |
| :--- | :--- | :--- | :--- |
| **Idle RAM Usage** | **~15 - 20 MB RAM** | **~40 - 60 MB RAM** | ~140 - 200 MB RAM |
| **Idle CPU Usage** | **0.0% - 0.1%** | **0.0% - 0.2%** | ~0.5% |
| **Disk Overhead** | **~150 MB** | Minimal (~1.2 GB) | Requires Docker engine & images |
| **Execution Model** | OpenRC daemon + daily cron | systemd daemon + systemd timer | docker-compose.yml with memory limits |

---

## Option A: Native Alpine Linux LXC (Lowest Footprint: ~15 MB RAM)

### 1. Clone to your Alpine Container
```bash
apk update
apk add --no-cache git bash nano curl ca-certificates
git clone https://github.com/IAndrexI/dropsClaim.git /opt/auto-loot-claimer
cd /opt/auto-loot-claimer
```

### 2. Run the Alpine Installer
```bash
bash deploy/alpine-native/install.sh
```
This automatically:
- Installs minimal packages via `apk` (`python3`, `nodejs`, `npm`, `chromium`, `bash`, `git`).
- Configures OpenRC service (`/etc/init.d/twitch-drops`).
- Configures OpenRC service for Web Dashboard (`/etc/init.d/auto-loot-web`).
- Configures BusyBox daily cron schedule (`/etc/periodic/daily/free-games`).

### 3. Configure Credentials
```bash
nano /opt/auto-loot-claimer/.env
```

### 4. Start the Services (Miner & Web Dashboard)
```bash
rc-service twitch-drops start
rc-service auto-loot-web start
```

---

## Mini Web Dashboard (`http://<server-ip>:8080`)

A self-contained, real-time web dashboard built using the standard library (zero external dependencies) that allows you to check active free games/items and see if you personally claimed them.

* **Twitch Drops Inventory**: View active campaigns with personal progress bars (`54/120 min`, 45%) and status badges (`CLAIMED`, `READY TO CLAIM`, `In Progress`).
* **Epic Games Store**: Displays active free games (e.g. *Astrea*, *Mechabellum*), end dates, and whether they have been claimed to your library, along with upcoming releases.
* **Amazon Prime Gaming & GOG**: Real-time status of your connected accounts.
* **One-Click Actions**: "Claim Ready Drops" and "Check All Games" buttons directly in the web UI.
* **Live Logs**: View real-time output from background mining.

Access it anytime in your browser at:
`http://<your-lxc-ip>:8080`

---

## Option B: Native Debian LXC

### 1. Transfer or Clone to your LXC Server
Copy this project folder to your Debian LXC container (for example into `/opt/auto-loot-claimer`).

### 2. Run the Native Installer
```bash
sudo bash deploy/debian-native/install.sh
```
This automatically:
- Installs minimal system packages (`python3`, `nodejs`, `chromium`).
- Configures `systemd` background daemon for Twitch Drops (`twitch-drops.service`).
- Configures `systemd` timer for Free Games (`free-games.timer`), which only wakes up once a day, claims games in ~60-90 seconds, and terminates immediately to free all RAM.

### 3. Configure Credentials
Edit `/opt/auto-loot-claimer/.env`:
```bash
nano /opt/auto-loot-claimer/.env
```
Fill in:
- `TWITCH_AUTH_TOKEN`: Your Twitch cookie (see instructions below).
- `EG_EMAIL` / `EG_PASSWORD`: Epic Games login.
- `PG_EMAIL` / `PG_PASSWORD`: Amazon Prime Gaming login.
- `DISCORD_WEBHOOK_URL`: (Optional) Discord webhook for claim notifications.

### 4. Start the Service
```bash
systemctl start twitch-drops.service
```

---

## Option C: Docker-Compose

If you prefer containerized deployment:

1. Copy `.env.example` to `.env` and fill in your credentials:
   ```bash
   cp .env.example .env
   nano .env
   ```
2. Start the stack:
   ```bash
   docker compose up -d
   ```
3. **First-Time Login / Captcha Solving**:
   If Epic Games or Amazon prompts for 2FA or a captcha on first login, open `http://<lxc-ip>:6080` in your web browser (noVNC interface). Complete the prompt once; your session cookies will be saved in `./data/fgc/` for all future automated runs.

---

## How to Get Your Twitch `auth-token` (Password-Free)

1. Open [https://www.twitch.tv](https://www.twitch.tv) in your browser and make sure you are logged in.
2. Press `F12` (or `Ctrl + Shift + I`) to open Developer Tools.
3. Click the **Application** tab (in Chrome/Edge) or **Storage** tab (in Firefox).
4. Under **Cookies**, select `https://www.twitch.tv`.
5. Find the cookie named **`auth-token`** and copy its value.
6. Paste it into your `.env` file:
   ```env
   TWITCH_AUTH_TOKEN=abcd1234efgh5678ijkl9012
   ```

---

## Commands & Utilities

### Preview Active Free Games (No Login Required)
Check what's free on the Epic Games Store right now:
```bash
python3 src/free_games/preview_promotions.py
```

### Inspect Twitch Drops Progress & Campaigns
```bash
# Check current drops progress in your inventory
python3 -m src.twitch_drops.main --inventory

# List all active drop campaigns currently on Twitch
python3 -m src.twitch_drops.main --campaigns

# Instantly claim any drops currently at 100%
python3 -m src.twitch_drops.main --claim-now
```

### Run Network Security Audit
```bash
python3 scripts/audit_network.py
```

### Check and Manage Bot Processes (Single Viewing)
```bash
# Check if any bots or miners are running on the system
python3 scripts/check_bots.py

# Terminate duplicate or conflicting bots to ensure single viewing
python3 scripts/check_bots.py --kill-duplicates

# Terminate all running miners and clear PID lockfile
python3 scripts/check_bots.py --kill-all
```

---

## Updating from GitHub

### 1. In-Browser One-Click Update
Open the Web Dashboard (`http://<server-ip>:8080`), go to the **System & GitHub** tab, and click **Update Now from GitHub**. The server pulls the latest code and reloads automatically.

### 2. Command-Line Updater
Run the dedicated updater script:
```bash
bash scripts/update_from_github.sh
```

### 3. Automated Daily Background Updates (Alpine Linux LXC)
Enable automatic daily updates via Alpine's daily periodic cron:
```bash
bash scripts/update_from_github.sh --install-cron
```

---

## Sync Claimed Loot to GitHub

To generate a human-readable summary of confirmed games and Twitch drops and automatically commit/push it to your GitHub repository:
```bash
# Generate CLAIMED_LOOT.md locally
python3 scripts/sync_loot_to_github.py

# Generate and push directly to GitHub origin/main
python3 scripts/sync_loot_to_github.py --push
```
Or click **Sync & Push to GitHub** in the Web Dashboard under **System & GitHub**.

---

## Autostart on System Reboot

Ensure the Twitch Drops Miner daemon, Web Dashboard, and cron claimers automatically start whenever your container or server reboots:

### 1. One-Command Autostart Setup
```bash
bash scripts/enable_autostart.sh
```
This automatically registers `twitch-drops`, `auto-loot-web`, and `crond` into OpenRC's `default` runlevel on Alpine Linux (or systemd enabled units on Debian/Ubuntu).

### 2. Verify Autostart Registration
```bash
# Verify services registered for boot
bash scripts/enable_autostart.sh --status

# On Alpine Linux:
rc-update show default
```

### 3. In-Browser One-Click
Navigate to **System & GitHub** in the Web Dashboard and click **Enable Autostart on Reboot**.

