# rp-inky Web Hub — Photo Frame, UK VPN, Network Scanner, Pi Health, Watchdog & Wake-on-LAN

A lightweight, zero-bloat web control panel (`http://<pi-ip>:8080`) that runs alongside the photo frame and UK VPN on your Raspberry Pi 4 (~12 MB RAM, <1% idle CPU, zero external CDN dependencies so it works even if your ISP is down).

---

## Features

1. **Live Pi System Health Bar (Always Visible)**
   - Compact live status strip right beneath the top bar showing **CPU temperature (°C)**, **RAM usage**, **SD card free/used space**, **power supply & thermal throttling status** (`vcgencmd get_throttled`), and **uptime + 1-minute load average**.
   - Color-coded health indicators (green / amber / red) plus automatic **Watchdog phone push alerts** if CPU ≥ 80°C, SD card ≥ 90% full, or active undervoltage/throttling occurs (with automatic recovery alerts).

2. **Phone Photo Uploader, Screen Modes & Album Manager (`🖼 Photos`)**
   - Select one or multiple photos straight from your iPhone/Android camera roll (supports JPEG, PNG, WebP, and iPhone HEIC when `pillow-heif` is installed).
   - Interactive **480 × 800 portrait crop preview** with horizontal/vertical focal-point sliders before uploading.
   - Preserves EXIF capture date & GPS coordinates automatically, with optional custom Place/Date overrides (`metadata.json`).
   - **1-Tap "Show"**, **"Pin"**, **"Weather & Quote"**, or **"New Fact/Quote"** triggers an immediate refresh on the e-ink screen via `SIGUSR1` (no SSH or service restart required).
   - Switch display modes (`Alternate`, `Weather & Quote Always On`, `Scheduled Hours`, or `Photos Only`) and enter custom notes/quotes up to 300 characters.

3. **1-Tap Surfshark UK VPN Control (`🛡 UK VPN`)**
   - View live tunnel status, public UK exit IP/city, WireGuard handshake age, RX/TX transfer, connected 5 GHz Wi-Fi Hotspot clients + PHY link speed, and your 500M LAN Gateway IP.
   - Switch between imported Surfshark configs (`uk-lon`, `uk-man`, `us-nyc`, etc.) with one tap, upload new `.conf` / `.ovpn` files directly from your browser, or run a live **25 MB WAN vs. VPN speed benchmark**.

4. **Home Network & VPN Hotspot Scanner (`🌐 Network`)**
   - **VPN Hotspot Clients (`wlan0`)**: Shows every device connected to `UK-Frame-VPN` with **live download/upload speed (Kbps/Mbps)**, **total session data (↓ / ↑)**, **Wi-Fi signal strength (dBm)**, and connected duration.
   - **Home Router LAN Devices (`eth0`)**: 1-tap **Scan Whole Network** runs a fast non-root Layer-2 ARP sweep across your subnet to discover active TVs, smart plugs, phones, speakers, and computers (even devices that drop ICMP ping). *(Note: per-device bandwidth is only visible for traffic routed through the Pi; devices talking directly to your main router are honestly marked `Bandwidth N/A from Pi`.)*
   - **Readable Names & Device Icons**: Combines mDNS/Bonjour (`avahi-browse`), DHCP leases, reverse DNS, and IEEE OUI MAC vendor lookup (`ieee-data` + built-in consumer smart-home table) to auto-label devices (`📺 TV`, `🔌 Smart Plug`, `📱 Phone`, `💻 Computer`, `🔊 Speaker`, `📷 Camera`, etc.), flags randomized **Private MAC** phones, and lets you save custom **Rename / Type** tags per MAC.
   - **New-Device & Unknown-Device Alerts**: Automatically baselines existing devices on first run, flags brand-new unknown MAC addresses with an amber **`NEW`** badge + **`✓ Trust`** button, and sends a phone push alert via your Watchdog notification channel (toggleable via **🔔 Alert on New Devices**).
   - 1-tap **+ Watch** and **+ WoL** shortcuts on every device row.

5. **ISP, UK VPN & Smart-Home Uptime Watchdog (`📡 Watchdog`)**
   - Built-in 60-second probes for **Home ISP Uplink (`eth0`)**, **Surfshark UK VPN Tunnel (`wg-vpn`)**, and **Pi System Health**, plus custom **ICMP Ping**, **HTTP/HTTPS**, and **TCP Port** monitors for your NAS, router, Plex, or smart-home devices.
   - Visual green/red heartbeat timelines, uptime percentages, and incident log (including new-device and thermal/disk incidents).
   - **Phone Push Alerts** via **ntfy.sh** (free iOS/Android push app, no account required), **Telegram Bot**, **Pushover**, or **Discord/Slack Webhooks**. If your home ISP drops, alerts are queued in memory and delivered as soon as WAN recovers.

6. **Wake-on-LAN (`⚡ Wake-on-LAN`)**
   - Wake sleeping desktop PCs, media servers, or NAS boxes over Ethernet (`eth0` UDP Magic Packet broadcast) with one tap from your phone.
   - Includes a live **Awake / Asleep** ping indicator and a **1-click ARP LAN Scanner**.

---

## Install on Raspberry Pi OS

Assuming `./install.sh` (or Python 3) is present on your Pi:

```bash
cd ~/rp-inky
git pull
./webui/install-webui.sh
```

Options:

```bash
# Custom port or optional browser access PIN
./webui/install-webui.sh --port 8080 --pin 2468
```

Then open **`http://<pi-lan-ip>:8080`** (e.g. `http://192.168.86.30:8080`) on your phone or laptop!
*(Tip: On iPhone Safari, tap **Share → Add to Home Screen** to pin it as a full-screen app icon.)*

---

## CLI Utilities & Local macOS Preview

You can also run the Web Hub locally on macOS for testing:

```bash
source .venv/bin/activate
python webui/server.py --port 8080
open http://127.0.0.1:8080
```

Set or clear the optional PIN, or wake a PC from SSH:

```bash
~/.virtualenvs/pimoroni/bin/python ~/rp-inky/webui/server.py --set-pin 2468
~/.virtualenvs/pimoroni/bin/python ~/rp-inky/webui/server.py --set-pin ""
~/.virtualenvs/pimoroni/bin/python ~/rp-inky/webui/server.py --wake "Desktop PC"
```
