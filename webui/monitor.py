"""ISP/VPN Uptime Watchdog, Push Notifications (Telegram/Pushover/ntfy/Webhook) & Wake-on-LAN."""

import ipaddress
import json
import logging
import platform
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from contextlib import suppress
from pathlib import Path
from typing import Any, Optional

from vpn import manager as vm

LOG = logging.getLogger("webui.monitor")

MAC_HEX_RE = re.compile(r"^[0-9A-F]{12}$")
SLUG_ID_RE = re.compile(r"[^a-z0-9-]+")


def normalize_mac(mac: str) -> str:
    """Validate and format a MAC address as 'AA:BB:CC:DD:EE:FF'."""
    hex_only = re.sub(r"[^0-9a-fA-F]", "", str(mac or "")).upper()
    if not MAC_HEX_RE.match(hex_only):
        raise ValueError(f"Invalid MAC address: {mac!r} (expected 6 hex pairs like AA:BB:CC:DD:EE:FF)")
    return ":".join(hex_only[idx : idx + 2] for idx in range(0, 12, 2))


def build_magic_packet(mac: str) -> bytes:
    """Build a standard 102-byte Wake-on-LAN magic packet (6x 0xFF + 16x MAC)."""
    normalized = normalize_mac(mac)
    mac_bytes = bytes.fromhex(normalized.replace(":", ""))
    return (b"\xff" * 6) + (mac_bytes * 16)


def send_wake_on_lan(
    mac: str,
    broadcast_ip: str = "255.255.255.255",
    port: int = 9,
    bind_ip: str = "",
) -> str:
    """Broadcast a Wake-on-LAN magic packet over UDP (ports 9 and 7)."""
    packet = build_magic_packet(mac)
    normalized = normalize_mac(mac)
    bcast = str(ipaddress.IPv4Address(broadcast_ip.strip() or "255.255.255.255"))
    target_port = int(port) if 1 <= int(port) <= 65535 else 9

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        if bind_ip:
            with suppress(OSError):
                sock.bind((bind_ip, 0))
        sock.sendto(packet, (bcast, target_port))
        if target_port != 7:
            with suppress(OSError):
                sock.sendto(packet, (bcast, 7))
    LOG.info("Sent Wake-on-LAN magic packet to %s via %s:%d", normalized, bcast, target_port)
    return normalized


def scan_lan_neighbors(interface: str = "eth0") -> list[dict[str, str]]:
    """Return discovered IPv4 + MAC entries from the kernel ARP / neighbor table."""
    entries: list[dict[str, str]] = []
    seen_macs: set[str] = set()

    if shutil.which("ip"):
        cmd = ["ip", "-4", "neigh", "show", "dev", interface] if interface else ["ip", "-4", "neigh", "show"]
        res = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=4)
        if res.returncode == 0:
            for line in res.stdout.splitlines():
                # Example: 192.168.86.42 lladdr aa:bb:cc:dd:ee:ff REACHABLE
                parts = line.split()
                if len(parts) >= 4 and "lladdr" in parts:
                    ip_str = parts[0]
                    idx = parts.index("lladdr")
                    if idx + 1 < len(parts):
                        with suppress(ValueError):
                            ipaddress.IPv4Address(ip_str)
                            mac_norm = normalize_mac(parts[idx + 1])
                            if mac_norm not in seen_macs and mac_norm != "FF:FF:FF:FF:FF:FF":
                                seen_macs.add(mac_norm)
                                state = parts[-1] if parts[-1].isupper() else ""
                                entries.append({"ip": ip_str, "mac": mac_norm, "state": state})

    if not entries and shutil.which("arp"):
        res = subprocess.run(["arp", "-an"], capture_output=True, text=True, check=False, timeout=4)
        if res.returncode == 0:
            for line in res.stdout.splitlines():
                ip_match = re.search(r"\((\d+\.\d+\.\d+\.\d+)\)", line)
                mac_match = re.search(r"([0-9a-fA-F]{1,2}(?::[0-9a-fA-F]{1,2}){5})", line)
                if ip_match and mac_match:
                    raw_octets = [part.zfill(2) for part in mac_match.group(1).split(":")]
                    with suppress(ValueError):
                        mac_norm = normalize_mac(":".join(raw_octets))
                        if mac_norm not in seen_macs and mac_norm != "FF:FF:FF:FF:FF:FF":
                            seen_macs.add(mac_norm)
                            entries.append({"ip": ip_match.group(1), "mac": mac_norm, "state": "ARP"})

    entries.sort(key=lambda item: tuple(int(part) for part in item["ip"].split(".")))
    return entries


# ---------------------------------------------------------------------------
# Probes (ICMP Ping, Interface Ping, TCP Port, HTTP/HTTPS)
# ---------------------------------------------------------------------------

PING_TIME_RE = re.compile(r"time[=<]\s*([0-9]+(?:\.[0-9]+)?)\s*ms", re.IGNORECASE)


def probe_ping(host: str, interface: str = "", timeout_sec: int = 3) -> tuple[bool, Optional[float], str]:
    """Run a single ICMP ping probe and return (up, latency_ms, detail)."""
    is_linux = platform.system().lower() == "linux"
    cmd = ["ping", "-c", "1"]
    if is_linux:
        cmd.extend(["-4", "-W", str(timeout_sec)])
        if interface:
            if not vm.interface_exists(interface):
                return False, None, f"Interface {interface} is down"
            cmd.extend(["-I", interface])
    else:
        cmd.extend(["-W", str(timeout_sec * 1000)])
    cmd.append(host)

    started = time.monotonic()
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=timeout_sec + 3)
    except subprocess.TimeoutExpired:
        return False, None, "Ping timed out"

    if res.returncode != 0:
        err = (res.stderr or res.stdout or "No reply").strip().splitlines()
        return False, None, (err[-1] if err else "No reply")[:120]

    match = PING_TIME_RE.search(res.stdout)
    latency = float(match.group(1)) if match else (time.monotonic() - started) * 1000.0
    return True, round(latency, 1), "OK"


def probe_tcp(host: str, port: int, timeout_sec: int = 4) -> tuple[bool, Optional[float], str]:
    """Measure TCP handshake reachability and RTT in milliseconds."""
    started = time.monotonic()
    try:
        with socket.create_connection((host, int(port)), timeout=timeout_sec):
            latency = (time.monotonic() - started) * 1000.0
            return True, round(latency, 1), f"TCP {port} open"
    except OSError as exc:
        return False, None, str(exc)[:120]


def probe_http(url: str, timeout_sec: int = 6) -> tuple[bool, Optional[float], str]:
    """Probe an HTTP/HTTPS endpoint and verify a 2xx/3xx response."""
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
    req = urllib.request.Request(url, headers={"User-Agent": "rp-inky-monitor/1.0"})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            code = getattr(resp, "status", 200)
            latency = (time.monotonic() - started) * 1000.0
            ok = 200 <= int(code) < 400
            return ok, round(latency, 1), f"HTTP {code}"
    except Exception as exc:
        return False, None, str(exc)[:120]


# ---------------------------------------------------------------------------
# Push Notifications (Telegram, Pushover, ntfy.sh, Discord / Slack / Webhook)
# ---------------------------------------------------------------------------


def send_notifications(notif_cfg: dict[str, Any], title: str, message: str) -> list[str]:
    """Dispatch push notification to all enabled channels; return list of error strings."""
    errors: list[str] = []
    full_text = f"{title}\n{message}".strip()

    tg = notif_cfg.get("telegram") or {}
    if tg.get("enabled") and tg.get("bot_token") and tg.get("chat_id"):
        try:
            tg_url = f"https://api.telegram.org/bot{tg['bot_token'].strip()}/sendMessage"
            payload = urllib.parse.urlencode({"chat_id": str(tg["chat_id"]).strip(), "text": full_text}).encode("utf-8")
            req = urllib.request.Request(tg_url, data=payload, method="POST")
            with urllib.request.urlopen(req, timeout=8) as resp:
                resp.read()
        except Exception as exc:
            errors.append(f"Telegram: {exc}")

    po = notif_cfg.get("pushover") or {}
    if po.get("enabled") and po.get("app_token") and po.get("user_key"):
        try:
            payload = urllib.parse.urlencode(
                {
                    "token": str(po["app_token"]).strip(),
                    "user": str(po["user_key"]).strip(),
                    "title": title,
                    "message": message,
                }
            ).encode("utf-8")
            req = urllib.request.Request("https://api.pushover.net/1/messages.json", data=payload, method="POST")
            with urllib.request.urlopen(req, timeout=8) as resp:
                resp.read()
        except Exception as exc:
            errors.append(f"Pushover: {exc}")

    ntfy = notif_cfg.get("ntfy") or {}
    if ntfy.get("enabled") and ntfy.get("topic"):
        try:
            server = str(ntfy.get("server") or "https://ntfy.sh").rstrip("/")
            topic = urllib.parse.quote(str(ntfy["topic"]).strip().lstrip("/"))
            req = urllib.request.Request(
                f"{server}/{topic}",
                data=message.encode("utf-8"),
                headers={"Title": title, "User-Agent": "rp-inky-monitor/1.0"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                resp.read()
        except Exception as exc:
            errors.append(f"ntfy: {exc}")

    wh = notif_cfg.get("webhook") or {}
    if wh.get("enabled") and wh.get("url"):
        try:
            body = json.dumps({"title": title, "message": message, "text": full_text, "content": full_text}).encode("utf-8")
            req = urllib.request.Request(
                str(wh["url"]).strip(),
                data=body,
                headers={"Content-Type": "application/json", "User-Agent": "rp-inky-monitor/1.0"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                resp.read()
        except Exception as exc:
            errors.append(f"Webhook: {exc}")

    return errors


def mask_notification_config(notif_cfg: dict[str, Any]) -> dict[str, Any]:
    """Return notification config safe for display in the browser (masks secret tokens)."""
    out = json.loads(json.dumps(notif_cfg or {}))
    tg = out.setdefault("telegram", {"enabled": False, "bot_token": "", "chat_id": ""})
    if tg.get("bot_token"):
        token = str(tg["bot_token"])
        tg["bot_token_masked"] = f"{token[:6]}...{token[-4:]}" if len(token) > 12 else "***"
        tg["bot_token"] = ""
    po = out.setdefault("pushover", {"enabled": False, "app_token": "", "user_key": ""})
    if po.get("app_token"):
        po["app_token_masked"] = "***configured***"
        po["app_token"] = ""
    out.setdefault("ntfy", {"enabled": False, "server": "https://ntfy.sh", "topic": ""})
    out.setdefault("webhook", {"enabled": False, "url": ""})
    return out


# ---------------------------------------------------------------------------
# Background Uptime & Smart-Home Monitor Service
# ---------------------------------------------------------------------------


def make_target_id(name: str) -> str:
    """Create a URL-safe target identifier from a friendly label."""
    slug = SLUG_ID_RE.sub("-", name.lower()).strip("-")
    return f"{slug[:32] or 'target'}-{int(time.time()) % 100000}"


class UptimeMonitor:
    """Periodic health monitor for ISP WAN, Surfshark UK VPN, and LAN/HTTP targets."""

    def __init__(
        self,
        webui_config_getter: Any,
        local_config_updater: Any,
        vpn_config_path: Path,
        state_dir: Path,
    ):
        self._get_config = webui_config_getter
        self._update_local = local_config_updater
        self.vpn_config_path = Path(vpn_config_path)
        self.state_dir = Path(state_dir)
        self.state_path = self.state_dir / "monitor_state.json"

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_flush = 0.0

        persisted = vm.read_json(self.state_path, {})
        if not isinstance(persisted, dict):
            persisted = {}
        self._metrics: dict[str, dict[str, Any]] = persisted.get("metrics") if isinstance(persisted.get("metrics"), dict) else {}
        self._incidents: list[dict[str, Any]] = persisted.get("incidents") if isinstance(persisted.get("incidents"), list) else []
        self._pending_alerts: list[tuple[str, str]] = []

    def start(self) -> None:
        """Start the background periodic probing thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="rp-inky-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop background probing and flush state to disk."""
        self._stop_event.set()
        self._wake_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._flush_state(force=True)

    def trigger_now(self) -> None:
        """Wake the monitor thread to run an immediate check pass."""
        self._wake_event.set()

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            cfg = self._get_config()
            mon_cfg = cfg.get("monitor") or {}
            if mon_cfg.get("enabled", True):
                with suppress(Exception):
                    self.run_once()
            interval = max(15, int(mon_cfg.get("interval_seconds", 60)))
            self._wake_event.wait(interval)
            self._wake_event.clear()

    def _probe_target(self, target: dict[str, Any], vpn_cfg: dict[str, Any]) -> tuple[bool, Optional[float], str]:
        ttype = str(target.get("type") or "ping").lower()
        host = str(target.get("host") or "1.1.1.1").strip()
        if ttype == "iface_ping":
            iface = str(target.get("interface") or vpn_cfg["wan_interface"])
            return probe_ping(host, interface=iface)
        if ttype == "vpn_tunnel":
            wg_iface = vpn_cfg["wireguard"]["interface"]
            ovpn_iface = vpn_cfg["openvpn"]["interface"]
            tun_iface = wg_iface if vm.interface_exists(wg_iface) else (ovpn_iface if vm.interface_exists(ovpn_iface) else "")
            if not tun_iface:
                return False, None, "VPN tunnel interface is down"
            return probe_ping(host, interface=tun_iface)
        if ttype == "tcp":
            port = int(target.get("port") or 80)
            return probe_tcp(host, port)
        if ttype == "http":
            return probe_http(host)
        return probe_ping(host)

    def run_once(self) -> dict[str, Any]:
        """Execute one health-check pass across all configured targets."""
        cfg = self._get_config()
        mon_cfg = cfg["monitor"]
        vpn_cfg = vm.load_vpn_config(self.vpn_config_path)
        history_size = max(10, min(120, int(mon_cfg.get("history_size", 60))))
        threshold = max(1, int(mon_cfg.get("alert_threshold", 2)))
        notif_cfg = mon_cfg.get("notifications") or {}

        targets = list(mon_cfg.get("targets") or [])
        now_iso = vm.utc_now_iso()
        state_changed = False
        new_alerts: list[tuple[str, str]] = []

        for target in targets:
            tid = str(target.get("id") or target.get("name") or "target")
            name = str(target.get("name") or tid)
            up, latency_ms, detail = self._probe_target(target, vpn_cfg)

            with self._lock:
                entry = self._metrics.setdefault(
                    tid,
                    {
                        "up": None,
                        "latency_ms": None,
                        "detail": "",
                        "checked_at": "",
                        "checks_total": 0,
                        "checks_up": 0,
                        "consecutive_failures": 0,
                        "alerting": False,
                        "down_since": "",
                        "history": [],
                    },
                )
                entry["up"] = up
                entry["latency_ms"] = latency_ms
                entry["detail"] = detail
                entry["checked_at"] = now_iso
                entry["checks_total"] = int(entry.get("checks_total", 0)) + 1
                if up:
                    entry["checks_up"] = int(entry.get("checks_up", 0)) + 1

                history: list[dict[str, Any]] = list(entry.get("history") or [])
                history.append({"t": now_iso, "up": up, "ms": latency_ms})
                entry["history"] = history[-history_size:]

                if up:
                    if entry.get("alerting"):
                        down_since = entry.get("down_since") or "earlier"
                        msg = f"{name} is back UP ({latency_ms or 0:.0f} ms). Was down since {down_since}."
                        new_alerts.append((f"[RECOVERED] {name}", msg))
                        self._record_incident(name, "recovered", msg, now_iso)
                        state_changed = True
                    entry["consecutive_failures"] = 0
                    entry["alerting"] = False
                    entry["down_since"] = ""
                else:
                    fails = int(entry.get("consecutive_failures", 0)) + 1
                    entry["consecutive_failures"] = fails
                    if not entry.get("down_since"):
                        entry["down_since"] = now_iso
                    if fails >= threshold and not entry.get("alerting"):
                        entry["alerting"] = True
                        msg = f"{name} failed {fails} consecutive checks ({detail})."
                        new_alerts.append((f"[DOWN] {name}", msg))
                        self._record_incident(name, "down", msg, now_iso)
                        state_changed = True

        # Queue and dispatch notifications (retaining failed sends if WAN was temporarily down)
        with self._lock:
            self._pending_alerts.extend(new_alerts)
            pending = list(self._pending_alerts)
            self._pending_alerts.clear()

        still_pending: list[tuple[str, str]] = []
        for title, message in pending:
            errs = send_notifications(notif_cfg, title, message)
            if errs:
                LOG.warning("Notification delivery deferred (%s): %s", title, "; ".join(errs))
                still_pending.append((title, message))
        if still_pending:
            with self._lock:
                self._pending_alerts = (still_pending + self._pending_alerts)[-20:]

        self._flush_state(force=state_changed)
        return self.get_snapshot()

    def _record_incident(self, target_name: str, status: str, summary: str, at_iso: str) -> None:
        self._incidents.insert(0, {"target": target_name, "status": status, "summary": summary, "at": at_iso})
        del self._incidents[50:]

    def _flush_state(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - self._last_flush) < 600.0:
            return
        self._last_flush = now
        with self._lock:
            payload = {"metrics": self._metrics, "incidents": self._incidents}
        with suppress(OSError):
            vm.write_json_atomic(self.state_path, payload, mode=0o600)

    def get_snapshot(self) -> dict[str, Any]:
        """Return current targets, heartbeat histories, uptime % and masked alert settings."""
        cfg = self._get_config()
        mon_cfg = cfg["monitor"]
        targets = list(mon_cfg.get("targets") or [])

        enriched: list[dict[str, Any]] = []
        with self._lock:
            for target in targets:
                tid = str(target.get("id") or target.get("name"))
                m = self._metrics.get(tid) or {}
                total = int(m.get("checks_total", 0))
                up_count = int(m.get("checks_up", 0))
                uptime_pct = round((up_count * 100.0) / total, 2) if total > 0 else None
                enriched.append(
                    {
                        "id": tid,
                        "name": target.get("name", tid),
                        "type": target.get("type", "ping"),
                        "host": target.get("host", ""),
                        "port": target.get("port"),
                        "interface": target.get("interface", ""),
                        "builtin": bool(target.get("builtin", False)),
                        "up": m.get("up"),
                        "latency_ms": m.get("latency_ms"),
                        "detail": m.get("detail", ""),
                        "checked_at": m.get("checked_at", ""),
                        "uptime_pct": uptime_pct,
                        "history": m.get("history", []),
                    }
                )
            incidents = list(self._incidents[:25])

        return {
            "enabled": bool(mon_cfg.get("enabled", True)),
            "interval_seconds": int(mon_cfg.get("interval_seconds", 60)),
            "targets": enriched,
            "incidents": incidents,
            "notifications": mask_notification_config(mon_cfg.get("notifications") or {}),
        }

    def add_target(self, payload: dict[str, Any]) -> dict[str, Any]:
        name = str(payload.get("name") or "").strip()[:60]
        ttype = str(payload.get("type") or "ping").strip().lower()
        host = str(payload.get("host") or "").strip()[:200]
        if not name or not host:
            raise ValueError("Target name and host/URL are required.")
        if ttype not in ("ping", "http", "tcp", "iface_ping", "vpn_tunnel"):
            raise ValueError(f"Unsupported monitor type: {ttype}")

        new_item: dict[str, Any] = {
            "id": make_target_id(name),
            "name": name,
            "type": ttype,
            "host": host,
            "builtin": False,
        }
        if ttype == "tcp":
            port = int(payload.get("port") or 80)
            if not (1 <= port <= 65535):
                raise ValueError("TCP port must be between 1 and 65535.")
            new_item["port"] = port

        cfg = self._get_config()
        targets = list(cfg["monitor"].get("targets") or [])
        targets.append(new_item)
        self._update_local({"monitor": {"targets": targets}})
        self.trigger_now()
        return self.get_snapshot()

    def remove_target(self, target_id: str) -> dict[str, Any]:
        cfg = self._get_config()
        targets = list(cfg["monitor"].get("targets") or [])
        kept = [t for t in targets if str(t.get("id")) != target_id or t.get("builtin")]
        if len(kept) == len(targets):
            raise ValueError("Monitor target not found (built-in ISP/VPN monitors cannot be deleted).")
        self._update_local({"monitor": {"targets": kept}})
        with self._lock:
            self._metrics.pop(target_id, None)
        return self.get_snapshot()

    def update_notifications(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Merge notification channel settings, preserving existing secret tokens when blank."""
        cfg = self._get_config()
        current = json.loads(json.dumps(cfg["monitor"].get("notifications") or {}))

        if isinstance(patch.get("telegram"), dict):
            tg_in = patch["telegram"]
            tg = current.setdefault("telegram", {})
            tg["enabled"] = bool(tg_in.get("enabled", tg.get("enabled", False)))
            if str(tg_in.get("bot_token") or "").strip():
                tg["bot_token"] = str(tg_in["bot_token"]).strip()
            if "chat_id" in tg_in:
                tg["chat_id"] = str(tg_in["chat_id"]).strip()

        if isinstance(patch.get("pushover"), dict):
            po_in = patch["pushover"]
            po = current.setdefault("pushover", {})
            po["enabled"] = bool(po_in.get("enabled", po.get("enabled", False)))
            if str(po_in.get("app_token") or "").strip():
                po["app_token"] = str(po_in["app_token"]).strip()
            if "user_key" in po_in:
                po["user_key"] = str(po_in["user_key"]).strip()

        if isinstance(patch.get("ntfy"), dict):
            nt_in = patch["ntfy"]
            nt = current.setdefault("ntfy", {})
            nt["enabled"] = bool(nt_in.get("enabled", nt.get("enabled", False)))
            if str(nt_in.get("server") or "").strip():
                nt["server"] = str(nt_in["server"]).strip()
            if "topic" in nt_in:
                nt["topic"] = str(nt_in["topic"]).strip()

        if isinstance(patch.get("webhook"), dict):
            wh_in = patch["webhook"]
            wh = current.setdefault("webhook", {})
            wh["enabled"] = bool(wh_in.get("enabled", wh.get("enabled", False)))
            if "url" in wh_in:
                wh["url"] = str(wh_in["url"]).strip()

        self._update_local({"monitor": {"notifications": current}})
        return self.get_snapshot()

    def send_test_alert(self) -> dict[str, Any]:
        cfg = self._get_config()
        notif_cfg = cfg["monitor"].get("notifications") or {}
        any_enabled = any(
            bool((notif_cfg.get(ch) or {}).get("enabled"))
            for ch in ("telegram", "pushover", "ntfy", "webhook")
        )
        if not any_enabled:
            raise ValueError("Enable and save at least one notification channel first.")
        errors = send_notifications(
            notif_cfg,
            "[TEST] rp-inky Watchdog",
            f"Test alert from your Raspberry Pi 4 at {vm.utc_now_iso()}.",
        )
        if errors:
            raise RuntimeError("; ".join(errors))
        return {"sent": True}

    # -----------------------------------------------------------------------
    # Wake-on-LAN device registry & wake actions
    # -----------------------------------------------------------------------

    def list_wol_devices(self, check_awake: bool = False) -> dict[str, Any]:
        cfg = self._get_config()
        wol_cfg = cfg.get("wol") or {}
        devices = list(wol_cfg.get("devices") or [])
        out: list[dict[str, Any]] = []
        for dev in devices:
            ip_addr = str(dev.get("ip") or "").strip()
            awake: Optional[bool] = None
            if check_awake and ip_addr:
                awake, _, _ = probe_ping(ip_addr, timeout_sec=1)
            out.append(
                {
                    "id": dev.get("id") or dev.get("mac"),
                    "name": dev.get("name") or dev.get("mac"),
                    "mac": dev.get("mac"),
                    "ip": ip_addr,
                    "broadcast_ip": dev.get("broadcast_ip") or wol_cfg.get("broadcast_ip", "255.255.255.255"),
                    "awake": awake,
                }
            )
        return {"devices": out}

    def add_wol_device(self, payload: dict[str, Any]) -> dict[str, Any]:
        name = str(payload.get("name") or "").strip()[:60]
        mac = normalize_mac(str(payload.get("mac") or ""))
        ip_raw = str(payload.get("ip") or "").strip()
        ip_clean = str(ipaddress.IPv4Address(ip_raw)) if ip_raw else ""
        if not name:
            name = ip_clean or mac

        cfg = self._get_config()
        devices = [d for d in (cfg["wol"].get("devices") or []) if d.get("mac") != mac]
        devices.append(
            {
                "id": mac.lower().replace(":", ""),
                "name": name,
                "mac": mac,
                "ip": ip_clean,
            }
        )
        self._update_local({"wol": {"devices": devices}})
        return self.list_wol_devices()

    def remove_wol_device(self, device_id: str) -> dict[str, Any]:
        cfg = self._get_config()
        devices = list(cfg["wol"].get("devices") or [])
        kept = [d for d in devices if str(d.get("id")) != device_id and str(d.get("mac")) != device_id]
        self._update_local({"wol": {"devices": kept}})
        return self.list_wol_devices()

    def wake_device(self, payload: dict[str, Any]) -> dict[str, Any]:
        cfg = self._get_config()
        wol_cfg = cfg["wol"]
        vpn_cfg = vm.load_vpn_config(self.vpn_config_path)
        wan_ip = vm.get_interface_ipv4(vpn_cfg["wan_interface"]) or ""

        target_id = str(payload.get("id") or "").strip()
        mac = str(payload.get("mac") or "").strip()
        bcast = str(payload.get("broadcast_ip") or wol_cfg.get("broadcast_ip") or "255.255.255.255")

        if target_id and not mac:
            for dev in wol_cfg.get("devices") or []:
                if str(dev.get("id")) == target_id or str(dev.get("mac")) == target_id:
                    mac = str(dev["mac"])
                    bcast = str(dev.get("broadcast_ip") or bcast)
                    break
        if not mac:
            raise ValueError("MAC address or saved device id is required.")

        # Also send to directed subnet broadcast (e.g. 192.168.86.255) when LAN IP is known
        normalized = send_wake_on_lan(mac, broadcast_ip=bcast, port=int(wol_cfg.get("port", 9)), bind_ip=wan_ip)
        if wan_ip and bcast == "255.255.255.255":
            subnet_bcast = ".".join([*wan_ip.split(".")[:3], "255"])
            with suppress(Exception):
                send_wake_on_lan(mac, broadcast_ip=subnet_bcast, port=int(wol_cfg.get("port", 9)), bind_ip=wan_ip)
        return {"woken": normalized}
