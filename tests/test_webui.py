"""Offline unit tests for Web Hub: Photo Uploader (A.1) & Watchdog/WoL (B.7)."""

import io
import json

import pytest
from PIL import Image, TiffImagePlugin

from photoframe import photoframe as pf
from webui import monitor as wm
from webui import network as wn
from webui import photos as wp
from webui import server as ws
from webui import system_health as sh


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


def test_network_parsers_oui_and_category_inference(tmp_path):
    iw_raw = """Station aa:bb:cc:dd:ee:01 (on wlan0)
\tinactive time:\t120 ms
\trx bytes:\t2000000
\ttx bytes:\t15000000
\tsignal:\t\t-54 dBm
\ttx bitrate:\t433.3 MBit/s VHT-MCS 9 80MHz
\tconnected time:\t3660 seconds
"""
    stations = wn.parse_iw_station_dump(iw_raw)
    assert "AA:BB:CC:DD:EE:01" in stations
    assert stations["AA:BB:CC:DD:EE:01"]["tx_bytes"] == 15000000
    assert stations["AA:BB:CC:DD:EE:01"]["rx_bytes"] == 2000000
    assert stations["AA:BB:CC:DD:EE:01"]["signal_dbm"] == -54
    assert stations["AA:BB:CC:DD:EE:01"]["connected_seconds"] == 3660

    lease_file = tmp_path / "dnsmasq.leases"
    lease_file.write_text("1799999999 aa:bb:cc:dd:ee:01 10.66.0.25 Kia-iPhone *\n", encoding="utf-8")
    leases = wn.parse_dnsmasq_leases((lease_file,))
    assert leases["AA:BB:CC:DD:EE:01"] == {"ip": "10.66.0.25", "hostname": "Kia-iPhone"}

    avahi_raw = (
        "=;eth0;IPv4;Living\\032Room\\032TV;_airplay._tcp;local;LGwebOSTV.local;192.168.86.44;7000;\n"
        "=;eth0;IPv4;Desk\\032Plug;_hap._tcp;local;kasa-plug.local;192.168.86.52;80;\n"
    )
    mdns = wn.parse_avahi_browse(avahi_raw)
    assert mdns["192.168.86.44"]["friendly"] == "Living Room TV"
    assert "_airplay._tcp" in mdns["192.168.86.44"]["services"]
    assert mdns["192.168.86.52"]["hostname"] == "kasa-plug"

    # OUI vendor + Private MAC detection
    vendor_pi, priv_pi = wn.lookup_mac_vendor("dc:a6:32:11:22:33")
    assert vendor_pi == "Raspberry Pi"
    assert priv_pi is False

    vendor_priv, is_priv = wn.lookup_mac_vendor("da:12:34:56:78:9a")
    assert is_priv is True
    assert "Private" in vendor_priv

    # Device category inference
    assert wn.infer_device_category(hostname="Kia-iPhone", is_private_mac=True) == "phone"
    assert wn.infer_device_category(hostname="LGwebOSTV", services=["_airplay._tcp"]) == "tv"
    assert wn.infer_device_category(vendor="TP-Link Kasa") == "smart_plug"
    assert wn.infer_device_category(vendor="Sonos") == "speaker"
    assert wn.infer_device_category(is_gateway=True) == "router"


def test_network_scanner_bandwidth_and_alias_endpoints(tmp_path, monkeypatch):
    for sub in ("photoframe", "vpn", "webui"):
        (tmp_path / sub).mkdir()
    cfg_path = tmp_path / "webui" / "config.json"
    cfg_path.write_text(json.dumps({"port": 8080}), encoding="utf-8")
    ctx = ws.AppContext(cfg_path)

    # Mock hotspot station dump + LAN neighbors + monotonic clock for live Mbps delta
    sample_idx = {"n": 0}
    iw_outputs = [
        "Station 50:c7:bf:11:22:33 (on wlan0)\n\trx bytes:\t1000000\n\ttx bytes:\t5000000\n\tsignal:\t-48 dBm\n\tconnected time:\t120 seconds\n",
        "Station 50:c7:bf:11:22:33 (on wlan0)\n\trx bytes:\t2000000\n\ttx bytes:\t15000000\n\tsignal:\t-48 dBm\n\tconnected time:\t125 seconds\n",
    ]
    clock = {"t": 1000.0}

    class FakeProc:
        def __init__(self, stdout):
            self.returncode = 0
            self.stdout = stdout
            self.stderr = ""

    def fake_run(cmd, **_kwargs):
        if cmd[:3] == ["iw", "dev", "wlan0"]:
            idx = min(sample_idx["n"], len(iw_outputs) - 1)
            return FakeProc(iw_outputs[idx])
        return FakeProc("")

    monkeypatch.setattr(wn.vm, "interface_exists", lambda iface: iface in ("eth0", "wlan0"))
    monkeypatch.setattr(wn.vm, "get_interface_ipv4", lambda iface: "192.168.86.30" if iface == "eth0" else None)
    monkeypatch.setattr(wn.shutil, "which", lambda name: f"/usr/bin/{name}" if name == "iw" else None)
    monkeypatch.setattr(wn.subprocess, "run", fake_run)
    monkeypatch.setattr(wn, "parse_dnsmasq_leases", lambda *a, **kw: {"50:C7:BF:11:22:33": {"ip": "10.66.0.15", "hostname": "living-room-tv"}})
    monkeypatch.setattr(wn.wm, "scan_lan_neighbors", lambda iface: [{"ip": "192.168.86.1", "mac": "30:FD:38:00:00:01", "state": "REACHABLE"}] if iface == "eth0" else [])
    monkeypatch.setattr(wn, "get_default_gateway", lambda _iface="": "192.168.86.1")
    monkeypatch.setattr(wn, "get_interface_mac", lambda _iface="": "DC:A6:32:AA:BB:CC")
    monkeypatch.setattr(wn, "reverse_dns_lookup", lambda _ip: "")
    monkeypatch.setattr(wn, "discover_mdns_hosts", lambda **_kw: {})
    monkeypatch.setattr(wn.time, "monotonic", lambda: clock["t"])

    # First scan establishes baseline byte counters
    snap1 = ctx.network.scan(active_sweep=False)
    assert snap1["summary"]["hotspot_devices"] == 1
    hs_dev1 = next(d for d in snap1["devices"] if d["mac"] == "50:C7:BF:11:22:33")
    assert hs_dev1["bandwidth_available"] is True
    assert hs_dev1["download_mbps"] is None

    # Second scan 5s later: +10,000,000 bytes DL = 16.0 Mbps, +1,000,000 bytes UL = 1.6 Mbps
    sample_idx["n"] = 1
    clock["t"] = 1005.0
    snap2 = ctx.network.scan(active_sweep=False)
    hs_dev2 = next(d for d in snap2["devices"] if d["mac"] == "50:C7:BF:11:22:33")
    assert hs_dev2["download_mbps"] == pytest.approx(16.0, abs=0.05)
    assert hs_dev2["upload_mbps"] == pytest.approx(1.6, abs=0.05)
    lan_gw = next(d for d in snap2["devices"] if d["mac"] == "30:FD:38:00:00:01")
    assert lan_gw["category"] == "router"
    assert lan_gw["bandwidth_available"] is False

    # Rename & re-categorize via POST /api/network/alias
    alias_payload = json.dumps({"mac": "50:c7:bf:11:22:33", "name": "Basement Smart Plug", "category": "smart_plug"}).encode()
    req = (
        b"POST /api/network/alias HTTP/1.1\r\n"
        b"Host: localhost\r\n"
        b"Content-Type: application/json\r\n"
        b"X-Requested-With: rp-inky\r\n"
        + f"Content-Length: {len(alias_payload)}\r\n\r\n".encode()
        + alias_payload
    )
    code, body = _dispatch_http(ctx, req)
    assert code == 200
    resp_data = json.loads(body.decode("utf-8"))
    renamed = next(d for d in resp_data["devices"] if d["mac"] == "50:C7:BF:11:22:33")
    assert renamed["name"] == "Basement Smart Plug"
    assert renamed["category"] == "smart_plug"
    assert renamed["icon"] == "🔌"


def test_discover_mdns_hosts_handles_non_utf8_txt_records(monkeypatch):
    class RawBytesProc:
        returncode = 0
        stdout = (
            b"=;eth0;IPv4;Living\\032Room\\032TV;_airplay._tcp;local;LGwebOSTV.local;192.168.86.44;7000;\"pk=\xff\xfe\x80\"\n"
        )

    monkeypatch.setattr(wn.shutil, "which", lambda name: "/usr/bin/avahi-browse" if name == "avahi-browse" else None)
    monkeypatch.setattr(wn.subprocess, "run", lambda *a, **kw: RawBytesProc())

    parsed = wn.discover_mdns_hosts()
    assert parsed["192.168.86.44"]["friendly"] == "Living Room TV"
    assert "_airplay._tcp" in parsed["192.168.86.44"]["services"]


def test_new_device_alerts_baseline_and_trust_workflow(tmp_path, monkeypatch):
    for sub in ("photoframe", "vpn", "webui"):
        (tmp_path / sub).mkdir()
    cfg_path = tmp_path / "webui" / "config.json"
    cfg_path.write_text(json.dumps({"port": 8080}), encoding="utf-8")

    pushed_alerts = []
    monkeypatch.setattr(wm, "send_notifications", lambda _cfg, title, msg: (pushed_alerts.append((title, msg)), [])[1])
    monkeypatch.setattr(wn.vm, "interface_exists", lambda iface: iface == "eth0")
    monkeypatch.setattr(wn.vm, "get_interface_ipv4", lambda _iface: "192.168.86.30")
    monkeypatch.setattr(wn.shutil, "which", lambda _name: None)
    monkeypatch.setattr(wn, "get_default_gateway", lambda _iface="": "192.168.86.1")
    monkeypatch.setattr(wn, "get_interface_mac", lambda _iface="": "DC:A6:32:AA:BB:CC")
    monkeypatch.setattr(wn, "reverse_dns_lookup", lambda _ip: "")
    monkeypatch.setattr(wn, "discover_mdns_hosts", lambda **_kw: {})

    lan_entries = [{"ip": "192.168.86.1", "mac": "30:FD:38:00:00:01", "state": "REACHABLE"}]
    monkeypatch.setattr(wn.wm, "scan_lan_neighbors", lambda _iface: list(lan_entries))

    ctx = ws.AppContext(cfg_path)

    # 1. First scan baselines existing devices silently (no alert storm)
    snap1 = ctx.network.scan(active_sweep=False)
    assert snap1["summary"]["unknown_devices"] == 0
    assert pushed_alerts == []

    # 2. Brand-new unknown device joins LAN -> triggers [NEW DEVICE] push alert + NEW badge
    lan_entries.append({"ip": "192.168.86.88", "mac": "50:C7:BF:99:88:77", "state": "REACHABLE"})
    snap2 = ctx.network.scan(active_sweep=False)
    assert snap2["summary"]["unknown_devices"] == 1
    assert len(pushed_alerts) == 1
    assert pushed_alerts[0][0].startswith("[NEW DEVICE]")
    new_dev = next(d for d in snap2["devices"] if d["mac"] == "50:C7:BF:99:88:77")
    assert new_dev["is_new"] is True
    assert new_dev["trusted"] is False

    # 3. Trust via POST /api/network/trust -> clears NEW badge
    trust_payload = json.dumps({"mac": "50:C7:BF:99:88:77"}).encode()
    req = (
        b"POST /api/network/trust HTTP/1.1\r\n"
        b"Host: localhost\r\n"
        b"Content-Type: application/json\r\n"
        b"X-Requested-With: rp-inky\r\n"
        + f"Content-Length: {len(trust_payload)}\r\n\r\n".encode()
        + trust_payload
    )
    code, body = _dispatch_http(ctx, req)
    assert code == 200
    snap3 = json.loads(body.decode("utf-8"))
    assert snap3["summary"]["unknown_devices"] == 0
    trusted_dev = next(d for d in snap3["devices"] if d["mac"] == "50:C7:BF:99:88:77")
    assert trusted_dev["is_new"] is False
    assert trusted_dev["trusted"] is True


def test_system_health_endpoint_and_watchdog_thermal_disk_alerts(tmp_path, monkeypatch):
    for sub in ("photoframe", "vpn", "webui"):
        (tmp_path / sub).mkdir()
    cfg_path = tmp_path / "webui" / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "monitor": {
                    "targets": [],
                    "system_alerts": {"enabled": True, "cpu_temp_c": 80, "disk_pct": 90, "alert_throttling": True},
                    "notifications": {"ntfy": {"enabled": True, "server": "https://ntfy.sh", "topic": "health"}},
                }
            }
        ),
        encoding="utf-8",
    )

    assert sh.parse_throttled_mask(0x0)["status"] == "ok"
    assert sh.parse_throttled_mask(0x50000)["status"] == "warn"
    assert sh.parse_throttled_mask(0x5)["status"] == "crit"
    assert sh.parse_log2ram_mounts("log2ram /var/log tmpfs rw,nosuid,nodev 0 0\n") is True
    assert sh.parse_log2ram_mounts("/dev/mmcblk0p2 / ext4 rw 0 0\n") is False

    alerts = []
    monkeypatch.setattr(wm, "send_notifications", lambda _cfg, title, msg: (alerts.append((title, msg)), [])[1])

    fake_health = {
        "status": "crit",
        "cpu_temp_c": 83.4,
        "load_1m": 1.25,
        "uptime_seconds": 3600,
        "uptime_human": "1h 0m",
        "memory": {"available": True, "total_mb": 4096, "used_mb": 1024, "pct": 25.0},
        "disk": {"available": True, "total_gb": 32.0, "used_gb": 29.5, "free_gb": 2.5, "pct": 92.2},
        "throttled": {"available": True, "mask": "0x1", "status": "crit", "summary": "Undervoltage now", "active": ["Undervoltage now"], "historical": []},
    }
    monkeypatch.setattr(wm.sh, "get_system_health", lambda: dict(fake_health))
    monkeypatch.setattr(ws.sh, "get_system_health", lambda: dict(fake_health))

    ctx = ws.AppContext(cfg_path)

    # HTTP GET /api/system/health returns structured metrics
    code, body = _dispatch_http(ctx, b"GET /api/system/health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
    assert code == 200
    parsed = json.loads(body.decode("utf-8"))
    assert parsed["cpu_temp_c"] == 83.4
    assert parsed["throttled"]["status"] == "crit"

    # Watchdog run_once triggers temp + disk + throttling alerts
    ctx.monitor.run_once()
    titles = [t for t, _ in alerts]
    assert "[ALERT] Pi High CPU Temp" in titles
    assert "[ALERT] Pi SD Card Nearly Full" in titles
    assert "[ALERT] Pi Power/Thermal Throttling" in titles

    # Cooling down + freeing space triggers recovery alerts
    fake_health["cpu_temp_c"] = 62.0
    fake_health["disk"] = {"available": True, "total_gb": 32.0, "used_gb": 16.0, "free_gb": 16.0, "pct": 50.0}
    fake_health["throttled"] = {"available": True, "mask": "0x0", "status": "ok", "summary": "OK", "active": [], "historical": []}
    alerts.clear()
    ctx.monitor.run_once()
    rec_titles = [t for t, _ in alerts]
    assert "[RECOVERED] Pi CPU Temp" in rec_titles
    assert "[RECOVERED] Pi SD Card" in rec_titles
    assert "[RECOVERED] Pi Power/Thermal" in rec_titles


def test_adguard_status_endpoint(tmp_path, monkeypatch):
    for sub in ("photoframe", "vpn", "webui"):
        (tmp_path / sub).mkdir()
    cfg_path = tmp_path / "webui" / "config.json"
    cfg_path.write_text(json.dumps({"port": 8080, "adguard": {"port": 3000}}), encoding="utf-8")
    ctx = ws.AppContext(cfg_path)

    monkeypatch.setattr(
        ws.wag,
        "get_adguard_status",
        lambda port=3000: {
            "installed": True,
            "running": True,
            "dns_listening": True,
            "port": port,
            "auth_required": True,
            "protection_enabled": True,
            "version": "v0.107",
            "dns_queries": 1000,
            "blocked_filtering": 220,
            "blocked_pct": 22.0,
        },
    )
    code, body = _dispatch_http(ctx, b"GET /api/adguard/status HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
    assert code == 200
    parsed = json.loads(body.decode("utf-8"))
    assert parsed["running"] is True
    assert parsed["blocked_pct"] == 22.0


def test_tailscale_status_parser_and_endpoint(tmp_path, monkeypatch):
    from webui import tailscale as wts

    sample = json.dumps(
        {
            "BackendState": "Running",
            "Self": {
                "HostName": "raspberrypi",
                "DNSName": "raspberrypi.tail1234.ts.net.",
                "TailscaleIPs": ["100.88.12.34", "fd7a:115c:a1e0::1"],
                "ExitNodeOption": True,
            },
            "Peer": {
                "p1": {"Online": True},
                "p2": {"Online": False},
            },
        }
    )
    parsed = wts.parse_tailscale_status_json(sample)
    assert parsed["running"] is True
    assert parsed["tailscale_ip"] == "100.88.12.34"
    assert parsed["dns_name"] == "raspberrypi.tail1234.ts.net"
    assert parsed["online_peers"] == 1
    assert parsed["peer_count"] == 2
    assert parsed["exit_node_advertised"] is True

    for sub in ("photoframe", "vpn", "webui"):
        (tmp_path / sub).mkdir()
    cfg_path = tmp_path / "webui" / "config.json"
    cfg_path.write_text(json.dumps({"port": 8080}), encoding="utf-8")
    ctx = ws.AppContext(cfg_path)
    monkeypatch.setattr(ws.wts, "get_tailscale_status", lambda: parsed)

    code, body = _dispatch_http(ctx, b"GET /api/tailscale/status HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
    assert code == 200
    assert json.loads(body.decode("utf-8"))["tailscale_ip"] == "100.88.12.34"


def test_vpn_country_switcher_and_hotspot_update_endpoints(tmp_path, monkeypatch):
    from webui import vpn_bridge as wb

    assert wb.country_meta("uk") == ("GB", "United Kingdom", "🇬🇧")
    assert wb.country_meta("ch") == ("CH", "Switzerland", "🇨🇭")
    assert wb.country_meta("mx") == ("MX", "Mexico", "🇲🇽")

    for sub in ("photoframe", "vpn", "webui"):
        (tmp_path / sub).mkdir()
    profiles_dir = tmp_path / "vpn" / "profiles"
    profiles_dir.mkdir(parents=True)
    for fname, ep in (("uk-lon.conf", "uk-lon.prod.surfshark.com:51820"), ("ch-zur.conf", "ch-zur.prod.surfshark.com:51820")):
        (profiles_dir / fname).write_text(
            f"[Interface]\nPrivateKey = AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=\nAddress = 10.14.0.2/16\n"
            f"[Peer]\nPublicKey = BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB=\nEndpoint = {ep}\nAllowedIPs = 0.0.0.0/0\n",
            encoding="utf-8",
        )

    cfg_path = tmp_path / "webui" / "config.json"
    cfg_path.write_text(json.dumps({"port": 8080}), encoding="utf-8")
    ctx = ws.AppContext(cfg_path)

    invoked = []
    monkeypatch.setattr(ctx.vpn_bridge, "_run_privileged", lambda args, timeout=30: (invoked.append(args), "")[1])
    monkeypatch.setattr(wb.vm, "interface_exists", lambda _iface: False)
    monkeypatch.setattr(wb.vm, "get_interface_ipv4", lambda _iface: "192.168.86.30")

    # Switch country CH -> picks ch-zur
    payload = json.dumps({"country": "CH"}).encode()
    req = (
        b"POST /api/vpn/country HTTP/1.1\r\n"
        b"Host: localhost\r\n"
        b"Content-Type: application/json\r\n"
        b"X-Requested-With: rp-inky\r\n"
        + f"Content-Length: {len(payload)}\r\n\r\n".encode()
        + payload
    )
    code, body = _dispatch_http(ctx, req)
    assert code == 200
    assert invoked[-1] == ["use", "ch-zur"]
    status = json.loads(body.decode("utf-8"))
    assert {c["code"] for c in status["countries"]} == {"GB", "CH"}

    # Update hotspot SSID & password
    hp_payload = json.dumps({"ssid": "Home-VPN", "passphrase": "SuperSecretPassword123"}).encode()
    hp_req = (
        b"POST /api/vpn/hotspot HTTP/1.1\r\n"
        b"Host: localhost\r\n"
        b"Content-Type: application/json\r\n"
        b"X-Requested-With: rp-inky\r\n"
        + f"Content-Length: {len(hp_payload)}\r\n\r\n".encode()
        + hp_payload
    )
    code2, _ = _dispatch_http(ctx, hp_req)
    assert code2 == 200
    assert invoked[-1] == ["configure-hotspot", "--ssid", "Home-VPN", "--passphrase", "SuperSecretPassword123"]





