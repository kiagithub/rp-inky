"""Home Network & VPN Hotspot Device Scanner, OUI Fingerprinter & Bandwidth Monitor."""

import concurrent.futures
import ipaddress
import logging
import platform
import re
import shutil
import socket
import subprocess
import threading
import time
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from vpn import manager as vm
from webui import monitor as wm

LOG = logging.getLogger("webui.network")

DEVICE_CATEGORIES: dict[str, dict[str, str]] = {
    "router": {"label": "Router / Gateway", "icon": "🌐"},
    "pi": {"label": "Raspberry Pi", "icon": "🍓"},
    "tv": {"label": "TV / Streaming", "icon": "📺"},
    "phone": {"label": "Phone / Tablet", "icon": "📱"},
    "computer": {"label": "Computer / Laptop", "icon": "💻"},
    "smart_plug": {"label": "Smart Plug / Switch", "icon": "🔌"},
    "speaker": {"label": "Smart Speaker", "icon": "🔊"},
    "camera": {"label": "Camera / Doorbell", "icon": "📷"},
    "printer": {"label": "Printer", "icon": "🖨"},
    "gaming": {"label": "Gaming Console", "icon": "🎮"},
    "iot": {"label": "Smart Home / IoT", "icon": "💡"},
    "other": {"label": "Network Device", "icon": "📶"},
}

# Built-in fallback OUI table (first 3 MAC octets 'AABBCC' -> Vendor) for common home devices.
# Automatically augmented at runtime by /usr/share/ieee-data/oui.txt if installed on Pi OS.
BUILTIN_OUI_VENDORS: dict[str, str] = {
    # Raspberry Pi
    "B827EB": "Raspberry Pi",
    "DCA632": "Raspberry Pi",
    "E45F01": "Raspberry Pi",
    "28CDC1": "Raspberry Pi",
    "D83ADD": "Raspberry Pi",
    "2CCF67": "Raspberry Pi",
    # Apple
    "000393": "Apple",
    "001CB3": "Apple",
    "18AF61": "Apple",
    "28CFE9": "Apple",
    "3C15C2": "Apple",
    "40A6D9": "Apple",
    "542696": "Apple",
    "60F81D": "Apple",
    "705681": "Apple",
    "7831C1": "Apple",
    "88665A": "Apple",
    "9801A7": "Apple",
    "A483E7": "Apple",
    "ACBC32": "Apple",
    "BC52B7": "Apple",
    "C82A14": "Apple",
    "D02598": "Apple",
    "DC2B2A": "Apple",
    "E0B52D": "Apple",
    "F01898": "Apple",
    "F45C89": "Apple",
    # Google / Nest
    "18B430": "Google Nest",
    "30FD38": "Google",
    "3C5AB4": "Google",
    "48D6D5": "Google",
    "546009": "Google",
    "641666": "Google Nest",
    "7C2EDD": "Google",
    "94EB2C": "Google",
    "A47733": "Google",
    "D8EB46": "Google",
    "E4F042": "Google",
    "F4F5D8": "Google",
    "F4F5E8": "Google",
    # Amazon / Echo / Fire TV / Ring
    "0C47C9": "Amazon",
    "18742E": "Amazon",
    "34D270": "Amazon",
    "40B4CD": "Amazon",
    "44650D": "Amazon",
    "50DCE7": "Amazon",
    "6837E9": "Amazon",
    "74C246": "Amazon",
    "84D6D0": "Amazon",
    "A002DC": "Amazon",
    "AC63BE": "Amazon",
    "B47C9C": "Amazon",
    "CC9E00": "Amazon",
    "F0272D": "Amazon",
    "FC65DE": "Amazon",
    "343EA4": "Ring",
    "54E019": "Ring",
    "9C7613": "Ring",
    # Samsung
    "001599": "Samsung",
    "14F42A": "Samsung",
    "244B81": "Samsung",
    "38AA3C": "Samsung",
    "50A4C8": "Samsung",
    "64B310": "Samsung",
    "78471D": "Samsung",
    "8C7712": "Samsung",
    "A0821F": "Samsung",
    "BC1485": "Samsung",
    "CC07AB": "Samsung",
    "D022BE": "Samsung",
    "E47CF9": "Samsung",
    "F409D8": "Samsung",
    # LG Electronics (TVs / Appliances)
    "001C62": "LG Electronics",
    "10683F": "LG Electronics",
    "203DBD": "LG Electronics",
    "388C50": "LG Electronics",
    "58A2B5": "LG Electronics",
    "64995D": "LG Electronics",
    "785DC8": "LG Electronics",
    "A823FE": "LG Electronics",
    "C4366C": "LG Electronics",
    "CC2D8C": "LG Electronics",
    # Sony / PlayStation
    "0013A9": "Sony",
    "0019C5": "Sony PlayStation",
    "280DFC": "Sony",
    "30F9ED": "Sony",
    "709E29": "Sony PlayStation",
    "A8E3EE": "Sony PlayStation",
    "BC60A7": "Sony PlayStation",
    "D44B5E": "Sony",
    "FC0FE6": "Sony",
    # Roku / Streaming
    "080581": "Roku",
    "20EFBD": "Roku",
    "84EAED": "Roku",
    "AC3A7A": "Roku",
    "B0A737": "Roku",
    "B83E59": "Roku",
    "C83A6B": "Roku",
    "D83134": "Roku",
    # Sonos
    "000E58": "Sonos",
    "347E5C": "Sonos",
    "48A6B8": "Sonos",
    "542A1B": "Sonos",
    "5CAAFD": "Sonos",
    "7828CA": "Sonos",
    "949F3E": "Sonos",
    "B8E937": "Sonos",
    # Smart Plugs & IoT (TP-Link/Kasa, Espressif/Tuya, Shelly, Wyze, Philips Hue)
    "1C3BF3": "TP-Link Kasa",
    "50C7BF": "TP-Link Kasa",
    "54AF97": "TP-Link",
    "6032B1": "TP-Link",
    "68FF7B": "TP-Link",
    "98DAC4": "TP-Link",
    "B09575": "TP-Link Kasa",
    "C006C3": "TP-Link",
    "D807B6": "TP-Link",
    "E848B8": "TP-Link",
    "18FE34": "Espressif IoT",
    "240AC4": "Espressif IoT",
    "246F28": "Espressif IoT",
    "30AEA4": "Espressif IoT",
    "3C71BF": "Espressif IoT",
    "40F520": "Espressif IoT",
    "4C11AE": "Espressif IoT",
    "5CCF7F": "Espressif IoT",
    "600194": "Espressif IoT",
    "68C63A": "Espressif IoT",
    "807D3A": "Espressif IoT",
    "84CCA8": "Espressif IoT",
    "84F3EB": "Espressif IoT",
    "A4CF12": "Espressif IoT",
    "BCFF4D": "Espressif IoT",
    "C44F33": "Espressif IoT",
    "CC50E3": "Espressif IoT",
    "DC4F22": "Espressif IoT",
    "ECFAB8": "Espressif IoT",
    "10D561": "Tuya Smart",
    "1869D8": "Tuya Smart",
    "508A06": "Tuya Smart",
    "68572D": "Tuya Smart",
    "708976": "Tuya Smart",
    "D81F12": "Tuya Smart",
    "2CAA8E": "Wyze",
    "7C78B2": "Wyze",
    "D03F27": "Wyze",
    "001788": "Philips Hue",
    "ECB5FA": "Philips Hue",
    "E8DB84": "Shelly Smart",
    "349454": "Shelly Smart",
    "C8C9A3": "Shelly Smart",
    "B4E62D": "Ecobee",
    "446132": "Ecobee",
    # Gaming (Nintendo / Microsoft Xbox)
    "001F32": "Nintendo",
    "34AF2C": "Nintendo",
    "58BDA3": "Nintendo",
    "7CBB8A": "Nintendo",
    "98B6E9": "Nintendo",
    "B8AE6E": "Nintendo",
    "0050F2": "Microsoft Xbox",
    "281878": "Microsoft",
    "501AC5": "Microsoft",
    "7C1E52": "Microsoft",
    "985FD3": "Microsoft",
    # Computers / Networking / Printers / NAS
    "001B21": "Intel",
    "3C970E": "Lenovo",
    "54E1AD": "Lenovo",
    "D4BED9": "Dell",
    "F8B156": "Dell",
    "3C5282": "HP",
    "8CDCD4": "HP",
    "001132": "Synology NAS",
    "9009D0": "Synology NAS",
    "001BA9": "Brother Printer",
    "30055C": "Brother Printer",
    "0026AB": "Epson Printer",
    "001E8F": "Canon Printer",
    "74ACB9": "Ubiquiti",
    "F09FC2": "Ubiquiti",
    "10BF48": "ASUS",
    "04D4C4": "ASUS",
    "A040A0": "Netgear",
    "C40415": "Netgear",
    "04D9F5": "Roborock",
    "B04A39": "Roborock",
    "4C1744": "Tesla",
}

OUI_SYSTEM_PATHS = (
    Path("/usr/share/ieee-data/oui.txt"),
    Path("/usr/share/misc/oui.txt"),
    Path("/usr/share/nmap/nmap-mac-prefixes"),
)

DNSMASQ_LEASE_PATHS = (
    Path("/var/lib/misc/dnsmasq.leases"),
    Path("/run/rp-vpn/dnsmasq.leases"),
    Path("/tmp/dnsmasq.leases"),
)

_OUI_CACHE_LOCK = threading.Lock()
_OUI_CACHE: Optional[dict[str, str]] = None

OUI_TXT_RE = re.compile(r"^([0-9A-Fa-f]{6})\s+\(base 16\)\s+(.+)$")
NMAP_OUI_RE = re.compile(r"^([0-9A-Fa-f]{6})\s+(.+)$")


def is_locally_administered_mac(mac: str) -> bool:
    """Return True if a MAC address has the locally-administered (private/randomized) bit set."""
    clean = wm.normalize_mac(mac)
    first_byte = int(clean[:2], 16)
    return bool(first_byte & 0x02) and not bool(first_byte & 0x01)


def _load_oui_database() -> dict[str, str]:
    """Load OUI vendor map from system IEEE database (if present) + built-in consumer map."""
    global _OUI_CACHE
    with _OUI_CACHE_LOCK:
        if _OUI_CACHE is not None:
            return _OUI_CACHE
        table = dict(BUILTIN_OUI_VENDORS)
        for path in OUI_SYSTEM_PATHS:
            if not path.is_file():
                continue
            try:
                with path.open("r", encoding="utf-8", errors="ignore") as fh:
                    for line in fh:
                        line = line.strip()
                        m = OUI_TXT_RE.match(line) or NMAP_OUI_RE.match(line)
                        if m:
                            prefix = m.group(1).upper()
                            vendor = m.group(2).strip()
                            if prefix not in table and vendor:
                                table[prefix] = vendor[:48]
            except OSError:
                continue
        _OUI_CACHE = table
        return table


def lookup_mac_vendor(mac: str) -> tuple[str, bool]:
    """Return (vendor_name, is_private_mac) for a MAC address."""
    clean = wm.normalize_mac(mac)
    if is_locally_administered_mac(clean):
        return "Private Wi-Fi Address", True
    prefix = clean.replace(":", "")[:6]
    vendor = _load_oui_database().get(prefix, "")
    return vendor, False


def infer_device_category(
    hostname: str = "",
    vendor: str = "",
    services: Optional[list[str]] = None,
    is_private_mac: bool = False,
    is_gateway: bool = False,
    is_self: bool = False,
) -> str:
    """Guess a human-friendly device category from hostname, mDNS services & OUI vendor."""
    if is_self:
        return "pi"
    if is_gateway:
        return "router"

    host_l = (hostname or "").lower()
    vend_l = (vendor or "").lower()
    svcs = {s.lower() for s in (services or [])}

    # 1. Explicit hostname keywords (highest signal)
    if any(k in host_l for k in ("iphone", "ipad", "pixel", "galaxy", "oneplus", "android", "phone")):
        return "phone"
    if any(k in host_l for k in ("apple-tv", "appletv", "lgwebos", "webos", "bravia", "roku", "firetv", "fire-tv", "chromecast", "shield", "samsung-tv", "smarttv", "tv")):
        return "tv"
    if any(k in host_l for k in ("kasa", "tapo", "plug", "switch", "shelly", "meross", "wemo", "outlet")):
        return "smart_plug"
    if any(k in host_l for k in ("sonos", "homepod", "echo", "alexa", "nest-mini", "nest-audio", "speaker", "bose")):
        return "speaker"
    if any(k in host_l for k in ("ring", "wyze", "nest-cam", "doorbell", "camera", "cam-", "eufy", "arlo", "blink")):
        return "camera"
    if any(k in host_l for k in ("printer", "laserjet", "officejet", "deskjet", "epson", "brother", "canon", "pixma")):
        return "printer"
    if any(k in host_l for k in ("playstation", "ps4", "ps5", "xbox", "nintendo", "steamdeck")):
        return "gaming"
    if any(k in host_l for k in ("macbook", "imac", "mac-mini", "mac-studio", "mac-pro", "desktop", "laptop", "thinkpad", "ubuntu", "synology", "nas", "workstation", "pc")):
        return "computer"
    if any(k in host_l for k in ("raspberrypi", "raspi", "pi-")):
        return "pi"
    if any(k in host_l for k in ("hue", "ecobee", "thermostat", "tuya", "smartlife", "roborock", "roomba", "dyson", "esp_", "esp-", "tasmota")):
        return "iot"
    if any(k in host_l for k in ("router", "gateway", "nest-wifi", "eero", "orbi", "unifi")):
        return "router"

    # 2. mDNS / Bonjour service types
    if any(s in svcs for s in ("_ipp._tcp", "_ipps._tcp", "_printer._tcp", "_pdl-datastream._tcp")):
        return "printer"
    if any(s in svcs for s in ("_hap._tcp", "_matter._tcp", "_matterc._udp")):
        return "smart_plug"
    if any(s in svcs for s in ("_sonos._tcp", "_spotify-connect._tcp")):
        return "speaker"
    if "_googlecast._tcp" in svcs:
        return "tv"
    if "_airplay._tcp" in svcs and "_smb._tcp" not in svcs:
        return "tv"
    if any(s in svcs for s in ("_smb._tcp", "_afpovertcp._tcp", "_ssh._tcp", "_sftp-ssh._tcp", "_workstation._tcp")):
        return "computer"
    if "_companion-link._tcp" in svcs:
        return "phone"

    # 3. OUI Vendor fallback
    if "raspberry pi" in vend_l:
        return "pi"
    if any(k in vend_l for k in ("roku", "lg electronics")):
        return "tv"
    if any(k in vend_l for k in ("sonos",)):
        return "speaker"
    if any(k in vend_l for k in ("tp-link kasa", "shelly")):
        return "smart_plug"
    if any(k in vend_l for k in ("espressif", "tuya", "philips hue", "ecobee", "roborock")):
        return "iot"
    if any(k in vend_l for k in ("ring", "wyze")):
        return "camera"
    if any(k in vend_l for k in ("brother", "epson", "canon")):
        return "printer"
    if any(k in vend_l for k in ("nintendo", "playstation", "xbox")):
        return "gaming"
    if any(k in vend_l for k in ("synology", "dell", "lenovo", "intel")):
        return "computer"
    if any(k in vend_l for k in ("ubiquiti", "netgear")):
        return "router"
    if is_private_mac:
        return "phone"

    return "other"


def format_rate_mbps(mbps: Optional[float]) -> str:
    """Format bandwidth rate cleanly as Kbps or Mbps."""
    if mbps is None:
        return "—"
    if mbps < 0.01:
        return "0 Mbps"
    if mbps < 1.0:
        return f"{mbps * 1000:.0f} Kbps"
    if mbps < 10.0:
        return f"{mbps:.2f} Mbps"
    return f"{mbps:.1f} Mbps"


def parse_iw_station_dump(raw: str) -> dict[str, dict[str, Any]]:
    """Parse `iw dev <iface> station dump` output keyed by normalized MAC address."""
    stations: dict[str, dict[str, Any]] = {}
    current_mac: Optional[str] = None
    for line in raw.splitlines():
        m_st = re.match(r"^Station\s+([0-9a-fA-F:]{17})\b", line)
        if m_st:
            current_mac = wm.normalize_mac(m_st.group(1))
            stations[current_mac] = {
                "mac": current_mac,
                "signal_dbm": None,
                "tx_bitrate": "",
                "rx_bitrate": "",
                "rx_bytes": 0,
                "tx_bytes": 0,
                "connected_seconds": 0,
            }
            continue
        if not current_mac:
            continue
        entry = stations[current_mac]
        stripped = line.strip()
        if stripped.startswith("rx bytes:"):
            with suppress(ValueError):
                entry["rx_bytes"] = int(stripped.split(":", 1)[1].strip())
        elif stripped.startswith("tx bytes:"):
            with suppress(ValueError):
                entry["tx_bytes"] = int(stripped.split(":", 1)[1].strip())
        elif stripped.startswith("signal:"):
            m_sig = re.search(r"(-?\d+)\s*dBm", stripped)
            if m_sig:
                entry["signal_dbm"] = int(m_sig.group(1))
        elif stripped.startswith("tx bitrate:"):
            entry["tx_bitrate"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("rx bitrate:"):
            entry["rx_bitrate"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("connected time:"):
            m_ct = re.search(r"(\d+)\s*seconds", stripped)
            if m_ct:
                entry["connected_seconds"] = int(m_ct.group(1))
    return stations


def parse_dnsmasq_leases(paths: tuple[Path, ...] = DNSMASQ_LEASE_PATHS) -> dict[str, dict[str, str]]:
    """Parse dnsmasq DHCP leases file into {MAC: {ip, hostname}}."""
    leases: dict[str, dict[str, str]] = {}
    for path in paths:
        if not path.is_file():
            continue
        try:
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                parts = line.split()
                if len(parts) < 4:
                    continue
                # Format: <expiry> <mac> <ip> <hostname> [client-id]
                with suppress(ValueError):
                    mac = wm.normalize_mac(parts[1])
                    ip_str = str(ipaddress.IPv4Address(parts[2]))
                    host = "" if parts[3] == "*" else parts[3].strip()
                    leases[mac] = {"ip": ip_str, "hostname": host}
        except OSError:
            continue
    return leases


def parse_avahi_browse(raw: str) -> dict[str, dict[str, Any]]:
    """Parse `avahi-browse -a -t -r -p` output into {ip: {hostname, services, friendly}}."""
    discovered: dict[str, dict[str, Any]] = {}
    for line in raw.splitlines():
        if not line.startswith("="):
            continue
        # =;eth0;IPv4;Living Room TV;_airplay._tcp;local;Living-Room-TV.local;192.168.86.44;7000;...
        parts = line.split(";")
        if len(parts) < 8:
            continue
        if parts[2] != "IPv4":
            continue
        svc_name = parts[3].replace("\\032", " ").strip()
        svc_type = parts[4].strip()
        host_fqdn = parts[6].strip().removesuffix(".local")
        ip_str = parts[7].strip()
        with suppress(ValueError):
            ipaddress.IPv4Address(ip_str)
            rec = discovered.setdefault(ip_str, {"hostname": "", "friendly": "", "services": []})
            if host_fqdn and not rec["hostname"]:
                rec["hostname"] = host_fqdn
            if svc_name and not rec["friendly"] and not re.match(r"^[0-9A-Fa-f]{12}@", svc_name):
                rec["friendly"] = svc_name
            if svc_type and svc_type not in rec["services"]:
                rec["services"].append(svc_type)
    return discovered


def read_interface_counters(interface: str) -> Optional[tuple[int, int]]:
    """Return (rx_bytes, tx_bytes) from /sys/class/net/<interface>/statistics if available."""
    if not interface:
        return None
    base = Path("/sys/class/net") / interface / "statistics"
    try:
        rx = int((base / "rx_bytes").read_text(encoding="utf-8").strip())
        tx = int((base / "tx_bytes").read_text(encoding="utf-8").strip())
        return rx, tx
    except (OSError, ValueError):
        return None


def get_interface_mac(interface: str) -> str:
    """Read hardware MAC address for a Linux network interface."""
    if not interface:
        return ""
    addr_path = Path("/sys/class/net") / interface / "address"
    try:
        raw = addr_path.read_text(encoding="utf-8").strip()
        return wm.normalize_mac(raw)
    except (OSError, ValueError):
        return ""


def get_default_gateway(interface: str = "") -> str:
    """Return the default IPv4 gateway IP for the host (or specific interface)."""
    if not shutil.which("ip"):
        return ""
    cmd = ["ip", "-4", "route", "show", "default"]
    if interface:
        cmd.extend(["dev", interface])
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, timeout=3)
    if res.returncode == 0:
        m = re.search(r"\bvia\s+(\d+\.\d+\.\d+\.\d+)", res.stdout)
        if m:
            return m.group(1)
    return ""


def get_interface_cidr(interface: str) -> Optional[ipaddress.IPv4Network]:
    """Return the IPv4 subnet for an interface (e.g. 192.168.86.0/24)."""
    if not interface or not shutil.which("ip"):
        return None
    res = subprocess.run(
        ["ip", "-4", "-o", "addr", "show", "dev", interface],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=3,
    )
    if res.returncode == 0:
        m = re.search(r"\binet\s+(\d+\.\d+\.\d+\.\d+/\d+)", res.stdout)
        if m:
            with suppress(ValueError):
                return ipaddress.IPv4Network(m.group(1), strict=False)
    return None


def sweep_lan_subnet(interface: str, max_hosts: int = 254) -> int:
    """Trigger Layer-2 ARP resolution across the local subnet without requiring root.

    Sends a single 1-byte UDP datagram to port 9 on each host in the /24 subnet;
    the kernel automatically emits ARP Who-Has frames and populates `ip neigh`
    even for devices that drop ICMP ping packets.
    """
    net = get_interface_cidr(interface)
    if net is None or net.num_addresses - 2 > max_hosts:
        return 0
    hosts = [str(ip) for ip in net.hosts()]

    def _poke(ip_str: str) -> None:
        with suppress(OSError), socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.05)
            sock.sendto(b"\x00", (ip_str, 9))

    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as pool:
        list(pool.map(_poke, hosts))
    # Brief pause so ARP replies land in kernel neighbor table
    time.sleep(0.25)
    return len(hosts)


def discover_mdns_hosts(timeout_sec: int = 3) -> dict[str, dict[str, Any]]:
    """Run avahi-browse if available to gather friendly Bonjour/mDNS device names & services."""
    if not shutil.which("avahi-browse"):
        return {}
    try:
        # Capture raw bytes because mDNS TXT records (AirPlay/HomeKit/Matter) often contain binary blobs
        res = subprocess.run(
            ["avahi-browse", "-a", "-t", "-r", "-p"],
            capture_output=True,
            check=False,
            timeout=timeout_sec + 2,
        )
        if res.returncode == 0 and res.stdout:
            text_out = res.stdout.decode("utf-8", errors="replace") if isinstance(res.stdout, bytes) else str(res.stdout)
            return parse_avahi_browse(text_out)
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError):
        pass
    return {}


def reverse_dns_lookup(ip_str: str) -> str:
    """Best-effort reverse DNS lookup for a LAN IP."""
    try:
        host, _, _ = socket.gethostbyaddr(ip_str)
        clean = host.strip().removesuffix(".lan").removesuffix(".local").removesuffix(".home")
        if clean and clean != ip_str:
            return clean
    except (OSError, socket.herror, socket.gaierror):
        pass
    return ""


class NetworkScanner:
    """Thread-safe Home LAN + Pi Hotspot device inventory & live bandwidth tracker."""

    def __init__(
        self,
        webui_config_getter: Callable[[], dict[str, Any]],
        local_config_updater: Callable[[dict[str, Any]], Path],
        vpn_config_path: Path,
        state_dir: Optional[Path] = None,
        on_new_devices: Optional[Callable[[list[dict[str, Any]]], None]] = None,
    ):
        self._get_webui_cfg = webui_config_getter
        self._update_local_cfg = local_config_updater
        self._vpn_config_path = Path(vpn_config_path).expanduser().resolve()
        self._known_path = (Path(state_dir).expanduser().resolve() / "known_devices.json") if state_dir else None
        self._on_new_devices = on_new_devices

        self._lock = threading.Lock()
        self._prev_client_bytes: dict[str, tuple[float, int, int]] = {}
        self._prev_iface_bytes: dict[str, tuple[float, int, int]] = {}
        self._mdns_cache: dict[str, dict[str, Any]] = {}
        self._mdns_updated_at: float = 0.0
        self._rdns_cache: dict[str, tuple[float, str]] = {}
        self._last_snapshot: Optional[dict[str, Any]] = None
        self._last_scan_mono: float = 0.0
        self._known_state: dict[str, Any] = self._load_known_state()

    def _load_known_state(self) -> dict[str, Any]:
        if self._known_path and self._known_path.is_file():
            raw = vm.read_json(self._known_path, {})
            if isinstance(raw, dict) and isinstance(raw.get("devices"), dict):
                return {"initialized": bool(raw.get("initialized", True)), "devices": dict(raw["devices"])}
        return {"initialized": False, "devices": {}}

    def _save_known_state(self) -> None:
        if not self._known_path:
            return
        with self._lock:
            payload = {
                "initialized": bool(self._known_state.get("initialized", False)),
                "devices": dict(self._known_state.get("devices") or {}),
            }
        with suppress(OSError):
            self._known_path.parent.mkdir(parents=True, exist_ok=True)
            vm.write_json_atomic(self._known_path, payload, mode=0o600)

    def _alert_new_devices_enabled(self) -> bool:
        cfg = self._get_webui_cfg()
        return bool((cfg.get("network") or {}).get("alert_new_devices", True))

    def trust_device(self, mac: str = "", trust_all: bool = False) -> dict[str, Any]:
        """Mark one MAC address or all currently known devices as trusted (clears NEW badge)."""
        changed = False
        with self._lock:
            known_devs: dict[str, Any] = self._known_state.setdefault("devices", {})
            if trust_all:
                for rec in known_devs.values():
                    if isinstance(rec, dict) and not rec.get("trusted", False):
                        rec["trusted"] = True
                        changed = True
            else:
                norm = wm.normalize_mac(mac)
                rec = known_devs.setdefault(
                    norm,
                    {
                        "first_seen": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "last_seen": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "trusted": True,
                    },
                )
                if not rec.get("trusted", False):
                    rec["trusted"] = True
                    changed = True
            if changed:
                self._known_state["initialized"] = True
        if changed:
            self._save_known_state()
        return self.scan(active_sweep=False)

    def update_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Update network alert settings in webui/config.local.json."""
        if "alert_new_devices" in payload:
            self._update_local_cfg({"network": {"alert_new_devices": bool(payload["alert_new_devices"])}})
        return self.scan(active_sweep=False)

    def _get_aliases(self) -> dict[str, dict[str, str]]:
        cfg = self._get_webui_cfg()
        raw = (cfg.get("network") or {}).get("aliases") or {}
        if not isinstance(raw, dict):
            return {}
        out: dict[str, dict[str, str]] = {}
        for mac, info in raw.items():
            if not isinstance(info, dict):
                continue
            with suppress(ValueError):
                norm = wm.normalize_mac(mac)
                out[norm] = {
                    "name": str(info.get("name") or "").strip()[:60],
                    "category": str(info.get("category") or "").strip(),
                }
        return out

    def set_alias(self, mac: str, name: str = "", category: str = "") -> dict[str, Any]:
        """Save or clear a custom friendly name & category for a MAC address."""
        norm_mac = wm.normalize_mac(mac)
        clean_name = name.strip()[:60]
        clean_cat = category.strip().lower()
        if clean_cat and clean_cat not in DEVICE_CATEGORIES:
            raise ValueError(f"Invalid device category: {clean_cat!r}")

        aliases = self._get_aliases()
        if not clean_name and not clean_cat:
            aliases.pop(norm_mac, None)
        else:
            aliases[norm_mac] = {"name": clean_name, "category": clean_cat}
        self._update_local_cfg({"network": {"aliases": aliases}})
        return self.scan(active_sweep=False)

    def _compute_rate(
        self,
        store: dict[str, tuple[float, int, int]],
        key: str,
        now_mono: float,
        dl_bytes: int,
        ul_bytes: int,
    ) -> tuple[Optional[float], Optional[float]]:
        """Compute (download_mbps, upload_mbps) from monotonic byte counter deltas."""
        prev = store.get(key)
        store[key] = (now_mono, dl_bytes, ul_bytes)
        if not prev:
            return None, None
        dt = now_mono - prev[0]
        if dt < 0.25:
            return None, None
        d_dl = max(0, dl_bytes - prev[1])
        d_ul = max(0, ul_bytes - prev[2])
        return round((d_dl * 8.0) / (dt * 1_000_000.0), 2), round((d_ul * 8.0) / (dt * 1_000_000.0), 2)

    def _sample_interface_stats(self, iface: str, now_mono: float) -> dict[str, Any]:
        counters = read_interface_counters(iface)
        if counters is None:
            return {
                "interface": iface,
                "available": False,
                "rx_bytes": 0,
                "tx_bytes": 0,
                "rx_human": "—",
                "tx_human": "—",
                "rx_mbps": None,
                "tx_mbps": None,
                "rx_rate_human": "—",
                "tx_rate_human": "—",
            }
        rx_b, tx_b = counters
        rx_mbps, tx_mbps = self._compute_rate(self._prev_iface_bytes, iface, now_mono, rx_b, tx_b)
        return {
            "interface": iface,
            "available": True,
            "rx_bytes": rx_b,
            "tx_bytes": tx_b,
            "rx_human": vm._format_bytes(rx_b),
            "tx_human": vm._format_bytes(tx_b),
            "rx_mbps": rx_mbps,
            "tx_mbps": tx_mbps,
            "rx_rate_human": format_rate_mbps(rx_mbps),
            "tx_rate_human": format_rate_mbps(tx_mbps),
        }

    def _resolve_rdns_batch(self, ips: list[str]) -> dict[str, str]:
        now = time.monotonic()
        to_lookup: list[str] = []
        results: dict[str, str] = {}
        for ip_str in ips:
            cached = self._rdns_cache.get(ip_str)
            if cached and (now - cached[0]) < 300.0:
                results[ip_str] = cached[1]
            else:
                to_lookup.append(ip_str)

        if to_lookup:
            with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
                futs = {pool.submit(reverse_dns_lookup, ip): ip for ip in to_lookup}
                with suppress(concurrent.futures.TimeoutError):
                    for fut in concurrent.futures.as_completed(futs, timeout=2.5):
                        ip_str = futs[fut]
                        with suppress(Exception):
                            name = fut.result()
                            self._rdns_cache[ip_str] = (now, name)
                            results[ip_str] = name
        return results

    def scan(self, active_sweep: bool = False) -> dict[str, Any]:
        """Discover Hotspot + Home LAN devices, resolve names/types & compute live bandwidth."""
        vpn_cfg = vm.load_vpn_config(self._vpn_config_path)
        wan_iface = str(vpn_cfg.get("wan_interface") or "eth0")
        ap_iface = str((vpn_cfg.get("hotspot") or {}).get("interface") or "wlan0")
        ap_ssid = str((vpn_cfg.get("hotspot") or {}).get("ssid") or "UK-Frame-VPN")
        wg_iface = str((vpn_cfg.get("wireguard") or {}).get("interface") or "wg-vpn")
        ovpn_iface = str((vpn_cfg.get("openvpn") or {}).get("interface") or "tun-vpn")
        tun_iface = wg_iface if vm.interface_exists(wg_iface) else (ovpn_iface if vm.interface_exists(ovpn_iface) else "")

        if active_sweep and platform.system().lower() == "linux":
            sweep_lan_subnet(wan_iface)

        now_mono = time.monotonic()
        if active_sweep or (now_mono - self._mdns_updated_at) > 120.0:
            mdns_map = discover_mdns_hosts(timeout_sec=2)
            with self._lock:
                if mdns_map or active_sweep:
                    self._mdns_cache = mdns_map
                self._mdns_updated_at = now_mono
        else:
            with self._lock:
                mdns_map = dict(self._mdns_cache)

        # 1. Hotspot clients via `iw station dump` + dnsmasq leases + AP neighbor table
        iw_stations: dict[str, dict[str, Any]] = {}
        if vm.interface_exists(ap_iface) and shutil.which("iw"):
            st = subprocess.run(
                ["iw", "dev", ap_iface, "station", "dump"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=3,
            )
            if st.returncode == 0:
                iw_stations = parse_iw_station_dump(st.stdout)

        leases = parse_dnsmasq_leases()
        ap_neigh = wm.scan_lan_neighbors(ap_iface) if vm.interface_exists(ap_iface) else []
        ap_ip_by_mac = {item["mac"]: item["ip"] for item in ap_neigh}

        # 2. Home LAN neighbors via kernel ARP/neighbor table
        lan_neigh = wm.scan_lan_neighbors(wan_iface)
        gateway_ip = get_default_gateway(wan_iface) or get_default_gateway("")
        self_ip = vm.get_interface_ipv4(wan_iface) or ""
        self_mac = get_interface_mac(wan_iface)

        # Collect all IPs for parallel reverse-DNS lookup
        all_ips = [item["ip"] for item in lan_neigh]
        for mac in iw_stations:
            ip_guess = leases.get(mac, {}).get("ip") or ap_ip_by_mac.get(mac) or ""
            if ip_guess:
                all_ips.append(ip_guess)
        rdns_map = self._resolve_rdns_batch(list(dict.fromkeys(all_ips)))

        aliases = self._get_aliases()
        devices_by_mac: dict[str, dict[str, Any]] = {}

        with self._lock:
            wan_stats = self._sample_interface_stats(wan_iface, now_mono)
            ap_stats = self._sample_interface_stats(ap_iface, now_mono) if vm.interface_exists(ap_iface) else None
            vpn_stats = self._sample_interface_stats(tun_iface, now_mono) if tun_iface else None

            # Add Hotspot Clients (exact per-device bandwidth!)
            for mac, st_info in iw_stations.items():
                lease = leases.get(mac, {})
                ip_str = lease.get("ip") or ap_ip_by_mac.get(mac) or ""
                mdns_info = mdns_map.get(ip_str, {})
                dhcp_host = lease.get("hostname") or ""
                mdns_host = mdns_info.get("friendly") or mdns_info.get("hostname") or ""
                rdns_host = rdns_map.get(ip_str, "")
                raw_hostname = mdns_host or dhcp_host or rdns_host

                vendor, is_priv = lookup_mac_vendor(mac)
                alias = aliases.get(mac, {})
                auto_cat = infer_device_category(
                    hostname=raw_hostname,
                    vendor=vendor,
                    services=mdns_info.get("services"),
                    is_private_mac=is_priv,
                )
                cat_key = alias.get("category") or auto_cat
                cat_meta = DEVICE_CATEGORIES.get(cat_key, DEVICE_CATEGORIES["other"])

                # AP perspective: tx_bytes = client download, rx_bytes = client upload
                dl_bytes = int(st_info["tx_bytes"])
                ul_bytes = int(st_info["rx_bytes"])
                dl_mbps, ul_mbps = self._compute_rate(self._prev_client_bytes, mac, now_mono, dl_bytes, ul_bytes)

                display_name = alias.get("name") or raw_hostname or (vendor if vendor and not is_priv else f"Hotspot Device ({ip_str or mac[-5:]})")
                devices_by_mac[mac] = {
                    "mac": mac,
                    "ip": ip_str,
                    "network": "hotspot",
                    "network_label": f"VPN Hotspot ({ap_ssid})",
                    "online": True,
                    "name": display_name,
                    "custom_name": alias.get("name", ""),
                    "hostname": raw_hostname,
                    "vendor": vendor or "Unknown Vendor",
                    "is_private_mac": is_priv,
                    "category": cat_key,
                    "category_label": cat_meta["label"],
                    "icon": cat_meta["icon"],
                    "services": mdns_info.get("services") or [],
                    "signal_dbm": st_info["signal_dbm"],
                    "phy_rate": st_info["tx_bitrate"],
                    "connected_seconds": st_info["connected_seconds"],
                    "bandwidth_available": True,
                    "bandwidth_note": "",
                    "download_bytes": dl_bytes,
                    "upload_bytes": ul_bytes,
                    "download_human": vm._format_bytes(dl_bytes),
                    "upload_human": vm._format_bytes(ul_bytes),
                    "download_mbps": dl_mbps,
                    "upload_mbps": ul_mbps,
                    "download_rate_human": format_rate_mbps(dl_mbps),
                    "upload_rate_human": format_rate_mbps(ul_mbps),
                    "is_gateway": False,
                    "is_self": False,
                }

            # Add Home LAN Devices (eth0)
            for item in lan_neigh:
                mac = item["mac"]
                if mac in devices_by_mac:
                    continue
                ip_str = item["ip"]
                state = (item.get("state") or "").upper()
                if state in ("FAILED", "INCOMPLETE"):
                    continue
                is_gw = bool(gateway_ip and ip_str == gateway_ip)
                mdns_info = mdns_map.get(ip_str, {})
                mdns_host = mdns_info.get("friendly") or mdns_info.get("hostname") or ""
                rdns_host = rdns_map.get(ip_str, "")
                raw_hostname = mdns_host or rdns_host

                vendor, is_priv = lookup_mac_vendor(mac)
                alias = aliases.get(mac, {})
                auto_cat = infer_device_category(
                    hostname=raw_hostname,
                    vendor=vendor,
                    services=mdns_info.get("services"),
                    is_private_mac=is_priv,
                    is_gateway=is_gw,
                )
                cat_key = alias.get("category") or auto_cat
                cat_meta = DEVICE_CATEGORIES.get(cat_key, DEVICE_CATEGORIES["other"])

                fallback_name = "Main Home Router" if is_gw else (vendor if vendor and not is_priv else f"LAN Device ({ip_str})")
                display_name = alias.get("name") or raw_hostname or fallback_name

                devices_by_mac[mac] = {
                    "mac": mac,
                    "ip": ip_str,
                    "network": "lan",
                    "network_label": "Home Router LAN",
                    "online": state in ("REACHABLE", "DELAY", "PROBE", "ARP", "PERMANENT", "STALE", ""),
                    "neigh_state": state,
                    "name": display_name,
                    "custom_name": alias.get("name", ""),
                    "hostname": raw_hostname,
                    "vendor": vendor or "Unknown Vendor",
                    "is_private_mac": is_priv,
                    "category": cat_key,
                    "category_label": cat_meta["label"],
                    "icon": cat_meta["icon"],
                    "services": mdns_info.get("services") or [],
                    "signal_dbm": None,
                    "phy_rate": "",
                    "connected_seconds": None,
                    "bandwidth_available": False,
                    "bandwidth_note": "Direct via main router (not routed through Pi)",
                    "download_bytes": None,
                    "upload_bytes": None,
                    "download_human": "—",
                    "upload_human": "—",
                    "download_mbps": None,
                    "upload_mbps": None,
                    "download_rate_human": "—",
                    "upload_rate_human": "—",
                    "is_gateway": is_gw,
                    "is_self": False,
                }

            # Include this Raspberry Pi itself if MAC/IP available
            if self_ip and self_mac and self_mac not in devices_by_mac:
                alias = aliases.get(self_mac, {})
                cat_key = alias.get("category") or "pi"
                cat_meta = DEVICE_CATEGORIES.get(cat_key, DEVICE_CATEGORIES["pi"])
                devices_by_mac[self_mac] = {
                    "mac": self_mac,
                    "ip": self_ip,
                    "network": "lan",
                    "network_label": "This Raspberry Pi",
                    "online": True,
                    "neigh_state": "LOCAL",
                    "name": alias.get("name") or f"{socket.gethostname()} (This Pi)",
                    "custom_name": alias.get("name", ""),
                    "hostname": socket.gethostname(),
                    "vendor": "Raspberry Pi",
                    "is_private_mac": False,
                    "category": cat_key,
                    "category_label": cat_meta["label"],
                    "icon": cat_meta["icon"],
                    "services": [],
                    "signal_dbm": None,
                    "phy_rate": "",
                    "connected_seconds": None,
                    "bandwidth_available": wan_stats["available"],
                    "bandwidth_note": "",
                    "download_bytes": wan_stats["rx_bytes"],
                    "upload_bytes": wan_stats["tx_bytes"],
                    "download_human": wan_stats["rx_human"],
                    "upload_human": wan_stats["tx_human"],
                    "download_mbps": wan_stats["rx_mbps"],
                    "upload_mbps": wan_stats["tx_mbps"],
                    "download_rate_human": wan_stats["rx_rate_human"],
                    "upload_rate_human": wan_stats["tx_rate_human"],
                    "is_gateway": False,
                    "is_self": True,
                }

        def _sort_key(dev: dict[str, Any]) -> tuple[int, int, tuple[int, ...]]:
            net_rank = 0 if dev["network"] == "hotspot" else (1 if dev["is_self"] else (2 if dev["is_gateway"] else 3))
            ip_tuple = tuple(int(p) for p in dev["ip"].split(".")) if dev["ip"] and "." in dev["ip"] else (999, 999, 999, 999)
            return (net_rank, 0 if dev["online"] else 1, ip_tuple)

        devices = sorted(devices_by_mac.values(), key=_sort_key)
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

        # Update known-device registry & detect brand-new unknown MAC addresses
        newly_discovered: list[dict[str, Any]] = []
        known_dirty = False
        with self._lock:
            known_devs: dict[str, Any] = self._known_state.setdefault("devices", {})
            initialized = bool(self._known_state.get("initialized", False))
            if not initialized and devices:
                # First-run baseline: mark all currently present devices as trusted to avoid alert spam
                for dev in devices:
                    known_devs[dev["mac"]] = {
                        "first_seen": now_iso,
                        "last_seen": now_iso,
                        "last_ip": dev["ip"],
                        "last_name": dev["name"],
                        "network": dev["network"],
                        "trusted": True,
                    }
                    dev["trusted"] = True
                    dev["is_new"] = False
                    dev["first_seen"] = now_iso
                self._known_state["initialized"] = True
                known_dirty = True
            else:
                for dev in devices:
                    mac = dev["mac"]
                    rec = known_devs.get(mac)
                    if rec is None:
                        auto_trust = bool(dev.get("is_self") or dev.get("is_gateway") or dev.get("custom_name"))
                        rec = {
                            "first_seen": now_iso,
                            "last_seen": now_iso,
                            "last_ip": dev["ip"],
                            "last_name": dev["name"],
                            "network": dev["network"],
                            "trusted": auto_trust,
                        }
                        known_devs[mac] = rec
                        known_dirty = True
                        if not auto_trust:
                            newly_discovered.append(dev)
                    else:
                        if rec.get("last_ip") != dev["ip"] or rec.get("last_name") != dev["name"]:
                            rec["last_ip"] = dev["ip"]
                            rec["last_name"] = dev["name"]
                            known_dirty = True
                        rec["last_seen"] = now_iso
                        if dev.get("custom_name") and not rec.get("trusted"):
                            rec["trusted"] = True
                            known_dirty = True
                    dev["trusted"] = bool(rec.get("trusted", False))
                    dev["is_new"] = not dev["trusted"]
                    dev["first_seen"] = str(rec.get("first_seen") or now_iso)

        if known_dirty:
            self._save_known_state()

        alert_enabled = self._alert_new_devices_enabled()
        if newly_discovered and alert_enabled and self._on_new_devices:
            with suppress(Exception):
                self._on_new_devices(newly_discovered)

        hotspot_count = sum(1 for d in devices if d["network"] == "hotspot")
        lan_count = sum(1 for d in devices if d["network"] == "lan")
        unknown_count = sum(1 for d in devices if d.get("is_new"))

        snapshot = {
            "scanned_at": now_iso,
            "active_sweep": active_sweep,
            "summary": {
                "total_devices": len(devices),
                "hotspot_devices": hotspot_count,
                "lan_devices": lan_count,
                "unknown_devices": unknown_count,
                "alert_new_devices": alert_enabled,
                "hotspot_ssid": ap_ssid,
                "wan_interface": wan_iface,
                "gateway_ip": gateway_ip,
                "pi_ip": self_ip,
                "wan_stats": wan_stats,
                "ap_stats": ap_stats,
                "vpn_stats": vpn_stats,
                "mdns_available": bool(shutil.which("avahi-browse")),
            },
            "categories": DEVICE_CATEGORIES,
            "devices": devices,
        }
        with self._lock:
            self._last_snapshot = snapshot
            self._last_scan_mono = now_mono
        return snapshot

    def get_snapshot(self, max_age_seconds: float = 8.0) -> dict[str, Any]:
        """Return cached snapshot if very fresh, otherwise run a lightweight refresh."""
        with self._lock:
            if self._last_snapshot is not None and (time.monotonic() - self._last_scan_mono) < max_age_seconds:
                return self._last_snapshot
        return self.scan(active_sweep=False)
