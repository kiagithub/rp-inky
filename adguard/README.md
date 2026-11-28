# AdGuard Home — Network-Wide Ad & Tracker Blocker

Runs official **AdGuard Home** (~80 MB RAM, <1% CPU) cleanly alongside `rp-inky` Photo Frame, `rp-inky` Web Hub (`:8080`), and `rp-vpn` Surfshark UK Hotspot.

## How It Coexists with Surfshark UK VPN (`rp-vpn`)

- **Home LAN (`eth0`, e.g. `192.168.86.30:53`)**: The installer sets `"local_dns_blocker": true` in `vpn/config.local.json`, telling `rp-vpn`'s `nftables` ruleset to let LAN DNS queries hit **AdGuard Home** on port 53 for whole-home ad blocking and per-device query logs.
- **UK VPN Hotspot (`wlan0` / `UK-Frame-VPN`)**: Continues forwarding DNS strictly inside the Surfshark UK tunnel so UK streaming apps (BBC iPlayer, ITVX, Channel 4) stay 100% leak-free.
- **Zero Port Conflicts**: AdGuard Home's web UI stays on port **`:3000`** (`http://192.168.86.30:3000`), while `rp-inky` Web Hub stays on **`:8080`**.

---

## 1. Install on Raspberry Pi OS

```bash
cd ~/rp-inky
git pull
chmod +x adguard/install-adguard.sh
./adguard/install-adguard.sh
```

## 2. Complete the 30-Second Setup Wizard

Open **`http://<pi-ip>:3000`** (e.g. `http://192.168.86.30:3000`) in your browser:

1. **Admin Web Interface**: Select **All interfaces**, keep **Port `3000`** (do not use `8080`).
2. **DNS Server**: Select **All interfaces**, keep **Port `53`**.
3. Create your admin username and password, then click **Open Dashboard**.

### Recommended Settings Inside AdGuard Home
- **Settings → DNS settings → Upstream DNS servers**:
  ```text
  https://dns.cloudflare.com/dns-query
  https://dns.quad9.net/dns-query
  ```
- **Filters → DNS blocklists → Add blocklist → Choose from the list**:
  - *AdGuard DNS filter* (enabled by default)
  - *OISD Blocklist Big* or *HaGeZi's Normal DNS Blocklist* (great coverage without breaking apps)

---

## 3. Enable Ad Blocking on Your Home Network

### Option A: Whole-Home (Recommended — Router DNS)
In your main router app (e.g., **Google Home → Wi-Fi → Settings → Advanced Networking → DNS → Custom**):
- **Primary DNS**: `192.168.86.30` (your Pi's static LAN IP)
- **Secondary DNS**: leave blank *(or `1.1.1.1` if you want fallback if the Pi reboots, though some devices will occasionally bypass ad blocking if a secondary is set)*

### Option B: Test on One Device First (iPhone / Mac / Smart TV)
- **iPhone / iPad**: *Settings → Wi-Fi → (i) next to your Wi-Fi → Configure DNS → Manual → Delete old entries → Add `192.168.86.30`*.
- **macOS**: *System Settings → Network → Wi-Fi → Details → DNS → `192.168.86.30`*.

---

## 4. Verify It's Working

From any computer or on the Pi itself:
```bash
# Should return 0.0.0.0 (blocked!)
dig @192.168.86.30 doubleclick.net +short

# Check service status
sudo systemctl status AdGuardHome
```

You'll also see an **AdGuard Home** status card + quick-launch link inside the **🌐 Network** tab of your `rp-inky` Web Hub (`http://192.168.86.30:8080`).
