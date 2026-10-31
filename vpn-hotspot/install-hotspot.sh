#!/bin/bash
# Install a WireGuard VPN WiFi hotspot alongside the Inky photo frame.
#
# The Pi keeps its Ethernet uplink to the home router and broadcasts a new
# WiFi network. Everything a device (e.g. a TV) sends over that WiFi is
# forwarded through the WireGuard tunnel, so it appears to be wherever the
# VPN server is. If the tunnel is down, hotspot clients get no internet
# rather than a plain home connection (kill switch).
#
# Usage:  sudo ./install-hotspot.sh /path/to/surfshark-uk.conf
#
# Safe to re-run: everything is regenerated, so switching VPN servers is
# just a matter of downloading another .conf and running this again.
set -euo pipefail

# ----------------------------- Settings ---------------------------------
# Edit these before running.
SSID="UK-Frame-VPN"          # WiFi name the TV will see
PASSPHRASE="CHANGE_ME"       # WiFi password, 8-63 characters
COUNTRY_CODE="GB"            # 2-letter WiFi regulatory code of where the
                             # Pi physically is (NOT the VPN country)
HW_MODE="g"                  # "g" = 2.4 GHz (most compatible), "a" = 5 GHz
CHANNEL="6"                  # 1/6/11 for 2.4 GHz; 36+ for 5 GHz
AP_IP="10.8.0.1"             # The Pi's address on the hotspot network
AP_DHCP_RANGE="10.8.0.2,10.8.0.32,255.255.255.0,24h"
# -------------------------------------------------------------------------

log() { echo "[hotspot] $*"; }
die() { echo "[hotspot] ERROR: $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run with sudo: sudo $0 <surfshark.conf>"
WG_SOURCE="${1:-}"
[[ -n "$WG_SOURCE" ]] || die "usage: sudo $0 <surfshark.conf>"
[[ -f "$WG_SOURCE" ]] || die "$WG_SOURCE not found"
grep -q '^[[:space:]]*\[Interface\]' "$WG_SOURCE" || die "$WG_SOURCE has no [Interface] section"
grep -q '^[[:space:]]*\[Peer\]' "$WG_SOURCE" || die "$WG_SOURCE has no [Peer] section"
grep -q '^[[:space:]]*Endpoint' "$WG_SOURCE" || die "$WG_SOURCE has no Endpoint; regenerate it from the Surfshark dashboard"

[[ ${#PASSPHRASE} -ge 8 && ${#PASSPHRASE} -le 63 ]] || die "set PASSPHRASE (8-63 characters) at the top of this script"
[[ "$PASSPHRASE" != "CHANGE_ME" ]] || die "set PASSPHRASE (8-63 characters) at the top of this script"
[[ "$COUNTRY_CODE" =~ ^[A-Z]{2}$ ]] || die "COUNTRY_CODE must be two letters, e.g. GB, US, DE"

if ! grep -qi 'uk' <(grep '^[[:space:]]*Endpoint' "$WG_SOURCE"); then
    log "warning: the Endpoint in $WG_SOURCE does not look like a UK server:"
    grep '^[[:space:]]*Endpoint' "$WG_SOURCE" || true
fi

# ----------------------------- Packages ---------------------------------
log "installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq wireguard hostapd dnsmasq openresolv >/dev/null

IPTABLES_RESTORE="$(command -v iptables-restore || echo /usr/sbin/iptables-restore)"
IP="$(command -v ip || echo /usr/sbin/ip)"

# --------------------------- WireGuard tunnel ----------------------------
log "installing the WireGuard config as /etc/wireguard/wg0.conf"
install -d -m 700 /etc/wireguard
install -m 600 "$WG_SOURCE" /etc/wireguard/wg0.conf
if ! grep -q '^[[:space:]]*PersistentKeepalive' /etc/wireguard/wg0.conf; then
    sed -i '/^[[:space:]]*\[Peer\]/a PersistentKeepalive = 25' /etc/wireguard/wg0.conf
    log "added PersistentKeepalive = 25"
fi

# DNS servers from the Surfshark config, so hotspot DNS rides the tunnel too.
DNS_SERVERS=$(sed -n 's/^[[:space:]]*DNS[[:space:]]*=[[:space:]]*//p' /etc/wireguard/wg0.conf | tr ',' ' ' | xargs)
if [[ -z "$DNS_SERVERS" ]]; then
    log "warning: no DNS line in the config; hotspot clients will use 1.1.1.1 (may leak outside the tunnel)"
    DNS_SERVERS="1.1.1.1"
fi
DNS_SERVER_LINES=$(printf 'server=%s\n' $DNS_SERVERS)

# --------------------------- Hotspot IP address -------------------------
log "creating hotspot-ip.service (static $AP_IP on wlan0)"
cat >/etc/systemd/system/hotspot-ip.service <<EOF
[Unit]
Description=Static IP for the VPN hotspot WiFi interface
Before=hostapd.service dnsmasq.service
After=network.target

[Service]
Type=oneshot
ExecStart=$IP addr replace $AP_IP/24 dev wlan0
ExecStart=$IP link set wlan0 up
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

# --------------------------- Access point --------------------------------
log "writing /etc/hostapd/hostapd.conf"
install -d -m 755 /etc/hostapd
cat >/etc/hostapd/hostapd.conf <<EOF
interface=wlan0
driver=nl80211
ssid=$SSID
country_code=$COUNTRY_CODE
ieee80211d=1
hw_mode=$HW_MODE
channel=$CHANNEL
ieee80211n=1
wmm_enabled=1
auth_algs=1
wpa=2
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
wpa_passphrase=$PASSPHRASE
EOF
chmod 600 /etc/hostapd/hostapd.conf

if [[ -f /etc/default/hostapd ]]; then
    sed -i 's|^#\?DAEMON_CONF=.*|DAEMON_CONF="/etc/hostapd/hostapd.conf"|' /etc/default/hostapd
    grep -q '^DAEMON_CONF=' /etc/default/hostapd || echo 'DAEMON_CONF="/etc/hostapd/hostapd.conf"' >>/etc/default/hostapd
fi

# --------------------------- DHCP + DNS ----------------------------------
log "writing /etc/dnsmasq.d/hotspot.conf"
install -d -m 755 /etc/dnsmasq.d
cat >/etc/dnsmasq.d/hotspot.conf <<EOF
# Serves the VPN hotspot only.
interface=wlan0
bind-dynamic
domain-needed
bogus-priv
dhcp-authoritative
dhcp-range=$AP_DHCP_RANGE
dhcp-option=option:router,$AP_IP
# Upstream DNS comes from the VPN provider, so queries ride the tunnel.
no-resolv
$DNS_SERVER_LINES
EOF

# --------------------------- Forwarding + kill switch -------------------
log "enabling IPv4 forwarding"
install -d -m 755 /etc/sysctl.d
cat >/etc/sysctl.d/99-hotspot.conf <<EOF
net.ipv4.ip_forward=1
EOF
sysctl -p /etc/sysctl.d/99-hotspot.conf >/dev/null

log "writing firewall rules (hotspot clients may only reach the tunnel)"
install -d -m 755 /etc/hotspot
cat >/etc/hotspot/rules.v4 <<EOF
*filter
:INPUT ACCEPT [0:0]
:FORWARD DROP [0:0]
:OUTPUT ACCEPT [0:0]
# Hotspot clients may only send traffic out through the WireGuard tunnel...
-A FORWARD -i wlan0 -o wg0 -j ACCEPT
# ...and only receive replies to their own connections.
-A FORWARD -i wg0 -o wlan0 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
COMMIT
*nat
:PREROUTING ACCEPT [0:0]
:INPUT ACCEPT [0:0]
:OUTPUT ACCEPT [0:0]
:POSTROUTING ACCEPT [0:0]
-A POSTROUTING -s ${AP_IP%.*}.0/24 -o wg0 -j MASQUERADE
COMMIT
EOF

log "creating hotspot-firewall.service"
cat >/etc/systemd/system/hotspot-firewall.service <<EOF
[Unit]
Description=Firewall rules for the VPN hotspot
After=wg-quick@wg0.service
Before=dnsmasq.service

[Service]
Type=oneshot
ExecStart=$IPTABLES_RESTORE /etc/hotspot/rules.v4
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

# --------------------------- NetworkManager ------------------------------
if [[ -d /etc/NetworkManager/conf.d ]]; then
    log "telling NetworkManager to leave wlan0 alone"
    cat >/etc/NetworkManager/conf.d/hotspot-wlan0.conf <<'EOF'
[keyfile]
unmanaged-devices=interface-name:wlan0
EOF
    command -v nmcli >/dev/null && nmcli device set wlan0 managed no 2>/dev/null || true
fi

# --------------------------- Enable + start ------------------------------
log "enabling services"
rfkill unblock wifi 2>/dev/null || true
systemctl unmask hostapd >/dev/null 2>&1 || true
systemctl daemon-reload
systemctl enable wg-quick@wg0 hotspot-ip hotspot-firewall hostapd dnsmasq >/dev/null
systemctl restart wg-quick@wg0
systemctl restart hotspot-ip
systemctl restart hotspot-firewall
systemctl restart hostapd
systemctl restart dnsmasq

# --------------------------- Summary -------------------------------------
cat <<EOF

[hotspot] installed. WiFi hotspot: "$SSID" (password: $PASSPHRASE)
[hotspot] hotspot network: ${AP_IP%.*}.0/24, DNS via $DNS_SERVERS (through the tunnel)

Next:
  1. Wait a few seconds, then check the tunnel:      sudo wg show
     (look for "latest handshake"; it can take a moment)
  2. Connect the TV to the WiFi "$SSID" and set its
     network settings to automatic (DHCP).
  3. On the TV, confirm the location, e.g. what is my IP.

To switch VPN server: download another .conf from the Surfshark
dashboard, then re-run:  sudo $0 <new.conf>
EOF