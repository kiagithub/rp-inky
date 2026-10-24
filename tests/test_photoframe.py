"""Offline tests for the Inky photo frame application."""

import argparse
import json
from datetime import date

import pytest
from PIL import Image, ImageOps, TiffImagePlugin

from photoframe import photoframe as pf
from photoframe import prepare_images as pfp


def make_photo(path, size=(480, 800), color=(120, 80, 40), exif=None):
    """Write a small JPEG with optional EXIF."""
    image = Image.new("RGB", size, color)
    options = {"exif": exif} if exif is not None else {}
    image.save(path, "JPEG", **options)


def exif_with_gps(date_value="2024:06:14 15:30:00", orientation=None):
    """Build EXIF containing a capture date, GPS coordinates and orientation."""
    exif = Image.Exif()
    exif[pf.EXIF_DATETIME_ORIGINAL] = date_value
    if orientation is not None:
        exif[274] = orientation
    exif[pf.EXIF_GPS_IFD] = {
        1: "N",
        2: (
            TiffImagePlugin.IFDRational(52, 1),
            TiffImagePlugin.IFDRational(12, 1),
            TiffImagePlugin.IFDRational(0, 1),
        ),
        3: "E",
        4: (
            TiffImagePlugin.IFDRational(0, 1),
            TiffImagePlugin.IFDRational(7, 1),
            TiffImagePlugin.IFDRational(0, 1),
        ),
    }
    return exif


def test_parse_exif_date():
    assert pf.parse_exif_date("2024:06:14 15:30:00") == date(2024, 6, 14)
    assert pf.parse_exif_date("2024-06-14") == date(2024, 6, 14)
    assert pf.parse_exif_date("14/06/2024") is None
    assert pf.parse_exif_date("2024:13:40 00:00:00") is None
    assert pf.parse_exif_date(None) is None


def test_gps_to_decimal():
    assert pf.gps_to_decimal((52, 12, 0), "N") == pytest.approx(52.2)
    assert pf.gps_to_decimal((0, 7, 0), "E") == pytest.approx(0.116667, abs=1e-6)
    assert pf.gps_to_decimal((52, 12, 0), "S") == pytest.approx(-52.2)
    assert pf.gps_to_decimal((0, 7, 0), "W") == pytest.approx(-0.116667, abs=1e-6)
    assert pf.gps_to_decimal((91, 0, 0), "N") is None
    assert pf.gps_to_decimal((0, 61, 0), "E") is None
    assert pf.gps_to_decimal(None, "N") is None


def test_format_place():
    assert pf.format_place({"city": "Cambridge", "country_code": "gb"}) == "Cambridge, GB"
    assert pf.format_place({"town": "Springfield", "country_code": "US"}) == "Springfield, US"
    assert pf.format_place({"village": "Smallville"}) == "Smallville"
    assert pf.format_place({}, "Fallback") == "Fallback"
    assert pf.format_place({}) is None


def test_weather_code_info():
    assert pf.weather_code_info(0) == ("Clear", "clear")
    assert pf.weather_code_info(95) == ("Thunderstorm", "thunder")
    assert pf.weather_code_info(61) == ("Rain", "rain")
    assert pf.weather_code_info("not a code") == ("Unknown", "unknown")
    assert pf.weather_code_info(1234) == ("Unknown", "unknown")


def test_parse_weather():
    payload = {
        "current": {"temperature_2m": 13.3, "is_day": 0, "weather_code": 53},
        "daily": {"temperature_2m_max": [14.8], "temperature_2m_min": [6.4]},
    }
    weather = pf.parse_weather(payload)
    assert weather.temperature == pytest.approx(13.3)
    assert weather.condition == "Drizzle"
    assert weather.icon == "drizzle"
    assert weather.is_day is False
    assert weather.high == pytest.approx(14.8)
    assert weather.low == pytest.approx(6.4)


def test_parse_weather_missing_fields():
    weather = pf.parse_weather({})
    assert weather.temperature is None
    assert weather.condition == "Unknown"
    assert weather.icon == "unknown"
    assert weather.high is None and weather.low is None


def test_read_photo_metadata(tmp_path):
    photo = tmp_path / "photo.jpg"
    make_photo(photo, exif=exif_with_gps())
    meta = pf.read_photo_metadata(photo)
    assert meta.taken == date(2024, 6, 14)
    assert meta.lat == pytest.approx(52.2)
    assert meta.lon == pytest.approx(0.116667, abs=1e-6)


def test_read_photo_metadata_from_sub_ifd(tmp_path):
    exif = Image.Exif()
    exif[pf.EXIF_EXIF_IFD] = {pf.EXIF_DATETIME_ORIGINAL: "2023:01:02 03:04:05"}
    photo = tmp_path / "nested.jpg"
    make_photo(photo, exif=exif)
    assert pf.read_photo_metadata(photo).taken == date(2023, 1, 2)


def test_read_photo_metadata_without_exif(tmp_path):
    photo = tmp_path / "plain.jpg"
    make_photo(photo)
    meta = pf.read_photo_metadata(photo)
    assert meta.taken is None
    assert meta.lat is None and meta.lon is None


def test_load_config_defaults(tmp_path):
    config = pf.load_config(tmp_path / "config.json")
    assert config["refresh_minutes"] == 20
    assert config["display"]["rotate"] == 90
    assert config["display"]["saturation"] == 0.5
    assert config["_image_dir"] == tmp_path / "image"
    assert config["_state_dir"] == tmp_path / ".state"
    assert config["_sidecar"] == tmp_path / "image" / "metadata.json"
    assert config["metadata"]["reverse_geocode"] is True


def test_load_config_overrides_and_validation(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "image_dir": "photos",
                "refresh_minutes": 5,
                "display": {"rotate": 270, "saturation": 2},
                "footer": {"height": 40},
                "weather": {"location": "Cambridge", "units": "IMPERIAL", "retry_delay_seconds": -1},
            }
        ),
        encoding="utf-8",
    )
    config = pf.load_config(path)
    assert config["refresh_minutes"] == 5
    assert config["display"]["rotate"] == 270
    assert config["display"]["saturation"] == 1.0
    assert config["footer"]["height"] == 120  # 40 is too small, falls back
    assert config["weather"]["units"] == "imperial"
    assert config["weather"]["retry_delay_seconds"] == 0.0
    assert config["_image_dir"] == tmp_path / "photos"


def test_list_images_and_choose_next(tmp_path):
    for name in ("b.jpg", "A.PNG", "notes.txt"):
        (tmp_path / name).write_bytes(b"placeholder")
    images = pf.list_images(tmp_path)
    assert [path.name for path in images] == ["A.PNG", "b.jpg"]
    assert pf.choose_next(images).name == "A.PNG"
    assert pf.choose_next(images, "A.PNG").name == "b.jpg"
    assert pf.choose_next(images, "b.jpg").name == "A.PNG"
    assert pf.choose_next(images, "gone.jpg").name == "A.PNG"
    assert pf.choose_next([]) is None


def test_state_round_trip(tmp_path):
    state = tmp_path / "state.json"
    assert pf.read_last_image(state) is None
    pf.write_last_image(state, "photo.jpg")
    assert pf.read_last_image(state) == "photo.jpg"


def test_fit_photo_and_rotation(tmp_path):
    photo = tmp_path / "wide.jpg"
    make_photo(photo, size=(1200, 800))
    image = pf.fit_photo(photo)
    assert image.size == (480, 800)
    assert pf.rotate_for_display(image, 90).size == (800, 480)
    assert pf.rotate_for_display(image, 270).size == (800, 480)
    with pytest.raises(ValueError):
        pf.rotate_for_display(image, 180)


def test_render_frame(tmp_path):
    photo = tmp_path / "photo.jpg"
    make_photo(photo)
    config = pf.load_config(tmp_path / "config.json")
    fonts = pf.load_fonts(config)
    meta = pf.PhotoMeta(taken=date(2024, 6, 14), place="Cambridge, GB", place_source="nominatim")
    weather = pf.Weather(temperature=13.3, condition="Partly cloudy", icon="partly", is_day=True, high=15, low=6)
    frame = pf.render_frame(photo, meta, weather, config, fonts)
    assert frame.size == (480, 800)
    assert frame.getpixel((5, 795)) == pytest.approx((120, 80, 40), abs=3)


def test_render_placeholder(tmp_path):
    config = pf.load_config(tmp_path / "config.json")
    fonts = pf.load_fonts(config)
    frame = pf.render_frame(None, None, None, config, fonts)
    assert frame.size == (480, 800)
    assert frame.getpixel((5, 795)) == (255, 255, 255)


class FakeDisplay:
    def __init__(self):
        self.image = None
        self.saturation = None
        self.shown = False

    def set_image(self, image, saturation=0.5):
        self.image = image
        self.saturation = saturation

    def show(self):
        self.shown = True


def test_show_frame_rotates_to_native(tmp_path):
    photo = tmp_path / "photo.jpg"
    make_photo(photo)
    config = pf.load_config(tmp_path / "config.json")
    fonts = pf.load_fonts(config)
    frame = pf.render_frame(photo, pf.PhotoMeta(), None, config, fonts)
    display = FakeDisplay()
    pf.show_frame(display, frame, config)
    assert display.image.size == (800, 480)
    assert display.saturation == 0.5
    assert display.shown is True


def test_run_cycle_displays_and_advances(tmp_path):
    image_dir = tmp_path / "image"
    image_dir.mkdir()
    make_photo(image_dir / "a.jpg")
    make_photo(image_dir / "b.jpg")
    config = pf.load_config(tmp_path / "config.json")
    fonts = pf.load_fonts(config)
    display = FakeDisplay()
    args = argparse.Namespace(preview=None, once=True, offline=True)
    state = config["_state_dir"] / "state.json"

    pf.run_cycle(config, args, fonts, None, display)
    assert display.shown is True
    assert display.image.size == (800, 480)
    assert pf.read_last_image(state) == "a.jpg"

    pf.run_cycle(config, args, fonts, None, display)
    assert pf.read_last_image(state) == "b.jpg"


def test_sidecar_overrides_exif(tmp_path):
    image_dir = tmp_path / "image"
    image_dir.mkdir()
    photo = image_dir / "paris.jpg"
    make_photo(photo, exif=exif_with_gps("2020:01:01 12:00:00"))
    (image_dir / "metadata.json").write_text(
        json.dumps({"paris.jpg": {"location": "Paris, France", "date": "2024-06-14"}}),
        encoding="utf-8",
    )
    config = pf.load_config(tmp_path / "config.json")
    meta = pf.build_photo_meta(photo, config, resolver=None)
    assert meta.place == "Paris, France"
    assert meta.place_source == "sidecar"
    assert meta.taken == date(2024, 6, 14)


def test_coordinates_fallback(tmp_path):
    photo = tmp_path / "photo.jpg"
    make_photo(photo, exif=exif_with_gps())
    config = pf.load_config(tmp_path / "config.json")
    config["metadata"]["show_unknown"] = True
    meta = pf.build_photo_meta(photo, config, resolver=None)
    assert meta.place == "52.200, 0.117"
    assert meta.place_source == "coordinates"


def test_place_resolver_cache(tmp_path, monkeypatch):
    resolver = pf.PlaceResolver(tmp_path, rate_limit_seconds=0)
    monkeypatch.setattr(
        pf,
        "http_json",
        lambda *args, **kwargs: {"address": {"city": "Cambridge", "country_code": "gb"}},
    )
    assert resolver.resolve(52.2, 0.12) == "Cambridge, GB"
    assert (tmp_path / "places.json").is_file()

    def fail(*args, **kwargs):
        raise RuntimeError("should have been cached")

    monkeypatch.setattr(pf, "http_json", fail)
    assert resolver.resolve(52.2, 0.12) == "Cambridge, GB"


def test_weather_uses_cache(tmp_path, monkeypatch):
    config = pf.load_config(tmp_path / "config.json")
    config["weather"]["location"] = "Cambridge"
    config["weather"]["retries"] = 0
    monkeypatch.setattr(
        pf,
        "geocode_city",
        lambda *args, **kwargs: {
            "latitude": 52.2,
            "longitude": 0.12,
            "name": "Cambridge",
            "country_code": "GB",
            "timezone": "Europe/London",
        },
    )
    monkeypatch.setattr(
        pf,
        "fetch_weather",
        lambda location, units: pf.Weather(
            temperature=10, condition="Clear", icon="clear", is_day=True, high=12, low=4
        ),
    )
    weather = pf.get_weather(config)
    assert weather.temperature == 10
    assert (tmp_path / ".state" / "weather.json").is_file()

    def fail(*args, **kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr(pf, "fetch_weather", fail)
    cached = pf.get_weather(config)
    assert cached.temperature == 10
    assert cached.condition == "Clear"


def test_offline_weather(tmp_path, monkeypatch):
    config = pf.load_config(tmp_path / "config.json")
    config["weather"]["location"] = "Cambridge"
    monkeypatch.setattr(
        pf,
        "geocode_city",
        lambda *args, **kwargs: {
            "latitude": 52.2,
            "longitude": 0.12,
            "name": "Cambridge",
            "country_code": "GB",
            "timezone": "Europe/London",
        },
    )
    assert pf.get_weather(config, offline=True) is None
    # Offline must not touch the network.
    monkeypatch.setattr(pf, "fetch_weather", lambda *args, **kwargs: pytest.fail("network used"))
    assert pf.get_weather(config, offline=True) is None


def test_prepare_images_preserves_exif(tmp_path):
    source = tmp_path / "source.jpg"
    make_photo(source, size=(960, 1600), exif=exif_with_gps())
    output = tmp_path / "out" / "source.jpg"
    assert pfp.convert_image(source, output) == "converted"
    with Image.open(output) as image:
        assert image.size == (480, 800)
        exif = image.getexif()
        assert exif.get(pf.EXIF_DATETIME_ORIGINAL) == "2024:06:14 15:30:00"
        gps = exif.get_ifd(pf.EXIF_GPS_IFD)
        assert gps.get(1) == "N"
        assert float(gps.get(2)[0]) == 52.0


def test_prepare_images_strip_metadata(tmp_path):
    source = tmp_path / "source.jpg"
    make_photo(source, exif=exif_with_gps())
    output = tmp_path / "out" / "source.jpg"
    assert pfp.convert_image(source, output, strip_metadata=True) == "converted"
    with Image.open(output) as image:
        assert not image.getexif().get(pf.EXIF_DATETIME_ORIGINAL)


def test_prepare_images_clears_orientation(tmp_path):
    source = tmp_path / "rotated.jpg"
    make_photo(source, exif=exif_with_gps(orientation=6))
    output = tmp_path / "out" / "rotated.jpg"
    assert pfp.convert_image(source, output) == "converted"
    with Image.open(output) as image:
        assert image.size == (480, 800)
        assert image.getexif().get(274) in (None, 1)
        assert ImageOps.exif_transpose(image).size == (480, 800)


def test_prepare_images_skips_existing(tmp_path):
    source = tmp_path / "source.jpg"
    make_photo(source)
    output = tmp_path / "out" / "source.jpg"
    output.parent.mkdir()
    output.write_bytes(b"existing")
    assert pfp.convert_image(source, output).startswith("skipped")
