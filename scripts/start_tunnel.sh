#!/usr/bin/env bash
# =========================================================================
# Cloudflare Tunnel Setup for External Access (HTTPS Domain)
# Automatically creates a secure public HTTPS domain for your dashboard.
# No router port forwarding or public IP needed!
# =========================================================================

set -e

if [ "$EUID" -ne 0 ]; then
  echo "[ERROR] Please run as root."
  exit 1
fi

echo "========================================================="
echo "   Auto Loot Claimer - External Domain Setup (Cloudflare) "
echo "========================================================="

# 1. Install cloudflared if missing
if ! command -v cloudflared &> /dev/null; then
    echo "Installing cloudflared tunnel client..."
    if [ -f /etc/alpine-release ]; then
        # Enable community repo if needed
        apk add --no-cache cloudflared || {
            echo "Downloading standalone cloudflared binary for Alpine..."
            curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /usr/local/bin/cloudflared
            chmod +x /usr/local/bin/cloudflared
        }
    elif command -v apt-get &> /dev/null; then
        curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /usr/local/bin/cloudflared
        chmod +x /usr/local/bin/cloudflared
    fi
fi

echo "Starting secure Cloudflare Tunnel to http://localhost:8080..."
echo "Your public HTTPS domain will be printed below (and saved to /var/log/tunnel.log):"
echo "---------------------------------------------------------"

# Start tunnel in background and capture the assigned domain
cloudflared tunnel --url http://localhost:8080 > /var/log/tunnel.log 2>&1 &
TUNNEL_PID=$!

sleep 4

# Extract the assigned trycloudflare.com domain
DOMAIN=$(grep -oE 'https://[a-zA-Z0-9-]+\.trycloudflare\.com' /var/log/tunnel.log | head -n 1)

if [ -n "$DOMAIN" ]; then
    echo ""
    echo "SUCCESS! Your dashboard is now accessible externally at:"
    echo "  $DOMAIN"
    echo ""
    echo "Use your sign-in credentials configured in /opt/auto-loot-claimer/.env"
    echo "Tunnel running in background (PID: $TUNNEL_PID)."
else
    echo "Tunnel started. Check /var/log/tunnel.log for your domain URL:"
    tail -n 15 /var/log/tunnel.log
fi
