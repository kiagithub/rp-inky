"""Photo gallery, EXIF-preserving mobile crop uploader, sidecar metadata & frame trigger."""

import io
import logging
import os
import re
import shutil
import signal
import subprocess
from contextlib import suppress
from pathlib import Path
from typing import Any, Optional

from PIL import Image, ImageOps

from photoframe import dashboard as pfd
from photoframe import photoframe as pf
from vpn import manager as vm

LOG = logging.getLogger("webui.photos")

SAFE_STEM_RE = re.compile(r"[^a-zA-Z0-9_-]+")
MAX_UPLOAD_BYTES = 35 * 1024 * 1024  # 35 MB per photo


def _exif_bytes(image: Image.Image) -> Optional[bytes]:
    """Return raw EXIF bytes from an opened Pillow image, or None."""
    try:
        data = image.getexif().tobytes()
    except (AttributeError, OSError, ValueError):
        return None
    return data or None


def sanitize_stem(filename: str) -> str:
    """Build a clean, filesystem-safe stem from an uploaded filename."""
    raw_stem = Path(filename).stem
    cleaned = SAFE_STEM_RE.sub("-", raw_stem).strip("-_")
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    return (cleaned or "photo")[:60]


def validate_photo_name(name: str) -> str:
    """Validate an existing photo filename in image_dir (prevents path traversal)."""
    clean = Path(name).name
    if not clean or clean != name or clean.startswith("."):
        raise ValueError(f"Invalid photo filename: {name!r}")
    if Path(clean).suffix.lower() not in pf.IMAGE_EXTENSIONS:
        raise ValueError(f"Unsupported image extension: {clean}")
    return clean


def save_uploaded_photo(
    raw_bytes: bytes,
    original_filename: str,
    pf_config: dict[str, Any],
    *,
    center_x: float = 0.5,
    center_y: float = 0.5,
    location: str = "",
    date_taken: str = "",
    overwrite: bool = False,
) -> dict[str, Any]:
    """Crop an uploaded photo to 480x800 JPEG preserving EXIF, and update optional sidecar."""
    if not raw_bytes:
        raise ValueError("Empty image upload.")
    if len(raw_bytes) > MAX_UPLOAD_BYTES:
        raise ValueError("Photo exceeds maximum upload size (35 MB).")

    cx = min(1.0, max(0.0, float(center_x)))
    cy = min(1.0, max(0.0, float(center_y)))

    image_dir: Path = pf_config["_image_dir"]
    image_dir.mkdir(parents=True, exist_ok=True)

    stem = sanitize_stem(original_filename)
    target = image_dir / f"{stem}.jpg"
    if not overwrite:
        counter = 2
        while target.exists():
            target = image_dir / f"{stem}-{counter}.jpg"
            counter += 1

    try:
        with Image.open(io.BytesIO(raw_bytes)) as src:
            transposed = ImageOps.exif_transpose(src)
            exif = _exif_bytes(transposed)
            if transposed.mode in ("RGBA", "LA") or (transposed.mode == "P" and "transparency" in transposed.info):
                rgba = transposed.convert("RGBA")
                bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
                transposed = Image.alpha_composite(bg, rgba)
            rgb = transposed.convert("RGB")
            fitted = ImageOps.fit(rgb, (pf.WIDTH, pf.HEIGHT), method=pf.LANCZOS, centering=(cx, cy))

            tmp_target = target.with_name(f".{target.name}.tmp.{os.getpid()}")
            save_opts: dict[str, Any] = {"quality": 92, "optimize": True}
            if exif:
                save_opts["exif"] = exif
            fitted.save(tmp_target, "JPEG", **save_opts)
            os.replace(tmp_target, target)
    except Exception as exc:
        suffix = Path(original_filename).suffix.lower()
        if suffix in (".heic", ".heif"):
            raise ValueError("HEIC photo could not be decoded; install pillow-heif in the virtualenv.") from exc
        raise ValueError(f"Could not process image '{original_filename}': {exc}") from exc

    if location.strip() or date_taken.strip():
        update_photo_sidecar(pf_config, target.name, location=location, date_taken=date_taken)

    return describe_photo(target, pf_config)


def update_photo_sidecar(
    pf_config: dict[str, Any],
    photo_name: str,
    *,
    location: Optional[str] = None,
    date_taken: Optional[str] = None,
) -> dict[str, Any]:
    """Create or update sidecar metadata (photoframe/image/metadata.json) for photo_name."""
    clean_name = validate_photo_name(photo_name)
    sidecar_path = pf_config.get("_sidecar") or (pf_config["_image_dir"] / "metadata.json")
    sidecar_path = Path(sidecar_path)
    data = pf.read_json(sidecar_path, {})
    if not isinstance(data, dict):
        data = {}

    entry = data.get(clean_name)
    if not isinstance(entry, dict):
        entry = {}

    if location is not None:
        loc_clean = location.strip()
        if loc_clean:
            entry["location"] = loc_clean[:120]
        else:
            entry.pop("location", None)

    if date_taken is not None:
        date_clean = date_taken.strip()
        if date_clean:
            parsed = pf.parse_exif_date(date_clean)
            if parsed is None:
                raise ValueError("Date must be in YYYY-MM-DD format.")
            entry["date"] = parsed.isoformat()
        else:
            entry.pop("date", None)

    if entry:
        data[clean_name] = entry
    else:
        data.pop(clean_name, None)

    pf.write_json_atomic(sidecar_path, data)
    target = pf_config["_image_dir"] / clean_name
    state = pf.read_frame_state(pf_config["_state_dir"] / "state.json")
    if target.is_file() and state.get("last_image") == clean_name and state.get("last_view") != "dashboard":
        _write_preview_cache(pf_config, view="photo", photo_name=clean_name)
        _signal_photoframe_process()
    return describe_photo(target, pf_config) if target.is_file() else {"name": clean_name, **entry}


def delete_photo(pf_config: dict[str, Any], photo_name: str) -> None:
    """Delete a photo from image_dir and clean up its sidecar/pinned references."""
    clean_name = validate_photo_name(photo_name)
    target = pf_config["_image_dir"] / clean_name
    if not target.is_file():
        raise FileNotFoundError(f"Photo not found: {clean_name}")
    target.unlink()

    sidecar_path = pf_config.get("_sidecar")
    if sidecar_path and Path(sidecar_path).is_file():
        data = pf.read_json(sidecar_path, {})
        if isinstance(data, dict) and clean_name in data:
            data.pop(clean_name, None)
            pf.write_json_atomic(sidecar_path, data)

    state_path = pf_config["_state_dir"] / "state.json"
    state = pf.read_frame_state(state_path)
    changed = False
    for key in ("next_image", "pinned_image"):
        if state.get(key) == clean_name:
            state.pop(key, None)
            changed = True
    if changed:
        pf.write_json_atomic(state_path, state)


def describe_photo(path: Path, pf_config: dict[str, Any]) -> dict[str, Any]:
    """Return metadata summary for one photo using cached reverse-geocoding (no slow HTTP)."""
    resolver = pf.PlaceResolver(pf_config["_state_dir"], enabled=True, offline=True)
    meta = pf.build_photo_meta(path, pf_config, resolver)
    sidecar = pf.load_sidecar(pf_config, path)
    state = pf.read_frame_state(pf_config["_state_dir"] / "state.json")
    stat_res = path.stat()
    return {
        "name": path.name,
        "size_bytes": stat_res.st_size,
        "modified_at": int(stat_res.st_mtime),
        "place": meta.place or "",
        "date": meta.taken.isoformat() if meta.taken else "",
        "date_display": pf.format_date(meta.taken) if meta.taken else "",
        "sidecar_location": str(sidecar.get("location") or ""),
        "sidecar_date": str(sidecar.get("date") or ""),
        "is_current": state.get("last_image") == path.name,
        "is_pinned": state.get("pinned_image") == path.name,
    }


def list_gallery(pf_config: dict[str, Any]) -> dict[str, Any]:
    """Return all photos (newest first) plus current/pinned frame state."""
    images = pf.list_images(pf_config["_image_dir"])
    state = pf.read_frame_state(pf_config["_state_dir"] / "state.json")
    items = [describe_photo(path, pf_config) for path in images]
    items.sort(key=lambda item: (item["modified_at"], item["name"]), reverse=True)
    return {
        "count": len(items),
        "current_image": state.get("last_image") or "",
        "pinned_image": state.get("pinned_image") or "",
        "last_view": state.get("last_view") or "photo",
        "refresh_minutes": pf_config.get("refresh_minutes", 20),
        "weather_location": pf_config["weather"].get("location") or "",
        "dashboard": pf_config.get("dashboard") or {},
        "photos": items,
    }


def render_live_preview_png(
    pf_config: dict[str, Any],
    photo_name: Optional[str] = None,
    view: Optional[str] = None,
) -> bytes:
    """Return PNG bytes for the live e-ink frame preview (photo or Weather+Quote card)."""
    state_dir: Path = pf_config["_state_dir"]
    state = pf.read_frame_state(state_dir / "state.json")

    if view == "dashboard":
        weather = pf.get_weather(pf_config, offline=False)
        offset = int(state.get("wisdom_offset") or 0)
        wisdom = pfd.select_daily_wisdom(pf_config, state_dir, offset=offset, offline=True)
        frame = pfd.render_dashboard_frame(weather, wisdom, pf_config)
        buf = io.BytesIO()
        frame.save(buf, "PNG")
        return buf.getvalue()

    cached_preview = state_dir / "latest_preview.png"
    if not photo_name and cached_preview.is_file():
        return cached_preview.read_bytes()

    images = pf.list_images(pf_config["_image_dir"])
    if not photo_name and pf.should_show_dashboard(pf_config, state, images_available=bool(images)):
        weather = pf.get_weather(pf_config, offline=True)
        offset = int(state.get("wisdom_offset") or 0)
        wisdom = pfd.select_daily_wisdom(pf_config, state_dir, offset=offset, offline=True)
        frame = pfd.render_dashboard_frame(weather, wisdom, pf_config)
        buf = io.BytesIO()
        frame.save(buf, "PNG")
        return buf.getvalue()

    chosen = None
    if photo_name:
        clean = validate_photo_name(photo_name)
        candidate = pf_config["_image_dir"] / clean
        if candidate.is_file():
            chosen = candidate
    if chosen is None:
        chosen = pf.choose_next(
            images,
            last_name=None,
            requested_name=state.get("last_image"),
            pinned_name=state.get("pinned_image"),
        )

    fonts = pf.load_fonts(pf_config)
    resolver = pf.PlaceResolver(state_dir, enabled=True, offline=True)
    meta = pf.build_photo_meta(chosen, pf_config, resolver) if chosen else pf.PhotoMeta()
    weather = pf.get_weather(pf_config, offline=True)
    frame = pf.render_frame(chosen, meta, weather, pf_config, fonts)
    buf = io.BytesIO()
    frame.save(buf, "PNG")
    return buf.getvalue()


def _write_preview_cache(pf_config: dict[str, Any], *, view: str, photo_name: Optional[str] = None) -> None:
    """Immediately render and cache latest_preview.png so the Web UI updates without waiting 32s."""
    state_dir: Path = pf_config["_state_dir"]
    state = pf.read_frame_state(state_dir / "state.json")
    weather = pf.get_weather(pf_config, offline=True)
    if weather is None or (view == "dashboard" and not getattr(weather, "hourly", None)):
        weather = pf.get_weather(pf_config, offline=False)

    if view == "dashboard":
        offset = int(state.get("wisdom_offset") or 0)
        wisdom = pfd.select_daily_wisdom(pf_config, state_dir, offset=offset, offline=True)
        frame = pfd.render_dashboard_frame(weather, wisdom, pf_config)
    else:
        images = pf.list_images(pf_config["_image_dir"])
        chosen = None
        if photo_name:
            candidate = pf_config["_image_dir"] / photo_name
            if candidate.is_file():
                chosen = candidate
        if chosen is None:
            chosen = pf.choose_next(
                images,
                last_name=None,
                requested_name=state.get("next_image") or state.get("last_image"),
                pinned_name=state.get("pinned_image"),
            )
        fonts = pf.load_fonts(pf_config)
        resolver = pf.PlaceResolver(state_dir, enabled=True, offline=True)
        meta = pf.build_photo_meta(chosen, pf_config, resolver) if chosen else pf.PhotoMeta()
        frame = pf.render_frame(chosen, meta, weather, pf_config, fonts)

    with suppress(OSError):
        pf._save_latest_preview(state_dir, frame)


def trigger_frame_action(
    pf_config: dict[str, Any],
    *,
    action: str = "next",
    photo_name: Optional[str] = None,
) -> dict[str, Any]:
    """Queue a photo/dashboard/pin state change, update preview immediately, and signal panel."""
    state_path = pf_config["_state_dir"] / "state.json"
    state = pf.read_frame_state(state_path)
    target_view = "photo"
    chosen_photo: Optional[str] = None

    if action == "show" and photo_name:
        clean = validate_photo_name(photo_name)
        if not (pf_config["_image_dir"] / clean).is_file():
            raise FileNotFoundError(f"Photo not found: {clean}")
        state.pop("pinned_image", None)
        state["next_image"] = clean
        state["force_view"] = "photo"
        state["last_image"] = clean
        state["last_view"] = "photo"
        chosen_photo = clean
    elif action == "pin" and photo_name:
        clean = validate_photo_name(photo_name)
        if not (pf_config["_image_dir"] / clean).is_file():
            raise FileNotFoundError(f"Photo not found: {clean}")
        state["pinned_image"] = clean
        state["next_image"] = clean
        state["force_view"] = "photo"
        state["last_image"] = clean
        state["last_view"] = "photo"
        chosen_photo = clean
    elif action == "unpin":
        state.pop("pinned_image", None)
        pf.write_json_atomic(state_path, state)
        return {"signaled": True, "pinned_image": "", "next_image": "", "force_view": ""}
    elif action == "next":
        state.pop("pinned_image", None)
        images = pf.list_images(pf_config["_image_dir"])
        picked = pf.choose_next(images, last_name=state.get("last_image"))
        if picked is not None:
            state["next_image"] = picked.name
            state["last_image"] = picked.name
            chosen_photo = picked.name
        state["force_view"] = "photo"
        state["last_view"] = "photo"
    elif action == "dashboard":
        state.pop("pinned_image", None)
        state.pop("next_image", None)
        state["force_view"] = "dashboard"
        state["last_view"] = "dashboard"
        target_view = "dashboard"
    elif action == "shuffle_wisdom":
        # Clear any sticky custom_text override so shuffling always advances to a new fact/quote
        if (pf_config.get("dashboard") or {}).get("custom_text"):
            vm.update_local_config(pf_config["_config_path"], {"dashboard": {"custom_text": ""}})
            pf_config = pf.load_config(pf_config["_config_path"])
        state.pop("pinned_image", None)
        state.pop("next_image", None)
        state["wisdom_offset"] = int(state.get("wisdom_offset") or 0) + 1
        state["force_view"] = "dashboard"
        state["last_view"] = "dashboard"
        target_view = "dashboard"
    else:
        raise ValueError(f"Unsupported frame action: {action}")

    pf.write_json_atomic(state_path, state)
    _write_preview_cache(pf_config, view=target_view, photo_name=chosen_photo)
    signaled = _signal_photoframe_process()
    return {
        "signaled": signaled,
        "pinned_image": state.get("pinned_image") or "",
        "next_image": state.get("next_image") or "",
        "force_view": state.get("force_view") or "",
        "last_view": state.get("last_view") or target_view,
    }


def update_dashboard_settings(pf_config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Persist screen mode, schedule & quote/fact preferences in photoframe/config.local.json."""
    patch: dict[str, Any] = {}
    if "mode" in payload:
        mode = str(payload["mode"]).strip().lower()
        if mode not in ("photos", "dashboard", "alternate", "schedule"):
            raise ValueError(f"Invalid mode: {mode}")
        patch["mode"] = mode
    if "schedule_hours" in payload:
        sched = str(payload["schedule_hours"]).strip()
        if sched and not re.match(r"^\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}$", sched):
            raise ValueError("Schedule hours must look like '06:30-10:00'.")
        patch["schedule_hours"] = sched or "06:30-10:00"
    if "content" in payload:
        content = str(payload["content"]).strip().lower()
        if content in ("both", "facts", "quotes"):
            patch["content"] = content
    if "use_24h" in payload:
        patch["use_24h"] = bool(payload["use_24h"])
    if "online_wisdom" in payload:
        patch["online_wisdom"] = bool(payload["online_wisdom"])
    if "custom_text" in payload:
        patch["custom_text"] = str(payload["custom_text"]).strip()[:300]
    if "custom_source" in payload:
        patch["custom_source"] = str(payload["custom_source"]).strip()[:48]

    vm.update_local_config(pf_config["_config_path"], {"dashboard": patch})
    reloaded = pf.load_config(pf_config["_config_path"])

    state_path = reloaded["_state_dir"] / "state.json"
    state = pf.read_frame_state(state_path)
    new_mode = reloaded["dashboard"]["mode"]
    show_dash = new_mode == "dashboard" or (
        new_mode != "photos" and bool(patch.get("custom_text") or "content" in patch)
    )
    if show_dash:
        state.pop("pinned_image", None)
        state.pop("next_image", None)
        state["force_view"] = "dashboard"
        state["last_view"] = "dashboard"
        pf.write_json_atomic(state_path, state)
        _write_preview_cache(reloaded, view="dashboard")
    elif new_mode == "photos":
        state["force_view"] = "photo"
        state["last_view"] = "photo"
        pf.write_json_atomic(state_path, state)
        _write_preview_cache(reloaded, view="photo")

    if payload.get("refresh_now", True):
        _signal_photoframe_process()
    return reloaded["dashboard"]


def _signal_photoframe_process() -> bool:
    """Wake or restart inky-photoframe so the physical e-ink screen updates immediately."""
    # 1. Prefer systemd USR1 when inky-photoframe.service is active
    if shutil.which("systemctl"):
        active = subprocess.run(
            ["systemctl", "is-active", "--quiet", "inky-photoframe.service"],
            capture_output=True,
            check=False,
        )
        if active.returncode == 0:
            # Try direct signal if running as same user via MainPID
            pid_res = subprocess.run(
                ["systemctl", "show", "-p", "MainPID", "--value", "inky-photoframe.service"],
                capture_output=True,
                text=True,
                check=False,
            )
            if pid_res.returncode == 0 and hasattr(signal, "SIGUSR1"):
                with suppress(ValueError, OSError):
                    main_pid = int(pid_res.stdout.strip())
                    if main_pid > 1:
                        os.kill(main_pid, signal.SIGUSR1)
                        return True
            kill_cmd = (
                ["systemctl", "kill", "-s", "USR1", "--kill-who=main", "inky-photoframe.service"]
                if os.geteuid() == 0
                else ["sudo", "-n", "systemctl", "kill", "-s", "USR1", "--kill-who=main", "inky-photoframe.service"]
            )
            if subprocess.run(kill_cmd, capture_output=True, check=False).returncode == 0:
                return True
            restart_cmd = (
                ["systemctl", "restart", "inky-photoframe.service"]
                if os.geteuid() == 0
                else ["sudo", "-n", "systemctl", "restart", "inky-photoframe.service"]
            )
            if subprocess.run(restart_cmd, capture_output=True, check=False).returncode == 0:
                return True
        else:
            # Service exists on Pi but was stopped — start it so screen updates!
            start_cmd = (
                ["systemctl", "restart", "inky-photoframe.service"]
                if os.geteuid() == 0
                else ["sudo", "-n", "systemctl", "restart", "inky-photoframe.service"]
            )
            if subprocess.run(start_cmd, capture_output=True, check=False).returncode == 0:
                return True

    # 2. Fallback for manual non-systemd runs: signal photoframe.py by PID
    if hasattr(signal, "SIGUSR1") and shutil.which("pgrep"):
        res = subprocess.run(["pgrep", "-f", "photoframe.py"], capture_output=True, text=True, check=False)
        if res.returncode == 0:
            sent = False
            for line in res.stdout.splitlines():
                with suppress(ValueError, OSError):
                    pid = int(line.strip())
                    if pid != os.getpid():
                        os.kill(pid, signal.SIGUSR1)
                        sent = True
            if sent:
                return True
    return False
