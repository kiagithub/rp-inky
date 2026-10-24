#!/usr/bin/env python3
"""Inky Impression 7.3" digital photo frame.

Displays 480x800 portrait photos on a PIM773 Inky Impression 7.3"
(800x480 native, 6-colour Spectra) and rotates them every 20 minutes.
The current weather and the photo's EXIF capture date and place are drawn
directly over the bottom of each photo (no banner).

Designed to run headless on Raspberry Pi OS Lite under systemd.
"""

import argparse
import copy
import json
import logging
import os
import random
import re
import signal
import sys
import threading
import time
import urllib.parse
import urllib.request
from contextlib import suppress
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps

LOG = logging.getLogger("photoframe")

WIDTH, HEIGHT = 480, 800
NATIVE_WIDTH, NATIVE_HEIGHT = 800, 480

BLACK = (0, 0, 0)
WHITE = (255, 255, 255)
YELLOW = (255, 255, 0)

LANCZOS = getattr(Image, "Resampling", Image).LANCZOS
ROTATE_90 = getattr(Image, "Transpose", Image).ROTATE_90
ROTATE_270 = getattr(Image, "Transpose", Image).ROTATE_270

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
    IMAGE_EXTENSIONS |= {".heic", ".heif"}
except (ImportError, OSError, RuntimeError):
    pass

# EXIF tags. Numeric constants keep this working on Pillow 9, which has no
# ExifTags.IFD / ExifTags.GPS.
EXIF_DATETIME = 306
EXIF_EXIF_IFD = 34665
EXIF_GPS_IFD = 34853
EXIF_DATETIME_ORIGINAL = 36867
EXIF_DATETIME_DIGITIZED = 36868

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

DEFAULT_CONFIG = {
    "image_dir": "image",
    "state_dir": ".state",
    "refresh_minutes": 20,
    "display": {
        "rotate": 90,
        # Spectra 6 sweet spot: 0.35 keeps skin tones realistic instead of
        # blobbing, with contrast/brightness lifts to recover face detail.
        "saturation": 0.35,
        "color": 1.0,
        "contrast": 1.25,
        "brightness": 1.1,
    },
    "footer": {"height": 100},
    "fonts": {
        "bold": "",
        "regular": "",
        "sizes": {"place": 28, "date": 24, "temperature": 64},
    },
    "metadata": {
        "reverse_geocode": True,
        "coordinates_fallback": True,
        "show_unknown": False,
        "sidecar": "image/metadata.json",
    },
    "weather": {
        "location": "McLean",
        "country_code": "US",
        "admin1": "Virginia",
        "units": "metric",
        "retries": 3,
        "retry_delay_seconds": 10,
    },
}

BOLD_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
]
REGULAR_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
]

USER_AGENT = "rp-inky-photoframe/1.0 (+https://github.com/kiagithub/rp-inky)"
HTTP_TIMEOUT = 15
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"


@dataclass
class PhotoMeta:
    """Metadata shown in the footer for one photo."""

    taken: Optional[date] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    place: Optional[str] = None


@dataclass
class Weather:
    """Current weather shown in the footer."""

    temperature: Optional[float] = None
    fetched_at: Optional[str] = None

    @classmethod
    def from_dict(cls, data):
        """Build a Weather from a cached dictionary, ignoring unknown keys."""
        if not isinstance(data, dict):
            raise TypeError("Weather cache entry must be a dictionary")
        names = {field.name for field in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in names})


@dataclass
class Fonts:
    """Font objects used by the renderer."""

    place: Any
    date: Any
    temp: Any
    placeholder_title: Any
    placeholder_body: Any
    placeholder_path: Any


def text_metrics(font, text):
    """Return (width, height, offset_x, offset_y) for text in a font.

    Pillow 9's built-in bitmap fallback only has getsize(); FreeType fonts and
    Pillow 10+ expose getbbox(). Supporting both keeps local previews working.
    """
    if hasattr(font, "getbbox"):
        bbox = font.getbbox(text)
        return max(0, bbox[2] - bbox[0]), max(0, bbox[3] - bbox[1]), bbox[0], bbox[1]
    if hasattr(font, "getsize"):
        width, height = font.getsize(text)
        return max(0, width), max(0, height), 0, 0
    return 0, 0, 0, 0


def text_width(font, text):
    """Return the rendered width of text."""
    return text_metrics(font, text)[0]


def text_mask(text, font):
    """Render text to a hard-edged 1-bit mask.

    Thresholding avoids grey anti-aliased pixels that would otherwise be
    dithered into red/green/yellow fringes by the 6-colour panel.
    """
    width, height, offset_x, offset_y = text_metrics(font, text)
    if width <= 0 or height <= 0:
        return None
    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).text((-offset_x, -offset_y), text, font=font, fill=255)
    return mask.point(lambda pixel: 255 if pixel >= 128 else 0)


def paste_text(canvas, xy, text, font, fill, shadow=0, shadow_fill=BLACK):
    """Paste crisp text onto the canvas in a solid palette colour.

    A positive shadow offset first pastes the same mask in shadow_fill,
    offset down and to the right, so text stays readable over photos.
    """
    if not text:
        return
    mask = text_mask(text, font)
    if mask is not None:
        if shadow:
            canvas.paste(shadow_fill, (xy[0] + shadow, xy[1] + shadow), mask)
        canvas.paste(fill, xy, mask)


def paste_centered(canvas, y, text, font, fill, max_width=WIDTH - 80):
    """Paste text centred horizontally."""
    text = truncate_text(text, font, max_width)
    x = max(0, (canvas.width - text_width(font, text)) // 2)
    paste_text(canvas, (x, y), text, font, fill)


def truncate_text(text, font, max_width):
    """Truncate text with an ellipsis so it fits max_width."""
    if text_width(font, text) <= max_width:
        return text
    ellipsis = "…"
    while text and text_width(font, text + ellipsis) > max_width:
        text = text[:-1]
    return (text.rstrip() + ellipsis) if text else ellipsis


def _first_existing(paths):
    for path in paths:
        if not path:
            continue
        candidate = Path(path).expanduser()
        if candidate.is_file():
            return str(candidate)
    return None


def _load_font(path, size):
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            LOG.warning("Could not load font %s; using the default font", path)
    return ImageFont.load_default()


def load_fonts(config):
    """Load all fonts, preferring configured paths and then DejaVu.

    Point sizes come from fonts.sizes in the config so they can be tuned
    without touching code.
    """
    bold_path = _first_existing([config["fonts"].get("bold"), *BOLD_FONT_CANDIDATES])
    regular_path = _first_existing([config["fonts"].get("regular"), *REGULAR_FONT_CANDIDATES])
    if bold_path is None or regular_path is None:
        LOG.warning("No TrueType font found; using a tiny fallback. Install fonts-dejavu-core.")
    sizes = config["fonts"].get("sizes") if isinstance(config["fonts"], dict) else None
    sizes = sizes if isinstance(sizes, dict) else {}

    def _size(key, default):
        try:
            return int(sizes.get(key, default))
        except (TypeError, ValueError):
            return default

    return Fonts(
        place=_load_font(bold_path, _size("place", 28)),
        date=_load_font(regular_path, _size("date", 24)),
        temp=_load_font(bold_path, _size("temperature", 64)),
        placeholder_title=_load_font(bold_path, 30),
        placeholder_body=_load_font(regular_path, 17),
        placeholder_path=_load_font(regular_path, 15),
    )


def format_date(value):
    """Format a date as 14 Jun 2024 without relying on locale."""
    return f"{value.day:02d} {MONTHS[value.month - 1]} {value.year}"


def parse_exif_date(value):
    """Parse an EXIF/sidecar date string into a date, or return None."""
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("ascii", "ignore")
    text = str(value).strip().replace("\x00", "")
    match = re.match(r"^(\d{4})[:\-](\d{2})[:\-](\d{2})", text)
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def gps_to_decimal(values, ref=None, positive="N", negative="S"):
    """Convert EXIF (degrees, minutes, seconds) to signed decimal degrees."""
    if values is None:
        return None
    try:
        if isinstance(values, (tuple, list)):
            parts = [_as_float(item) for item in values]
            if not parts or any(item is None for item in parts):
                return None
            if len(parts) >= 2 and not 0 <= parts[1] < 60:
                return None
            if len(parts) >= 3 and not 0 <= parts[2] < 60:
                return None
            if len(parts) == 1:
                decimal = parts[0]
            elif len(parts) == 2:
                decimal = parts[0] + parts[1] / 60.0
            else:
                decimal = parts[0] + parts[1] / 60.0 + parts[2] / 3600.0
        else:
            decimal = float(values)
    except (TypeError, ValueError, ZeroDivisionError):
        return None

    if ref is not None:
        if isinstance(ref, bytes):
            ref = ref.decode("ascii", "ignore")
        ref = str(ref).strip().upper()
        if ref in ("E", "W") and (positive, negative) == ("N", "S"):
            positive, negative = "E", "W"

    limit = 90.0 if positive == "N" else 180.0
    if not -limit <= decimal <= limit:
        return None

    if ref is not None:
        if ref == negative:
            decimal = -decimal
        elif ref and ref != positive:
            return None
    return round(decimal, 6)


def _exif_tag(exif, tag):
    """Read a tag from the top-level EXIF dict or the Exif sub-IFD."""
    try:
        value = exif.get(tag)
    except Exception:
        value = None
    if value:
        return value
    try:
        sub_ifd = exif.get_ifd(EXIF_EXIF_IFD)
    except Exception:
        return None
    return sub_ifd.get(tag) if isinstance(sub_ifd, dict) else None


def _gps_ifd(exif):
    try:
        gps = exif.get_ifd(EXIF_GPS_IFD)
    except Exception:
        gps = None
    if isinstance(gps, dict) and gps:
        return gps
    try:
        value = exif.get(EXIF_GPS_IFD)
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def read_photo_metadata(path):
    """Read capture date and GPS coordinates from a photo's EXIF."""
    meta = PhotoMeta()
    try:
        with Image.open(path) as image:
            exif = image.getexif()
            raw_date = _exif_tag(exif, EXIF_DATETIME_ORIGINAL)
            if raw_date is None:
                raw_date = _exif_tag(exif, EXIF_DATETIME_DIGITIZED)
            if raw_date is None:
                raw_date = _exif_tag(exif, EXIF_DATETIME)
            meta.taken = parse_exif_date(raw_date)

            gps = _gps_ifd(exif)
            if gps:
                meta.lat = gps_to_decimal(gps.get(2), gps.get(1), "N", "S")
                meta.lon = gps_to_decimal(gps.get(4), gps.get(3), "E", "W")
    except Exception as exc:
        LOG.warning("Could not read EXIF from %s: %s", path, exc)
    return meta


def format_place(address, fallback_name=None):
    """Build a short place name from a Nominatim address dictionary."""
    if not isinstance(address, dict):
        address = {}
    locality = None
    for key in ("city", "town", "village", "hamlet", "municipality", "county", "state_district", "state"):
        value = address.get(key)
        if value:
            locality = str(value)
            break
    country_code = str(address.get("country_code") or "").strip().upper()
    if locality and country_code:
        return f"{locality}, {country_code}"
    if locality:
        return locality
    if fallback_name:
        return str(fallback_name)
    return None


def format_coordinates(lat, lon):
    """Format coordinates for display when reverse geocoding is unavailable."""
    return f"{lat:.3f}, {lon:.3f}"


def read_json(path, default):
    """Read JSON from path, returning default on any problem."""
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        LOG.warning("Could not read %s: %s", path, exc)
        return default


def write_json_atomic(path, data):
    """Write JSON atomically so a crash cannot leave a partial cache file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def deep_merge(base, override):
    """Recursively merge override into a copy of base."""
    result = copy.deepcopy(base)
    if not isinstance(override, dict):
        return result
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def resolve_path(base, value):
    """Resolve a possibly-relative path against the config file directory."""
    path = Path(os.path.expanduser(str(value)))
    return path if path.is_absolute() else base / path


def _ensure_dict(config, key):
    if not isinstance(config.get(key), dict):
        config[key] = copy.deepcopy(DEFAULT_CONFIG[key])
    return config[key]


def validate_config(config):
    """Apply defaults, clamp unsafe values and resolve paths."""
    if not isinstance(config.get("image_dir"), str) or not config["image_dir"].strip():
        config["image_dir"] = DEFAULT_CONFIG["image_dir"]
    if not isinstance(config.get("state_dir"), str) or not config["state_dir"].strip():
        config["state_dir"] = DEFAULT_CONFIG["state_dir"]

    try:
        refresh = float(config.get("refresh_minutes", 20))
    except (TypeError, ValueError):
        refresh = 20.0
    if refresh <= 0:
        LOG.warning("refresh_minutes must be greater than zero; using 20")
        refresh = 20.0
    config["refresh_minutes"] = refresh

    display = _ensure_dict(config, "display")
    try:
        rotate = int(display.get("rotate", 90))
    except (TypeError, ValueError):
        rotate = 90
    if rotate not in (90, 270):
        LOG.warning("display.rotate must be 90 or 270; using 90")
        rotate = 90
    display["rotate"] = rotate
    saturation = _as_float(display.get("saturation"))
    display["saturation"] = 0.35 if saturation is None else min(1.0, max(0.0, saturation))
    for key, default in (("color", 1.0), ("contrast", 1.25), ("brightness", 1.1)):
        value = _as_float(display.get(key))
        display[key] = default if value is None else min(2.0, max(0.0, value))

    footer = _ensure_dict(config, "footer")
    try:
        footer_height = int(footer.get("height", 100))
    except (TypeError, ValueError):
        footer_height = 100
    if footer_height != 0 and footer_height < 80:
        LOG.warning("footer.height must be 0 or at least 80; using 100")
        footer_height = 100
    footer["height"] = max(0, min(footer_height, 300))

    fonts_config = _ensure_dict(config, "fonts")
    sizes = fonts_config.get("sizes")
    if not isinstance(sizes, dict):
        sizes = {}
    for key, default in (("place", 28), ("date", 24), ("temperature", 64)):
        try:
            size = int(sizes.get(key, default))
        except (TypeError, ValueError):
            size = default
        sizes[key] = min(200, max(8, size))
    fonts_config["sizes"] = sizes

    metadata = _ensure_dict(config, "metadata")
    for key in ("reverse_geocode", "coordinates_fallback", "show_unknown"):
        metadata[key] = bool(metadata.get(key, DEFAULT_CONFIG["metadata"][key]))
    if not isinstance(metadata.get("sidecar"), str):
        metadata["sidecar"] = ""

    weather = _ensure_dict(config, "weather")
    units = str(weather.get("units", "metric")).lower()
    weather["units"] = units if units in ("metric", "imperial") else "metric"
    try:
        weather["retries"] = max(0, int(weather.get("retries", 3)))
    except (TypeError, ValueError):
        weather["retries"] = 3
    delay = _as_float(weather.get("retry_delay_seconds"))
    weather["retry_delay_seconds"] = 10.0 if delay is None else max(0.0, delay)
    weather["location"] = str(weather.get("location") or "").strip()
    weather["country_code"] = str(weather.get("country_code") or "").strip().upper()
    weather["admin1"] = str(weather.get("admin1") or "").strip()
    return config


def load_config(config_path):
    """Load config.json (or defaults), validate it and resolve paths."""
    config_path = Path(config_path).expanduser().resolve()
    user_config = read_json(config_path, {})
    if not isinstance(user_config, dict):
        LOG.warning("%s does not contain a JSON object; using defaults", config_path)
        user_config = {}
    config = validate_config(deep_merge(DEFAULT_CONFIG, user_config))

    base_dir = config_path.parent
    config["_config_path"] = config_path
    config["_base_dir"] = base_dir
    config["_image_dir"] = resolve_path(base_dir, config["image_dir"])
    config["_state_dir"] = resolve_path(base_dir, config["state_dir"])
    sidecar = config["metadata"].get("sidecar")
    config["_sidecar"] = resolve_path(base_dir, sidecar) if sidecar else None
    return config


def http_json(url, params=None, timeout=HTTP_TIMEOUT):
    """Fetch and decode a JSON object over HTTP."""
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read().decode("utf-8")
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError("Unexpected JSON response")
    return data


def geocode_city(city, country_code="", admin1="", timeout=HTTP_TIMEOUT):
    """Look up a city with Open-Meteo's geocoder."""
    params = {"name": city, "count": 5, "language": "en", "format": "json"}
    data = http_json(GEOCODE_URL, params, timeout=timeout)
    results = data.get("results") or []
    if not results:
        return None

    wanted_country = (country_code or "").strip().upper()
    wanted_admin = (admin1 or "").strip().lower()
    selected = None
    for item in results:
        if wanted_country and str(item.get("country_code") or "").upper() != wanted_country:
            continue
        if wanted_admin and str(item.get("admin1") or "").strip().lower() != wanted_admin:
            continue
        selected = item
        break
    if selected is None and wanted_country:
        for item in results:
            if str(item.get("country_code") or "").upper() == wanted_country:
                selected = item
                break
    if selected is None:
        selected = results[0]

    try:
        return {
            "latitude": float(selected["latitude"]),
            "longitude": float(selected["longitude"]),
            "name": selected.get("name") or city,
            "country_code": selected.get("country_code") or wanted_country,
            "admin1": selected.get("admin1") or str(admin1 or ""),
            "timezone": selected.get("timezone") or "",
        }
    except (KeyError, TypeError, ValueError):
        return None


def resolve_weather_location(config, offline=False):
    """Return cached/geocoded coordinates for the configured weather city."""
    weather = config["weather"]
    latitude = weather.get("latitude")
    longitude = weather.get("longitude")
    if latitude is not None and longitude is not None:
        try:
            return {
                "latitude": float(latitude),
                "longitude": float(longitude),
                "name": weather.get("location") or "Configured location",
                "country_code": weather.get("country_code") or "",
                "admin1": weather.get("admin1") or "",
                "timezone": weather.get("timezone") or "",
            }
        except (TypeError, ValueError):
            LOG.error("Invalid latitude/longitude in config")
            return None

    city = str(weather.get("location") or "").strip()
    if not city:
        LOG.info("No weather location configured; weather will not be shown")
        return None

    cache_path = config["_state_dir"] / "location.json"
    admin1 = weather.get("admin1") or ""
    cache_key = f"{city.lower()}|{str(weather.get('country_code') or '').upper()}|{admin1.lower()}"
    cache = read_json(cache_path, {})
    if isinstance(cache, dict):
        cached = cache.get(cache_key)
        if isinstance(cached, dict) and cached.get("latitude") is not None and cached.get("longitude") is not None:
            return cached

    if offline:
        LOG.info("Offline: no cached coordinates for %s", city)
        return None

    try:
        location = geocode_city(city, weather.get("country_code") or "", admin1)
    except Exception as exc:
        LOG.warning("Could not geocode %s: %s", city, exc)
        return None
    if location is None:
        LOG.warning("No geocoding result for %s", city)
        return None

    if not isinstance(cache, dict):
        cache = {}
    cache[cache_key] = location
    try:
        write_json_atomic(cache_path, cache)
    except OSError as exc:
        LOG.warning("Could not cache geocoding result: %s", exc)
    LOG.info("Weather location: %s (%.4f, %.4f)", location.get("name"), location["latitude"], location["longitude"])
    return location


def parse_weather(payload):
    """Build a Weather from an Open-Meteo response."""
    current = payload.get("current") if isinstance(payload, dict) else None
    current = current if isinstance(current, dict) else {}
    return Weather(temperature=_as_float(current.get("temperature_2m")))


def fetch_weather(location, units="metric", timeout=HTTP_TIMEOUT):
    """Fetch the current temperature from Open-Meteo."""
    temperature_unit = "fahrenheit" if units == "imperial" else "celsius"
    params = {
        "latitude": f"{location['latitude']:.6f}",
        "longitude": f"{location['longitude']:.6f}",
        "current": "temperature_2m",
        "temperature_unit": temperature_unit,
    }
    weather = parse_weather(http_json(FORECAST_URL, params, timeout=timeout))
    weather.fetched_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return weather


def _fetch_weather_once(location, units, cache_path, cache_key):
    """Fetch weather and update its cache; return (weather, error)."""
    try:
        weather = fetch_weather(location, units)
    except Exception as exc:
        return None, exc
    try:
        write_json_atomic(cache_path, {"key": cache_key, "weather": asdict(weather)})
    except OSError as exc:
        LOG.warning("Could not cache weather: %s", exc)
    return weather, None


def get_weather(config, offline=False):
    """Return weather, using a cache and short retries when possible."""
    location = resolve_weather_location(config, offline=offline)
    if location is None:
        return None

    units = config["weather"].get("units", "metric")
    cache_key = f"{location['latitude']:.4f},{location['longitude']:.4f},{units}"
    cache_path = config["_state_dir"] / "weather.json"
    cache = read_json(cache_path, {})
    cached = None
    if isinstance(cache, dict) and cache.get("key") == cache_key:
        try:
            cached = Weather.from_dict(cache.get("weather") or {})
        except (TypeError, ValueError):
            cached = None

    if offline:
        if cached is not None:
            LOG.info("Offline: using cached weather")
        return cached

    attempts = 1 + max(0, int(config["weather"].get("retries", 0)))
    delay = max(0.0, float(config["weather"].get("retry_delay_seconds", 0)))
    last_error = None
    for attempt in range(attempts):
        weather, error = _fetch_weather_once(location, units, cache_path, cache_key)
        if weather is not None:
            return weather
        last_error = error
        LOG.warning("Weather fetch failed (%d/%d): %s", attempt + 1, attempts, error)
        if attempt + 1 < attempts and delay:
            time.sleep(delay)

    if cached is not None:
        LOG.warning("Using cached weather after fetch failure: %s", last_error)
        return cached
    LOG.error("Weather unavailable: %s", last_error)
    return Weather()


class PlaceResolver:
    """Reverse-geocode photo GPS coordinates through Nominatim, with caching."""

    def __init__(self, state_dir, enabled=True, offline=False, timeout=HTTP_TIMEOUT, rate_limit_seconds=1.1):
        self.path = Path(state_dir) / "places.json"
        self.enabled = enabled
        self.offline = offline
        self.timeout = timeout
        self.rate_limit_seconds = rate_limit_seconds
        self._last_request = 0.0

    def resolve(self, lat, lon):
        """Return a cached/network place name for coordinates, or None."""
        key = f"{lat:.3f},{lon:.3f},z12"
        cache = read_json(self.path, {})
        if not isinstance(cache, dict):
            cache = {}
        if key in cache:
            entry = cache[key]
            return entry.get("name") if isinstance(entry, dict) else None

        if not self.enabled or self.offline:
            return None

        try:
            name = self._fetch(lat, lon)
        except Exception as exc:
            LOG.warning("Reverse geocoding failed for %s: %s", key, exc)
            return None

        cache[key] = {"name": name, "fetched_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat()}
        try:
            write_json_atomic(self.path, cache)
        except OSError as exc:
            LOG.warning("Could not cache place name: %s", exc)
        if name:
            LOG.info("Photo location: %s -> %s", key, name)
        return name

    def _fetch(self, lat, lon):
        wait = self.rate_limit_seconds - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()
        params = {
            "format": "jsonv2",
            "lat": f"{lat:.6f}",
            "lon": f"{lon:.6f}",
            "zoom": 12,
            "addressdetails": 1,
            "accept-language": "en",
        }
        payload = http_json(NOMINATIM_URL, params, timeout=self.timeout)
        return format_place(payload.get("address"), payload.get("name"))


def load_sidecar(config, photo_path):
    """Return sidecar metadata for a photo, or an empty dict."""
    sidecar_path = config.get("_sidecar")
    if not sidecar_path:
        return {}
    data = read_json(sidecar_path, {})
    if not isinstance(data, dict):
        return {}
    entry = data.get(photo_path.name)
    if entry is None:
        entry = data.get(photo_path.stem)
    return entry if isinstance(entry, dict) else {}


def build_photo_meta(photo_path, config, resolver):
    """Combine EXIF, sidecar and reverse-geocoded metadata for a photo."""
    meta = read_photo_metadata(photo_path)
    sidecar = load_sidecar(config, photo_path)
    if sidecar:
        location = sidecar.get("location")
        if isinstance(location, str) and location.strip():
            meta.place = location.strip()
        sidecar_date = parse_exif_date(sidecar.get("date"))
        if sidecar_date is not None:
            meta.taken = sidecar_date

    if not meta.place and meta.lat is not None and meta.lon is not None:
        if resolver is not None:
            place = resolver.resolve(meta.lat, meta.lon)
            if place:
                meta.place = place
        if not meta.place and config["metadata"].get("coordinates_fallback", True):
            meta.place = format_coordinates(meta.lat, meta.lon)

    if not meta.place and config["metadata"].get("show_unknown", False):
        meta.place = "Location unknown"
    return meta


def list_images(image_dir):
    """Return supported photo files sorted case-insensitively by name."""
    directory = Path(image_dir)
    if not directory.is_dir():
        return []
    return sorted(
        (path for path in directory.iterdir() if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS),
        key=lambda path: path.name.lower(),
    )


def choose_next(images, last_name=None):
    """Pick a random photo, avoiding an immediate repeat."""
    if not images:
        return None
    candidates = [path for path in images if path.name != last_name]
    if not candidates:
        candidates = images
    return random.choice(candidates)


def read_last_image(state_path):
    """Read the last displayed image name from the state file."""
    data = read_json(state_path, {})
    if isinstance(data, dict) and isinstance(data.get("last_image"), str):
        return data["last_image"]
    return None


def write_last_image(state_path, name):
    """Persist the last displayed image name."""
    write_json_atomic(state_path, {"last_image": name})


def fit_photo(path):
    """Open a photo, honour EXIF orientation and cover-fit it to 480x800."""
    try:
        with Image.open(path) as source:
            image = ImageOps.exif_transpose(source)
            if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
                image = image.convert("RGBA")
                background = Image.new("RGBA", image.size, (255, 255, 255, 255))
                image = Image.alpha_composite(background, image)
            image = image.convert("RGB")
            return ImageOps.fit(image, (WIDTH, HEIGHT), method=LANCZOS, centering=(0.5, 0.5))
    except Exception as exc:
        LOG.warning("Could not load photo %s: %s", path, exc)
        return None


def rotate_for_display(image, degrees):
    """Rotate the portrait canvas to the panel's native landscape size."""
    if degrees == 90:
        return image.transpose(ROTATE_90)
    if degrees == 270:
        return image.transpose(ROTATE_270)
    raise ValueError("display.rotate must be 90 or 270")


def draw_placeholder(image_dir, fonts):
    """Build a friendly screen shown when image/ has no usable photos."""
    image = Image.new("RGB", (WIDTH, HEIGHT), WHITE)
    draw = ImageDraw.Draw(image)
    draw.rectangle((48, 90, WIDTH - 48, 600), outline=BLACK, width=3)
    draw.rectangle(((WIDTH - 100) // 2, 282, (WIDTH + 100) // 2, 286), fill=YELLOW)
    paste_centered(image, 230, "No photos yet", fonts.placeholder_title, BLACK)
    paste_centered(image, 320, "Add 480 x 800 images to:", fonts.placeholder_body, BLACK)
    paste_centered(image, 352, str(image_dir), fonts.placeholder_path, BLACK)
    paste_centered(image, 420, "They will appear here automatically.", fonts.placeholder_body, BLACK)
    return image


def draw_overlay(image, meta, weather, config, fonts):
    """Overlay location/date and temperature directly over the photo.

    There is no banner: the text is pasted onto the photo itself, with a
    small black drop shadow so it stays legible over light areas.
    """
    height = int(config["footer"]["height"])
    if height <= 0:
        return
    top = HEIGHT - height

    unit = "°F" if config["weather"].get("units") == "imperial" else "°C"
    temperature = (
        f"{round(weather.temperature)}{unit}"
        if weather is not None and weather.temperature is not None
        else "--"
    )
    temp_width = text_width(fonts.temp, temperature)
    temp_height = text_metrics(fonts.temp, temperature)[1]
    temp_x = WIDTH - 26 - temp_width
    temp_y = top + (height - temp_height) // 2
    shadow = 2

    if meta is not None:
        place = meta.place
        if not place and config["metadata"].get("show_unknown", False):
            place = "Location unknown"

        date_text = None
        if meta.taken is not None:
            date_text = format_date(meta.taken)
        elif config["metadata"].get("show_unknown", False):
            date_text = "Date unknown"

        left_max = max(80, temp_x - 26 - 16)
        line_height = max(text_metrics(fonts.place, "Mg")[1], text_metrics(fonts.date, "Mg")[1])
        line_y = top + (height - line_height) // 2

        if place and date_text:
            suffix = " · " + date_text
            place_max = max(60, left_max - text_width(fonts.date, suffix))
            place_text = truncate_text(place, fonts.place, place_max)
            paste_text(image, (26, line_y), place_text, fonts.place, YELLOW, shadow=shadow)
            paste_text(
                image,
                (26 + text_width(fonts.place, place_text), line_y),
                suffix,
                fonts.date,
                YELLOW,
                shadow=shadow,
            )
        elif place:
            place_text = truncate_text(place, fonts.place, left_max)
            paste_text(image, (26, line_y), place_text, fonts.place, YELLOW, shadow=shadow)
        elif date_text:
            paste_text(image, (26, line_y), date_text, fonts.date, YELLOW, shadow=shadow)

    paste_text(image, (temp_x, temp_y), temperature, fonts.temp, YELLOW, shadow=shadow)


def enhance_photo(image, config):
    """Apply optional colour, contrast and brightness boosts to the photo."""
    display = config["display"]
    for key, enhancer in (
        ("color", ImageEnhance.Color),
        ("contrast", ImageEnhance.Contrast),
        ("brightness", ImageEnhance.Brightness),
    ):
        factor = float(display.get(key, 1.0))
        if factor != 1.0:
            image = enhancer(image).enhance(factor)
    return image


def render_frame(photo_path, meta, weather, config, fonts):
    """Compose one 480x800 portrait frame."""
    if photo_path is not None:
        image = fit_photo(photo_path)
        if image is not None:
            image = enhance_photo(image, config)
            draw_overlay(image, meta, weather, config, fonts)
            return image
    return draw_placeholder(config["_image_dir"], fonts)


def init_display():
    """Create the Inky display, importing hardware modules only when needed."""
    try:
        from inky.auto import auto

        display = auto()
    except Exception as exc:
        LOG.warning("Inky auto-detect failed (%s); trying InkyE673 directly", exc)
        from inky.inky_e673 import Inky

        display = Inky(resolution=(NATIVE_WIDTH, NATIVE_HEIGHT))

    resolution = getattr(display, "resolution", None)
    if tuple(resolution or ()) != (NATIVE_WIDTH, NATIVE_HEIGHT):
        raise RuntimeError(f"Expected an 800x480 Inky Impression 7.3, got {resolution}")
    return display


def show_frame(display, frame, config):
    """Rotate a portrait frame to native size and send it to the panel."""
    native = rotate_for_display(frame, int(config["display"]["rotate"]))
    saturation = float(config["display"]["saturation"])
    try:
        display.set_image(native, saturation=saturation)
    except TypeError:
        display.set_image(native)
    display.show()


def _install_signal_handlers(stop_event):
    def handler(signum, _frame):
        LOG.info("Received signal %s; stopping after this cycle", signum)
        stop_event.set()

    for signum in (signal.SIGTERM, signal.SIGINT):
        with suppress(OSError, ValueError):
            signal.signal(signum, handler)


def _wait_until(deadline, stop_event):
    while not stop_event.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        stop_event.wait(min(remaining, 30.0))


def run_cycle(config, args, fonts, resolver, display):
    """Choose, compose and display one frame."""
    images = list_images(config["_image_dir"])
    LOG.info("Found %d photo(s) in %s", len(images), config["_image_dir"])
    state_path = config["_state_dir"] / "state.json"
    photo = choose_next(images, read_last_image(state_path))

    meta = PhotoMeta()
    if photo is not None:
        meta = build_photo_meta(photo, config, resolver)
    weather = get_weather(config, offline=args.offline)
    frame = render_frame(photo, meta, weather, config, fonts)

    if args.preview:
        output = Path(args.preview).expanduser()
        if not output.is_absolute():
            output = Path.cwd() / output
        output.parent.mkdir(parents=True, exist_ok=True)
        frame.save(output)
        LOG.info("Preview written to %s (%dx%d)", output, frame.width, frame.height)
        return

    show_frame(display, frame, config)
    if photo is not None:
        try:
            write_last_image(state_path, photo.name)
        except OSError as exc:
            LOG.warning("Could not save state: %s", exc)
    LOG.info("Displayed %s", photo.name if photo is not None else "(placeholder)")


def run(args):
    """Main loop: display immediately, then every refresh_minutes."""
    config = load_config(args.config)
    if args.image_dir:
        config["_image_dir"] = Path(args.image_dir).expanduser().resolve()
    try:
        config["_image_dir"].mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        LOG.warning("Could not create image directory %s: %s", config["_image_dir"], exc)

    fonts = load_fonts(config)
    resolver = None
    if config["metadata"].get("reverse_geocode", True):
        resolver = PlaceResolver(config["_state_dir"], offline=args.offline)

    display = None if args.preview else init_display()
    stop_event = threading.Event()
    _install_signal_handlers(stop_event)
    interval = float(config["refresh_minutes"]) * 60.0

    while not stop_event.is_set():
        cycle_start = time.monotonic()
        try:
            run_cycle(config, args, fonts, resolver, display)
        except Exception:
            LOG.exception("Frame update failed")
            if args.once or args.preview:
                return 1

        if args.once or args.preview:
            return 0

        _wait_until(cycle_start + interval, stop_event)
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description="Inky Impression 7.3 photo frame")
    parser.add_argument(
        "--config",
        default=str(Path(__file__).resolve().parent / "config.json"),
        help="path to config.json (default: next to this script)",
    )
    parser.add_argument("--image-dir", help="override the configured photo directory")
    parser.add_argument("--preview", metavar="PATH", help="render one frame to a PNG and exit (no hardware)")
    parser.add_argument("--once", action="store_true", help="display one frame and exit")
    parser.add_argument("--offline", action="store_true", help="do not make network requests")
    parser.add_argument("--verbose", "-v", action="store_true", help="enable debug logging")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    LOG.setLevel(logging.DEBUG if args.verbose else logging.INFO)
    try:
        return run(args)
    except Exception:
        LOG.exception("Fatal error")
        return 1


if __name__ == "__main__":
    sys.exit(main())
