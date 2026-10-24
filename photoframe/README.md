# rp-inky photo frame

A headless digital photo frame for the **Inky Impression 7.3" (PIM773)** on
Raspberry Pi OS Lite. Photos are shown in portrait (480 × 800), chosen randomly
every 20 minutes, with a footer showing the current weather and the photo's
EXIF capture date and place.

- Panel: 800 × 480 native, 6-colour Spectra, mounted vertically.
- Weather: Open-Meteo — free, no API key (temperature only).
- Photo places: GPS EXIF reverse-geocoded with Nominatim/OpenStreetMap (cached, optional).
- Random selection: avoids repeating the current photo when more than one is
  available; new files are picked up automatically.
- No current clock is shown; the only date on screen is when the photo was taken.

## Requirements

- Raspberry Pi supported by the driver in this repo, running **Raspberry Pi OS
  Lite** Bookworm or later.
- Inky Impression 7.3" connected to the 40-pin header.
- The driver in this repo installed, with I2C and SPI enabled:

```bash
git clone https://github.com/kiagithub/rp-inky.git
cd rp-inky
./install.sh
```

`install.sh` creates the `~/.virtualenvs/pimoroni` virtualenv, installs the
driver and enables I2C/SPI. Reboot if the installer asks you to.

The photo frame uses only Pillow (installed with Inky) and the Python standard
library. No extra packages are required for JPEG/PNG/WebP photos.

## 1. Add photos

Put **480 × 800 portrait** photos in `photoframe/image/`.

If your originals are a different size, use the helper to centre-crop them and
preserve their EXIF metadata:

```bash
source ~/.virtualenvs/pimoroni/bin/activate
python photoframe/prepare_images.py ~/Pictures/Holiday
```

This writes JPEGs into `photoframe/image/`. Options:

```text
--out DIR          Output directory (default: photoframe/image)
--quality N        JPEG quality (default: 92)
--force            Overwrite existing output files
--strip-metadata   Do not preserve EXIF date/GPS
```

HEIC/HEIF (common for iPhone photos) works only when `pillow-heif` is
installed: `pip install pillow-heif`. Otherwise export the photos as JPEG first.
TIFF is supported through Pillow; RAW formats are not supported.

## 2. Configure

The checked-in `photoframe/config.json` defaults to **McLean, Virginia, US**.
Edit it to change the weather city:

```bash
nano photoframe/config.json
```

```json
"weather": {
  "location": "McLean",
  "country_code": "US",
  "admin1": "Virginia",
  "units": "metric"
}
```

- `country_code` is the ISO country code; `admin1` is an optional state/region
  used to disambiguate cities with the same name.
- `units` is `metric` (°C) or `imperial` (°F).
- Instead of a city you can supply `latitude` and `longitude` to skip
  geocoding.
- You can also update the config through the installer:
  `./photoframe/install-service.sh --city "Your City" --country US --state "Your State"`.
  Run it without flags to keep the default McLean, Virginia, US. Passing
  `--city` without country/state clears those fields and searches worldwide.

`config.json` is tracked in git; if you edit it, keep your weather settings when
resolving a later `git pull`.

Other useful settings:

| Setting | Default | Meaning |
| --- | --- | --- |
| `refresh_minutes` | `20` | Minutes between photo changes. |
| `display.rotate` | `90` | `90` or `270`. Use `270` if the frame is upside down. |
| `display.saturation` | `0.5` | 0–1 colour saturation. Lower is more muted. |
| `footer.height` | `220` | Footer area height in pixels; `0` hides it, otherwise minimum 220. |
| `footer.outline` | `true` | Thin black outline around the yellow footer text. |
| `metadata.reverse_geocode` | `true` | Look up place names from photo GPS. |
| `metadata.coordinates_fallback` | `true` | Show coordinates if no place name is available. |
| `metadata.show_unknown` | `false` | Show "Location unknown" / "Date unknown". |
| `metadata.sidecar` | `image/metadata.json` | Optional per-photo overrides. |
| `weather.admin1` | `Virginia` | State/region for ambiguous city names. |

Paths in the config are resolved relative to `config.json`.

## 3. Preview before installing

Render a single frame to a PNG — no Inky hardware or GPIO access needed:

```bash
source ~/.virtualenvs/pimoroni/bin/activate
python photoframe/photoframe.py --preview /tmp/frame.png --offline
```

`--offline` skips all network calls. Without it, the preview uses the real
weather and photo place names. The preview is the **portrait** 480 × 800 frame;
the app rotates it to 800 × 480 for the panel.

To test the real panel once:

```bash
python photoframe/photoframe.py --once
```

## 4. Install the startup service

```bash
./photoframe/install-service.sh    # uses the default McLean, Virginia, US config
```

The script:

- uses the tracked `photoframe/config.json` (defaults to McLean, Virginia, US);
- updates `weather.location`, `weather.country_code` and `weather.admin1` when
  `--city`/`--country`/`--state` are supplied;
- installs `/etc/systemd/system/inky-photoframe.service`;
- adds the current user to the service and includes the `spi`/`gpio`/`i2c`
  groups that exist;
- enables the service and starts it.

It calls `sudo` itself — do not run the script with `sudo`.

Options: `--city`, `--country`, `--state`, `--units imperial`, `--interval 30`,
`--venv PATH`, `--no-start`.

If no weather city is configured, the service is installed and enabled but not
started. Edit `config.json`, then run:

```bash
sudo systemctl start inky-photoframe
```

## 5. Operation

```bash
# Follow the log (photo changes, weather requests, errors)
sudo journalctl -u inky-photoframe -f

# Status / restart / stop
sudo systemctl status inky-photoframe
sudo systemctl restart inky-photoframe
sudo systemctl stop inky-photoframe

# Disable at boot
sudo systemctl disable --now inky-photoframe
```

Photos are re-scanned every cycle and a random photo is chosen, avoiding the one
currently displayed when more than one is available. You can add or remove files
in `photoframe/image/` without restarting; new files enter the pool on the next
20-minute refresh. To force an immediate refresh, restart the service. The frame
shows a friendly "No photos yet" screen when the folder is empty.

To import many photos at once, see
[Add many photos at once](../README.md#add-many-photos-at-once) in the root
README.

The service is enabled at boot and keeps its configuration, photos and state on
disk, so a reboot or power cut needs no repeated setup.

`config.json` is read at startup; restart the service after changing it:

```bash
sudo systemctl restart inky-photoframe
```

A full 6-colour e-ink refresh takes roughly 20–40 seconds. The app schedules
refreshes every 20 minutes from the start of each cycle, so refresh time does
not accumulate drift.

## Photo metadata

The footer shows the place and capture date, plus a large temperature, all in
yellow and overlaid directly on the bottom 220 px of the photo. A thin black
outline keeps it readable on bright images; set `footer.outline` to `false` for
plain yellow text. Adjust `footer.height`, or set it to `0` to hide the footer
entirely.

The footer shows:

- **Place** — from the photo's GPS EXIF, reverse-geocoded to a city name
  (`McLean, US`). Results are cached in `photoframe/.state/places.json`.
- **Date taken** — from EXIF `DateTimeOriginal` (falling back to
  `DateTimeDigitized` / `DateTime`).

Missing values are simply omitted by default. If a photo has GPS but no network
or place match, coordinates are shown (disable with
`metadata.coordinates_fallback: false`).

### Sidecar overrides

For scans, edited photos, or photos whose GPS/date was stripped, create
`photoframe/image/metadata.json`:

```json
{
  "paris.jpg": { "location": "Paris, France", "date": "2024-06-14" }
}
```

Keys can be the full filename or the stem. Values override EXIF.

### Privacy and attribution

When `metadata.reverse_geocode` is enabled, the coordinates of each new photo
are sent to OpenStreetMap's Nominatim service to obtain a place name. Set it to
`false` to keep coordinates local (the frame then shows the coordinates).

Reverse-geocoded place names use data © OpenStreetMap contributors, available
under the Open Data Commons Open Database License (ODbL). Requests use a
descriptive User-Agent and are limited to one per new photo. The frame itself
does not draw a copyright line.

## Troubleshooting

**The service starts but the panel stays blank.**
Check the log: `sudo journalctl -u inky-photoframe -n 100`. Make sure I2C/SPI
are enabled (`sudo raspi-config nonint do_i2c 0`, `do_spi 0`) and reboot. Confirm
the panel is detected:

```bash
source ~/.virtualenvs/pimoroni/bin/activate
python -c "from inky.auto import auto; print(auto())"
```

**Permission errors on `/dev/spidev*` or `/dev/gpiochip*`.**
Add your user to the hardware groups and reboot:

```bash
sudo usermod -aG spi,gpio,i2c "$USER"
```

**The image is upside down.**
Set `"display": {"rotate": 270}` in `config.json` and restart the service. The
default `90` assumes the panel was rotated clockwise into portrait.

**No weather is shown.**
Check that `weather.location` is set and that the Pi has DNS/network access to
the Open-Meteo service. Weather failures are cached and logged; the frame
continues to work offline.

**No place is shown.**
The photo may have no GPS EXIF, or reverse geocoding failed. Add a sidecar entry
(above) or enable `metadata.show_unknown`. Not all messaging apps preserve EXIF;
use the originals where possible.

**Text looks tiny or missing.**
Install the DejaVu fonts: `sudo apt install fonts-dejavu-core`. You can also point
`fonts.bold` / `fonts.regular` at any TrueType font in `config.json`.

**HEIC photos are skipped.**
Install `pillow-heif` in the virtualenv, or export the photos as JPEG.

## Files

```
photoframe/
├── photoframe.py            main app (render, weather, metadata, scheduler)
├── prepare_images.py        convert sources to 480x800 JPEGs
├── config.json              default config (McLean, VA) and your edits
├── image/                   your photos (+ optional metadata.json)
├── .state/                  caches and last-photo state (git-ignored)
├── inky-photoframe.service.in
├── install-service.sh
└── README.md
```

## Manual run (without systemd)

```bash
source ~/.virtualenvs/pimoroni/bin/activate
python photoframe/photoframe.py --config photoframe/config.json
```

Use `--once` for a single update, `--offline` to skip network calls, and
`--verbose` for debug logging.
