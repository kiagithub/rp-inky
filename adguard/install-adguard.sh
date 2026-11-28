#!/bin/bash
# Install AdGuard Home on Raspberry Pi OS cleanly alongside rp-inky Web Hub (:8080) & rp-vpn.
#
# - Installs official AdGuard Home into /opt/AdGuardHome as systemd service AdGuardHome.service
# - Frees port 53 if systemd-resolved stub listener is holding it
# - Enables vpn.local_dns_blocker=true so eth0:53 reaches AdGuard Home while wlan0 (UK-Frame-VPN)
#   continues using leak-proof Surfshark UK DNS for UK streaming
# - Keeps AdGuard Home Web UI on port 3000 so it never conflicts with rp-inky Web Hub on :8080
set -euo pipefail

if [ "$(uname -s)" != "Linux" ]; then
    echo "This installer is for Raspberry Pi OS (Linux)." >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
INSTALL_DIR="/opt/AdGuardHome"
AGH_PORT="${AGH_PORT:-3000}"

echo "=== 1. Checking port 53 (systemd-resolved stub listener) ==="
if systemctl is-active --quiet systemd-resolved 2>/dev/null; then
    if ss -tulpn 2>/dev/null | grep -q '127\.0\.0\.53:53\b'; then
        echo "Disabling systemd-resolved DNSStubListener so AdGuard Home can bind port 53..."
        sudo mkdir -p /etc/systemd/resolved.conf.d
        sudo tee /etc/systemd/resolved.conf.d/adguardhome.conf >/dev/null <<'EOF'
[Resolve]
DNS=1.1.1.1 9.9.9.9
DNSStubListener=no
EOF
        if [ -L /etc/resolv.conf ] && readlink -f /etc/resolv.conf | grep -q stub-resolv.conf; then
            sudo ln -sf /run/systemd/resolve/resolv.conf /etc/resolv.conf
        fi
        sudo systemctl reload-or-restart systemd-resolved
    fi
fi

echo "=== 2. Downloading & installing AdGuard Home ==="
ARCH="$(uname -m)"
case "$ARCH" in
    aarch64|arm64) AGH_ARCH="arm64" ;;
    armv7l)        AGH_ARCH="armv7" ;;
    armv6l)        AGH_ARCH="armv6" ;;
    x86_64)        AGH_ARCH="amd64" ;;
    *)
        echo "Unsupported CPU architecture: $ARCH" >&2
        exit 1
        ;;
esac

if [ -x "$INSTALL_DIR/AdGuardHome" ]; then
    echo "AdGuard Home is already installed at $INSTALL_DIR."
else
    TMP_DIR="$(mktemp -d)"
    trap 'rm -rf "$TMP_DIR"' EXIT
    TARBALL="AdGuardHome_linux_${AGH_ARCH}.tar.gz"
    URL="https://static.adguard.com/adguardhome/release/${TARBALL}"
    echo "Fetching $URL..."
    curl -fsSL "$URL" -o "$TMP_DIR/$TARBALL"
    tar -xzf "$TMP_DIR/$TARBALL" -C "$TMP_DIR"
    sudo mkdir -p /opt
    sudo mv "$TMP_DIR/AdGuardHome" "$INSTALL_DIR"
    sudo chmod +x "$INSTALL_DIR/AdGuardHome"
    echo "Registering AdGuardHome.service..."
    sudo "$INSTALL_DIR/AdGuardHome" -s install
fi

sudo systemctl enable --now AdGuardHome.service

echo "=== 3. Configuring rp-vpn to allow Home LAN DNS (eth0:53) to reach AdGuard Home ==="
if [ -f "$REPO_DIR/vpn/config.json" ]; then
    python3 - "$REPO_DIR/vpn/config.json" <<'PY'
import sys
from pathlib import Path
cfg_path = Path(sys.argv[1])
sys.path.insert(0, str(cfg_path.resolve().parent.parent))
from vpn.manager import update_local_config
out = update_local_config(cfg_path, {"local_dns_blocker": True})
print(f"Updated {out} with local_dns_blocker=true")
PY
    if command -v rp-vpn >/dev/null 2>&1 && systemctl is-active --quiet rp-vpn.service 2>/dev/null; then
        echo "Reloading rp-vpn nftables rules so eth0:53 routes to AdGuard Home..."
        sudo rp-vpn restart || true
    fi
fi

echo "=== 4. Saving AdGuard Home URL in webui/config.local.json ==="
if [ -f "$REPO_DIR/webui/config.json" ]; then
    python3 - "$REPO_DIR/webui/config.json" "$AGH_PORT" <<'PY'
import sys
from pathlib import Path
cfg_path = Path(sys.argv[1])
port = int(sys.argv[2])
sys.path.insert(0, str(cfg_path.resolve().parent.parent))
from vpn.manager import update_local_config
update_local_config(cfg_path, {"adguard": {"enabled": True, "port": port}})
PY
    if systemctl is-active --quiet inky-webui.service 2>/dev/null; then
        sudo systemctl restart inky-webui.service || true
    fi
fi

LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
cat <<EOF

====================================================================
AdGuard Home installed and running!
====================================================================

1. Open the 30-second Setup Wizard in your browser:
     http://${LAN_IP:-raspberrypi.local}:${AGH_PORT}

2. Important Wizard Settings:
   - Admin Web Interface -> Listen interface: All interfaces, Port: ${AGH_PORT}
     (Keep port ${AGH_PORT} so it never conflicts with rp-inky Web Hub on :8080!)
   - DNS Server -> Listen interface: All interfaces, Port: 53
   - Create an admin username & password.

3. Point your devices or main router's DNS to:
     ${LAN_IP:-192.168.86.30}

   (Your UK-Frame-VPN hotspot continues using Surfshark UK DNS so UK
    streaming apps remain 100% leak-free.)

Useful commands:
  sudo systemctl status AdGuardHome
  sudo journalctl -u AdGuardHome -f
EOF
