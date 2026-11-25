#!/bin/bash
# Install the rp-inky Always-On Surfshark VPN, 5 GHz Wi-Fi Hotspot and UK Watchdog.
#
# Run as your normal user (not root); the script invokes sudo where required.
set -euo pipefail

if [ "$(id -u)" -eq 0 ]; then
    if [ -z "${SUDO_USER:-}" ]; then
        echo "Run this script as your normal user, not directly as root." >&2
        exit 1
    fi
fi

if [ "$(uname -s)" != "Linux" ]; then
    echo "This installer is for Raspberry Pi OS (Linux)." >&2
    exit 1
fi

VPN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANAGER="$VPN_DIR/manager.py"
CLI_DEST="/usr/local/bin/rp-vpn"
SYSTEMD_DIR="/etc/systemd/system"

SSID=""
PASSPHRASE=""
COUNTRY=""
BAND=""
CHANNEL=""
AP_IFACE=""
WAN_IFACE=""
IMPORT_PATH=""
NO_HOTSPOT=false
NO_START=false

usage() {
    cat <<EOF
Usage: $0 [options]

Installs WireGuard, OpenVPN, hostapd, dnsmasq, nftables, Raspberry Pi 4 500+ Mbps
kernel sysctl tuning, the /usr/local/bin/rp-vpn CLI, and systemd boot services.

Options:
  --ssid NAME          Wi-Fi Hotspot SSID (default: RP-UK-VPN)
  --passphrase PASS    Wi-Fi WPA2 passphrase (8-63 chars)
  --country CODE       2-letter Wi-Fi regulatory country code (default: US, or GB)
  --band 5g|2.4g       Wi-Fi band (default: 5g for 802.11ac VHT80 speed)
  --channel NUM        Wi-Fi channel (default: 36 for 5g, 6 for 2.4g)
  --ap-iface IFACE     Wi-Fi AP interface (default: wlan0, or wlan1 for USB3 Wi-Fi 6)
  --wan-iface IFACE    Wired uplink interface (default: eth0)
  --import PATH        Import a Surfshark .conf/.ovpn file or directory right away
  --no-hotspot         LAN Gateway mode only (do not run a Wi-Fi AP on wlan0)
  --no-start           Install and enable units, but do not start them yet
  -h, --help           Show this help
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --ssid) SSID="${2:-}"; shift 2 ;;
        --passphrase) PASSPHRASE="${2:-}"; shift 2 ;;
        --country) COUNTRY="${2:-}"; shift 2 ;;
        --band) BAND="${2:-}"; shift 2 ;;
        --channel) CHANNEL="${2:-}"; shift 2 ;;
        --ap-iface) AP_IFACE="${2:-}"; shift 2 ;;
        --wan-iface) WAN_IFACE="${2:-}"; shift 2 ;;
        --import) IMPORT_PATH="${2:-}"; shift 2 ;;
        --no-hotspot) NO_HOTSPOT=true; shift ;;
        --no-start) NO_START=true; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
    esac
done

chmod +x "$MANAGER"

echo "1. Installing system packages (wireguard-tools, openvpn, hostapd, dnsmasq, nftables, iw, curl)..."
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
    wireguard-tools \
    openvpn \
    hostapd \
    dnsmasq \
    nftables \
    iw \
    rfkill \
    iproute2 \
    curl \
    ca-certificates

# Disable stock distro hostapd/dnsmasq units so they never clash on boot with rp-vpn-hotspot.service
sudo systemctl stop hostapd.service dnsmasq.service >/dev/null 2>&1 || true
sudo systemctl disable hostapd.service dnsmasq.service >/dev/null 2>&1 || true
sudo systemctl mask hostapd.service >/dev/null 2>&1 || true

# Cleanly migrate & tear down legacy vpn-hotspot (wg-quick@wg0 / hotspot-firewall) if present
if sudo test -f /etc/hostapd/hostapd.conf; then
    [ -z "$SSID" ] && SSID="$(sudo sed -n 's/^ssid=//p' /etc/hostapd/hostapd.conf | head -n1)"
    [ -z "$PASSPHRASE" ] && PASSPHRASE="$(sudo sed -n 's/^wpa_passphrase=//p' /etc/hostapd/hostapd.conf | head -n1)"
    [ -z "$COUNTRY" ] && COUNTRY="$(sudo sed -n 's/^country_code=//p' /etc/hostapd/hostapd.conf | head -n1)"
    sudo mv -f /etc/hostapd/hostapd.conf /etc/hostapd/hostapd.conf.migrated
fi

LEGACY_WG_TMP=""
if sudo test -f /etc/wireguard/wg0.conf; then
    echo "Found legacy /etc/wireguard/wg0.conf; staging it for migration into vpn/profiles/..."
    LEGACY_WG_TMP="$(mktemp --suffix=.conf)"
    sudo cat /etc/wireguard/wg0.conf >"$LEGACY_WG_TMP"
    chmod 600 "$LEGACY_WG_TMP"
    sudo mv -f /etc/wireguard/wg0.conf /etc/wireguard/wg0.conf.migrated
fi

# Unconditionally stop & disable legacy units (note: wg-quick@wg0 is a template instance!)
sudo systemctl stop wg-quick@wg0.service hotspot-ip.service hotspot-firewall.service >/dev/null 2>&1 || true
sudo systemctl disable wg-quick@wg0.service hotspot-ip.service hotspot-firewall.service >/dev/null 2>&1 || true
sudo rm -f /etc/systemd/system/hotspot-ip.service /etc/systemd/system/hotspot-firewall.service \
    /etc/systemd/system/multi-user.target.wants/wg-quick@wg0.service
sudo wg-quick down wg0 >/dev/null 2>&1 || true
sudo ip link del dev wg0 >/dev/null 2>&1 || true
while sudo ip -4 rule del lookup 51820 >/dev/null 2>&1; do :; done
sudo ip -4 route flush table 51820 >/dev/null 2>&1 || true

# Always clear any legacy iptables FORWARD DROP / NAT rules left by old hotspot-firewall
if command -v iptables >/dev/null 2>&1; then
    sudo iptables -P FORWARD ACCEPT >/dev/null 2>&1 || true
    sudo iptables -F FORWARD >/dev/null 2>&1 || true
    sudo iptables -t nat -F >/dev/null 2>&1 || true
fi
sudo rm -f /etc/dnsmasq.d/hotspot.conf /etc/sysctl.d/99-hotspot.conf /etc/NetworkManager/conf.d/hotspot-wlan0.conf /etc/hotspot/rules.v4

# Repair /etc/resolv.conf if legacy openresolv left it pointing only to VPN-internal DNS
if command -v resolvconf >/dev/null 2>&1; then
    sudo resolvconf -d wg0 -f >/dev/null 2>&1 || true
fi
if sudo grep -Eq '162\.252\.172\.57|149\.154\.159\.92|^# Generated by resolvconf' /etc/resolv.conf 2>/dev/null; then
    echo "Repairing host /etc/resolv.conf (removing stale VPN-only resolvconf DNS)..."
    sudo rm -f /etc/resolv.conf
    if systemctl is-active --quiet NetworkManager; then
        sudo systemctl reload-or-restart NetworkManager >/dev/null 2>&1 || true
        sleep 2
    fi
    if [ ! -s /etc/resolv.conf ] || ! grep -q '^nameserver' /etc/resolv.conf 2>/dev/null || grep -Eq '162\.252\.172\.57' /etc/resolv.conf 2>/dev/null; then
        printf 'nameserver 1.1.1.1\nnameserver 8.8.8.8\n' | sudo tee /etc/resolv.conf >/dev/null
    fi
fi

echo "2. Installing /usr/local/bin/rp-vpn CLI wrapper..."
WRAPPER_TMP="$(mktemp)"
trap 'rm -f "$WRAPPER_TMP" ${LEGACY_WG_TMP:+"$LEGACY_WG_TMP"}' EXIT
cat >"$WRAPPER_TMP" <<EOF
#!/bin/sh
exec /usr/bin/python3 "$MANAGER" --config "$VPN_DIR/config.json" "\$@"
EOF
sudo install -m 755 "$WRAPPER_TMP" "$CLI_DEST"

echo "3. Applying Raspberry Pi 4 500+ Mbps kernel & BBR tuning..."
sudo "$CLI_DEST" write-sysctl

if [ -n "$WAN_IFACE" ] || $NO_HOTSPOT; then
    sudo /usr/bin/python3 - "$VPN_DIR/config.json" "$WAN_IFACE" "$NO_HOTSPOT" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]).resolve().parent.parent))
from vpn.manager import update_local_config

config_path = Path(sys.argv[1])
wan_iface = sys.argv[2]
no_hotspot = sys.argv[3].lower() == "true"
patch = {}
if wan_iface:
    patch["wan_interface"] = wan_iface
if no_hotspot:
    patch["hotspot"] = {"enabled": False}
if patch:
    update_local_config(config_path, patch)
PY
fi

HOTSPOT_ARGS=()
[ -n "$SSID" ] && HOTSPOT_ARGS+=(--ssid "$SSID")
[ -n "$PASSPHRASE" ] && HOTSPOT_ARGS+=(--passphrase "$PASSPHRASE")
[ -n "$COUNTRY" ] && HOTSPOT_ARGS+=(--country "$COUNTRY")
[ -n "$BAND" ] && HOTSPOT_ARGS+=(--band "$BAND")
[ -n "$CHANNEL" ] && HOTSPOT_ARGS+=(--channel "$CHANNEL")
[ -n "$AP_IFACE" ] && HOTSPOT_ARGS+=(--interface "$AP_IFACE")
if [ ${#HOTSPOT_ARGS[@]} -gt 0 ]; then
    sudo "$CLI_DEST" configure-hotspot "${HOTSPOT_ARGS[@]}"
fi

if [ -n "$LEGACY_WG_TMP" ] && [ -s "$LEGACY_WG_TMP" ]; then
    LEGACY_NAME="$(/usr/bin/python3 - "$LEGACY_WG_TMP" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent if '__file__' in globals() else '.'))
text = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
endpoint = ""
for line in text.splitlines():
    s = line.split("#", 1)[0].strip()
    if s.lower().startswith("endpoint") and "=" in s:
        endpoint = s.split("=", 1)[1].strip().split(":")[0]
        break
print(endpoint or "migrated-wg0")
PY
)"
    sudo "$CLI_DEST" import "$LEGACY_WG_TMP" --name "$LEGACY_NAME" --force || true
fi

if [ -n "$IMPORT_PATH" ]; then
    sudo "$CLI_DEST" import "$IMPORT_PATH" --force
fi

echo "4. Installing systemd services and 45s UK failover watchdog timer..."
for unit_in in rp-vpn-hotspot.service.in rp-vpn.service.in rp-vpn-watchdog.service.in; do
    unit_name="${unit_in%.in}"
    unit_tmp="$(mktemp)"
    sed "s|__VPN_DIR__|$VPN_DIR|g" "$VPN_DIR/systemd/$unit_in" >"$unit_tmp"
    sudo install -m 644 "$unit_tmp" "$SYSTEMD_DIR/$unit_name"
    rm -f "$unit_tmp"
done
sudo install -m 644 "$VPN_DIR/systemd/rp-vpn-watchdog.timer" "$SYSTEMD_DIR/rp-vpn-watchdog.timer"

sudo systemctl daemon-reload
sudo systemctl enable rp-vpn-hotspot.service rp-vpn.service rp-vpn-watchdog.timer

HAS_PASS="$(sudo /usr/bin/python3 - "$VPN_DIR/config.json" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]).resolve().parent.parent))
from vpn.manager import load_vpn_config
cfg = load_vpn_config(Path(sys.argv[1]))
ok = (not cfg["hotspot"]["enabled"]) or (len(cfg["hotspot"]["passphrase"]) >= 8)
print("yes" if ok else "no")
PY
)"

PROFILE_COUNT="$(sudo /usr/bin/python3 - "$VPN_DIR/config.json" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]).resolve().parent.parent))
from vpn.manager import discover_profiles, load_vpn_config
cfg = load_vpn_config(Path(sys.argv[1]))
print(len(discover_profiles(cfg["_profiles_dir"])))
PY
)"

echo
if $NO_START; then
    echo "Installed and enabled (--no-start)."
else
    if [ "$HAS_PASS" = "yes" ]; then
        sudo systemctl restart rp-vpn-hotspot.service
        echo "Started rp-vpn-hotspot.service."
    else
        echo "Note: Hotspot passphrase not set yet. Set it with:"
        echo "  sudo rp-vpn configure-hotspot --ssid \"RP-UK-VPN\" --passphrase \"your-strong-password\" --country US"
        echo "  sudo systemctl restart rp-vpn-hotspot.service"
    fi

    if [ "$PROFILE_COUNT" -gt 0 ]; then
        sudo systemctl restart rp-vpn.service
        sudo systemctl start rp-vpn-watchdog.timer
        echo "Started rp-vpn.service and rp-vpn-watchdog.timer."
    else
        echo "Note: No Surfshark profiles imported yet. Import your .conf / .ovpn files with:"
        echo "  sudo rp-vpn import ~/surfshark-configs/"
        echo "  sudo rp-vpn up"
        echo "  sudo systemctl start rp-vpn-watchdog.timer"
    fi
fi

cat <<EOF

Quick commands:
  rp-vpn list                        # see all imported Surfshark configs
  sudo rp-vpn use uk-lon             # switch config immediately
  sudo rp-vpn next                   # jump to next healthy UK config
  sudo rp-vpn status                 # check exit IP, UK city, Wi-Fi clients & kill-switch
  sudo rp-vpn benchmark              # test WAN speed vs Surfshark tunnel speed
  sudo journalctl -u rp-vpn -f       # follow VPN logs
EOF
