#!/usr/bin/env bash
# =========================================================================
# Ultra-Low Resource Native Installer for Alpine Linux LXC
# Footprint: ~15 MB RAM idle, ~100 MB disk!
# =========================================================================

set -e

# Ensure running as root
if [ "$EUID" -ne 0 ]; then
  echo "[ERROR] Please run as root."
  exit 1
fi

APP_DIR="/opt/auto-loot-claimer"
CURRENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

echo "========================================================="
echo "   Auto Loot Claimer - Native Alpine Linux Installer     "
echo "========================================================="

echo "[1/6] Updating APK repositories and installing minimal packages..."
apk update
apk add --no-cache \
    python3 \
    nodejs \
    npm \
    chromium \
    nss \
    freetype \
    harfbuzz \
    ttf-freefont \
    git \
    curl \
    bash \
    ca-certificates

# Ensure Puppeteer knows where system Chromium is on Alpine
export PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true
export PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium-browser

echo "[2/6] Setting up application directory in $APP_DIR..."
mkdir -p "$APP_DIR"
mkdir -p "$APP_DIR/data/fgc"
mkdir -p "$APP_DIR/data/twitch"
mkdir -p "$APP_DIR/vendor"

if [ "$CURRENT_DIR" != "$APP_DIR" ]; then
    cp -r "$CURRENT_DIR"/* "$APP_DIR/"
fi

if [ ! -f "$APP_DIR/.env" ]; then
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    echo "PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium-browser" >> "$APP_DIR/.env"
    echo "Created $APP_DIR/.env from template."
fi

echo "[3/6] Installing Free Games Claimer engine in vendor directory..."
cd "$APP_DIR/vendor"
if [ ! -d "free-games-claimer" ]; then
    git clone --depth 1 https://github.com/vogler/free-games-claimer.git
    cd free-games-claimer
    npm install --production --no-audit --no-fund
else
    echo "Vendor directory already exists, skipping clone."
fi

echo "[4/6] Setting up script permissions..."
chmod +x "$APP_DIR/scripts/"*.sh

echo "[5/6] Installing OpenRC service for Twitch Drops Miner..."
cp "$APP_DIR/deploy/alpine-native/openrc/twitch-drops" /etc/init.d/twitch-drops
chmod 755 /etc/init.d/twitch-drops
rc-update add twitch-drops default

echo "[6/6] Setting up daily cron schedule for Free Games Claimer..."
# Create daily cron runner in /etc/periodic/daily/free-games
cat << 'EOF' > /etc/periodic/daily/free-games
#!/bin/sh
/opt/auto-loot-claimer/scripts/run_games_claimer.sh >> /var/log/free-games.log 2>&1
EOF
chmod +x /etc/periodic/daily/free-games

# Ensure Busybox crond is enabled and running
rc-update add crond default
rc-service crond start || true

echo "========================================================="
echo "Installation on Alpine Linux Complete!"
echo "========================================================="
echo "NEXT STEPS:"
echo "1. Configure your credentials:"
echo "   nano $APP_DIR/.env"
echo ""
echo "2. Start the Twitch Drops background daemon:"
echo "   rc-service twitch-drops start"
echo ""
echo "3. Check service status anytime:"
echo "   rc-service twitch-drops status"
echo "   tail -f /var/log/twitch-drops.log"
echo "========================================================="
