# rp-inky Web Hub — Phone Photo Uploader, 1-Tap UK VPN, Uptime Watchdog & Wake-on-LAN

A lightweight, zero-bloat web control panel (`http://<pi-ip>:8080`) that runs alongside the photo frame and UK VPN on your Raspberry Pi 4 (~12 MB RAM, <1% idle CPU, zero external CDN dependencies so it works even if your ISP is down).

---

## Features

1. **Phone Photo Uploader & Album Manager (`🖼 Photos`)**
   - Select one or multiple photos straight from your iPhone/Android camera roll (supports JPEG, PNG, WebP, and iPhone HEIC when `pillow-heif` is installed).
   - Interactive **480 × 800 portrait crop preview** with horizontal/vertical focal-point sliders before uploading.
   - Preserves EXIF capture date & GPS coordinates automatically, with optional custom Place/Date overrides (`metadata.json`).
   - **1-Tap "Show"** or **"Pin"** on any photo in the gallery triggers an immediate refresh on the e-ink screen via `SIGUSR1` (no SSH or service restart required).

2. **1-Tap Surfshark UK VPN Control (`🛡 UK VPN`)**
   - View live tunnel status, public UK exit IP/city, WireGuard handshake age, RX/TX transfer, connected 5 GHz Wi-Fi Hotspot clients + PHY link speed, and your 500M LAN Gateway IP.
   - Switch between imported Surfshark configs (`uk-lon`, `uk-man`, `us-nyc`, etc.) with one tap, upload new `.conf` / `.ovpn` files directly from your browser, or run a live **25 MB WAN vs. VPN speed benchmark**.

3. **ISP, UK VPN & Smart-Home Uptime Watchdog (`📡 Watchdog`)**
   - Built-in 60-second probes for **Home ISP Uplink (`eth0`)** and **Surfshark UK VPN Tunnel (`wg-vpn`)**, plus custom **ICMP Ping**, **HTTP/HTTPS**, and **TCP Port** monitors for your NAS, router, Plex, or smart-home devices.
   - Visual green/red heartbeat timelines, uptime percentages, and incident log.
   - **Phone Push Alerts** via **ntfy.sh** (free iOS/Android push app, no account required), **Telegram Bot**, **Pushover**, or **Discord/Slack Webhooks**. If your home ISP drops, alerts are queued in memory and delivered with the outage summary as soon as WAN recovers.

4. **Wake-on-LAN (`⚡ Wake-on-LAN`)**
   - Wake sleeping desktop PCs, media servers, or NAS boxes over Ethernet (`eth0` UDP Magic Packet broadcast) with one tap from your phone.
   - Includes a live **Awake / Asleep** ping indicator and a **1-click ARP LAN Scanner** so you don't have to look up MAC addresses manually.

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
