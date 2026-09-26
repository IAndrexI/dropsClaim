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

echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Starting Free Games Claimer check..."

# Run preview check first to show active store giveaways
python3 src/free_games/preview_promotions.py

# Run the automated claimer runner
VENDOR_DIR="$DIR/vendor/free-games-claimer"
DATA_DIR="$DIR/data/fgc"
mkdir -p "$DATA_DIR"

if [ -d "$VENDOR_DIR" ]; then
    echo "Running native Node.js claimer from $VENDOR_DIR..."
    cd "$VENDOR_DIR"
    
    # Run Epic Games claimer if credentials are provided
    if [ -n "$EG_EMAIL" ]; then
        echo "Claiming Epic Games Store freebies..."
        npm run start -- epic-games || true
    fi

    # Run Amazon Prime Gaming claimer if credentials are provided
    if [ -n "$PG_EMAIL" ]; then
        echo "Claiming Amazon Prime Gaming freebies & loot..."
        npm run start -- prime-gaming || true
    fi

    # Run GOG claimer if credentials are provided
    if [ -n "$GOG_EMAIL" ]; then
        echo "Claiming GOG giveaways..."
        npm run start -- gog || true
    fi
else
    # If running with docker-compose
    if command -v docker &> /dev/null && [ -f "$DIR/docker-compose.yml" ]; then
        echo "Triggering docker-compose free-games-claimer container..."
        docker compose run --rm free-games-claimer || true
    else
        echo "[INFO] Vendor directory not found. Please run deploy/debian-native/install.sh to set up native dependencies."
    fi
fi

echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Claimer check completed successfully."
