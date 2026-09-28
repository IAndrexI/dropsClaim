#!/bin/bash
# =========================================================================
# Run Free Games Claimer (Epic Games, Amazon Prime Gaming, GOG)
# One-shot execution script - runs, claims, reports, and exits immediately.
# =========================================================================

set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

# Source environment variables if .env exists
if [ -f .env ]; then
    set -a
    source .env
    set +a
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

# Export 2FA / TOTP keys for compatibility with Free Games Claimer
if [ -n "$EG_OTP_SECRET" ]; then
    export EG_OTPKEY="$EG_OTP_SECRET"
    export TOTP_SECRET="$EG_OTP_SECRET"
fi
if [ -n "$PG_OTP_SECRET" ]; then
    export PG_OTPKEY="$PG_OTP_SECRET"
fi

TARGET_STORE="${1:-all}"
echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Starting Free Games Claimer check (Target: $TARGET_STORE)..."

# Run preview check first to show active store giveaways
python3 src/free_games/preview_promotions.py

# Run the automated claimer runner
VENDOR_DIR="$DIR/vendor/free-games-claimer"
DATA_DIR="$DIR/data/fgc"
mkdir -p "$DATA_DIR"
export DATA_DIR="$DATA_DIR"

if [ -d "$VENDOR_DIR" ]; then
    echo "Running native Node.js claimer from $VENDOR_DIR..."
    cd "$VENDOR_DIR"

    # Epic Games Store
    if [ "$TARGET_STORE" = "all" ] || [ "$TARGET_STORE" = "epic" ] || [ "$TARGET_STORE" = "epic-games" ]; then
        if [ -n "$EG_EMAIL" ]; then
            echo "[EPIC] Claiming Epic Games Store freebies..."
            if [ -f "epic-games.js" ]; then
                node epic-games.js || npm run start -- epic-games || true
            else
                npm run start -- epic-games || true
            fi
        fi
    fi

    # Amazon Prime Gaming
    if [ "$TARGET_STORE" = "all" ] || [ "$TARGET_STORE" = "prime" ] || [ "$TARGET_STORE" = "amazon" ] || [ "$TARGET_STORE" = "prime-gaming" ]; then
        if [ -n "$PG_EMAIL" ]; then
            echo "[AMAZON] Claiming Amazon Prime Gaming freebies & loot..."
            if [ -f "prime-gaming.js" ]; then
                node prime-gaming.js || npm run start -- prime-gaming || true
            else
                npm run start -- prime-gaming || true
            fi
        fi
    fi

    # GOG Giveaways
    if [ "$TARGET_STORE" = "all" ] || [ "$TARGET_STORE" = "gog" ]; then
        if [ -n "$GOG_EMAIL" ]; then
            echo "[GOG] Claiming GOG giveaways..."
            if [ -f "gog.js" ]; then
                node gog.js || npm run start -- gog || true
            else
                npm run start -- gog || true
            fi
        fi
    fi

    cd "$DIR"
else
    # If running with docker-compose
    if command -v docker &> /dev/null && [ -f "$DIR/docker-compose.yml" ]; then
        echo "Triggering docker-compose free-games-claimer container..."
        docker compose run --rm free-games-claimer || true
    else
        echo "[INFO] Vendor directory not found. Please run deploy/alpine-native/install.sh to set up native dependencies."
    fi
fi

# Sync claimed items into persistent database
echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Syncing claimed games records..."
python3 "$DIR/src/free_games/sync_claimed.py" || true

echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Claimer check completed successfully."
