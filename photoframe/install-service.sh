#!/bin/bash
# Install the Inky photo frame as a systemd service.
#
# Run as your normal user (not root); the script calls sudo only for the
# systemd unit. Assumes ./install.sh in the repo has already set up the
# project virtualenv with the inky package.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$APP_DIR/config.json"
UNIT_SOURCE="$APP_DIR/inky-photoframe.service.in"
UNIT_NAME="inky-photoframe.service"
UNIT_DEST="/etc/systemd/system/$UNIT_NAME"

if [ "$(id -u)" -eq 0 ]; then
    RUN_USER="${SUDO_USER:-}"
    if [ -z "$RUN_USER" ]; then
        echo "Run this script as your normal user, not as root." >&2
        exit 1
    fi
else
    RUN_USER="$USER"
fi
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"

CITY=""
COUNTRY=""
STATE=""
UNITS=""
INTERVAL=""
VENV="${VENV:-$RUN_HOME/.virtualenvs/pimoroni}"
NO_START=false

usage() {
    cat <<EOF
Usage: $0 [options]

  --city NAME       Weather city (default: keep the current config.json)
  --country CODE    ISO country code, e.g. US (cleared if --city is used alone)
  --state NAME      State/region for ambiguous cities, e.g. Virginia
  --units UNIT      metric or imperial (leave unchanged if omitted)
  --interval MIN    Photo rotation interval in minutes (leave unchanged if omitted)
  --venv PATH       Python virtualenv (default: ~/.virtualenvs/pimoroni)
  --no-start        Install and enable, but do not start the service
  -h, --help        Show this help
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --city) CITY="${2:-}"; shift 2 ;;
        --country) COUNTRY="${2:-}"; shift 2 ;;
        --state) STATE="${2:-}"; shift 2 ;;
        --units) UNITS="${2:-}"; shift 2 ;;
        --interval) INTERVAL="${2:-}"; shift 2 ;;
        --venv) VENV="${2:-}"; shift 2 ;;
        --no-start) NO_START=true; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
    esac
done

if [ -n "$UNITS" ]; then
    case "$UNITS" in
        metric|imperial) ;;
        *) echo "--units must be metric or imperial" >&2; exit 1 ;;
    esac
fi

if [ -n "$INTERVAL" ]; then
    if ! [[ "$INTERVAL" =~ ^[0-9]+([.][0-9]+)?$ ]] || [ "$(awk "BEGIN {print ($INTERVAL > 0)}")" != "1" ]; then
        echo "--interval must be a positive number of minutes" >&2
        exit 1
    fi
fi

PYTHON="$VENV/bin/python"
if [ ! -x "$PYTHON" ]; then
    echo "No virtualenv found at $VENV." >&2
    echo "Run ./install.sh from the repository root first." >&2
    exit 1
fi
if ! "$PYTHON" -c "import inky, PIL" >/dev/null 2>&1; then
    echo "The inky and Pillow packages are not importable from $VENV." >&2
    echo "Run ./install.sh from the repository root first." >&2
    exit 1
fi

if [ ! -f "$CONFIG" ]; then
    echo "Missing $CONFIG." >&2
    echo "Restore it from git: git -C \"$(dirname "$APP_DIR")\" checkout -- photoframe/config.json" >&2
    exit 1
fi

if [ -n "$CITY$COUNTRY$STATE$UNITS$INTERVAL" ]; then
    "$PYTHON" - "$CONFIG" "$CITY" "$COUNTRY" "$STATE" "$UNITS" "$INTERVAL" <<'PY'
import json
import os
import sys

path, city, country, state, units, interval = sys.argv[1:7]
with open(path, encoding="utf-8") as handle:
    config = json.load(handle)
weather = config.setdefault("weather", {})
if city:
    weather["location"] = city
    weather["country_code"] = country.upper()
    weather["admin1"] = state
else:
    if country:
        weather["country_code"] = country.upper()
    if state:
        weather["admin1"] = state
if units:
    weather["units"] = units
if interval:
    value = float(interval)
    config["refresh_minutes"] = int(value) if value.is_integer() else value
temporary = path + ".tmp"
with open(temporary, "w", encoding="utf-8") as handle:
    json.dump(config, handle, indent=2)
    handle.write("\n")
os.replace(temporary, path)
PY
    if [ -n "$CITY" ]; then
        echo "Updated $CONFIG with weather location '$CITY'."
    else
        echo "Updated $CONFIG."
    fi
else
    echo "Using existing $CONFIG. Edit weather.location to change the city."
fi

CITY_CONFIGURED="$("$PYTHON" - "$CONFIG" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    config = json.load(handle)
weather = config.get("weather", {})
location = weather.get("location") or ""
if not location and weather.get("latitude") is not None and weather.get("longitude") is not None:
    location = "coordinates"
print(location)
PY
)"

GROUPS=""
for group in spi gpio i2c; do
    if getent group "$group" >/dev/null 2>&1; then
        GROUPS="${GROUPS:+$GROUPS }$group"
        if ! id -nG "$RUN_USER" | tr ' ' '\n' | grep -qx "$group"; then
            echo "Warning: $RUN_USER is not in the '$group' group. Add it with:" >&2
            echo "  sudo usermod -aG $group $RUN_USER" >&2
        fi
    fi
done

TEMP_UNIT="$(mktemp)"
trap 'rm -f "$TEMP_UNIT"' EXIT
"$PYTHON" - "$UNIT_SOURCE" "$TEMP_UNIT" "$RUN_USER" "$APP_DIR" "$VENV" "$GROUPS" <<'PY'
import sys

source, destination, user, app_dir, venv, groups = sys.argv[1:7]
with open(source, encoding="utf-8") as handle:
    text = handle.read()
text = text.replace("__USER__", user).replace("__APP_DIR__", app_dir).replace("__VENV__", venv)
if groups:
    text = text.replace("__GROUPS__", groups)
else:
    text = "\n".join(line for line in text.splitlines() if "SupplementaryGroups=__GROUPS__" not in line) + "\n"
with open(destination, "w", encoding="utf-8") as handle:
    handle.write(text)
PY

sudo install -m 644 "$TEMP_UNIT" "$UNIT_DEST"
sudo systemctl daemon-reload
sudo systemctl enable "$UNIT_NAME"

if $NO_START; then
    echo "Installed and enabled, but not started (--no-start)."
elif [ -z "$CITY_CONFIGURED" ]; then
    echo "Installed and enabled, but not started: no weather location is configured."
    echo "Edit $CONFIG, then run: sudo systemctl restart $UNIT_NAME"
else
    sudo systemctl restart "$UNIT_NAME"
    echo "Started $UNIT_NAME."
fi

echo
echo "Status:  sudo systemctl status $UNIT_NAME"
echo "Logs:    sudo journalctl -u $UNIT_NAME -f"
echo "Stop:    sudo systemctl stop $UNIT_NAME"
echo "Disable: sudo systemctl disable --now $UNIT_NAME"
