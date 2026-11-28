#!/bin/bash
# Install and configure Tailscale on Raspberry Pi OS alongside rp-inky, AdGuard Home & rp-vpn.
#
# - Uses --accept-dns=false so Tailscale never overwrites local DNS / AdGuard Home
# - Auto-detects eth0 LAN subnet (e.g. 192.168.86.0/24) and advertises it as a Subnet Router
#   so you can reach http://192.168.86.30:8080, AdGuard Home (:3000), and home devices remotely
# - Optionally advertises the Pi as a Tailscale Exit Node (enabled by default; pass --no-exit-node to skip)
set -euo pipefail

if [ "$(uname -s)" != "Linux" ]; then
    echo "This installer is for Raspberry Pi OS (Linux)." >&2
    exit 1
fi

ADVERTISE_EXIT=true
ADVERTISE_ROUTES=true
WAN_IFACE="${WAN_IFACE:-eth0}"

usage() {
    cat <<EOF
Usage: $0 [options]

Options:
  --no-exit-node   Do not advertise this Pi as a Tailscale Exit Node
  --no-routes      Do not advertise the home LAN subnet route
  --iface IFACE    LAN interface to detect subnet from (default: eth0)
  -h, --help       Show this help
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --no-exit-node) ADVERTISE_EXIT=false; shift ;;
        --no-routes)    ADVERTISE_ROUTES=false; shift ;;
        --iface)        WAN_IFACE="${2:-eth0}"; shift 2 ;;
        -h|--help)      usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
    esac
done

echo "=== 1. Installing Tailscale ==="
if command -v tailscale >/dev/null 2>&1; then
    echo "Tailscale is already installed ($(tailscale version | head -n 1))."
else
    curl -fsSL https://tailscale.com/install.sh | sh
fi

sudo systemctl enable --now tailscaled.service

echo "=== 2. Ensuring IP forwarding is enabled ==="
if [ "$(cat /proc/sys/net/ipv4/ip_forward 2>/dev/null || echo 0)" != "1" ]; then
    echo 'net.ipv4.ip_forward = 1' | sudo tee /etc/sysctl.d/99-tailscale.conf >/dev/null
    sudo sysctl -p /etc/sysctl.d/99-tailscale.conf >/dev/null || true
fi

# Optional UDP GRO forwarding tuning for higher Tailscale throughput on Pi 4
if command -v ethtool >/dev/null 2>&1 && [ -d "/sys/class/net/$WAN_IFACE" ]; then
    sudo ethtool -K "$WAN_IFACE" rx-udp-gro-forwarding on rx-gro-list off 2>/dev/null || true
fi

echo "=== 3. Bringing Tailscale up (--accept-dns=false to protect AdGuard Home & rp-vpn) ==="
TS_ARGS=(--accept-dns=false)

if $ADVERTISE_ROUTES; then
    LAN_CIDR="$(python3 - "$WAN_IFACE" <<'PY'
import ipaddress, re, subprocess, sys
iface = sys.argv[1]
res = subprocess.run(["ip", "-4", "-o", "addr", "show", "dev", iface], capture_output=True, text=True, check=False)
m = re.search(r"\binet\s+(\d+\.\d+\.\d+\.\d+/\d+)", res.stdout)
if m:
    print(str(ipaddress.IPv4Network(m.group(1), strict=False)))
PY
)"
    if [ -n "$LAN_CIDR" ]; then
        echo "Detected home LAN subnet on $WAN_IFACE: $LAN_CIDR"
        TS_ARGS+=("--advertise-routes=$LAN_CIDR")
    fi
fi

if $ADVERTISE_EXIT; then
    TS_ARGS+=(--advertise-exit-node)
fi

echo "Running: sudo tailscale up ${TS_ARGS[*]}"
sudo tailscale up "${TS_ARGS[@]}"

if systemctl is-active --quiet inky-webui.service 2>/dev/null; then
    sudo systemctl restart inky-webui.service || true
fi

TS_IP="$(tailscale ip -4 2>/dev/null | head -n 1 || true)"
cat <<EOF

====================================================================
Tailscale is connected!
====================================================================

Pi Tailscale IP: ${TS_IP:-pending login}

Next steps (in https://login.tailscale.com/admin/machines):
  1. Find your Raspberry Pi in the Machines list -> click "..." -> Edit route settings.
  2. Approve the Subnet Route (${LAN_CIDR:-192.168.86.0/24}) and Exit Node (if enabled).
  3. Install the Tailscale app on your iPhone / Mac and sign into the same account.

From anywhere in the world (cellular / hotel Wi-Fi), open:
  - rp-inky Web Hub : http://${TS_IP:-100.x.y.z}:8080
  - AdGuard Home    : http://${TS_IP:-100.x.y.z}:3000

Useful commands:
  tailscale status
  tailscale ip -4
EOF
