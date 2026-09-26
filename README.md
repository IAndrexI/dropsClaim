# Auto Loot & Drops Claimer 🎮📦

An ultra-lightweight, security-audited, zero-telemetry application designed to automatically claim:
- **Epic Games Store**: Weekly free games and promotional drops
- **Amazon Prime Gaming**: Monthly free PC games, codes, and in-game loot
- **Twitch Drops**: Streamless background mining and automated reward claiming
- **GOG**: Promotional DRM-free giveaways

Engineered specifically for **LXC containers** (Proxmox, LXD) running **Plain Debian** or **Docker**, with an extreme focus on minimal CPU, RAM (<60 MB idle), and storage usage.

---

## 🛡️ Security & Zero-Telemetry Guarantee

Before running any tool handling game accounts, security and privacy are paramount.
- **Zero Third-Party Telemetry**: No Google Analytics, Mixpanel, Sentry, or external tracking beacons.
- **Strict Network Whitelist**: All outbound HTTPS connections are strictly locked to official endpoints (`twitch.tv`, `epicgames.com`, `amazon.com`, `gog.com`) and your optional webhook.
- **No Password Needed for Twitch**: Twitch Drops mining uses your existing browser `auth-token` cookie. You never provide your Twitch password.
- **Audited Source Code**: Includes an automated network audit script ([`scripts/audit_network.py`](file:///scripts/audit_network.py)) and optional strict egress firewall ([`scripts/firewall_lockdown.sh`](file:///scripts/firewall_lockdown.sh)). See [SECURITY.md](file:///SECURITY.md) for the full audit.

---

## ⚡ Deployment Options (Plain Debian vs Docker)

| Metric | Option A: Native Debian LXC (Recommended) | Option B: Docker in LXC |
| :--- | :--- | :--- |
| **Idle RAM Usage** | **~40 - 60 MB RAM** | ~140 - 200 MB RAM |
| **Idle CPU Usage** | **0.0% - 0.2%** | ~0.5% |
| **Disk Overhead** | Minimal (no container layer duplicates) | Requires Docker engine & images |
| **Execution Model** | `twitch-drops.service` (daemon) + `free-games.timer` (runs daily for 90s, then 0 MB RAM) | `docker-compose.yml` with memory limits |

---

## 🏔️ Option A: Native Alpine Linux LXC (Absolute Lowest Footprint: ~15 MB RAM)

### 1. Clone to your Alpine Container
```bash
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
- Configures BusyBox daily cron schedule (`/etc/periodic/daily/free-games`).

### 3. Configure Credentials
```bash
nano /opt/auto-loot-claimer/.env
```

### 4. Start the Service
```bash
rc-service twitch-drops start
```

---

## 🚀 Option B: Native Debian LXC

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

## 🐳 Option B: Docker-Compose

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

## 🔑 How to Get Your Twitch `auth-token` (Password-Free)

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

## 🔍 Commands & Utilities

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

---

## 📡 Git Repository Status

This project is fully initialized as a local Git repository with all credentials, session files, and cache excluded in `.gitignore`.

**To push this repository to your new Git provider (GitHub, GitLab, Gitea):**
```bash
# 1. Add your remote repository URL:
git remote add origin <YOUR_GIT_REPO_URL>

# 2. Push the codebase:
git branch -M main
git push -u origin main
```
