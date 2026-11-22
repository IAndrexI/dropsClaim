#!/bin/bash
# =========================================================================
# Run Free Games Claimer (Epic Games, Amazon Prime Gaming, GOG)
# One-shot execution script - runs, claims, reports, and exits cleanly.
# =========================================================================

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

mkdir -p "$DIR/data"
LOCK_FILE="$DIR/data/store_claimer.lock"
if [ -f "$LOCK_FILE" ]; then
    OLD_PID=$(cat "$LOCK_FILE" 2>/dev/null || true)
    if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
        echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Store auto-claimer already running (PID $OLD_PID). Skipping duplicate run."
        exit 0
    fi
fi
echo "$$" > "$LOCK_FILE"
cleanup_lock() {
    rm -f "$LOCK_FILE" 2>/dev/null || true
}
trap cleanup_lock EXIT

# Safely load environment variables from .env if it exists (handles unquoted values with spaces)
if [ -f .env ]; then
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            \#*|"") continue ;;
            *=*)
                key="${line%%=*}"
                val="${line#*=}"
                key=$(echo "$key" | tr -d '[:space:]')
                val="${val#\"}"
                val="${val%\"}"
                val="${val#\'}"
                val="${val%\'}"
                export "$key"="$val"
                ;;
        esac
    done < .env
fi

# Detect Chromium binary on Alpine (/usr/bin/chromium-browser) or Debian (/usr/bin/chromium)
if [ -z "$PUPPETEER_EXECUTABLE_PATH" ]; then
    if [ -x "/usr/bin/chromium-browser" ]; then
        export PUPPETEER_EXECUTABLE_PATH="/usr/bin/chromium-browser"
    elif [ -x "/usr/bin/chromium" ]; then
        export PUPPETEER_EXECUTABLE_PATH="/usr/bin/chromium"
    fi
fi
export PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true

# Export 2FA / TOTP keys & claimer options for compatibility with Free Games Claimer
if [ -n "$EG_OTP_SECRET" ]; then
    export EG_OTPKEY="$EG_OTP_SECRET"
    export TOTP_SECRET="$EG_OTP_SECRET"
fi
if [ -n "$PG_OTP_SECRET" ]; then
    export PG_OTPKEY="$PG_OTP_SECRET"
fi
export NOTIFY="${NOTIFY:-$DISCORD_WEBHOOK_URL}"
export PG_CLAIMDLC="${PG_CLAIMDLC:-1}"
export GOG_NEWSLETTER="${GOG_NEWSLETTER:-0}"
export LOGIN_TIMEOUT="${LOGIN_TIMEOUT:-45}"

TARGET_STORE="${1:-all}"
echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Starting Free Games Claimer check (Target: $TARGET_STORE)..."

# Run preview check first to show active store giveaways (non-fatal)
python3 src/free_games/preview_promotions.py || true

VENDOR_DIR="$DIR/vendor/free-games-claimer"
DATA_DIR="$DIR/data/fgc"
mkdir -p "$DATA_DIR"
export DATA_DIR="$DATA_DIR"

# Symlink vendor/free-games-claimer/data -> data/fgc so cookies/db persist across updates
if [ -d "$VENDOR_DIR" ]; then
    if [ -d "$VENDOR_DIR/data" ] && [ ! -L "$VENDOR_DIR/data" ]; then
        cp -rn "$VENDOR_DIR/data/"* "$DATA_DIR/" 2>/dev/null || true
        rm -rf "$VENDOR_DIR/data"
    fi
    ln -sfn "$DATA_DIR" "$VENDOR_DIR/data"
fi

# Check if Playwright Firefox is runnable (not Alpine musl, Node + vendor + firefox binary present)
USE_FGC=0
if [ ! -f /etc/alpine-release ] && [ -d "$VENDOR_DIR" ] && command -v node >/dev/null 2>&1; then
    if (cd "$VENDOR_DIR" && node -e "import fs from 'node:fs'; import('playwright-firefox').then(p => process.exit(fs.existsSync(p.firefox.executablePath()) ? 0 : 1)).catch(() => process.exit(1))" 2>/dev/null); then
        USE_FGC=1
    fi
fi

if [ "$USE_FGC" = "1" ]; then
    echo "Running Free Games Claimer (Playwright Firefox) from $VENDOR_DIR..."
    cd "$VENDOR_DIR"

    if [ "$TARGET_STORE" = "all" ] || [ "$TARGET_STORE" = "epic" ] || [ "$TARGET_STORE" = "epic-games" ]; then
        if [ -n "$EG_EMAIL" ]; then
            echo "[EPIC] Claiming Epic Games Store freebies..."
            if ! node epic-games.js < /dev/null; then
                echo "[EPIC] Playwright runner failed; falling back to native Chromium CDP..."
                (cd "$DIR" && python3 -m src.free_games.auto_claimer --store epic || true)
            fi
        fi
    fi

    if [ "$TARGET_STORE" = "all" ] || [ "$TARGET_STORE" = "prime" ] || [ "$TARGET_STORE" = "amazon" ] || [ "$TARGET_STORE" = "prime-gaming" ]; then
        if [ -n "$PG_EMAIL" ]; then
            echo "[AMAZON] Claiming Amazon Prime Gaming freebies & loot..."
            if ! node prime-gaming.js < /dev/null; then
                echo "[AMAZON] Playwright runner failed; falling back to native Chromium CDP..."
                (cd "$DIR" && python3 -m src.free_games.auto_claimer --store amazon || true)
            fi
        fi
    fi

    if [ "$TARGET_STORE" = "all" ] || [ "$TARGET_STORE" = "gog" ]; then
        if [ -n "$GOG_EMAIL" ]; then
            echo "[GOG] Claiming GOG giveaways..."
            if ! node gog.js < /dev/null; then
                echo "[GOG] Playwright runner failed; falling back to native Chromium CDP..."
                (cd "$DIR" && python3 -m src.free_games.auto_claimer --store gog || true)
            fi
        fi
    fi

    cd "$DIR"
else
    echo "Running native Chromium CDP store auto-claimer (Target: $TARGET_STORE)..."
    python3 -m src.free_games.auto_claimer --store "$TARGET_STORE" || true
fi

# Sync claimed items into persistent database
echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Syncing claimed games records..."
python3 "$DIR/src/free_games/sync_claimed.py" || true

echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Claimer check completed."
