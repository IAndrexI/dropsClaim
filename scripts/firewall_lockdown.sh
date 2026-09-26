#!/usr/bin/env bash
# =========================================================================
# Strict Egress Firewall Lockdown for Debian LXC
# Drops all unauthorized egress traffic, preventing data leaks or malware C2.
# =========================================================================

set -e

if [ "$EUID" -ne 0 ]; then
  echo "[ERROR] Please run as root (or with sudo)."
  exit 1
fi

echo "Applying strict egress firewall rules (iptables)..."

# Flush existing rules
iptables -F
iptables -X
iptables -t nat -F
iptables -t nat -X

# Set Default Policies: DROP EVERYTHING by default
iptables -P INPUT DROP
iptables -P FORWARD DROP
iptables -P OUTPUT DROP

# 1. Allow Loopback (Localhost inter-process communication)
iptables -A INPUT -i lo -j ACCEPT
iptables -A OUTPUT -o lo -j ACCEPT

# 2. Allow established/related incoming and outgoing connections
iptables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

# 3. Allow Outbound DNS resolution (Port 53 UDP/TCP)
iptables -A OUTPUT -p udp --dport 53 -j ACCEPT
iptables -A OUTPUT -p tcp --dport 53 -j ACCEPT

# 4. Allow Outbound NTP time sync (Port 123 UDP)
iptables -A OUTPUT -p udp --dport 123 -j ACCEPT

# 5. Allow Outbound HTTPS (Port 443 TCP) for official game APIs
iptables -A OUTPUT -p tcp --dport 443 -j ACCEPT

# 6. Allow Inbound SSH (Port 22 TCP) and optional VNC Web (Port 6080 TCP)
iptables -A INPUT -p tcp --dport 22 -j ACCEPT
iptables -A INPUT -p tcp --dport 6080 -j ACCEPT

echo "[SUCCESS] Strict egress firewall applied successfully."
echo "Egress is strictly restricted to DNS (53), NTP (123), and HTTPS (443)."
