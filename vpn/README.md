# rp-inky Always-On Surfshark UK VPN & High-Speed Hotspot

Turns your Raspberry Pi 4 into a **dual-purpose** device:

1. **Inky Impression 7.3" digital photo frame** (`photoframe/`), whose weather and OpenStreetMap reverse-geocoding stay on your native home connection so Nominatim never blocks VPN exit IPs.
2. **Always-On, leak-proof UK VPN router & 5 GHz Wi-Fi Hotspot** (`vpn/`) supporting **multiple Surfshark WireGuard (`.conf`) and OpenVPN (`.ovpn`) profiles**, instant profile switching (`rp-vpn use`), automatic UK server failover, TCP MSS clamping, `tcp_bbr`, and **500+ Mbps** forwarding over Gigabit Ethernet.

---

## Why Raspberry Pi 4 VPNs Are Often Slow & Flaky — and What This Fixes

| Bottleneck | Typical Setup | `rp-vpn` Solution |
| --- | --- | --- |
| **Crypto CPU wall** | OpenVPN (`.ovpn`) runs single-threaded AES in user space without ARM AES-NI -> caps at **70–130 Mbps**. | Prioritizes **WireGuard (`.conf`)** using multi-core kernel `ChaCha20-Poly1305` (**550–750 Mbps** capable on Pi 4), while also tuning OpenVPN (`CHACHA20-POLY1305` NCP + 384 KB socket buffers + removing `auth-nocache` 1-hour drops). |
| **MTU stalls / buffering** | Surfshark tunnel overhead drops full-size 1500B TLS packets -> pages hang, streams buffer. | Sets `MTU = 1420` + automatic **`nftables` TCP MSS clamping** (`maxseg size set rt mtu`) on all forwarded traffic. |
| **Silent tunnel death** | Missing `PersistentKeepalive`, stale endpoint DNS when Surfshark rotates IPs, or overloaded UK node. | Injects `PersistentKeepalive = 25` and runs a **45s systemd watchdog** (`rp-vpn-watchdog.timer`) that probes tunnel reachability, refreshes Surfshark DNS in-place, and **auto-fails over** across your UK profiles (`uk-lon` -> `uk-man` -> `uk-gla` -> `uk-edi`). |
| **2.4 GHz / sleeping Wi-Fi AP** | Default `hostapd` uses 20 MHz 2.4 GHz (~20 Mbps) and `brcmfmac` power-save drops packets. | Configures **5 GHz 802.11ac 80 MHz (`VHT80`)**, unmanages `wlan0` in NetworkManager, disables Wi-Fi `power_save`, and enables 4-core Receive Packet Steering (`RPS`) + `tcp_bbr`. |
| **DNS / IPv6 geo-leaks** | Smart TVs hardcode `8.8.8.8` or leak IPv6, breaking UK streaming apps (BBC iPlayer, ITVX, Channel 4). | Blocks client IPv6, runs DHCP-only `dnsmasq`, and transparently redirects (`DNAT`) all client port-53 DNS into Surfshark's tunnel DNS (`162.252.172.57`) inside a strict **`nftables` kill-switch**. |

### Important Hardware Note: Reaching the Full **500 Mbps** on Raspberry Pi 4

The Pi 4's **WireGuard tunnel + Gigabit Ethernet (`eth0`)** easily passes **500+ Mbps** (verify anytime with `sudo rp-vpn benchmark`). However, the Pi 4's **built-in `wlan0` radio** (Cypress CYW43455) is connected internally via an SDIO bus that physically tops out around **110–180 Mbps** even on 5 GHz 80 MHz `VHT80`.

`rp-vpn` gives you **two ways to get the full 500 Mbps** whenever a device needs it:

1. **Zero Extra Hardware — Simultaneous 500 Mbps LAN Gateway Mode (Enabled by Default)**:
   Keep devices connected to your fast **main home Wi-Fi 6/6E router** (or Ethernet switch) and simply set that device's manual network settings to:
   - **Router / Gateway**: `<Pi's eth0 LAN IP>` (shown in `sudo rp-vpn status`)
   - **DNS**: `<Pi's eth0 LAN IP>`
   Traffic enters and exits `eth0` at full Gigabit speed (**500+ Mbps**) through the exact same UK VPN tunnel and leak-proof kill-switch, while your Pi's `wlan0` Wi-Fi Hotspot (`RP-UK-VPN`) stays active at the same time for guests/phones/IoT!
2. **500 Mbps Dedicated Wi-Fi Hotspot (USB 3.0 Adapter)**:
   Plug a Linux in-kernel **USB 3.0 Wi-Fi 6 / AC1300 AP adapter** (such as MediaTek `MT7921AU` or `MT7612U`) into one of the Pi 4's **blue USB 3.0 ports**, then point the hotspot at `wlan1`:
   ```bash
   sudo rp-vpn configure-hotspot --interface wlan1
   ```

---

## 1. Download Multiple Surfshark UK Configs

For 500 Mbps speed and multi-server UK redundancy, download **3–4 UK WireGuard `.conf` files** (plus optional `.ovpn` backups or other countries):

1. Log in to **Surfshark Dashboard** -> **VPN** -> **Manual setup** -> **Router** -> **WireGuard**.
2. Generate or select a key pair once.
3. Download several UK locations so the watchdog can fail over automatically if one city is under maintenance:
   - `uk-lon.prod.surfshark.com.conf` (London)
   - `uk-man.prod.surfshark.com.conf` (Manchester)
   - `uk-gla.prod.surfshark.com.conf` (Glasgow)
   - `uk-edi.prod.surfshark.com.conf` (Edinburgh)
   - *(Optional)* Any other countries you want to switch to manually (e.g. `us-nyc`, `nl-ams`, `de-fra`) or OpenVPN `.ovpn` files.

Copy the folder of configs from your Mac to the Pi:

```bash
rsync -av ~/Downloads/surfshark-configs/ pi@raspberrypi.local:~/surfshark-configs/
```

---

## 2. Install & Start on Raspberry Pi OS

Make sure your Raspberry Pi 4 is plugged into your router via **Gigabit Ethernet (`eth0`)**, then run:

```bash
ssh pi@raspberrypi.local
cd ~/rp-inky

./vpn/install-vpn.sh \
  --ssid "RP-UK-VPN" \
  --passphrase "ChooseAStrongPassword" \
  --country US \
  --import ~/surfshark-configs/
```

*(Change `--country` to your physical Wi-Fi regulatory country code, e.g. `US`, `GB`, `CA`, `DE`, so 5 GHz channels comply with local spectrum rules.)*

If you also imported **OpenVPN (`.ovpn`)** configs, save your Surfshark manual service credentials once (found under **Surfshark -> VPN -> Manual setup -> OpenVPN -> Credentials**):

```bash
sudo rp-vpn set-auth
```

All secrets (`vpn/config.local.json`, `vpn/auth.txt`, `vpn/profiles/*`, `vpn/.state/`) are **git-ignored** with `0600` permissions so they will never be committed or conflict with `git pull`.

---

## 3. Switching Configs & Daily Operation (`rp-vpn`)

```bash
# List all imported profiles (* marks the active one)
rp-vpn list

# Switch immediately to a specific UK city or another country
sudo rp-vpn use uk-lon
sudo rp-vpn use uk-man
sudo rp-vpn use us-nyc

# Jump to the next healthy UK profile
sudo rp-vpn next

# Check active tunnel, UK exit IP/city, Wi-Fi client link speeds & kill-switch
sudo rp-vpn status

# Benchmark Direct WAN speed vs Surfshark VPN tunnel speed right on the Pi
sudo rp-vpn benchmark

# Import more configs later
sudo rp-vpn import ~/more-configs/ --force

# Stop or start the VPN (kill-switch remains active when stopped so clients never leak)
sudo rp-vpn down
sudo rp-vpn up
```

### Following Logs

```bash
sudo journalctl -u rp-vpn -u rp-vpn-hotspot -u rp-vpn-watchdog -f
```

---

## 4. Configuration Reference

Tracked defaults live in [vpn/config.json](config.json). Put personal overrides in **`vpn/config.local.json`** (created automatically by `rp-vpn configure-hotspot` / `install-vpn.sh`):

| Setting | Default | Description |
| --- | --- | --- |
| `wan_interface` | `"eth0"` | Wired Gigabit Ethernet interface connected to your home router. |
| `preferred_country` | `"uk"` | Country tag used for initial profile choice and automatic watchdog failover (`"uk"` matches both `uk-*` and `gb-*`, and never auto-switches to non-UK profiles). |
| `prefer_protocol` | `"wireguard"` | Prioritizes WireGuard `.conf` profiles ahead of OpenVPN `.ovpn` for 500+ Mbps speed. |
| `allow_lan_gateway` | `true` | Allows devices on your main LAN/Wi-Fi 6 router to set Gateway & DNS = `<Pi eth0 IP>` for full 500 Mbps VPN throughput. |
| `force_vpn_dns` | `true` | Redirects all client port-53 DNS queries into Surfshark's tunnel DNS (`162.252.172.57`), preventing Smart TV DNS leaks. |
| `disable_ipv6` | `true` | Blocks IPv6 on client interfaces so Dual-Stack networks cannot leak non-VPN IPv6 traffic. |
| `wireguard.mtu` | `1420` | Tunnel MTU. Lower to `1380` in `vpn/config.local.json` if your ISP uses PPPoE fiber/DSL. |
| `hotspot.interface` | `"wlan0"` | Wi-Fi AP interface (`"wlan0"` onboard, or `"wlan1"` for a USB 3.0 Wi-Fi 6 adapter). |
| `hotspot.band` | `"5g"` | `"5g"` (802.11ac) or `"2.4g"` (wider wall range, lower speed). |
| `hotspot.channel` | `36` | `36` or `149` recommended for 5 GHz 80 MHz `VHT80`; `1`, `6`, or `11` for 2.4 GHz. |
| `watchdog.auto_failover` | `true` | Automatically switches to the next UK profile after `failover_threshold` (default `2`) failed health checks. |

---

## 5. How Routing & Photo Frame Coexistence Work

* **Dedicated Policy Routing Table (`51820`)**:
  - The Pi's own `main` routing table stays on `eth0`. That means `ssh pi@raspberrypi.local`, `git pull`, NTP, and `inky-photoframe.service` (Open-Meteo weather + Nominatim photo geocoding) work directly over your home connection — you are **never locked out of SSH** if a VPN server goes down, and Nominatim never blocks your photo lookups.
  - Traffic entering `wlan0` (Hotspot) or forwarded through `eth0` (LAN Gateway clients) matches `ip rule ... lookup 51820`, which contains the VPN default route (`metric 100`) backed by a `blackhole default metric 9999` route.
* **Double-Layer Leak Protection**:
  - Even while switching profiles or booting before WireGuard connects, `nftables` (`table inet rp_vpn`) drops 100% of Hotspot and LAN-Gateway forward packets not exiting `wg-vpn` or `tun-vpn`.

---

## 6. Troubleshooting

* **`sudo rp-vpn benchmark` shows 450–500 Mbps on `wg-vpn`, but my phone on `RP-UK-VPN` gets ~130 Mbps**:
  - The tunnel is running at full speed; you are hitting the Pi 4 onboard `wlan0` SDIO bus ceiling. Point high-bandwidth devices on your main Wi-Fi 6 router to the Pi's `eth0` IP as Gateway & DNS, or add a USB 3.0 Wi-Fi 6 AP dongle (`wlan1`).
* **`wg-vpn` speed is below 250 Mbps on `sudo rp-vpn benchmark`**:
  - Verify `rp-vpn status` shows `(wireguard, ...)` and not `(openvpn, ...)`.
  - Try another UK WireGuard location (`sudo rp-vpn use uk-man` or `sudo rp-vpn use uk-lon`).
  - If your ISP uses PPPoE (e.g. CenturyLink, BT/Openreach FTTP), set `"wireguard": {"mtu": 1380}` in `vpn/config.local.json` and run `sudo rp-vpn restart`.
* **`hostapd` fails to start on 5 GHz**:
  - Ensure `--country` is a valid 2-letter code (`US` or `GB`) and use non-DFS channel `36` (works in both US and UK/EU) or `149` (US):
    `sudo rp-vpn configure-hotspot --country US --band 5g --channel 36`
* **Older IoT device only supports 2.4 GHz Wi-Fi**:
  - Switch the hotspot to 2.4 GHz: `sudo rp-vpn configure-hotspot --band 2.4g --channel 6`
