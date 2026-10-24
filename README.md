# rp-inky

A Raspberry Pi e-ink digital photo frame for the **Inky Impression 7.3" (PIM773)**.

The panel is mounted in portrait: photos are composed at **480 × 800**, rotated
to the panel's native 800 × 480 size, and changed every **20 minutes**. A footer
overlaid on the bottom of each photo shows:

- the current weather (Open-Meteo, no API key);
- where the photo was taken (EXIF GPS, reverse-geocoded to a place name);
- when it was taken (EXIF capture date).

`main` is the release branch.

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
│   ├── config.example.json
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

# Optional: create a config and set weather.location for live weather/place
cp photoframe/config.example.json photoframe/config.json

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

### 2. Put the code on the Pi

The repository is private. The simplest route is to copy the working tree from
the Mac with `rsync` (run from the Mac, in the repo directory):

```bash
rsync -av --progress \
  --exclude '.git' --exclude '.venv' --exclude '__pycache__' \
  --exclude 'photoframe/config.json' --exclude 'photoframe/.state' \
  ./ pi@raspberrypi.local:~/rp-inky/
```

Alternatively, clone on the Pi using an SSH key registered with GitHub:

```bash
ssh pi@raspberrypi.local
git clone git@github.com:kiagithub/rp-inky.git
```

### 3. Install and configure

On the Pi:

```bash
cd ~/rp-inky
sudo apt update
sudo apt install -y python3-venv git rsync

./install.sh

source ~/.virtualenvs/pimoroni/bin/activate

# If you did not copy prepared photos with rsync:
python photoframe/prepare_images.py ~/Pictures

cp photoframe/config.example.json photoframe/config.json
nano photoframe/config.json     # set weather.location and country_code
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
./photoframe/install-service.sh --city "Cambridge" --country GB

# Check it is running
sudo systemctl status inky-photoframe
sudo journalctl -u inky-photoframe -f
```

The service starts at boot, picks up new photos from `photoframe/image/`
without a restart, and can be restarted after config changes:

```bash
sudo systemctl restart inky-photoframe
```

## Configuration and operation

See [photoframe/README.md](photoframe/README.md) for all settings, EXIF/sidecar
metadata, preview mode, logs, troubleshooting and orientation options. The
config template is [photoframe/config.example.json](photoframe/config.example.json).

Quick reference:

- photos: `photoframe/image/` (480 × 800, or convert with `prepare_images.py`);
- weather city: `weather.location` in `photoframe/config.json`;
- rotation interval: `refresh_minutes` (default 20);
- orientation: `display.rotate` (`90` default, `270` if upside down);
- footer: `footer.height` (default 120 px, `0` hides it) and `footer.outline`
  (default true — thin black outline around the white text);
- service: `inky-photoframe.service`.

## License

MIT. The `inky/` driver is derived from the MIT-licensed Inky library; see
[LICENSE](LICENSE).
