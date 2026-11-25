#!/usr/bin/env python3
"""Unified HTTP server for the rp-inky Web Dashboard, Photo Uploader, VPN, Monitor & WoL."""

import argparse
import copy
import hmac
import json
import logging
import mimetypes
import re
import signal
import sys
import threading
import urllib.parse
from contextlib import suppress
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

# Ensure repository root is importable when invoked directly as a script
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from photoframe import photoframe as pf  # noqa: E402
from vpn import manager as vm  # noqa: E402
from webui import monitor as wm  # noqa: E402
from webui import photos as wp  # noqa: E402
from webui import vpn_bridge as wb  # noqa: E402

LOG = logging.getLogger("webui")
STATIC_DIR = Path(__file__).resolve().parent / "static"

DEFAULT_WEBUI_CONFIG: dict[str, Any] = {
    "host": "0.0.0.0",
    "port": 8080,
    "pin": "",
    "photoframe_config": "../photoframe/config.json",
    "vpn_config": "../vpn/config.json",
    "state_dir": ".state",
    "monitor": {
        "enabled": True,
        "interval_seconds": 60,
        "history_size": 60,
        "alert_threshold": 2,
        "targets": [
            {
                "id": "isp-wan",
                "name": "Home ISP Uplink (WAN)",
                "type": "iface_ping",
                "host": "1.1.1.1",
                "interface": "eth0",
                "builtin": True,
            },
            {
                "id": "uk-vpn",
                "name": "Surfshark UK VPN Tunnel",
                "type": "vpn_tunnel",
                "host": "1.1.1.1",
                "builtin": True,
            },
        ],
        "notifications": {
            "telegram": {"enabled": False, "bot_token": "", "chat_id": ""},
            "pushover": {"enabled": False, "app_token": "", "user_key": ""},
            "ntfy": {"enabled": False, "server": "https://ntfy.sh", "topic": ""},
            "webhook": {"enabled": False, "url": ""},
        },
    },
    "wol": {
        "broadcast_ip": "255.255.255.255",
        "port": 9,
        "devices": [],
    },
}


def load_webui_config(config_path: Path) -> dict[str, Any]:
    """Load webui/config.json + optional config.local.json and resolve sibling paths."""
    config_path = Path(config_path).expanduser().resolve()
    raw = vm.read_json(config_path, {})
    if not isinstance(raw, dict):
        raw = {}
    local_path = vm.local_config_path_for(config_path)
    if local_path != config_path and local_path.is_file():
        local_raw = vm.read_json(local_path, {})
        if isinstance(local_raw, dict):
            raw = vm.deep_merge(raw, local_raw)

    cfg = vm.deep_merge(copy.deepcopy(DEFAULT_WEBUI_CONFIG), raw)
    base_dir = config_path.parent
    cfg["_config_path"] = config_path
    cfg["_base_dir"] = base_dir
    cfg["_pf_config_path"] = pf.resolve_path(base_dir, cfg["photoframe_config"]).resolve()
    cfg["_vpn_config_path"] = pf.resolve_path(base_dir, cfg["vpn_config"]).resolve()
    cfg["_state_dir"] = pf.resolve_path(base_dir, cfg["state_dir"]).resolve()
    return cfg


def parse_multipart_form_data(content_type: str, body: bytes) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Parse RFC 7578 multipart/form-data without deprecated cgi module (Python 3.9-3.14+)."""
    match = re.search(r'boundary=(?:"([^"]+)"|([^;\s]+))', content_type, flags=re.IGNORECASE)
    if not match:
        raise ValueError("Missing multipart boundary in Content-Type.")
    boundary_str = match.group(1) or match.group(2)
    delimiter = f"--{boundary_str}".encode()

    fields: dict[str, str] = {}
    files: list[dict[str, Any]] = []

    parts = body.split(delimiter)
    for part in parts[1:]:
        if part.startswith(b"--"):
            break
        if part.startswith(b"\r\n"):
            part = part[2:]
        elif part.startswith(b"\n"):
            part = part[1:]

        if part.endswith(b"\r\n"):
            part = part[:-2]
        elif part.endswith(b"\n"):
            part = part[:-1]

        header_sep = b"\r\n\r\n"
        sep_idx = part.find(header_sep)
        if sep_idx == -1:
            header_sep = b"\n\n"
            sep_idx = part.find(header_sep)
        if sep_idx == -1:
            continue

        raw_headers = part[:sep_idx].decode("utf-8", errors="replace")
        content = part[sep_idx + len(header_sep) :]

        disp = ""
        part_ctype = "application/octet-stream"
        for hline in raw_headers.splitlines():
            if ":" not in hline:
                continue
            hname, _, hval = hline.partition(":")
            if hname.strip().lower() == "content-disposition":
                disp = hval.strip()
            elif hname.strip().lower() == "content-type":
                part_ctype = hval.strip()

        name_match = re.search(r'\bname="([^"]+)"', disp)
        if not name_match:
            continue
        field_name = name_match.group(1)
        fn_match = re.search(r'\bfilename="([^"]*)"', disp)
        if fn_match is not None:
            filename = fn_match.group(1)
            if filename:
                files.append(
                    {
                        "field": field_name,
                        "filename": filename,
                        "content_type": part_ctype,
                        "data": content,
                    }
                )
        else:
            fields[field_name] = content.decode("utf-8", errors="replace")

    return fields, files


class AppContext:
    """Shared application services accessed by HTTP request handlers."""

    def __init__(self, config_path: Path):
        self.config_path = Path(config_path).expanduser().resolve()
        cfg = self.get_config()
        cfg["_state_dir"].mkdir(parents=True, exist_ok=True)
        self.vpn_bridge = wb.VpnBridge(cfg["_vpn_config_path"])
        self.monitor = wm.UptimeMonitor(
            webui_config_getter=self.get_config,
            local_config_updater=self.update_local_config,
            vpn_config_path=cfg["_vpn_config_path"],
            state_dir=cfg["_state_dir"],
        )

    def get_config(self) -> dict[str, Any]:
        return load_webui_config(self.config_path)

    def get_pf_config(self) -> dict[str, Any]:
        return pf.load_config(self.get_config()["_pf_config_path"])

    def update_local_config(self, patch: dict[str, Any]) -> Path:
        return vm.update_local_config(self.config_path, patch)


class DashboardRequestHandler(BaseHTTPRequestHandler):
    """REST API + static SPA handler."""

    server_version = "rp-inky-webui/1.0"

    @property
    def ctx(self) -> AppContext:
        return self.server.app_context  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.debug("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _send_bytes(self, data: bytes, content_type: str, cache_control: str = "no-cache") -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache_control)
        self.end_headers()
        self.wfile.write(data)

    def _check_auth(self) -> bool:
        cfg = self.ctx.get_config()
        expected_pin = str(cfg.get("pin") or "").strip()
        if not expected_pin:
            return True
        provided = (self.headers.get("X-Pin") or "").strip()
        if hmac.compare_digest(provided, expected_pin):
            return True
        self._send_json({"error": "PIN required", "auth_required": True}, status=HTTPStatus.UNAUTHORIZED)
        return False

    def _check_csrf(self) -> bool:
        """Require X-Requested-With header on state-changing API requests to block cross-site form posts."""
        if (self.headers.get("X-Requested-With") or "").lower() == "rp-inky":
            return True
        self._send_json({"error": "Missing X-Requested-With security header"}, status=HTTPStatus.FORBIDDEN)
        return False

    def _read_body(self, max_bytes: int = 80 * 1024 * 1024) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0 or length > max_bytes:
            raise ValueError(f"Request payload too large (max {max_bytes // (1024 * 1024)} MB).")
        return self.rfile.read(length) if length > 0 else b""

    def _read_json_body(self) -> dict[str, Any]:
        raw = self._read_body(max_bytes=1024 * 1024)
        if not raw:
            return {}
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Expected JSON object.")
        return data

    def _serve_static(self, rel_path: str) -> None:
        clean = rel_path.lstrip("/") or "index.html"
        target = (STATIC_DIR / clean).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
            self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)
            return
        ctype, _ = mimetypes.guess_type(target.name)
        self._send_bytes(target.read_bytes(), ctype or "application/octet-stream", cache_control="no-cache")

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        try:
            if not path.startswith("/api/"):
                self._serve_static("index.html" if path == "/" else path)
                return

            if path == "/api/auth/check":
                cfg = self.ctx.get_config()
                has_pin = bool(str(cfg.get("pin") or "").strip())
                self._send_json({"pin_enabled": has_pin})
                return

            if not self._check_auth():
                return

            if path == "/api/photos":
                self._send_json(wp.list_gallery(self.ctx.get_pf_config()))
            elif path.startswith("/api/photos/file/"):
                name = urllib.parse.unquote(path.removeprefix("/api/photos/file/"))
                clean = wp.validate_photo_name(name)
                pf_cfg = self.ctx.get_pf_config()
                file_path = pf_cfg["_image_dir"] / clean
                if not file_path.is_file():
                    self._send_json({"error": "Photo not found"}, status=HTTPStatus.NOT_FOUND)
                    return
                ctype, _ = mimetypes.guess_type(clean)
                self._send_bytes(file_path.read_bytes(), ctype or "image/jpeg", cache_control="public, max-age=60")
            elif path == "/api/frame/preview":
                photo_param = (query.get("photo") or [""])[0] or None
                view_param = (query.get("view") or [""])[0] or None
                png_bytes = wp.render_live_preview_png(self.ctx.get_pf_config(), photo_name=photo_param, view=view_param)
                self._send_bytes(png_bytes, "image/png", cache_control="no-store")
            elif path == "/api/vpn/status":
                refresh = (query.get("refresh") or [""])[0] == "1"
                self._send_json(self.ctx.vpn_bridge.get_status(refresh_exit_ip=refresh))
            elif path == "/api/monitor/status":
                self._send_json(self.ctx.monitor.get_snapshot())
            elif path == "/api/wol/devices":
                check = (query.get("ping") or ["1"])[0] == "1"
                self._send_json(self.ctx.monitor.list_wol_devices(check_awake=check))
            elif path == "/api/wol/scan":
                vpn_cfg = vm.load_vpn_config(self.ctx.get_config()["_vpn_config_path"])
                self._send_json({"neighbors": wm.scan_lan_neighbors(vpn_cfg["wan_interface"])})
            else:
                self._send_json({"error": "Unknown endpoint"}, status=HTTPStatus.NOT_FOUND)
        except (ValueError, FileNotFoundError) as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            LOG.exception("GET %s failed", path)
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path

        if not self._check_auth() or not self._check_csrf():
            return

        try:
            if path == "/api/photos/upload":
                ctype = self.headers.get("Content-Type") or ""
                body = self._read_body(max_bytes=120 * 1024 * 1024)
                fields, files = parse_multipart_form_data(ctype, body)
                if not files:
                    raise ValueError("No image files found in upload.")
                cx = float(fields.get("center_x") or 0.5)
                cy = float(fields.get("center_y") or 0.5)
                location = fields.get("location") or ""
                date_taken = fields.get("date") or ""
                show_now = fields.get("show_now") == "1"

                pf_cfg = self.ctx.get_pf_config()
                saved = []
                for fitem in files:
                    info = wp.save_uploaded_photo(
                        fitem["data"],
                        fitem["filename"],
                        pf_cfg,
                        center_x=cx,
                        center_y=cy,
                        location=location,
                        date_taken=date_taken,
                    )
                    saved.append(info)
                if show_now and saved:
                    wp.trigger_frame_action(pf_cfg, action="show", photo_name=saved[-1]["name"])
                self._send_json({"uploaded": saved, "count": len(saved)})

            elif path == "/api/photos/metadata":
                body_json = self._read_json_body()
                updated = wp.update_photo_sidecar(
                    self.ctx.get_pf_config(),
                    str(body_json.get("name") or ""),
                    location=body_json.get("location"),
                    date_taken=body_json.get("date"),
                )
                self._send_json({"photo": updated})

            elif path == "/api/photos/delete":
                body_json = self._read_json_body()
                wp.delete_photo(self.ctx.get_pf_config(), str(body_json.get("name") or ""))
                self._send_json(wp.list_gallery(self.ctx.get_pf_config()))

            elif path == "/api/frame/action":
                body_json = self._read_json_body()
                result = wp.trigger_frame_action(
                    self.ctx.get_pf_config(),
                    action=str(body_json.get("action") or "next"),
                    photo_name=body_json.get("name"),
                )
                self._send_json(result)

            elif path == "/api/frame/dashboard":
                body_json = self._read_json_body()
                updated = wp.update_dashboard_settings(self.ctx.get_pf_config(), body_json)
                self._send_json({"dashboard": updated})

            elif path == "/api/vpn/switch":
                body_json = self._read_json_body()
                profile = str(body_json.get("profile") or "").strip()
                if not profile:
                    raise ValueError("Profile name is required.")
                self._send_json(self.ctx.vpn_bridge.switch_profile(profile))

            elif path == "/api/vpn/action":
                body_json = self._read_json_body()
                act = str(body_json.get("action") or "").strip()
                self._send_json(self.ctx.vpn_bridge.action(act))

            elif path == "/api/vpn/upload":
                ctype = self.headers.get("Content-Type") or ""
                body = self._read_body(max_bytes=2 * 1024 * 1024)
                fields, files = parse_multipart_form_data(ctype, body)
                if not files:
                    raise ValueError("Choose a .conf or .ovpn file to import.")
                custom_name = fields.get("name") or ""
                status = {}
                for fitem in files:
                    status = self.ctx.vpn_bridge.upload_profile(
                        fitem["filename"],
                        fitem["data"],
                        custom_name=custom_name if len(files) == 1 else "",
                    )
                self._send_json(status)

            elif path == "/api/vpn/benchmark":
                body_json = self._read_json_body()
                mb = int(body_json.get("megabytes") or 25)
                self._send_json(self.ctx.vpn_bridge.start_benchmark(megabytes=mb))

            elif path == "/api/monitor/check":
                self._send_json(self.ctx.monitor.run_once())

            elif path == "/api/monitor/targets/add":
                self._send_json(self.ctx.monitor.add_target(self._read_json_body()))

            elif path == "/api/monitor/targets/delete":
                body_json = self._read_json_body()
                self._send_json(self.ctx.monitor.remove_target(str(body_json.get("id") or "")))

            elif path == "/api/monitor/notifications":
                self._send_json(self.ctx.monitor.update_notifications(self._read_json_body()))

            elif path == "/api/monitor/notifications/test":
                self._send_json(self.ctx.monitor.send_test_alert())

            elif path == "/api/wol/add":
                self._send_json(self.ctx.monitor.add_wol_device(self._read_json_body()))

            elif path == "/api/wol/delete":
                body_json = self._read_json_body()
                self._send_json(self.ctx.monitor.remove_wol_device(str(body_json.get("id") or "")))

            elif path == "/api/wol/wake":
                self._send_json(self.ctx.monitor.wake_device(self._read_json_body()))

            else:
                self._send_json({"error": "Unknown endpoint"}, status=HTTPStatus.NOT_FOUND)
        except (ValueError, FileNotFoundError) as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            LOG.exception("POST %s failed", path)
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="rp-inky Web Dashboard, Photo Uploader, Monitor & Wake-on-LAN")
    parser.add_argument(
        "--config",
        default=str(Path(__file__).resolve().parent / "config.json"),
        help="path to webui/config.json",
    )
    parser.add_argument("--host", help="override bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, help="override bind port (default: 8080)")
    parser.add_argument("--set-pin", metavar="PIN", help="save or clear ('') dashboard access PIN in config.local.json and exit")
    parser.add_argument("--wake", metavar="MAC_OR_NAME", help="send a Wake-on-LAN magic packet from CLI and exit")
    parser.add_argument("--verbose", "-v", action="store_true", help="enable debug logging")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    LOG.setLevel(logging.DEBUG if args.verbose else logging.INFO)

    config_path = Path(args.config).expanduser().resolve()
    if args.set_pin is not None:
        local_path = vm.update_local_config(config_path, {"pin": args.set_pin.strip()})
        state_msg = "enabled" if args.set_pin.strip() else "cleared"
        print(f"Dashboard PIN {state_msg} in {local_path}")
        return 0

    ctx = AppContext(config_path)
    if args.wake:
        target = args.wake.strip()
        cfg = ctx.get_config()
        matched_id = ""
        for dev in cfg["wol"].get("devices") or []:
            if target.lower() in (str(dev.get("name", "")).lower(), str(dev.get("id", "")).lower()):
                matched_id = str(dev["id"])
                break
        payload = {"id": matched_id} if matched_id else {"mac": target}
        res = ctx.monitor.wake_device(payload)
        print(f"Sent Wake-on-LAN magic packet to {res['woken']}")
        return 0

    cfg = ctx.get_config()
    host = args.host or str(cfg.get("host") or "0.0.0.0")
    port = int(args.port or cfg.get("port") or 8080)

    server = ThreadingHTTPServer((host, port), DashboardRequestHandler)
    server.app_context = ctx  # type: ignore[attr-defined]
    ctx.monitor.start()

    def _shutdown(signum: int, _frame: Any) -> None:
        LOG.info("Received signal %s; shutting down web dashboard", signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    for sig in (signal.SIGTERM, signal.SIGINT):
        with suppress(OSError, ValueError):
            signal.signal(sig, _shutdown)

    LOG.info("rp-inky Web Dashboard listening on http://%s:%d", host, port)
    try:
        server.serve_forever()
    finally:
        ctx.monitor.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
