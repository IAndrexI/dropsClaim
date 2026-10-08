#!/usr/bin/env bash
# ===========================================================
# Auto Loot Claimer - Desktop Application Launcher (Linux / macOS)
# Opens the dashboard as a standalone windowed desktop application
# ===========================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

if command -v python3 >/dev/null 2>&1; then
    exec python3 scripts/desktop_app.py "$@"
elif command -v python >/dev/null 2>&1; then
    exec python scripts/desktop_app.py "$@"
else
    echo "[NOTICE] Python not found in PATH. Attempting direct browser app launch..."
    for BROWSER in google-chrome google-chrome-stable chromium chromium-browser microsoft-edge; do
        if command -v "$BROWSER" >/dev/null 2>&1; then
            exec "$BROWSER" --app=http://localhost:8080 --window-size=1280,840
        fi
    done
    echo "[ERROR] No compatible browser found. Please install Chrome, Chromium, or Python."
    exit 1
fi
