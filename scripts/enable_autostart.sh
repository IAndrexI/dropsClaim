#!/usr/bin/env bash
# =========================================================================
# Auto Loot Claimer - Enable Automatic Startup on System Reboot
# Configures OpenRC (Alpine Linux) or systemd (Debian/Ubuntu) so that:
#   1. twitch-drops (Miner & Drops Claimer)
#   2. auto-loot-web (Web Dashboard)
#   3. crond / systemd timer (Daily Free Games Claimer)
# automatically start upon system reboot or container restart.
#
# ZERO EMOJIS compliant. POSIX / Bash compatible.
# =========================================================================

set -e

if [ "$EUID" -ne 0 ]; then
    echo "[ERROR] Please run as root (or with sudo)."
    exit 1
fi

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

echo "=========================================================="
echo "      AUTO LOOT CLAIMER - AUTOSTART CONFIGURATION         "
echo "=========================================================="
echo "Application directory: $DIR"

# Mode check: Status only
if [ "$1" = "--status" ] || [ "$1" = "status" ]; then
    echo "Checking current autostart status..."
    if command -v rc-update >/dev/null 2>&1; then
        echo "Init System: OpenRC (Alpine Linux)"
        echo "Services configured in default runlevel:"
        rc-update show default | grep -E "twitch-drops|auto-loot-web|crond" || echo "None of the auto-loot services found in default runlevel."
    elif command -v systemctl >/dev/null 2>&1; then
        echo "Init System: systemd (Debian / Ubuntu)"
        for s in twitch-drops.service auto-loot-web.service free-games.timer; do
            state=$(systemctl is-enabled "$s" 2>/dev/null || echo "not-installed")
            echo "  $s: $state"
        done
    else
        echo "[WARN] Unknown init system."
    fi
    exit 0
fi

# Detect init system
if command -v rc-update >/dev/null 2>&1; then
    echo "[INIT] Detected OpenRC (Alpine Linux)"
    echo ""

    echo "[1/4] Installing OpenRC service descriptors to /etc/init.d/..."
    cp "$DIR/deploy/alpine-native/openrc/twitch-drops" /etc/init.d/twitch-drops
    chmod 755 /etc/init.d/twitch-drops

    cp "$DIR/deploy/alpine-native/openrc/auto-loot-web" /etc/init.d/auto-loot-web
    chmod 755 /etc/init.d/auto-loot-web

    echo "[2/4] Registering services to OpenRC default boot runlevel..."
    rc-update add twitch-drops default
    rc-update add auto-loot-web default
    rc-update add crond default

    echo "[3/4] Ensuring periodic daily schedules exist..."
    mkdir -p /etc/periodic/daily
    cat << 'EOF' > /etc/periodic/daily/free-games
#!/bin/sh
/opt/auto-loot-claimer/scripts/run_games_claimer.sh >> /var/log/free-games.log 2>&1
EOF
    chmod +x /etc/periodic/daily/free-games

    cat << 'EOF' > /etc/periodic/daily/refresh-twitch-campaigns
#!/bin/sh
python3 /opt/auto-loot-claimer/scripts/refresh_campaigns.py >> /var/log/twitch-campaigns.log 2>&1
EOF
    chmod +x /etc/periodic/daily/refresh-twitch-campaigns

    echo "[4/4] Starting services now if not already active..."
    rc-service crond start 2>/dev/null || true
    rc-service twitch-drops restart 2>/dev/null || rc-service twitch-drops start 2>/dev/null || true
    rc-service auto-loot-web restart 2>/dev/null || rc-service auto-loot-web start 2>/dev/null || true

    echo ""
    echo "=========================================================="
    echo "  AUTOSTART VERIFICATION (OpenRC Default Runlevel)        "
    echo "=========================================================="
    rc-update show default | grep -E "twitch-drops|auto-loot-web|crond"
    echo "=========================================================="
    echo "[SUCCESS] Services are fully registered to autostart upon system reboot!"

elif command -v systemctl >/dev/null 2>&1; then
    echo "[INIT] Detected systemd (Debian / Ubuntu)"
    echo ""

    echo "[1/4] Installing systemd unit files to /etc/systemd/system/..."
    cp "$DIR/deploy/debian-native/systemd/twitch-drops.service" /etc/systemd/system/
    cp "$DIR/deploy/debian-native/systemd/auto-loot-web.service" /etc/systemd/system/
    cp "$DIR/deploy/debian-native/systemd/free-games.service" /etc/systemd/system/
    cp "$DIR/deploy/debian-native/systemd/free-games.timer" /etc/systemd/system/

    echo "[2/4] Reloading systemd daemon..."
    systemctl daemon-reload

    echo "[3/4] Enabling services to autostart on system boot..."
    systemctl enable twitch-drops.service
    systemctl enable auto-loot-web.service
    systemctl enable free-games.timer

    echo "[4/4] Starting services now..."
    systemctl restart twitch-drops.service || systemctl start twitch-drops.service || true
    systemctl restart auto-loot-web.service || systemctl start auto-loot-web.service || true
    systemctl start free-games.timer || true

    echo ""
    echo "=========================================================="
    echo "  AUTOSTART VERIFICATION (systemd Enabled Units)          "
    echo "=========================================================="
    for s in twitch-drops.service auto-loot-web.service free-games.timer; do
        state=$(systemctl is-enabled "$s" 2>/dev/null || echo "unknown")
        echo "  $s: $state"
    done
    echo "=========================================================="
    echo "[SUCCESS] Services are fully registered to autostart upon system reboot!"

else
    echo "[ERROR] Unsupported init system. Could not find OpenRC (rc-update) or systemd (systemctl)."
    exit 1
fi
