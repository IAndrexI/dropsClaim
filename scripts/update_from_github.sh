#!/bin/bash
# =========================================================================
# Auto Loot Claimer - Automatic GitHub Updater
# Fetches latest commits from https://github.com/IAndrexI/dropsClaim,
# updates local files, preserves permissions, and restarts active services.
#
# ZERO EMOJIS compliant. POSIX / Bash compatible for Alpine and Debian.
# =========================================================================

set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

REMOTE_BRANCH="${GIT_BRANCH:-main}"
ACTION="${1:-update}"

echo "=========================================================="
echo "          AUTO LOOT CLAIMER - GITHUB UPDATER              "
echo "=========================================================="
echo "Application directory: $DIR"
echo "Target branch: origin/$REMOTE_BRANCH"

if [ ! -d ".git" ]; then
    echo "[ERROR] $DIR is not a git repository."
    exit 1
fi

# Ensure remote is reachable
git fetch origin "$REMOTE_BRANCH" --quiet || {
    echo "[ERROR] Failed to connect to GitHub remote. Please check your network connection."
    exit 1
}

LOCAL_HEAD=$(git rev-parse HEAD)
REMOTE_HEAD=$(git rev-parse "origin/$REMOTE_BRANCH")

echo "Current local commit:  $LOCAL_HEAD"
echo "Latest GitHub commit:  $REMOTE_HEAD"

if [ "$ACTION" = "--check" ] || [ "$ACTION" = "check" ]; then
    if [ "$LOCAL_HEAD" = "$REMOTE_HEAD" ]; then
        echo "[STATUS] System is fully UP TO DATE with GitHub."
        exit 0
    else
        BEHIND_COUNT=$(git rev-list --count HEAD.."origin/$REMOTE_BRANCH")
        echo "[STATUS] Update AVAILABLE: Local is $BEHIND_COUNT commit(s) behind GitHub."
        exit 0
    fi
fi

if [ "$ACTION" = "--install-cron" ]; then
    echo "[SETUP] Installing automated GitHub update schedule..."
    if [ -d "/etc/periodic/daily" ]; then
        cat << 'EOF' > /etc/periodic/daily/auto-loot-update
#!/bin/sh
/opt/auto-loot-claimer/scripts/update_from_github.sh >> /var/log/auto-loot-update.log 2>&1
EOF
        chmod +x /etc/periodic/daily/auto-loot-update
        echo "[SUCCESS] Installed daily auto-update runner to /etc/periodic/daily/auto-loot-update"
    else
        # Fallback to crontab
        (crontab -l 2>/dev/null | grep -v "update_from_github.sh" ; echo "0 4 * * * $DIR/scripts/update_from_github.sh >> /var/log/auto-loot-update.log 2>&1") | crontab -
        echo "[SUCCESS] Installed daily cron job (04:00 AM) to crontab."
    fi
    exit 0
fi

# Performing update
if [ "$LOCAL_HEAD" = "$REMOTE_HEAD" ] && [ "$ACTION" != "--force" ]; then
    echo "[OK] Already up to date. No new commits found on GitHub."
    echo "Use '--force' if you want to re-pull and restart services anyway."
    exit 0
fi

echo "[1/4] Pulling latest updates from origin/$REMOTE_BRANCH..."
git pull origin "$REMOTE_BRANCH" --rebase || git reset --hard "origin/$REMOTE_BRANCH"


echo "[2/4] Ensuring script execute permissions..."
chmod +x "$DIR/scripts/"*.sh || true
chmod +x "$DIR/deploy/alpine-native/openrc/"* 2>/dev/null || true

echo "[3/4] Updating dependencies if needed..."
if [ -d "$DIR/vendor/free-games-claimer" ]; then
    (cd "$DIR/vendor/free-games-claimer" && git pull origin master --quiet 2>/dev/null || true)
fi

echo "[4/4] Restarting active background services..."
if command -v rc-service >/dev/null 2>&1; then
    # Alpine Linux OpenRC
    if rc-service twitch-drops status >/dev/null 2>&1; then
        echo "Restarting twitch-drops OpenRC service..."
        rc-service twitch-drops restart || true
    fi
    if rc-service auto-loot-web status >/dev/null 2>&1; then
        echo "Restarting auto-loot-web OpenRC service..."
        rc-service auto-loot-web restart || true
    fi
elif command -v systemctl >/dev/null 2>&1; then
    # Debian / Ubuntu systemd
    if systemctl is-active --quiet twitch-drops 2>/dev/null; then
        echo "Restarting twitch-drops systemd service..."
        systemctl restart twitch-drops || true
    fi
    if systemctl is-active --quiet auto-loot-web 2>/dev/null; then
        echo "Restarting auto-loot-web systemd service..."
        systemctl restart auto-loot-web || true
    fi
fi

NEW_HEAD=$(git rev-parse --short HEAD)
echo "=========================================================="
echo "Update successfully applied! Current version: $NEW_HEAD"
echo "=========================================================="
