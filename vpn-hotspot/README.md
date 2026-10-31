# VPN WiFi hotspot

Turn the photo-frame Pi into a **VPN travel gateway**: it keeps showing
photos, and in addition broadcasts its own WiFi network that routes
everything through a WireGuard tunnel to a Surfshark server (e.g. in the
UK). A TV — or any device — that joins that WiFi appears to be in the
VPN server's country.

```
                 ┌─────────────────────── Raspberry Pi ───────────────────────┐
  TV ──WiFi──▶  │ wlan0  hotspot "UK-Frame-VPN" (10.8.0.1, DHCP + DNS)         │
                 │   │                                                     │
                 │   └──▶ FORWARD (allowed only into wg0, NAT)  kill switch    │
                 │ wg0   WireGuard tunnel to Surfshark UK                     │
  router ──────▶ │ eth0  normal home uplink (frame, SSH, tunnel packets)       │
                 └───────────────────────────────────────────────────────────┘
```

- The **frame keeps working unchanged**; its own traffic (photos, weather)
  also goes through the tunnel.
- **Kill switch**: hotspot clients can only reach the internet through
  `wg0`. If the tunnel drops, the TV loses internet instead of silently
  falling back to your home IP.
- **Isolation**: hotspot clients cannot reach your other home devices.
- SSH to the Pi over Ethernet from your home network keeps working.

## Requirements

- The photo frame Pi (Pi 4/5) **connected to the router by Ethernet** —
  the built-in WiFi is used for the hotspot, so it cannot also be the
  uplink.
- Raspberry Pi OS Lite **Bookworm or later** (same as the frame).
- A Surfshark subscription.

## 1. Get a Surfshark UK WireGuard config

1. Log in at [surfshark.com](https://surfshark.com) → **VPN → Manual setup**.
2. Choose **WireGuard** and click **"I don't have a key pair"**; name it
   (e.g. `pi-frame`) and **generate a new key pair**.
3. Click **Choose a location**, pick a **UK** server, and hit the download
   icon to save the `uk-*.conf` file (e.g. `uk-lon.conf`).
4. Copy the file to the Pi, e.g. from your computer:

```bash
scp ~/Downloads/uk-lon.conf pi@<pi-address>:~/rp-inky/
```

The generated key pair is used only by this config file — if you lose the
file, generate a new key pair in the dashboard.

## 2. Install

Edit the settings at the top of the script (WiFi name, password, and
`COUNTRY_CODE` — the two-letter code of where the Pi **physically** is,
not the VPN country), then:

```bash
cd ~/rp-inky/vpn-hotspot
sudo ./install-hotspot.sh ~/rp-inky/uk-lon.conf
```

Then connect the TV to the new WiFi network and set its network settings
to automatic (DHCP). On the TV, check "what is my IP" to confirm it shows
a UK address.

## Switch VPN server or country

Download another `.conf` from the Surfshark dashboard (e.g. a different
UK server, or another country) and re-run the script — it is safe to run
any number of times:

```bash
sudo ./install-hotspot.sh ~/rp-inky/uk-lon-2.conf
```

## How it works

| Piece | File | What it does |
| --- | --- | --- |
| Tunnel | `/etc/wireguard/wg0.conf` | Surfshark config (+ `PersistentKeepalive`) via `wg-quick@wg0` |
| Access point | `/etc/hostapd/hostapd.conf` | `hostapd` broadcasts the WiFi (2.4 GHz ch. 6, WPA2) |
| DHCP + DNS | `/etc/dnsmasq.d/hotspot.conf` | `dnsmasq` serves the hotspot; upstream DNS is Surfshark's, so queries ride the tunnel |
| Hotspot IP | `hotspot-ip.service` | static `10.8.0.1/24` on `wlan0` |
| Kill switch | `hotspot-firewall.service` + `/etc/hotspot/rules.v4` | forwarding allowed only `wlan0 → wg0`, with NAT |

NetworkManager is told to leave `wlan0` unmanaged so it does not fight
with `hostapd`. No router changes are needed.

## Troubleshooting

- **Tunnel**: `sudo wg show` — look for `latest handshake` and growing
  transfer counters. No handshake after a minute → try another UK server
  config. `systemctl status wg-quick@wg0` shows routing errors.
- **`wg-quick` fails with `resolvconf: command not found`**: the VPN
  config's `DNS =` line needs `openresolv`; the installer installs it, but
  if it was missing during a first failed run,
  `sudo apt install -y openresolv` and
  `sudo systemctl restart wg-quick@wg0` fix it.
- **`hotspot-firewall.service` fails with exit status 203**: `iptables`
  is not installed (Pi OS Lite does not include it by default).
  `sudo apt install -y iptables` and
  `sudo systemctl restart hotspot-firewall` fix it.
- **WiFi not visible**: `systemctl status hostapd`; a config typo is the
  usual cause (`journalctl -u hostapd -e`). Check the password is 8-63
  characters.
- **TV connects but no internet**: `systemctl status dnsmasq`, then
  `sudo wg show`. If the tunnel is down the hotspot is *supposed* to have
  no internet.
- **Check the exit IP from a hotspot client**: connect a laptop to the
  hotspot WiFi and visit an IP-echo site — it must show the Surfshark
  server, not your home address.
- **2.4 GHz is congested**: edit `HW_MODE`/`CHANNEL` at the top of the
  script (e.g. `CHANNEL="1"` or `"11"`), re-run it. 5 GHz (`HW_MODE="a"`,
  `CHANNEL="36"`) needs a valid `COUNTRY_CODE` and shorter range.

## Streaming notes

- BBC iPlayer, ITVX and Channel 4 usually work on Surfshark UK servers,
  but if one server is blocked, download a different UK server config and
  re-run the script — blocks are per-IP, not per-provider.
- Netflix and some others actively block known VPN ranges; Surfshark's
  dedicated streaming servers do best (see their server list).
- iPlayer requires signing in with a UK TV licence account, and
  circumventing a service's geo-restrictions may breach its terms of use
  — that's between you and the service.