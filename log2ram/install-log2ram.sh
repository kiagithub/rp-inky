#!/bin/bash
# Install and tune log2ram on Raspberry Pi OS to protect the microSD card from log wear.
#
# - Mounts /var/log in RAM (128 MB tmpfs — ~3% of a 4 GB Pi 4) and syncs to SD hourly + on shutdown
# - Caps systemd-journald at 64 MB so logs never overflow the 128 MB RAM partition
# - Safe and idempotent; requires one reboot to activate the /var/log RAM mount
set -euo pipefail

if [ "$(uname -s)" != "Linux" ]; then
    echo "This installer is for Raspberry Pi OS (Linux)." >&2
    exit 1
fi

LOG2RAM_SIZE="${LOG2RAM_SIZE:-128M}"
JOURNAL_MAX="${JOURNAL_MAX:-64M}"

echo "=== 1. Installing log2ram + rsync ==="
sudo apt-get update -qq
sudo apt-get install -y -qq rsync curl gnupg

if dpkg -s log2ram >/dev/null 2>&1 || [ -x /usr/local/bin/log2ram ]; then
    echo "log2ram is already installed."
else
    CODENAME="$(. /etc/os-release && echo "${VERSION_CODENAME:-bookworm}")"
    # Official Azlux repo supports Debian/RPi OS codenames (bookworm, bullseye, trixie)
    if curl -fsSL https://azlux.fr/repo.gpg.key | sudo gpg --dearmor --yes -o /usr/share/keyrings/azlux-archive-keyring.gpg 2>/dev/null; then
        echo "deb [signed-by=/usr/share/keyrings/azlux-archive-keyring.gpg] http://packages.azlux.fr/debian/ ${CODENAME} main" \
            | sudo tee /etc/apt/sources.list.d/azlux.list >/dev/null
        if sudo apt-get update -qq && sudo apt-get install -y -qq log2ram; then
            echo "Installed log2ram via Azlux APT repository."
        else
            echo "APT repo unavailable for '${CODENAME}'; falling back to official GitHub release..."
            sudo rm -f /etc/apt/sources.list.d/azlux.list
            TMP_DIR="$(mktemp -d)"
            trap 'rm -rf "$TMP_DIR"' EXIT
            curl -fsSL https://github.com/azlux/log2ram/archive/master.tar.gz -o "$TMP_DIR/log2ram.tar.gz"
            tar -xzf "$TMP_DIR/log2ram.tar.gz" -C "$TMP_DIR"
            (cd "$TMP_DIR/log2ram-master" && sudo ./install.sh)
        fi
    else
        echo "Falling back to official GitHub release..."
        TMP_DIR="$(mktemp -d)"
        trap 'rm -rf "$TMP_DIR"' EXIT
        curl -fsSL https://github.com/azlux/log2ram/archive/master.tar.gz -o "$TMP_DIR/log2ram.tar.gz"
        tar -xzf "$TMP_DIR/log2ram.tar.gz" -C "$TMP_DIR"
        (cd "$TMP_DIR/log2ram-master" && sudo ./install.sh)
    fi
fi

echo "=== 2. Tuning /etc/log2ram.conf (SIZE=${LOG2RAM_SIZE}, USE_RSYNC=true) ==="
if [ -f /etc/log2ram.conf ]; then
    sudo sed -i -E "s|^SIZE=.*|SIZE=${LOG2RAM_SIZE}|" /etc/log2ram.conf
    sudo sed -i -E "s|^USE_RSYNC=.*|USE_RSYNC=true|" /etc/log2ram.conf
    sudo sed -i -E "s|^MAIL=.*|MAIL=false|" /etc/log2ram.conf
    sudo sed -i -E "s|^ZL2R=.*|ZL2R=false|" /etc/log2ram.conf
fi

echo "=== 3. Capping systemd-journald at ${JOURNAL_MAX} so /var/log never fills ==="
sudo mkdir -p /etc/systemd/journald.conf.d
sudo tee /etc/systemd/journald.conf.d/log2ram.conf >/dev/null <<EOF
[Journal]
SystemMaxUse=${JOURNAL_MAX}
RuntimeMaxUse=${JOURNAL_MAX}
EOF
sudo systemctl restart systemd-journald || true

sudo systemctl enable log2ram.service >/dev/null 2>&1 || true

if systemctl is-active --quiet inky-webui.service 2>/dev/null; then
    sudo systemctl restart inky-webui.service || true
fi

cat <<EOF

====================================================================
log2ram installed and tuned (${LOG2RAM_SIZE} RAM buffer)!
====================================================================

One reboot is required to mount /var/log in RAM:
  sudo reboot

After rebooting, verify it is active:
  df -h /var/log
  sudo systemctl status log2ram

You will also see a live "Log2RAM" badge in the Pi System Health Bar
at the top of http://192.168.86.30:8080!
EOF
