#!/bin/bash
# Install the rp-inky Web Dashboard, Phone Photo Uploader, Uptime Watchdog & Wake-on-LAN.
#
# Run as your normal user (not root); invokes sudo only for /etc/sudoers.d and systemd.
set -euo pipefail

if [ "$(id -u)" -eq 0 ]; then
    if [ -z "${SUDO_USER:-}" ]; then
        echo "Run this script as your normal user, not directly as root." >&2
        exit 1
    fi
    RUN_USER="$SUDO_USER"
else
    RUN_USER="$USER"
fi

if [ "$(uname -s)" != "Linux" ]; then
    echo "This installer is for Raspberry Pi OS (Linux)." >&2
    exit 1
fi

RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
WEBUI_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${VENV:-$RUN_HOME/.virtualenvs/pimoroni}"
PORT=""
PIN=""
NO_START=false

usage() {
    cat <<EOF
Usage: $0 [options]

Installs the rp-inky Web Dashboard (Phone Photo Uploader + 1-Tap UK VPN Control
+ ISP/Smart-Home Uptime Watchdog + Wake-on-LAN) as a systemd service.

Options:
  --port PORT      HTTP port (default: 8080)
  --pin PIN        Optional dashboard PIN required in browser
  --venv PATH      Python virtualenv (default: ~/.virtualenvs/pimoroni)
  --no-start       Install and enable service without starting immediately
  -h, --help       Show this help
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --port) PORT="${2:-}"; shift 2 ;;
        --pin) PIN="${2:-}"; shift 2 ;;
        --venv) VENV="${2:-}"; shift 2 ;;
        --no-start) NO_START=true; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
    esac
done

PYTHON="$VENV/bin/python"
if [ ! -x "$PYTHON" ]; then
    echo "Creating virtualenv at $VENV..."
    python3 -m venv --system-site-packages "$VENV"
    "$PYTHON" -m pip install --upgrade pip pillow
fi

# Ensure DejaVu TrueType fonts + mDNS/OUI databases are installed
if [ ! -f /usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf ] || ! command -v avahi-browse >/dev/null 2>&1 || [ ! -f /usr/share/ieee-data/oui.txt ]; then
    sudo apt-get update -qq && sudo apt-get install -y -qq fonts-dejavu-core avahi-utils ieee-data || true
fi

# Ensure Pillow (and optional pillow-heif for iPhone HEIC uploads) are available
if ! "$PYTHON" -c "import PIL" >/dev/null 2>&1; then
    "$PYTHON" -m pip install pillow
fi
"$PYTHON" -m pip install --quiet pillow-heif >/dev/null 2>&1 || true

chmod +x "$WEBUI_DIR/server.py"

# Ensure photoframe state & image directories are owned by RUN_USER (in case photoframe was ever run with sudo)
REPO_DIR="$(dirname "$WEBUI_DIR")"
mkdir -p "$REPO_DIR/photoframe/.state" "$REPO_DIR/photoframe/image" "$WEBUI_DIR/.state"
sudo chown -R "$RUN_USER:$RUN_USER" "$REPO_DIR/photoframe/.state" "$REPO_DIR/photoframe/image" "$WEBUI_DIR/.state"
if [ -f "$REPO_DIR/photoframe/config.local.json" ]; then
    sudo chown "$RUN_USER:$RUN_USER" "$REPO_DIR/photoframe/config.local.json"
fi

if [ -n "$PORT" ] || [ -n "$PIN" ]; then
    "$PYTHON" - "$WEBUI_DIR/config.json" "$PORT" "$PIN" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]).resolve().parent.parent))
from vpn.manager import update_local_config

cfg_path = Path(sys.argv[1])
port_str, pin_str = sys.argv[2], sys.argv[3]
patch = {}
if port_str:
    patch["port"] = int(port_str)
if pin_str:
    patch["pin"] = pin_str
if patch:
    update_local_config(cfg_path, patch)
PY
fi

echo "1. Configuring least-privilege sudoers rule for 1-tap VPN & frame actions..."
SUDOERS_TMP="$(mktemp)"
trap 'rm -f "$SUDOERS_TMP"' EXIT
WG_BIN="$(command -v wg || echo /usr/bin/wg)"
SYSTEMCTL_BIN="$(command -v systemctl || echo /bin/systemctl)"
cat >"$SUDOERS_TMP" <<EOF
# Managed by rp-inky webui installer — allows Web UI user to control rp-vpn & signal/restart photoframe
$RUN_USER ALL=(root) NOPASSWD: /usr/local/bin/rp-vpn *, $WG_BIN show *, $SYSTEMCTL_BIN kill -s USR1 --kill-who=main inky-photoframe.service, $SYSTEMCTL_BIN restart inky-photoframe.service
EOF
chmod 440 "$SUDOERS_TMP"
sudo visudo -cf "$SUDOERS_TMP" >/dev/null
sudo install -m 440 "$SUDOERS_TMP" /etc/sudoers.d/rp-inky-webui

echo "2. Installing systemd unit inky-webui.service..."
UNIT_TMP="$(mktemp)"
sed -e "s|__USER__|$RUN_USER|g" \
    -e "s|__WEBUI_DIR__|$WEBUI_DIR|g" \
    -e "s|__VENV__|$VENV|g" \
    "$WEBUI_DIR/systemd/inky-webui.service.in" >"$UNIT_TMP"
sudo install -m 644 "$UNIT_TMP" /etc/systemd/system/inky-webui.service
rm -f "$UNIT_TMP"

sudo systemctl daemon-reload
sudo systemctl enable inky-webui.service

if ! $NO_START; then
    sudo systemctl restart inky-webui.service
    # Restart photoframe once if active so it picks up the new SIGUSR1 immediate-refresh handler
    if systemctl is-active --quiet inky-photoframe.service; then
        sudo systemctl restart inky-photoframe.service
    fi
fi

ACTIVE_PORT="$("$PYTHON" - "$WEBUI_DIR/config.json" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]).resolve().parent.parent))
from webui.server import load_webui_config
print(load_webui_config(Path(sys.argv[1]))["port"])
PY
)"
LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"

cat <<EOF

Installed inky-webui.service!

Open from your phone or computer on home Wi-Fi:
  http://${LAN_IP:-raspberrypi.local}:$ACTIVE_PORT

Useful commands:
  sudo systemctl status inky-webui
  sudo journalctl -u inky-webui -f
  $PYTHON $WEBUI_DIR/server.py --set-pin 1234    # optional browser PIN
  $PYTHON $WEBUI_DIR/server.py --wake "AA:BB:CC:DD:EE:FF"
EOF
