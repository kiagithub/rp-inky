# rp-inky

A Raspberry Pi e-ink digital photo frame for the **Inky Impression 7.3" (PIM773)**.

The panel is mounted in portrait: photos are composed at **480 × 800**, rotated
to the panel's native 800 × 480 size, and changed every **20 minutes**. A new
photo is chosen randomly each cycle (avoiding an immediate repeat when more than
one photo is available). A footer
overlaid on the bottom of each photo shows:

- the current temperature (Open-Meteo, no API key);
- where the photo was taken (EXIF GPS, reverse-geocoded to a place name);
- when it was taken (EXIF capture date).

`main` is the release branch.

![Example rp-inky output](docs/screenshot.png)

*Example output (480 × 800 portrait; synthetic sample photo).*

## Requirements

- Raspberry Pi supported by the Inky driver.
- Inky Impression 7.3" (PIM773) connected to the 40-pin header.
- Raspberry Pi OS Lite Bookworm or later.
- Network access for weather/place lookup. Cached data is used when offline.

## Repository layout

```text
rp-inky/
├── inky/             driver package (vendored, MIT)
├── photoframe/       the photo frame application
│   ├── image/        your 480x800 photos
│   ├── config.json   settings; defaults to McLean, Virginia, US
│   └── README.md     full configuration and operation guide
├── tests/            tests
├── install.sh        Raspberry Pi setup script
├── pyproject.toml
└── LICENSE
```

## Test locally on macOS

The preview mode composes a frame with Pillow and writes a PNG. It does not
touch GPIO or SPI, so it runs on a Mac without an Inky panel.

```bash
git clone https://github.com/kiagithub/rp-inky.git
cd rp-inky

python3 -m venv .venv
source .venv/bin/activate
pip install pillow pytest ruff==0.15.20

# Optional: convert your own photos into photoframe/image/
python photoframe/prepare_images.py ~/Pictures/Holiday

# Optional: change the weather city (the default is McLean, Virginia, US)
nano photoframe/config.json

# Render an offline preview (placeholder if image/ is empty)
python photoframe/photoframe.py --preview /tmp/frame.png --offline
open /tmp/frame.png

# Run the app tests and linter
python -m pytest tests/test_photoframe.py -q
ruff check photoframe tests/test_photoframe.py
```

Notes:

- `--preview` outputs the **portrait** 480 × 800 composition. The app rotates it
  to 800 × 480 only when sending it to the panel.
- `--offline` skips weather and reverse-geocoding requests. Remove it, and set
  `weather.location` in `config.json`, to preview live data.
- The `--once` option and the actual panel output require Raspberry Pi hardware.
- macOS uses Arial if DejaVu Sans is not installed. Raspberry Pi OS uses DejaVu.

## Deploy to Raspberry Pi OS Lite over SSH

### 1. Enable SSH and connect

Enable SSH with Raspberry Pi Imager (the gear icon) or `sudo raspi-config` on
the Pi, then connect from the Mac:

```bash
ssh pi@raspberrypi.local
```

Replace `pi` and `raspberrypi.local` with your username and hostname/IP.

### 2. Clone the code on the Pi

The repository is public, so clone it directly on the Pi:

```bash
ssh pi@raspberrypi.local
git clone https://github.com/kiagithub/rp-inky.git
cd rp-inky
```

To update an existing checkout later:

```bash
cd ~/rp-inky
git pull
sudo systemctl restart inky-photoframe
```

### 3. Install and configure

On the Pi:

```bash
cd ~/rp-inky
sudo apt update
sudo apt install -y python3-venv git rsync

./install.sh

source ~/.virtualenvs/pimoroni/bin/activate

# Add photos (optional; see "Add many photos at once" below):
python photoframe/prepare_images.py ~/Pictures

# Optional: change the default weather city (McLean, Virginia, US)
nano photoframe/config.json
```

`install.sh` creates `~/.virtualenvs/pimoroni`, installs the driver, enables
I2C/SPI and adds the required overlays to `/boot/firmware/config.txt`.

Reboot if the installer newly enabled I2C/SPI:

```bash
sudo reboot
```

### 4. Install the startup service

After reconnecting:

```bash
cd ~/rp-inky
./photoframe/install-service.sh    # uses the default McLean, Virginia, US config

# Check it is running
sudo systemctl status inky-photoframe
sudo journalctl -u inky-photoframe -f
```

To use a different city, see [Change the weather city](#change-the-weather-city).

The service starts at boot, picks up new photos from `photoframe/image/`
without a restart, and can be restarted after config changes:

```bash
sudo systemctl restart inky-photoframe
```

## Add many photos at once

To copy a folder of originals from the Mac to the Pi and convert it into the
album:

```bash
# On the Mac
rsync -av --progress ~/Pictures/Holiday/ pi@raspberrypi.local:~/rp-inky/photoframe/source/

# On the Pi
ssh pi@raspberrypi.local
cd ~/rp-inky
source ~/.virtualenvs/pimoroni/bin/activate
python photoframe/prepare_images.py photoframe/source
```

`prepare_images.py` centre-crops the originals to 480 × 800 and preserves their
EXIF date/GPS. Add `--force` to overwrite files that already exist in the album.

If the photos are already 480 × 800, copy them straight into the album:

```bash
# On the Mac
rsync -av --progress ~/Pictures/prepared/ pi@raspberrypi.local:~/rp-inky/photoframe/image/
```

`photoframe/source/` is a staging folder and is ignored by git.

## Random selection and live updates

At the start of every cycle the app re-scans `photoframe/image/` and picks a
random photo, excluding the one currently on screen. This means:

- new photos added while the frame is running are in the pool on the next
  20-minute refresh;
- removed photos disappear from the pool automatically;
- no restart is needed.

To force a refresh immediately after copying photos:

```bash
sudo systemctl restart inky-photoframe
```

The service log reports the number of photos found each cycle:

```bash
sudo journalctl -u inky-photoframe -f
```

## Reboots and power loss

Once the service is installed, a reboot or power cut does not require any
repeated setup:

- `install-service.sh` enables `inky-photoframe.service` at boot;
- `config.json`, the photo album, state and caches live on disk under
  `photoframe/`;
- the I2C/SPI overlays are written to `/boot/firmware/config.txt`;
- the virtualenv lives in `~/.virtualenvs/pimoroni`;
- the last-photo state is written atomically, so a power cut cannot leave a
  half-written state file.

Verify the unit is enabled and survives a reboot:

```bash
systemctl is-enabled inky-photoframe     # prints: enabled
sudo reboot

# after reconnecting
sudo systemctl status inky-photoframe
```

If the network is not ready at boot, the app retries weather requests, then
falls back to cached data; no interactive setup is needed. The first e-ink
refresh after boot can take around 30 seconds.

## Configuration and operation

See [photoframe/README.md](photoframe/README.md) for all settings, EXIF/sidecar
metadata, preview mode, logs, troubleshooting and orientation options. The
default configuration is [photoframe/config.json](photoframe/config.json)
(McLean, Virginia, US).

### Change the weather city

Edit `photoframe/config.json`:

```json
"weather": {
  "location": "Your City",
  "country_code": "US",
  "admin1": "Your State",
  "units": "metric"
}
```

`admin1` is optional, but it disambiguates cities with the same name (for
example, McLean exists in several US states). Set `units` to `imperial` for °F.

You can also let the service installer update the config. Run it without flags
to keep the default (McLean, Virginia, US), or supply your own values:

```bash
./photoframe/install-service.sh --city "Your City" --country US --state "Your State"
```

Passing `--city` without `--country`/`--state` clears those fields and searches
worldwide. After editing the config manually, restart the service:

```bash
sudo systemctl restart inky-photoframe
```

`config.json` is tracked in git. If you edit it and later run `git pull`, keep
your weather settings if Git reports a conflict.

Quick reference:

- photos: `photoframe/image/` (480 × 800, or convert with `prepare_images.py`);
  chosen randomly each cycle, with new files picked up automatically;
- weather city: `weather.location`, `weather.country_code` and optional
  `weather.admin1` in `photoframe/config.json` (default: McLean, Virginia, US);
- rotation interval: `refresh_minutes` (default 20);
- orientation: `display.rotate` (`90` default, `270` if upside down);
- footer: `footer.height` (default 120 px, `0` hides it) and `footer.outline`
  (default true — thin black outline around the yellow text);
- service: `inky-photoframe.service`.

## License

MIT. The `inky/` driver is derived from the MIT-licensed Inky library; see
[LICENSE](LICENSE).
