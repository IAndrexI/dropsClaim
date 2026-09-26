#!/usr/bin/env bash
# =========================================================================
# Ultra-Low Resource Native Installer for Debian LXC
# Zero-Docker overhead: persistent idle memory < 60 MB RAM!
# =========================================================================

set -e

# Ensure running as root
if [ "$EUID" -ne 0 ]; then
  echo "[ERROR] Please run as root (or with sudo)."
  exit 1
fi

APP_DIR="/opt/auto-loot-claimer"
CURRENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

echo "========================================================="
echo "   Auto Loot Claimer - Native Debian LXC Installer       "
echo "========================================================="

echo "[1/6] Updating APT repositories and installing minimal prerequisites..."
apt-get update -y
apt-get install -y --no-install-recommends \
    python3 \
    python3-minimal \
    nodejs \
    npm \
    chromium \
    chromium-sandbox \
    ca-certificates \
    curl \
    git

echo "[2/6] Setting up application directory in $APP_DIR..."
mkdir -p "$APP_DIR"
mkdir -p "$APP_DIR/data/fgc"
mkdir -p "$APP_DIR/data/twitch"
mkdir -p "$APP_DIR/vendor"

# Copy repository files to /opt/auto-loot-claimer if not already there
if [ "$CURRENT_DIR" != "$APP_DIR" ]; then
    cp -r "$CURRENT_DIR"/* "$APP_DIR/"
fi

# Create default .env if missing
if [ ! -f "$APP_DIR/.env" ]; then
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
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

echo "[5/6] Installing systemd services and timers..."
cp "$APP_DIR/deploy/debian-native/systemd/twitch-drops.service" /etc/systemd/system/
cp "$APP_DIR/deploy/debian-native/systemd/free-games.service" /etc/systemd/system/
cp "$APP_DIR/deploy/debian-native/systemd/free-games.timer" /etc/systemd/system/

systemctl daemon-reload
systemctl enable twitch-drops.service
systemctl enable free-games.timer
systemctl start free-games.timer

echo "[6/6] Installation Complete!"
echo "========================================================="
echo "NEXT STEPS:"
echo "1. Edit your configuration file:"
echo "   nano $APP_DIR/.env"
echo ""
echo "2. Add your Twitch 'auth-token' and store credentials."
echo ""
echo "3. Start the Twitch Drops background daemon:"
echo "   systemctl start twitch-drops.service"
echo ""
echo "4. Check system status anytime:"
echo "   systemctl status twitch-drops.service"
echo "   systemctl list-timers free-games.timer"
echo "   journalctl -u twitch-drops.service -f"
echo "========================================================="
