# log2ram — MicroSD Card Lifespan Protector

Mounts `/var/log` in a **128 MB RAM disk** (~3% of a 4 GB Raspberry Pi 4) and syncs logs back to the microSD card once per hour and on clean shutdown via `rsync`. This cuts continuous SD card writes dramatically so your Pi can run 24/7 for years.

## What the Installer Configures
- **RAM Buffer**: `SIZE=128M` (3× larger than the default 40 MB so `rp-inky`, `rp-vpn`, `AdGuardHome`, and `Tailscale` logs never run out of room).
- **Fast Sync**: `USE_RSYNC=true` for incremental hourly/shutdown flushes.
- **Journald Cap**: Caps `systemd-journald` at `64M` (`/etc/systemd/journald.conf.d/log2ram.conf`) so journal logs can never fill the 128 MB RAM disk.
- **Web Hub Health Bar**: Automatically displays a live **`🪵 Log2RAM: X/128M (Y%)`** badge in the Pi System Health Bar at `http://<pi-ip>:8080`.

---

## 1. Install on Raspberry Pi OS

```bash
cd ~/rp-inky
git pull
./log2ram/install-log2ram.sh
sudo reboot
```

## 2. Verify After Reboot

```bash
df -h /var/log
# Expected filesystem: log2ram   128M  ...  /var/log

sudo systemctl status log2ram
```

## Uninstall (Optional)

```bash
sudo apt-get remove --purge log2ram
sudo rm -f /etc/systemd/journald.conf.d/log2ram.conf
sudo reboot
```
