#!/usr/bin/env bash
# =========================================================================
# Auto Loot Claimer - 1-Step Universal Updater
# Pulls latest commits from GitHub, fixes any divergent git state,
# refreshes scripts, updates autostart, refreshes campaigns, and restarts.
#
# ZERO EMOJIS compliant. Standard POSIX / Bash compatible.
# =========================================================================

set -e

# Target directory
TARGET_DIR="${AUTO_LOOT_DIR:-/opt/auto-loot-claimer}"

# If running inside repo directory, use current dir
if [ -d "./.git" ]; then
    DIR="$(pwd)"
elif [ -d "$TARGET_DIR/.git" ]; then
    DIR="$TARGET_DIR"
else
    echo "[ERROR] Auto Loot Claimer directory not found at $TARGET_DIR or current directory."
    exit 1
fi

cd "$DIR"

echo "=========================================================="
echo "          AUTO LOOT CLAIMER - 1-STEP UPDATER              "
echo "=========================================================="
echo "Target directory : $DIR"
echo "Remote branch    : origin/main"
echo "----------------------------------------------------------"

# 1. Fetch remote and reset hard to origin/main (reconciles divergent branches safely)
echo "[1/5] Fetching latest updates from GitHub..."
git fetch origin main --quiet || {
    echo "[ERROR] Could not connect to GitHub. Check internet connection."
    exit 1
}

PREV_COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
git reset --hard origin/main
NEW_COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")

echo "[2/5] Setting executable permissions on scripts..."
chmod +x "$DIR"/scripts/*.sh "$DIR"/scripts/*.py "$DIR"/*.sh 2>/dev/null || true

# 2. Install global shortcut command 'update-loot'
echo "[3/5] Installing global 'update-loot' command shortcut..."
for bin_dir in /usr/local/bin /usr/bin; do
    if [ -d "$bin_dir" ] && [ -w "$bin_dir" ]; then
        ln -sf "$DIR/update.sh" "$bin_dir/update-loot" 2>/dev/null || true
        ln -sf "$DIR/update.sh" "$bin_dir/update" 2>/dev/null || true
    fi
done

# Ensure alias in root shell profiles if present
for prof in /root/.bashrc /root/.profile /root/.ashrc; do
    if [ -f "$prof" ] && ! grep -q "update-loot" "$prof" 2>/dev/null; then
        echo "alias update-loot='$DIR/update.sh'" >> "$prof"
        echo "alias update='$DIR/update.sh'" >> "$prof"
    fi
done

# 3. Ensure autostart services & daily cron schedules
echo "[4/5] Verifying autostart services and daily cron schedules..."
if [ -f "$DIR/scripts/enable_autostart.sh" ]; then
    bash "$DIR/scripts/enable_autostart.sh" >/dev/null 2>&1 || true
fi

# 4. Refresh drop campaigns cache
if [ -f "$DIR/scripts/refresh_campaigns.py" ]; then
    python3 "$DIR/scripts/refresh_campaigns.py" >/dev/null 2>&1 || true
fi

# 5. Restart services
echo "[5/5] Restarting background services..."
if command -v rc-service >/dev/null 2>&1; then
    # OpenRC (Alpine Linux)
    rc-service twitch-drops restart 2>/dev/null || rc-service twitch-drops start 2>/dev/null || true
    rc-service auto-loot-web restart 2>/dev/null || rc-service auto-loot-web start 2>/dev/null || true
elif command -v systemctl >/dev/null 2>&1; then
    # systemd (Debian / Ubuntu)
    systemctl restart twitch-drops.service 2>/dev/null || systemctl start twitch-drops.service 2>/dev/null || true
    systemctl restart auto-loot-web.service 2>/dev/null || systemctl start auto-loot-web.service 2>/dev/null || true
fi

echo "=========================================================="
echo "  UPDATE COMPLETE: Successfully updated to $NEW_COMMIT"
if [ "$PREV_COMMIT" != "$NEW_COMMIT" ]; then
    echo "  Updated from: $PREV_COMMIT -> $NEW_COMMIT"
else
    echo "  Already on latest commit ($NEW_COMMIT)"
fi
echo "  Shortcut installed: You can now type 'update-loot' anytime!"
echo "=========================================================="
