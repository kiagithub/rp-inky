"""Distance-readable (2-3 yards) Spectra 6 e-ink Weather, Rain & Daily Quote/Fact card."""

import math
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from PIL import Image, ImageDraw

WIDTH, HEIGHT = 480, 800

# Pure Spectra 6 hardware pigment RGBs (zero dithering when saturation=0.0)
BLACK = (0, 0, 0)
WHITE = (255, 255, 255)
YELLOW = (255, 255, 0)
RED = (255, 0, 0)
BLUE = (0, 0, 255)
GREEN = (0, 255, 0)

WEEKDAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
MONTHS_UPPER = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]

# Curated, concise (<= 145 chars) fun facts & quotes sized for 26-32px bold type at 2-3 yards.
# Pure ASCII punctuation avoids missing-glyph tofu boxes on fallback fonts.
CURATED_WISDOM: list[dict[str, str]] = [
    {"kind": "fact", "text": "Honey never spoils - sealed jars found in 3,000-year-old Egyptian tombs are still edible.", "source": "Archaeology"},
    {"kind": "quote", "text": "Simplicity is the ultimate sophistication.", "source": "Leonardo da Vinci"},
    {"kind": "fact", "text": "Octopuses have three hearts, blue copper-based blood, and nine brains - one in each arm.", "source": "Marine Biology"},
    {"kind": "quote", "text": "We are what we repeatedly do. Excellence, then, is not an act, but a habit.", "source": "Will Durant"},
    {"kind": "fact", "text": "A day on Venus is longer than a year on Venus - it takes 243 Earth days just to rotate once.", "source": "Astronomy"},
    {"kind": "quote", "text": "In the middle of difficulty lies opportunity.", "source": "Albert Einstein"},
    {"kind": "fact", "text": "Bananas are botanically berries, while strawberries and raspberries are not berries at all.", "source": "Botany"},
    {"kind": "quote", "text": "The best time to plant a tree was twenty years ago. The second best time is now.", "source": "Proverb"},
    {"kind": "fact", "text": "There are more trees on Earth - over three trillion - than stars in the Milky Way galaxy.", "source": "Nature Journal"},
    {"kind": "quote", "text": "Well done is better than well said.", "source": "Benjamin Franklin"},
    {"kind": "fact", "text": "Sharks existed on Earth for roughly 50 million years before the very first trees evolved.", "source": "Paleontology"},
    {"kind": "quote", "text": "Do what you can, with what you have, where you are.", "source": "Theodore Roosevelt"},
    {"kind": "fact", "text": "Light from the Sun takes 8 minutes and 20 seconds to travel across space and reach your eyes.", "source": "Physics"},
    {"kind": "quote", "text": "Not all those who wander are lost.", "source": "J.R.R. Tolkien"},
    {"kind": "fact", "text": "Oxford University is older than the Aztec Empire - teaching began there as early as 1096.", "source": "History"},
    {"kind": "quote", "text": "Everything you can imagine is real.", "source": "Pablo Picasso"},
    {"kind": "fact", "text": "Wombats are the only known animals in the world that produce cube-shaped droppings.", "source": "Zoology"},
    {"kind": "quote", "text": "Stay hungry, stay foolish.", "source": "Whole Earth Catalog"},
    {"kind": "fact", "text": "Sea otters hold hands while sleeping on the water so they do not drift apart in the current.", "source": "Wildlife"},
    {"kind": "quote", "text": "Fortune favors the prepared mind.", "source": "Louis Pasteur"},
    {"kind": "fact", "text": "The Eiffel Tower grows up to 15 cm taller in summer as heat expands its iron frame.", "source": "Engineering"},
    {"kind": "quote", "text": "Knowledge speaks, but wisdom listens.", "source": "Jimi Hendrix"},
    {"kind": "fact", "text": "A bolt of lightning is five times hotter than the surface of the Sun - nearly 30,000°C.", "source": "Meteorology"},
    {"kind": "quote", "text": "It always seems impossible until it is done.", "source": "Nelson Mandela"},
    {"kind": "fact", "text": "Cleopatra lived closer in time to the Apollo Moon landing than to the Great Pyramid's construction.", "source": "History"},
    {"kind": "quote", "text": "Make things as simple as possible, but no simpler.", "source": "Albert Einstein"},
    {"kind": "fact", "text": "Crows can recognize individual human faces and hold grudges - or gratitude - for years.", "source": "Ornithology"},
    {"kind": "quote", "text": "Action is the foundational key to all success.", "source": "Pablo Picasso"},
    {"kind": "fact", "text": "Neutron stars are so dense that a single teaspoon of their matter weighs about a billion tons.", "source": "Astrophysics"},
    {"kind": "quote", "text": "Courage is grace under pressure.", "source": "Ernest Hemingway"},
    {"kind": "fact", "text": "Scotland chose the unicorn as its official national animal back in the late 14th century.", "source": "History"},
    {"kind": "quote", "text": "What you do today can improve all your tomorrows.", "source": "Ralph Marston"},
]


UNICODE_PUNCT_MAP = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201b": "'",
        "\u2032": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u201f": '"',
        "\u00ab": '"',
        "\u00bb": '"',
        "\u2033": '"',
        "\u2013": "-",
        "\u2014": "-",
        "\u2015": "-",
        "\u2212": "-",
        "\u00a0": " ",
    }
)


def clean_display_text(text: str) -> str:
    """Normalize smart quotes, dashes and ellipses to font-safe Latin-1 characters."""
    cleaned = str(text or "").translate(UNICODE_PUNCT_MAP).replace("\u2026", "...")
    # Strip any remaining non-Latin-1 glyphs (e.g. emoji) that render as x-boxes
    return "".join(ch for ch in cleaned if ord(ch) <= 0x00FF)


def select_daily_wisdom(
    config: dict[str, Any],
    state_dir: Path,
    *,
    today: Optional[date] = None,
    offset: int = 0,
    offline: bool = False,
) -> dict[str, str]:
    """Return a concise daily quote or fun fact (custom, cached online, or curated)."""
    dash_cfg = config.get("dashboard") or {}
    custom_text = str(dash_cfg.get("custom_text") or "").strip()
    if custom_text:
        return {
            "kind": str(dash_cfg.get("custom_kind") or "note"),
            "text": custom_text[:200],
            "source": str(dash_cfg.get("custom_source") or "").strip()[:48],
        }

    content_filter = str(dash_cfg.get("content") or "both").lower()
    pool = [
        item
        for item in CURATED_WISDOM
        if content_filter == "both"
        or (content_filter == "facts" and item["kind"] == "fact")
        or (content_filter == "quotes" and item["kind"] == "quote")
    ] or CURATED_WISDOM

    ref_day = today or date.today()

    # Optional online fetch (cached once per calendar day, filtered for distance-readable length)
    if dash_cfg.get("online_wisdom", False) and not offline and offset == 0:
        cached = _fetch_or_load_online_wisdom(state_dir, ref_day, content_filter)
        if cached:
            return cached

    idx = (ref_day.toordinal() + int(offset)) % len(pool)
    return dict(pool[idx])


def _fetch_or_load_online_wisdom(state_dir: Path, ref_day: date, content_filter: str) -> Optional[dict[str, str]]:
    """Fetch a short quote or fun fact once per day, caching it on disk."""
    import json

    from photoframe import photoframe as pf

    cache_path = Path(state_dir) / "daily_wisdom.json"
    day_key = f"{ref_day.isoformat()}:{content_filter}"
    cached = pf.read_json(cache_path, {})
    if isinstance(cached, dict) and cached.get("key") == day_key and isinstance(cached.get("entry"), dict):
        return cached["entry"]

    want_fact = content_filter == "facts" or (content_filter == "both" and ref_day.toordinal() % 2 == 0)
    try:
        if want_fact:
            req = urllib.request.Request(
                "https://uselessfacts.jsph.pl/api/v2/facts/today?language=en",
                headers={"Accept": "application/json", "User-Agent": pf.USER_AGENT},
            )
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            text = str(data.get("text") or "").strip()
            if 20 <= len(text) <= 155:
                entry = {"kind": "fact", "text": text, "source": "Daily Fact"}
                pf.write_json_atomic(cache_path, {"key": day_key, "entry": entry})
                return entry
        else:
            req = urllib.request.Request(
                "https://zenquotes.io/api/today",
                headers={"Accept": "application/json", "User-Agent": pf.USER_AGENT},
            )
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if isinstance(data, list) and data:
                q = str(data[0].get("q") or "").strip()
                a = str(data[0].get("a") or "").strip()
                if 15 <= len(q) <= 155:
                    entry = {"kind": "quote", "text": q, "source": a[:40]}
                    pf.write_json_atomic(cache_path, {"key": day_key, "entry": entry})
                    return entry
    except Exception:
        return None
    return None


def format_hour_label(iso_time: str, use_24h: bool = False) -> str:
    """Format '2026-11-24T15:00' as '3PM' (or '15:00' in 24h mode)."""
    try:
        hour = int(iso_time.split("T", 1)[1].split(":", 1)[0])
    except (IndexError, ValueError):
        return iso_time[-5:]
    if use_24h:
        return f"{hour:02d}:00"
    suffix = "AM" if hour < 12 else "PM"
    hour12 = hour % 12 or 12
    return f"{hour12}{suffix}"


def extract_hourly_slots(
    payload: dict[str, Any],
    *,
    columns: int = 4,
    step_hours: int = 3,
    use_24h: bool = False,
) -> list[dict[str, Any]]:
    """Build upcoming forecast slots, using max rain % across each step window."""
    hourly = payload.get("hourly") if isinstance(payload, dict) else None
    if not isinstance(hourly, dict):
        return []
    times = hourly.get("time") or []
    temps = hourly.get("temperature_2m") or []
    rains = hourly.get("precipitation_probability") or []
    codes = hourly.get("weather_code") or []
    days = hourly.get("is_day") or []
    if not isinstance(times, list) or not times:
        return []

    current = payload.get("current") if isinstance(payload.get("current"), dict) else {}
    current_time = str(current.get("time") or "")
    current_hour_prefix = current_time[:13]  # 'YYYY-MM-DDTHH'

    start_idx = 0
    if current_hour_prefix:
        for idx, t_str in enumerate(times):
            if str(t_str)[:13] > current_hour_prefix:
                start_idx = idx
                break

    slots: list[dict[str, Any]] = []
    step = max(1, int(step_hours))
    total = len(times)

    for col in range(columns):
        idx = start_idx + col * step
        if idx >= total:
            break
        window_end = min(total, idx + step)
        rain_vals = [
            round(float(rains[j]))
            for j in range(idx, window_end)
            if j < len(rains) and rains[j] is not None
        ]
        rain_pct = max(rain_vals) if rain_vals else 0

        temp_val = float(temps[idx]) if idx < len(temps) and temps[idx] is not None else None
        code_val = int(codes[idx]) if idx < len(codes) and codes[idx] is not None else 0
        is_day_val = bool(days[idx]) if idx < len(days) and days[idx] is not None else True

        slots.append(
            {
                "time": str(times[idx]),
                "label": format_hour_label(str(times[idx]), use_24h=use_24h),
                "temp": temp_val,
                "rain": max(0, min(100, rain_pct)),
                "code": code_val,
                "is_day": is_day_val,
            }
        )
    return slots


# ---------------------------------------------------------------------------
# High-contrast 1-bit multi-colour icon & text drawing helpers
# ---------------------------------------------------------------------------


def _hard_mask(size: tuple[int, int], draw_fn: Any) -> Image.Image:
    mask = Image.new("L", size, 0)
    draw_fn(ImageDraw.Draw(mask))
    return mask.point(lambda p: 255 if p >= 128 else 0)


def paste_raindrop(canvas: Image.Image, xy: tuple[int, int], size: int, fill: tuple[int, int, int]) -> None:
    """Draw a smooth tangent teardrop icon with a crisp inner highlight."""
    s = max(16, int(size))
    cx = s * 0.50
    cy = s * 0.65
    r = s * 0.31
    apex_y = s * 0.06
    dist = max(r + 1.0, cy - apex_y)
    sin_a = min(0.95, r / dist)
    cos_a = math.sqrt(max(0.0, 1.0 - sin_a * sin_a))
    tx_left = cx - r * cos_a
    tx_right = cx + r * cos_a
    ty = cy - r * sin_a

    def _drop_body(d: ImageDraw.ImageDraw) -> None:
        d.polygon([(cx, apex_y), (tx_left, ty), (tx_right, ty)], fill=255)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=255)

    def _glint(d: ImageDraw.ImageDraw) -> None:
        gr = max(1.5, s * 0.075)
        gx, gy = cx - r * 0.34, cy + r * 0.14
        d.ellipse([gx - gr, gy - gr, gx + gr, gy + gr], fill=255)

    canvas.paste(fill, xy, _hard_mask((s, s), _drop_body))
    if s >= 20:
        canvas.paste(WHITE, xy, _hard_mask((s, s), _glint))


def paste_multicolor_weather_icon(
    canvas: Image.Image,
    xy: tuple[int, int],
    icon: str,
    is_day: bool,
    size: int,
) -> None:
    """Draw a bold, multi-colour Spectra 6 weather icon engineered for 2-3 yard legibility."""
    s = max(24, int(size))
    x0, y0 = xy
    stroke = max(3, s // 14)

    def _sun(cx: float, cy: float, r: float) -> None:
        inner = r + max(3.0, s * 0.06)
        outer = r + max(6.0, s * 0.18)

        def _rays_and_ring(d: ImageDraw.ImageDraw) -> None:
            for i in range(8):
                ang = math.pi * i / 4.0
                d.line(
                    [
                        cx + inner * math.cos(ang),
                        cy + inner * math.sin(ang),
                        cx + outer * math.cos(ang),
                        cy + outer * math.sin(ang),
                    ],
                    fill=255,
                    width=stroke,
                )
            d.ellipse([cx - r - stroke, cy - r - stroke, cx + r + stroke, cy + r + stroke], fill=255)

        def _core(d: ImageDraw.ImageDraw) -> None:
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=255)

        canvas.paste(BLACK, (x0, y0), _hard_mask((s, s), _rays_and_ring))
        canvas.paste(YELLOW, (x0, y0), _hard_mask((s, s), _core))

    def _cloud(x: float, y: float, w: float) -> None:
        r = w * 0.24
        h = w * 0.62

        def _shape(d: ImageDraw.ImageDraw, pad: float = 0.0) -> None:
            d.ellipse([x - pad, y + h - 2 * r - pad, x + 2 * r + pad, y + h + pad], fill=255)
            d.ellipse([x + w - 2 * r - pad, y + h - 2 * r - pad, x + w + pad, y + h + pad], fill=255)
            d.ellipse([x + w * 0.27 - pad, y - pad, x + w * 0.27 + 2 * r + pad, y + 2 * r + pad], fill=255)
            d.rectangle([x + r, y + h - r - pad, x + w - r, y + h + pad], fill=255)

        # Bold black cloud silhouette with crisp white inner fill for clean contrast
        canvas.paste(BLACK, (x0, y0), _hard_mask((s, s), lambda d: _shape(d, pad=float(stroke))))
        canvas.paste(WHITE, (x0, y0), _hard_mask((s, s), lambda d: _shape(d, pad=-1.0)))

    if icon == "clear":
        if is_day:
            _sun(s * 0.5, s * 0.5, s * 0.21)
        else:
            def _moon(d: ImageDraw.ImageDraw) -> None:
                r = s * 0.30
                d.ellipse([s * 0.5 - r, s * 0.5 - r, s * 0.5 + r, s * 0.5 + r], fill=255)
                cut_x, cut_y = s * 0.64, s * 0.40
                d.ellipse([cut_x - r * 0.88, cut_y - r * 0.88, cut_x + r * 0.88, cut_y + r * 0.88], fill=0)

            canvas.paste(BLACK, (x0, y0), _hard_mask((s, s), _moon))

    elif icon == "partly":
        if is_day:
            _sun(s * 0.34, s * 0.32, s * 0.14)
        else:
            def _small_moon(d: ImageDraw.ImageDraw) -> None:
                mr = s * 0.18
                mcx, mcy = s * 0.34, s * 0.30
                d.ellipse([mcx - mr, mcy - mr, mcx + mr, mcy + mr], fill=255)
                d.ellipse([mcx + mr * 0.45 - mr * 0.85, mcy - mr * 0.3 - mr * 0.85, mcx + mr * 0.45 + mr * 0.85, mcy - mr * 0.3 + mr * 0.85], fill=0)

            canvas.paste(BLACK, (x0, y0), _hard_mask((s, s), _small_moon))
        _cloud(s * 0.22, s * 0.36, s * 0.66)

    elif icon == "cloudy":
        _cloud(s * 0.14, s * 0.24, s * 0.72)

    elif icon == "fog":
        _cloud(s * 0.14, s * 0.12, s * 0.72)

        def _fog_bars(d: ImageDraw.ImageDraw) -> None:
            for i in range(3):
                yy = s * (0.68 + 0.11 * i)
                d.line([s * 0.16, yy, s * 0.84, yy], fill=255, width=stroke)

        canvas.paste(BLACK, (x0, y0), _hard_mask((s, s), _fog_bars))

    elif icon in ("drizzle", "rain"):
        _cloud(s * 0.14, s * 0.08, s * 0.72)
        y_end = s * 0.82 if icon == "drizzle" else s * 0.94

        def _rain_lines(d: ImageDraw.ImageDraw) -> None:
            for i in range(3):
                xx = s * (0.30 + 0.19 * i)
                d.line([xx, s * 0.60, xx - s * 0.06, y_end], fill=255, width=max(3, s // 11))

        canvas.paste(BLUE, (x0, y0), _hard_mask((s, s), _rain_lines))

    elif icon == "snow":
        _cloud(s * 0.14, s * 0.08, s * 0.72)

        def _flakes(d: ImageDraw.ImageDraw) -> None:
            r = max(3, s // 13)
            for i in range(3):
                xx = s * (0.30 + 0.20 * i)
                yy = s * 0.80
                d.ellipse([xx - r, yy - r, xx + r, yy + r], fill=255)

        canvas.paste(BLUE, (x0, y0), _hard_mask((s, s), _flakes))

    elif icon == "thunder":
        _cloud(s * 0.14, s * 0.06, s * 0.72)
        bolt_pts = [
            (s * 0.52, s * 0.50),
            (s * 0.34, s * 0.76),
            (s * 0.47, s * 0.76),
            (s * 0.38, s * 0.97),
            (s * 0.66, s * 0.66),
            (s * 0.53, s * 0.66),
        ]

        def _bolt_outline(d: ImageDraw.ImageDraw) -> None:
            d.polygon(bolt_pts, fill=255)
            d.line([*bolt_pts, bolt_pts[0]], fill=255, width=max(3, stroke))

        canvas.paste(BLACK, (x0, y0), _hard_mask((s, s), _bolt_outline))
        canvas.paste(YELLOW, (x0, y0), _hard_mask((s, s), lambda d: d.polygon(bolt_pts, fill=255)))

    else:
        _cloud(s * 0.14, s * 0.24, s * 0.72)


def wrap_text_lines(text: str, font: Any, max_width: int, text_width_fn: Any) -> list[str]:
    """Greedy word-wrap text so every line fits within max_width."""
    words = text.split()
    if not words:
        return []
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        trial = f"{current} {word}"
        if text_width_fn(font, trial) <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def render_dashboard_frame(
    weather: Any,
    wisdom: dict[str, str],
    config: dict[str, Any],
    *,
    now: Optional[datetime] = None,
) -> Image.Image:
    """Compose a bold 480x800 Hourly Weather + Rain Chance + Daily Quote/Fact card."""
    from photoframe import photoframe as pf

    canvas = Image.new("RGB", (WIDTH, HEIGHT), WHITE)
    draw = ImageDraw.Draw(canvas)

    bold_path = pf._first_existing([config["fonts"].get("bold"), *pf.BOLD_FONT_CANDIDATES])
    f_header_city = pf._load_font(bold_path, 26)
    f_header_date = pf._load_font(bold_path, 23)
    f_hero_temp = pf._load_font(bold_path, 82)
    f_hero_cond = pf._load_font(bold_path, 26)
    f_hero_sub = pf._load_font(bold_path, 24)
    f_col_hour = pf._load_font(bold_path, 26)
    f_col_temp = pf._load_font(bold_path, 38)
    f_col_rain = pf._load_font(bold_path, 26)
    f_badge = pf._load_font(bold_path, 19)
    f_source = pf._load_font(bold_path, 21)

    ref_dt = now or datetime.now()
    unit = "°F" if config["weather"].get("units") == "imperial" else "°C"

    # 1. Header bar (0..62) — solid BLACK with WHITE city & YELLOW weekday/date
    draw.rectangle((0, 0, WIDTH, 62), fill=BLACK)
    city_label = clean_display_text(
        getattr(weather, "location_name", None) or config["weather"].get("location") or "WEATHER"
    ).upper()
    date_label = f"{WEEKDAYS[ref_dt.weekday()]} {ref_dt.day:02d} {MONTHS_UPPER[ref_dt.month - 1]}"

    date_w = pf.text_width(f_header_date, date_label)
    max_city_w = max(120, WIDTH - date_w - 56)
    city_trunc = pf.truncate_text(city_label, f_header_city, max_city_w)
    pf.paste_text(canvas, (20, 17), city_trunc, f_header_city, WHITE)
    pf.paste_text(canvas, (WIDTH - 20 - date_w, 19), date_label, f_header_date, YELLOW)

    # 2. Hero current weather (62..248)
    temp_val = getattr(weather, "temperature", None) if weather else None
    temp_str = f"{round(temp_val)}{unit}" if temp_val is not None else f"--{unit}"
    code_val = getattr(weather, "code", None) if weather else None
    is_day = (getattr(weather, "is_day", True) is not False) if weather else True
    cond_label, icon_key = pf.weather_code_info(code_val) if code_val is not None else ("Forecast", "partly")

    paste_multicolor_weather_icon(canvas, (18, 80), icon_key, is_day, 108)

    hero_x = 140
    pf.paste_text(canvas, (hero_x, 72), temp_str, f_hero_temp, BLACK)
    pf.paste_text(
        canvas,
        (hero_x, 166),
        pf.truncate_text(clean_display_text(cond_label), f_hero_cond, WIDTH - hero_x - 18),
        f_hero_cond,
        BLACK,
    )

    rain_now = getattr(weather, "rain_chance", None) if weather else None
    high_val = getattr(weather, "high", None) if weather else None
    low_val = getattr(weather, "low", None) if weather else None

    sub_x = hero_x
    sub_y = 203
    if rain_now is not None:
        rain_color = RED if rain_now >= 70 else BLUE
        paste_raindrop(canvas, (sub_x, sub_y), 26, rain_color)
        rain_txt = f"{int(rain_now)}%"
        pf.paste_text(canvas, (sub_x + 30, sub_y), rain_txt, f_hero_sub, rain_color)
        sub_x += 30 + pf.text_width(f_hero_sub, rain_txt) + 18

    if high_val is not None and low_val is not None:
        hl_txt = f"H:{round(high_val)}° L:{round(low_val)}°"
        pf.paste_text(
            canvas,
            (sub_x, sub_y),
            pf.truncate_text(hl_txt, f_hero_sub, max(60, WIDTH - sub_x - 16)),
            f_hero_sub,
            BLACK,
        )

    # Thick divider
    draw.rectangle((16, 248, WIDTH - 16, 251), fill=BLACK)

    # 3. 4-Column Hourly Forecast + Rain Chance (252..530) — clean, large icons & droplets (no boxes)
    slots: list[dict[str, Any]] = list(getattr(weather, "hourly", None) or []) if weather else []
    col_count = 4
    col_w = (WIDTH - 24) // col_count  # 114 px each

    if not slots:
        pf.paste_centered(canvas, 372, "Hourly forecast unavailable", f_hero_cond, BLACK)
    else:
        for idx, slot in enumerate(slots[:col_count]):
            cx0 = 12 + idx * col_w
            center_x = cx0 + col_w // 2
            if idx > 0:
                draw.line([(cx0, 266), (cx0, 516)], fill=BLACK, width=1)

            # Time label (e.g. 3PM)
            lbl = clean_display_text(str(slot.get("label") or ""))
            lw = pf.text_width(f_col_hour, lbl)
            pf.paste_text(canvas, (center_x - lw // 2, 266), lbl, f_col_hour, BLACK)

            # Large Weather symbol (72x72)
            _, s_icon = pf.weather_code_info(slot.get("code"))
            paste_multicolor_weather_icon(canvas, (center_x - 36, 306), s_icon, bool(slot.get("is_day", True)), 72)

            # Hourly temp (38px bold)
            stemp = f"{round(slot['temp'])}°" if slot.get("temp") is not None else "--"
            tw = pf.text_width(f_col_temp, stemp)
            pf.paste_text(canvas, (center_x - tw // 2, 394), stemp, f_col_temp, BLACK)

            # Rain droplet + bold percentage (26px)
            rain_pct = int(slot.get("rain") or 0)
            r_txt = f"{rain_pct}%"
            r_col = RED if rain_pct >= 70 else (BLUE if rain_pct >= 20 else BLACK)
            drop_col = RED if rain_pct >= 70 else BLUE
            drop_sz = 26
            group_w = drop_sz + 5 + pf.text_width(f_col_rain, r_txt)
            gx = center_x - group_w // 2
            paste_raindrop(canvas, (gx, 462), drop_sz, drop_col)
            pf.paste_text(canvas, (gx + drop_sz + 5, 461), r_txt, f_col_rain, r_col)

    # Thick divider above Daily Wisdom card
    draw.rectangle((16, 530, WIDTH - 16, 533), fill=BLACK)

    # 4. Daily Fun Fact / Quote Card (536..800)
    kind = str(wisdom.get("kind") or "quote").lower()
    badge_text = "DID YOU KNOW?" if kind == "fact" else ("DAILY NOTE" if kind == "note" else "QUOTE OF THE DAY")
    bw = pf.text_width(f_badge, badge_text) + 24
    bh_text = pf.text_metrics(f_badge, badge_text)[1]
    draw.rectangle((22, 548, 22 + bw, 584), fill=YELLOW, outline=BLACK, width=2)
    pf.paste_text(canvas, (34, 548 + max(2, (36 - bh_text) // 2)), badge_text, f_badge, BLACK)

    raw_body = clean_display_text(str(wisdom.get("text") or "").strip()).strip('"')
    body_text = f'"{raw_body}"' if kind == "quote" and raw_body else raw_body
    source_text = clean_display_text(str(wisdom.get("source") or "").strip())

    max_text_w = WIDTH - 48
    avail_bottom = 750 if source_text else 784
    avail_top = 598
    avail_h = avail_bottom - avail_top

    chosen_font = pf._load_font(bold_path, 24)
    wrapped_lines: list[str] = []
    line_step = 30

    # Auto-pick the largest bold font size (32 -> 28 -> 25 -> 22) that fits cleanly without clipping
    for pt in (32, 29, 26, 23):
        candidate_font = pf._load_font(bold_path, pt)
        lines = wrap_text_lines(body_text, candidate_font, max_text_w, pf.text_width)
        lh = pf.text_metrics(candidate_font, "Ay")[1] + 7
        if len(lines) * lh <= avail_h:
            chosen_font = candidate_font
            wrapped_lines = lines
            line_step = lh
            break
    else:
        chosen_font = pf._load_font(bold_path, 22)
        lh = pf.text_metrics(chosen_font, "Ay")[1] + 6
        max_lines = max(2, avail_h // lh)
        all_lines = wrap_text_lines(body_text, chosen_font, max_text_w, pf.text_width)
        if len(all_lines) > max_lines:
            kept = all_lines[:max_lines]
            kept[-1] = pf.truncate_text(kept[-1], chosen_font, max_text_w)
            wrapped_lines = kept
        else:
            wrapped_lines = all_lines
        line_step = lh

    total_block_h = len(wrapped_lines) * line_step
    start_y = avail_top + max(0, (avail_h - total_block_h) // 2)
    for idx, line in enumerate(wrapped_lines):
        pf.paste_text(canvas, (24, start_y + idx * line_step), line, chosen_font, BLACK)

    if source_text:
        attr = f"- {source_text}"
        attr_trunc = pf.truncate_text(attr, f_source, WIDTH - 48)
        aw = pf.text_width(f_source, attr_trunc)
        pf.paste_text(canvas, (WIDTH - 24 - aw, 758), attr_trunc, f_source, BLUE if kind == "fact" else BLACK)

    return canvas
