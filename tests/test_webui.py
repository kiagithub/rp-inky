"""Offline unit tests for Web Hub: Photo Uploader (A.1) & Watchdog/WoL (B.7)."""

import io
import json

import pytest
from PIL import Image, TiffImagePlugin

from photoframe import photoframe as pf
from webui import monitor as wm
from webui import photos as wp
from webui import server as ws


def _make_jpeg_bytes(size=(1200, 900), color=(40, 120, 200)) -> bytes:
    exif = Image.Exif()
    exif[pf.EXIF_DATETIME_ORIGINAL] = "2025:07:20 14:10:00"
    exif[pf.EXIF_GPS_IFD] = {
        1: "N",
        2: (
            TiffImagePlugin.IFDRational(51, 1),
            TiffImagePlugin.IFDRational(30, 1),
            TiffImagePlugin.IFDRational(0, 1),
        ),
        3: "W",
        4: (
            TiffImagePlugin.IFDRational(0, 1),
            TiffImagePlugin.IFDRational(7, 1),
            TiffImagePlugin.IFDRational(0, 1),
        ),
    }
    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    return buf.getvalue()


def test_save_uploaded_photo_crops_preserves_exif_and_sidecar(tmp_path):
    pf_cfg = pf.load_config(tmp_path / "photoframe" / "config.json")
    raw = _make_jpeg_bytes(size=(1600, 1200))

    info = wp.save_uploaded_photo(
        raw,
        "London Trip!.JPG",
        pf_cfg,
        center_x=0.25,
        center_y=0.75,
        location="London, UK",
        date_taken="2025-07-21",
    )
    assert info["name"] == "London-Trip.jpg"
    assert info["place"] == "London, UK"
    assert info["date"] == "2025-07-21"

    saved_path = pf_cfg["_image_dir"] / "London-Trip.jpg"
    with Image.open(saved_path) as out_img:
        assert out_img.size == (480, 800)
        assert out_img.getexif().get(pf.EXIF_DATETIME_ORIGINAL) == "2025:07:20 14:10:00"

    # Uploading same filename again avoids overwriting by appending -2
    second = wp.save_uploaded_photo(raw, "London Trip!.JPG", pf_cfg)
    assert second["name"] == "London-Trip-2.jpg"


def test_photoframe_choose_next_honors_requested_and_pinned(tmp_path):
    a = tmp_path / "a.jpg"
    b = tmp_path / "b.jpg"
    c = tmp_path / "c.jpg"
    for p in (a, b, c):
        p.write_bytes(b"x")

    assert pf.choose_next([a, b, c], last_name="b.jpg", requested_name="b.jpg") == b
    assert pf.choose_next([a, b, c], last_name="a.jpg", requested_name="b.jpg", pinned_name="c.jpg") == c


def test_parse_multipart_form_data():
    boundary = "----WebKitFormBoundary7MA4YWxkTrZu0gW"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="location"\r\n\r\n'
        "Edinburgh, UK\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="photos"; filename="castle.jpg"\r\n'
        "Content-Type: image/jpeg\r\n\r\n"
        "FAKEJPEGDATA\r\n"
        f"--{boundary}--\r\n"
    ).encode()

    fields, files = ws.parse_multipart_form_data(f"multipart/form-data; boundary={boundary}", body)
    assert fields["location"] == "Edinburgh, UK"
    assert len(files) == 1
    assert files[0]["filename"] == "castle.jpg"
    assert files[0]["data"] == b"FAKEJPEGDATA"


def test_wake_on_lan_magic_packet_and_mac_validation():
    assert wm.normalize_mac("aa-bb-cc-dd-ee-ff") == "AA:BB:CC:DD:EE:FF"
    assert wm.normalize_mac("aabb.ccdd.eeff") == "AA:BB:CC:DD:EE:FF"
    with pytest.raises(ValueError, match="Invalid MAC"):
        wm.normalize_mac("12:34:56")

    pkt = wm.build_magic_packet("01:23:45:67:89:ab")
    assert len(pkt) == 102
    assert pkt[:6] == b"\xff" * 6
    assert pkt[6:12] == bytes.fromhex("0123456789ab")
    assert pkt[-6:] == bytes.fromhex("0123456789ab")


def test_uptime_monitor_alerts_on_down_and_recovery(tmp_path, monkeypatch):
    pf_dir = tmp_path / "photoframe"
    vpn_dir = tmp_path / "vpn"
    webui_dir = tmp_path / "webui"
    pf_dir.mkdir()
    vpn_dir.mkdir()
    webui_dir.mkdir()

    cfg_path = webui_dir / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "monitor": {
                    "alert_threshold": 2,
                    "targets": [{"id": "nas", "name": "Home NAS", "type": "ping", "host": "192.168.86.50"}],
                    "notifications": {"ntfy": {"enabled": True, "server": "https://ntfy.sh", "topic": "test-topic"}},
                }
            }
        ),
        encoding="utf-8",
    )

    sent_alerts = []

    def fake_send(_notif_cfg, title, message):
        sent_alerts.append((title, message))
        return []

    probe_results = [(False, None, "Timeout"), (False, None, "Timeout"), (True, 4.2, "OK")]
    monkeypatch.setattr(wm, "send_notifications", fake_send)
    monkeypatch.setattr(wm, "probe_ping", lambda *a, **kw: probe_results.pop(0))

    ctx = ws.AppContext(cfg_path)
    # Failure 1: below threshold=2 -> no alert yet
    ctx.monitor.run_once()
    assert sent_alerts == []

    # Failure 2: hits threshold=2 -> sends [DOWN] alert
    snap = ctx.monitor.run_once()
    assert len(sent_alerts) == 1
    assert sent_alerts[0][0] == "[DOWN] Home NAS"
    assert snap["targets"][0]["up"] is False

    # Recovery: sends [RECOVERED] alert
    snap = ctx.monitor.run_once()
    assert len(sent_alerts) == 2
    assert sent_alerts[1][0] == "[RECOVERED] Home NAS"
    assert snap["targets"][0]["up"] is True
    assert snap["targets"][0]["uptime_pct"] == pytest.approx(33.33, abs=0.05)


def _dispatch_http(ctx: ws.AppContext, raw_request: bytes) -> tuple[int, bytes]:
    """Drive DashboardRequestHandler over in-memory BytesIO buffers."""

    class DummyServer:
        app_context = ctx

    out_buf = io.BytesIO()

    class MemorySocket:
        def __init__(self, data: bytes):
            self._in = io.BytesIO(data)

        def makefile(self, mode: str, *_args, **_kwargs):
            return self._in if "r" in mode else out_buf

        def sendall(self, data: bytes) -> None:
            out_buf.write(data)

    ws.DashboardRequestHandler(MemorySocket(raw_request), ("127.0.0.1", 12345), DummyServer())  # type: ignore[arg-type]
    raw_resp = out_buf.getvalue()
    head, _, body = raw_resp.partition(b"\r\n\r\n")
    status_line = head.splitlines()[0].decode("utf-8")
    status_code = int(status_line.split()[1])
    return status_code, body


def test_webui_http_server_endpoints_and_csrf_protection(tmp_path):
    for sub in ("photoframe", "vpn", "webui"):
        (tmp_path / sub).mkdir()
    cfg_path = tmp_path / "webui" / "config.json"
    cfg_path.write_text(json.dumps({"port": 8080}), encoding="utf-8")
    ctx = ws.AppContext(cfg_path)

    # Static index.html loads cleanly
    code, body = _dispatch_http(ctx, b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
    assert code == 200
    assert "rp-inky Hub" in body.decode("utf-8")

    # POST without X-Requested-With header is rejected (CSRF protection)
    payload = json.dumps({"name": "Gaming PC", "mac": "aa:bb:cc:dd:ee:11", "ip": "192.168.86.99"}).encode()
    bad_req = (
        b"POST /api/wol/add HTTP/1.1\r\n"
        b"Host: localhost\r\n"
        b"Content-Type: application/json\r\n"
        + f"Content-Length: {len(payload)}\r\n\r\n".encode()
        + payload
    )
    code, _ = _dispatch_http(ctx, bad_req)
    assert code == 403

    # POST with X-Requested-With succeeds
    good_req = (
        b"POST /api/wol/add HTTP/1.1\r\n"
        b"Host: localhost\r\n"
        b"Content-Type: application/json\r\n"
        b"X-Requested-With: rp-inky\r\n"
        + f"Content-Length: {len(payload)}\r\n\r\n".encode()
        + payload
    )
    code, resp_body = _dispatch_http(ctx, good_req)
    assert code == 200
    parsed = json.loads(resp_body.decode("utf-8"))
    assert parsed["devices"][0]["mac"] == "AA:BB:CC:DD:EE:11"


def test_frame_actions_update_preview_and_shuffle_wisdom_immediately(tmp_path, monkeypatch):
    for sub in ("photoframe", "vpn", "webui"):
        (tmp_path / sub).mkdir()
    cfg_path = tmp_path / "webui" / "config.json"
    cfg_path.write_text(json.dumps({"port": 8080}), encoding="utf-8")
    ctx = ws.AppContext(cfg_path)
    pf_cfg = ctx.get_pf_config()

    # Upload two photos
    wp.save_uploaded_photo(_make_jpeg_bytes(color=(200, 30, 30)), "red.jpg", pf_cfg)
    wp.save_uploaded_photo(_make_jpeg_bytes(color=(30, 30, 200)), "blue.jpg", pf_cfg)
    monkeypatch.setattr(wp, "_signal_photoframe_process", lambda: True)
    monkeypatch.setattr(pf, "get_weather", lambda *a, **kw: pf.Weather(temperature=20.0, code=0, is_day=True))

    # 1. Show blue.jpg -> updates state and writes latest_preview.png immediately
    res = wp.trigger_frame_action(pf_cfg, action="show", photo_name="blue.jpg")
    assert res["signaled"] is True
    assert res["next_image"] == "blue.jpg"
    preview_path = pf_cfg["_state_dir"] / "latest_preview.png"
    assert preview_path.is_file()
    preview_1 = preview_path.read_bytes()

    # 2. Next photo -> must switch away from blue.jpg to red.jpg immediately
    res_next = wp.trigger_frame_action(pf_cfg, action="next")
    assert res_next["next_image"] == "red.jpg"
    preview_2 = preview_path.read_bytes()
    assert preview_2 != preview_1

    # 3. Shuffle wisdom -> clears sticky custom_text, increments wisdom_offset, renders dashboard preview
    wp.update_dashboard_settings(pf_cfg, {"custom_text": "Sticky note", "refresh_now": False})
    reloaded = ctx.get_pf_config()
    assert reloaded["dashboard"]["custom_text"] == "Sticky note"

    wp.trigger_frame_action(reloaded, action="shuffle_wisdom")
    after_shuffle = ctx.get_pf_config()
    assert after_shuffle["dashboard"]["custom_text"] == ""
    state = pf.read_frame_state(after_shuffle["_state_dir"] / "state.json")
    assert state["wisdom_offset"] == 1
    assert state["force_view"] == "dashboard"
    preview_dash_1 = preview_path.read_bytes()

    wp.trigger_frame_action(after_shuffle, action="shuffle_wisdom")
    preview_dash_2 = preview_path.read_bytes()
    assert preview_dash_2 != preview_dash_1

