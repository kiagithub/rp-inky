# Tailscale — Secure Remote Access to Your Raspberry Pi & Home Network

Runs **Tailscale** (~45 MB RAM, <1% CPU) cleanly alongside `rp-inky` Web Hub (`:8080`), **AdGuard Home** (`:3000`), and **Surfshark UK VPN** (`rp-vpn`).

## What You Can Do with Tailscale
- **Upload photos to your e-ink frame from anywhere** (vacations, cellular, office) via `http://<tailscale-ip>:8080`.
- **Check your home network & AdGuard Home** (`:3000`) remotely with zero router port-forwarding.
- **Wake your home PC** via Wake-on-LAN from outside your home.
- **Use your home internet as a private Exit Node** when on untrusted hotel/airport Wi-Fi.

## How It Coexists with `rp-vpn` & AdGuard Home
- **`--accept-dns=false`**: Keeps Tailscale from overwriting `/etc/resolv.conf`, so AdGuard Home and `rp-vpn` continue resolving DNS normally.
- **Separate Routing Table**: `rp-vpn` isolates `wlan0` (`UK-Frame-VPN`) traffic in routing table `52000`, while Tailscale uses its own `tailscale0` table (`52`) on the host — they do not conflict.

---

## 1. Install on Raspberry Pi OS

```bash
cd ~/rp-inky
git pull
./tailscale/install-tailscale.sh
```

During installation, Tailscale will print a login URL (`https://login.tailscale.com/a/...`). Open it on your phone or laptop and sign in.

## 2. Approve Subnet Route & Exit Node (One-Time)

1. Open **[Tailscale Admin Console → Machines](https://login.tailscale.com/admin/machines)**.
2. Click **`...` → Edit route settings** next to your Raspberry Pi.
3. Check **Subnet routes** (e.g. `192.168.86.0/24`) and **Use as exit node** → **Save**.

## 3. Connect from Your Phone or Mac

1. Install the official **Tailscale** app on iPhone/Android/macOS and sign in with the same account.
2. Turn Tailscale **ON** — you can now open:
   - **`http://<pi-tailscale-ip>:8080`** (or `http://192.168.86.30:8080` once subnet routes are approved) → **rp-inky Web Hub**
   - **`http://<pi-tailscale-ip>:3000`** → **AdGuard Home**
3. You'll also see a live **Tailscale** card (Tailscale IP, MagicDNS name, and online peers) inside the **🌐 Network** tab of the Web Hub.
