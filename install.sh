#!/bin/bash
# Install rp-inky and its Python dependencies on Raspberry Pi OS.
#
# Run as your normal user (not root). This creates a virtualenv, installs the
# local driver package, and enables I2C/SPI for the Inky Impression panel.
set -euo pipefail

if [ "$(id -u)" -eq 0 ]; then
    echo "Run this script as your normal user, not as root." >&2
    exit 1
fi

if [ "$(uname -s)" != "Linux" ]; then
    echo "This installer is for Raspberry Pi OS." >&2
    echo "For local macOS previews, follow the README's macOS test instructions." >&2
    exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${VENV:-$HOME/.virtualenvs/pimoroni}"
PYTHON="$VENV/bin/python"

echo "Repository: $REPO_DIR"
echo "Virtualenv:  $VENV"

if [ ! -x "$PYTHON" ]; then
    echo "Creating virtualenv..."
    python3 -m venv --system-site-packages "$VENV"
fi

"$PYTHON" -m pip install --upgrade pip
"$PYTHON" -m pip install "$REPO_DIR"

echo
if [ ! -f /usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf ]; then
    echo "Installing DejaVu fonts..."
    sudo apt-get update
    sudo apt-get install -y fonts-dejavu-core || echo "Warning: could not install fonts-dejavu-core; footer text may use a tiny fallback font."
fi

echo "Enabling I2C and SPI..."
if command -v raspi-config >/dev/null 2>&1; then
    sudo raspi-config nonint do_i2c 0 || echo "Warning: could not enable I2C; enable it with raspi-config."
    sudo raspi-config nonint do_spi 0 || echo "Warning: could not enable SPI; enable it with raspi-config."
else
    echo "Warning: raspi-config not found; enable I2C and SPI manually."
fi

CONFIG="/boot/firmware/config.txt"
if [ -f "$CONFIG" ]; then
    for overlay in dtoverlay=i2c1 dtoverlay=i2c1-pi5 dtoverlay=spi0-0cs; do
        if ! grep -qx "$overlay" "$CONFIG"; then
            echo "$overlay" | sudo tee -a "$CONFIG" >/dev/null
            echo "Added $overlay to $CONFIG"
        fi
    done
else
    echo "Warning: $CONFIG not found; add dtoverlay=i2c1, dtoverlay=i2c1-pi5 and dtoverlay=spi0-0cs manually."
fi

cat <<EOF

Installed.

Next steps:
  1. source $VENV/bin/activate
  2. python $REPO_DIR/photoframe/prepare_images.py ~/Pictures
  3. The default weather city is McLean, Virginia, US.
     Edit $REPO_DIR/photoframe/config.json to change weather.location,
     or pass --city/--country/--state to install-service.sh.
  4. $REPO_DIR/photoframe/install-service.sh
     e.g. $REPO_DIR/photoframe/install-service.sh --city "Your City" --country US --state "Your State"
  5. sudo reboot

Preview without the panel:
  python $REPO_DIR/photoframe/photoframe.py --preview /tmp/frame.png --offline
EOF
