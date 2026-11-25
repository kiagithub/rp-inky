#!/usr/bin/env python3
"""Always-on high-speed Surfshark VPN manager, Wi-Fi hotspot and UK failover watchdog.

Supports importing multiple WireGuard (.conf) and OpenVPN (.ovpn) profiles,
switching between them atomically, broadcasting a 5 GHz 802.11ac VHT80 Wi-Fi
hotspot (plus optional Gigabit Ethernet LAN gateway forwarding), enforcing a
leak-proof nftables kill-switch, and keeping the Pi's own photo frame traffic
(Open-Meteo & Nominatim) on the native WAN route.
"""

import argparse
import copy
import getpass
import ipaddress
import json
import logging
import os
import random
import re
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.request
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

LOG = logging.getLogger("rp-vpn")

RUNTIME_DIR = Path("/run/rp-vpn")
SYSCTL_DEST = Path("/etc/sysctl.d/99-rp-vpn.conf")
MODULES_DEST = Path("/etc/modules-load.d/rp-vpn.conf")
NM_UNMANAGED_DEST = Path("/etc/NetworkManager/conf.d/99-rp-vpn-unmanaged.conf")

RULE_PRIO_MULTICAST = 80
RULE_PRIO_VPN_SRC = 95
RULE_PRIO_HOTSPOT = 100
RULE_PRIO_LAN_GW = 105

DEFAULT_CONFIG: dict[str, Any] = {
    "wan_interface": "eth0",
    "preferred_country": "uk",
    "prefer_protocol": "wireguard",
    "route_table": 52000,
    "dns_servers": ["162.252.172.57", "149.154.159.92"],
    "force_vpn_dns": True,
    "disable_ipv6": True,
    "allow_lan_gateway": True,
    "wireguard": {
        "interface": "wg-vpn",
        "mtu": 1420,
        "persistent_keepalive": 25,
        "private_key": "",
    },
    "openvpn": {
        "interface": "tun-vpn",
        "auth_file": "auth.txt",
        "data_ciphers": "CHACHA20-POLY1305:AES-128-GCM:AES-256-GCM:AES-256-CBC",
    },
    "hotspot": {
        "enabled": True,
        "interface": "wlan0",
        "ssid": "RP-UK-VPN",
        "passphrase": "",
        "country_code": "US",
        "band": "5g",
        "channel": 36,
        "vht80": True,
        "gateway_ip": "10.42.0.1",
        "subnet_cidr": "10.42.0.0/24",
        "dhcp_start": "10.42.0.100",
        "dhcp_end": "10.42.0.200",
        "dhcp_lease": "12h",
        "allow_ssh": True,
    },
    "watchdog": {
        "enabled": True,
        "auto_failover": True,
        "failover_threshold": 2,
        "max_handshake_age_seconds": 180,
        "probe_targets": ["1.1.1.1", "9.9.9.9", "8.8.8.8"],
        "probe_timeout_seconds": 3,
    },
}

# 5 GHz 80 MHz VHT channel blocks -> center frequency segment 0 index
VHT80_CENTER_SEG0: dict[int, int] = {
    36: 42,
    40: 42,
    44: 42,
    48: 42,
    52: 58,
    56: 58,
    60: 58,
    64: 58,
    100: 106,
    104: 106,
    108: 106,
    112: 106,
    116: 122,
    120: 122,
    124: 122,
    128: 122,
    132: 138,
    136: 138,
    140: 138,
    144: 138,
    149: 155,
    153: 155,
    157: 155,
    161: 155,
}

HT40_PLUS_CHANNELS = {36, 44, 52, 60, 100, 108, 116, 124, 132, 140, 149, 157}
HT40_MINUS_CHANNELS = {40, 48, 56, 64, 104, 112, 120, 128, 136, 144, 153, 161}
VALID_5G_CHANNELS = HT40_PLUS_CHANNELS | HT40_MINUS_CHANNELS | {165}

IFACE_RE = re.compile(r"^[a-zA-Z0-9_.-]{1,15}$")
PROFILE_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
COUNTRY_ALIASES = {"gb": "uk", "uk": "uk", "greatbritain": "uk"}


@dataclass(frozen=True)
class Profile:
    """Metadata for an imported WireGuard or OpenVPN profile."""

    name: str
    protocol: str  # "wireguard" or "openvpn"
    path: Path
    endpoint_host: str = ""
    endpoint_port: int = 0
    transport: str = "udp"
    country: str = ""
    public_key: str = ""
    requires_auth: bool = False


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_json(path: Path, default: Any) -> Any:
    """Read JSON from path, returning default if missing or malformed."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        LOG.warning("Could not read %s: %s", path, exc)
        return default


def write_text_atomic(path: Path, content: str, mode: int = 0o644) -> None:
    """Atomically write text to disk with explicit file permissions."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except Exception:
        with suppress(OSError):
            temporary.unlink()
        raise


def write_json_atomic(path: Path, data: Any, mode: int = 0o644) -> None:
    """Atomically write formatted JSON to disk."""
    write_text_atomic(path, json.dumps(data, indent=2, sort_keys=True) + "\n", mode=mode)


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge override into a deep copy of base."""
    result = copy.deepcopy(base)
    if not isinstance(override, dict):
        return result
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def canonical_country(code: str) -> str:
    """Normalize country tags so 'gb' and 'uk' match seamlessly."""
    cleaned = re.sub(r"[^a-z]", "", (code or "").lower())
    return COUNTRY_ALIASES.get(cleaned, cleaned)


def infer_country_tag(name: str, endpoint_host: str = "") -> str:
    """Infer a 2-letter country tag from a Surfshark hostname or profile slug."""
    for candidate in (endpoint_host.lower(), name.lower()):
        if not candidate:
            continue
        first_label = candidate.split(".")[0]
        match = re.match(r"^([a-z]{2})(?:[-_]|$)", first_label)
        if match:
            return canonical_country(match.group(1))
    return ""


def normalize_profile_name(raw_name: str) -> str:
    """Convert a filename or user label into a clean profile slug.

    Examples:
      'uk-lon.prod.surfshark.com.conf'     -> 'uk-lon'
      'uk-man.prod.surfshark.com_udp.ovpn' -> 'uk-man-udp'
      'Surfshark_WireGuard_uk-gla.conf'    -> 'uk-gla'
    """
    text = Path(raw_name).name
    for suffix in (".conf", ".ovpn"):
        if text.lower().endswith(suffix):
            text = text[: -len(suffix)]
            break
    text = re.sub(r"\.prod\.surfshark\.com", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^(?:surfshark[-_]+)?(?:wireguard|openvpn)[-_]+", "", text, flags=re.IGNORECASE)
    slug = re.sub(r"[^a-z0-9-]+", "-", text.lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    if not slug:
        slug = "profile"
    if not slug[0].isalnum():
        slug = f"p-{slug}"
    return slug[:63]


def _validate_iface(value: Any, default: str) -> str:
    text = str(value or "").strip()
    if IFACE_RE.match(text):
        return text
    LOG.warning("Invalid interface %r; using %s", value, default)
    return default


def _validate_ipv4(value: Any, default: str) -> str:
    try:
        addr = ipaddress.IPv4Address(str(value).strip())
        return str(addr)
    except ValueError:
        LOG.warning("Invalid IPv4 address %r; using %s", value, default)
        return default


def validate_vpn_config(config: dict[str, Any]) -> dict[str, Any]:
    """Validate and clamp VPN, hotspot and watchdog settings."""
    config["wan_interface"] = _validate_iface(config.get("wan_interface"), "eth0")
    config["preferred_country"] = canonical_country(str(config.get("preferred_country") or "uk"))
    prefer_proto = str(config.get("prefer_protocol") or "wireguard").lower()
    config["prefer_protocol"] = prefer_proto if prefer_proto in ("wireguard", "openvpn") else "wireguard"

    try:
        table = int(config.get("route_table", 52000))
    except (TypeError, ValueError):
        table = 52000
    if table in (0, 253, 254, 255) or table < 1 or table > 2147483647:
        LOG.warning("Invalid route_table %r; using 52000", config.get("route_table"))
        table = 52000
    config["route_table"] = table

    raw_dns = config.get("dns_servers")
    dns_list: list[str] = []
    if isinstance(raw_dns, list):
        for item in raw_dns:
            with suppress(ValueError):
                dns_list.append(str(ipaddress.IPv4Address(str(item).strip())))
    config["dns_servers"] = dns_list or list(DEFAULT_CONFIG["dns_servers"])

    for flag in ("force_vpn_dns", "disable_ipv6", "allow_lan_gateway"):
        config[flag] = bool(config.get(flag, DEFAULT_CONFIG[flag]))

    wg = config.get("wireguard")
    if not isinstance(wg, dict):
        wg = copy.deepcopy(DEFAULT_CONFIG["wireguard"])
        config["wireguard"] = wg
    wg["interface"] = _validate_iface(wg.get("interface"), "wg-vpn")
    try:
        mtu = int(wg.get("mtu", 1420))
    except (TypeError, ValueError):
        mtu = 1420
    wg["mtu"] = max(1280, min(1500, mtu))
    try:
        keepalive = int(wg.get("persistent_keepalive", 25))
    except (TypeError, ValueError):
        keepalive = 25
    wg["persistent_keepalive"] = max(0, min(300, keepalive))
    wg["private_key"] = str(wg.get("private_key") or "").strip()

    ovpn = config.get("openvpn")
    if not isinstance(ovpn, dict):
        ovpn = copy.deepcopy(DEFAULT_CONFIG["openvpn"])
        config["openvpn"] = ovpn
    ovpn["interface"] = _validate_iface(ovpn.get("interface"), "tun-vpn")
    ovpn["auth_file"] = str(ovpn.get("auth_file") or "auth.txt").strip() or "auth.txt"
    ovpn["data_ciphers"] = str(ovpn.get("data_ciphers") or "").strip()

    hotspot = config.get("hotspot")
    if not isinstance(hotspot, dict):
        hotspot = copy.deepcopy(DEFAULT_CONFIG["hotspot"])
        config["hotspot"] = hotspot
    hotspot["enabled"] = bool(hotspot.get("enabled", True))
    hotspot["interface"] = _validate_iface(hotspot.get("interface"), "wlan0")
    ssid = str(hotspot.get("ssid") or "RP-UK-VPN").replace("\n", "").replace("\r", "").strip()
    hotspot["ssid"] = (ssid or "RP-UK-VPN")[:32]
    hotspot["passphrase"] = str(hotspot.get("passphrase") or "").replace("\n", "").replace("\r", "")
    cc = str(hotspot.get("country_code") or "US").strip().upper()
    hotspot["country_code"] = cc if re.match(r"^[A-Z]{2}$", cc) else "US"
    band = str(hotspot.get("band") or "5g").strip().lower()
    hotspot["band"] = band if band in ("5g", "2.4g") else "5g"
    try:
        channel = int(hotspot.get("channel", 36 if hotspot["band"] == "5g" else 6))
    except (TypeError, ValueError):
        channel = 36 if hotspot["band"] == "5g" else 6
    if hotspot["band"] == "5g" and channel not in VALID_5G_CHANNELS:
        LOG.warning("Unsupported 5 GHz channel %s; using 36", channel)
        channel = 36
    elif hotspot["band"] == "2.4g" and not (1 <= channel <= 13):
        LOG.warning("Unsupported 2.4 GHz channel %s; using 6", channel)
        channel = 6
    hotspot["channel"] = channel
    hotspot["vht80"] = bool(hotspot.get("vht80", True))

    try:
        subnet = ipaddress.IPv4Network(str(hotspot.get("subnet_cidr", "10.42.0.0/24")), strict=False)
    except ValueError:
        subnet = ipaddress.IPv4Network("10.42.0.0/24")
    hotspot["subnet_cidr"] = str(subnet)
    hotspot["gateway_ip"] = _validate_ipv4(hotspot.get("gateway_ip"), str(subnet.network_address + 1))
    hotspot["dhcp_start"] = _validate_ipv4(hotspot.get("dhcp_start"), str(subnet.network_address + 100))
    hotspot["dhcp_end"] = _validate_ipv4(hotspot.get("dhcp_end"), str(subnet.network_address + 200))
    lease = str(hotspot.get("dhcp_lease") or "12h").strip()
    hotspot["dhcp_lease"] = lease if re.match(r"^\d+[smhd]$", lease) else "12h"
    hotspot["allow_ssh"] = bool(hotspot.get("allow_ssh", True))

    watchdog = config.get("watchdog")
    if not isinstance(watchdog, dict):
        watchdog = copy.deepcopy(DEFAULT_CONFIG["watchdog"])
        config["watchdog"] = watchdog
    watchdog["enabled"] = bool(watchdog.get("enabled", True))
    watchdog["auto_failover"] = bool(watchdog.get("auto_failover", True))
    try:
        threshold = int(watchdog.get("failover_threshold", 2))
    except (TypeError, ValueError):
        threshold = 2
    watchdog["failover_threshold"] = max(1, min(20, threshold))
    try:
        max_age = int(watchdog.get("max_handshake_age_seconds", 180))
    except (TypeError, ValueError):
        max_age = 180
    watchdog["max_handshake_age_seconds"] = max(60, min(3600, max_age))
    try:
        probe_timeout = int(watchdog.get("probe_timeout_seconds", 3))
    except (TypeError, ValueError):
        probe_timeout = 3
    watchdog["probe_timeout_seconds"] = max(1, min(15, probe_timeout))
    targets: list[str] = []
    if isinstance(watchdog.get("probe_targets"), list):
        for item in watchdog["probe_targets"]:
            with suppress(ValueError):
                targets.append(str(ipaddress.IPv4Address(str(item).strip())))
    watchdog["probe_targets"] = targets or list(DEFAULT_CONFIG["watchdog"]["probe_targets"])
    return config


def local_config_path_for(config_path: Path) -> Path:
    """Return the git-ignored config.local.json path adjacent to config_path."""
    return config_path.with_name(f"{config_path.stem}.local{config_path.suffix}")


def load_vpn_config(config_path: Path) -> dict[str, Any]:
    """Load config.json + optional config.local.json, validate and attach paths."""
    config_path = Path(config_path).expanduser().resolve()
    raw = read_json(config_path, {})
    if not isinstance(raw, dict):
        raw = {}
    local_path = local_config_path_for(config_path)
    if local_path != config_path and local_path.is_file():
        local_raw = read_json(local_path, {})
        if isinstance(local_raw, dict):
            raw = deep_merge(raw, local_raw)
    config = validate_vpn_config(deep_merge(DEFAULT_CONFIG, raw))

    base_dir = config_path.parent
    auth_rel = Path(config["openvpn"]["auth_file"]).expanduser()
    config["_config_path"] = config_path
    config["_local_config_path"] = local_path
    config["_base_dir"] = base_dir
    config["_profiles_dir"] = base_dir / "profiles"
    config["_state_dir"] = base_dir / ".state"
    config["_state_path"] = base_dir / ".state" / "state.json"
    config["_auth_path"] = auth_rel if auth_rel.is_absolute() else (base_dir / auth_rel)
    return config


def update_local_config(config_path: Path, patch: dict[str, Any]) -> Path:
    """Deep-merge patch into git-ignored config.local.json with 0600 permissions."""
    config_path = Path(config_path).expanduser().resolve()
    local_path = local_config_path_for(config_path)
    existing = read_json(local_path, {})
    if not isinstance(existing, dict):
        existing = {}
    merged = deep_merge(existing, patch)
    write_json_atomic(local_path, merged, mode=0o600)
    return local_path


def _split_endpoint(endpoint: str) -> tuple[str, int]:
    """Split a WireGuard Endpoint 'host:port' or '[ipv6]:port'."""
    text = endpoint.strip()
    if not text:
        return "", 0
    if text.startswith("[") and "]:" in text:
        host, _, port_str = text[1:].partition("]:")
    else:
        host, _, port_str = text.rpartition(":")
    try:
        port = int(port_str)
    except ValueError:
        return text, 0
    return host.strip(), port


def parse_wireguard_profile(path: Path) -> Profile:
    """Extract endpoint, key and country metadata from a WireGuard .conf file."""
    path = Path(path)
    name = path.stem.lower()
    section = ""
    endpoint_host = ""
    endpoint_port = 0
    public_key = ""

    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key_lower = key.strip().lower()
        val = value.strip()
        if section == "peer":
            if key_lower == "endpoint" and not endpoint_host:
                endpoint_host, endpoint_port = _split_endpoint(val)
            elif key_lower == "publickey" and not public_key:
                public_key = val

    return Profile(
        name=name,
        protocol="wireguard",
        path=path,
        endpoint_host=endpoint_host,
        endpoint_port=endpoint_port,
        transport="udp",
        country=infer_country_tag(name, endpoint_host),
        public_key=public_key,
        requires_auth=False,
    )


def parse_openvpn_profile(path: Path) -> Profile:
    """Extract remote server, transport and auth metadata from an OpenVPN .ovpn file."""
    path = Path(path)
    name = path.stem.lower()
    remote_host = ""
    remote_port = 0
    transport = "udp"
    requires_auth = False
    in_inline_block = False

    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("<") and not stripped.startswith("</"):
            in_inline_block = True
            continue
        if stripped.startswith("</"):
            in_inline_block = False
            continue
        if in_inline_block:
            continue
        line = stripped.split("#", 1)[0].split(";", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        directive = parts[0].lower()
        if directive == "remote" and len(parts) >= 2 and not remote_host:
            remote_host = parts[1]
            if len(parts) >= 3:
                with suppress(ValueError):
                    remote_port = int(parts[2])
            if len(parts) >= 4 and parts[3].lower().startswith(("tcp", "udp")):
                transport = "tcp" if parts[3].lower().startswith("tcp") else "udp"
        elif directive == "proto" and len(parts) >= 2:
            transport = "tcp" if parts[1].lower().startswith("tcp") else "udp"
        elif directive == "auth-user-pass":
            requires_auth = True

    return Profile(
        name=name,
        protocol="openvpn",
        path=path,
        endpoint_host=remote_host,
        endpoint_port=remote_port,
        transport=transport,
        country=infer_country_tag(name, remote_host),
        requires_auth=requires_auth,
    )


def discover_profiles(profiles_dir: Path) -> list[Profile]:
    """Return all valid WireGuard and OpenVPN profiles sorted by name."""
    directory = Path(profiles_dir)
    if not directory.is_dir():
        return []
    profiles: list[Profile] = []
    seen_names: set[str] = set()
    # Load .conf before .ovpn so WireGuard wins if identical stems were placed manually.
    files = sorted(
        (p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in (".conf", ".ovpn")),
        key=lambda p: (0 if p.suffix.lower() == ".conf" else 1, p.stem.lower()),
    )
    for file_path in files:
        stem = file_path.stem.lower()
        if not PROFILE_SLUG_RE.match(stem):
            LOG.warning("Skipping profile with invalid filename %s (use lowercase letters, digits, - or _)", file_path.name)
            continue
        if stem in seen_names:
            LOG.warning("Skipping duplicate profile stem %s (%s)", stem, file_path.name)
            continue
        seen_names.add(stem)
        if file_path.suffix.lower() == ".conf":
            profiles.append(parse_wireguard_profile(file_path))
        else:
            profiles.append(parse_openvpn_profile(file_path))
    return sorted(profiles, key=lambda item: item.name)


def import_profiles(
    source: Path,
    profiles_dir: Path,
    custom_name: Optional[str] = None,
    overwrite: bool = False,
) -> list[Profile]:
    """Import a .conf/.ovpn file or directory of files into profiles_dir."""
    source = Path(source).expanduser().resolve()
    profiles_dir = Path(profiles_dir).expanduser().resolve()
    profiles_dir.mkdir(parents=True, exist_ok=True)

    if source.is_file():
        if source.suffix.lower() not in (".conf", ".ovpn"):
            raise ValueError(f"Unsupported profile extension: {source.name} (expected .conf or .ovpn)")
        candidates = [source]
    elif source.is_dir():
        if custom_name:
            raise ValueError("--name can only be used when importing a single file")
        candidates = sorted(
            (p for p in source.rglob("*") if p.is_file() and p.suffix.lower() in (".conf", ".ovpn")),
            key=lambda p: (0 if p.suffix.lower() == ".conf" else 1, str(p).lower()),
        )
    else:
        raise FileNotFoundError(f"Source not found: {source}")

    if not candidates:
        raise ValueError(f"No .conf or .ovpn files found in {source}")

    imported: list[Profile] = []
    for candidate in candidates:
        ext = candidate.suffix.lower()
        slug = normalize_profile_name(custom_name or candidate.name)
        if not PROFILE_SLUG_RE.match(slug):
            raise ValueError(f"Invalid profile name: {slug}")

        opposite_ext = ".ovpn" if ext == ".conf" else ".conf"
        if custom_name is None and (profiles_dir / f"{slug}{opposite_ext}").exists():
            suffix_tag = "wg" if ext == ".conf" else "ovpn"
            slug = f"{slug}-{suffix_tag}"[:63]

        target = profiles_dir / f"{slug}{ext}"
        if target.exists() and not overwrite and target.resolve() != candidate.resolve():
            raise FileExistsError(f"Profile '{slug}' already exists at {target} (pass --force to overwrite)")

        content = candidate.read_text(encoding="utf-8", errors="replace")
        write_text_atomic(target, content, mode=0o600)
        profile = parse_wireguard_profile(target) if ext == ".conf" else parse_openvpn_profile(target)
        imported.append(profile)
    return imported


def select_failover_candidates(
    profiles: list[Profile],
    current_name: Optional[str] = None,
    preferred_country: str = "uk",
    prefer_protocol: str = "wireguard",
) -> list[Profile]:
    """Order candidate profiles for initial selection or automatic UK failover.

    When preferred_country is set (e.g. 'uk'), only profiles matching that country
    (or untagged custom profiles if zero country-matched profiles exist) are
    eligible for automatic failover, guaranteeing the UK exit requirement.
    Preferred protocol (WireGuard) is always prioritized ahead of OpenVPN.
    """
    if not profiles:
        return []

    wanted_country = canonical_country(preferred_country)
    if wanted_country:
        country_pool = [p for p in profiles if p.country == wanted_country]
        if not country_pool:
            country_pool = [p for p in profiles if not p.country]
    else:
        country_pool = list(profiles)

    if not country_pool:
        return []

    def _rotate_after_current(group: list[Profile]) -> list[Profile]:
        if not group:
            return []
        ordered = sorted(group, key=lambda p: (0 if p.transport == "udp" else 1, p.name))
        if current_name:
            for idx, item in enumerate(ordered):
                if item.name == current_name:
                    return ordered[idx + 1 :] + ordered[:idx]
        return ordered

    primary_group = [p for p in country_pool if p.protocol == prefer_protocol]
    secondary_group = [p for p in country_pool if p.protocol != prefer_protocol]
    return _rotate_after_current(primary_group) + _rotate_after_current(secondary_group)


def build_wireguard_runtime_config(
    profile_text: str,
    config: dict[str, Any],
    endpoint_override: Optional[str] = None,
) -> tuple[str, list[str], list[str]]:
    """Sanitize a WireGuard .conf into a safe, high-speed wg-quick config.

    Returns (rendered_config, ipv4_addresses, extracted_dns_servers).
    """
    disable_ipv6 = bool(config.get("disable_ipv6", True))
    mtu = int(config["wireguard"]["mtu"])
    keepalive = int(config["wireguard"]["persistent_keepalive"])
    fallback_privkey = str(config["wireguard"].get("private_key") or "").strip()

    interface_kv: list[tuple[str, str]] = []
    peers: list[list[tuple[str, str]]] = []
    current_peer: Optional[list[tuple[str, str]]] = None
    section = ""

    for raw_line in profile_text.splitlines():
        stripped = raw_line.split("#", 1)[0].strip()
        if not stripped:
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip().lower()
            if section == "peer":
                current_peer = []
                peers.append(current_peer)
            else:
                current_peer = None
            continue
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        k = key.strip()
        v = value.strip()
        if section == "interface":
            interface_kv.append((k, v))
        elif section == "peer" and current_peer is not None:
            current_peer.append((k, v))

    private_key = ""
    ipv4_addrs: list[str] = []
    other_addrs: list[str] = []
    extracted_dns: list[str] = []
    extra_iface_lines: list[str] = []

    blocked_iface_keys = {"dns", "mtu", "table", "preup", "postup", "predown", "postdown", "saveconfig"}

    for key, value in interface_kv:
        key_low = key.lower()
        if key_low == "privatekey":
            if value and not (value.startswith("<") and value.endswith(">")):
                private_key = value
        elif key_low == "address":
            for token in (part.strip() for part in value.split(",") if part.strip()):
                with suppress(ValueError):
                    iface_ip = ipaddress.ip_interface(token)
                    if isinstance(iface_ip, ipaddress.IPv4Interface):
                        ipv4_addrs.append(str(iface_ip))
                    elif not disable_ipv6:
                        other_addrs.append(str(iface_ip))
        elif key_low == "dns":
            for token in (part.strip() for part in value.split(",") if part.strip()):
                with suppress(ValueError):
                    extracted_dns.append(str(ipaddress.IPv4Address(token)))
        elif key_low in blocked_iface_keys:
            continue
        else:
            extra_iface_lines.append(f"{key} = {value}")

    if not private_key:
        private_key = fallback_privkey
    if not private_key:
        raise ValueError(
            "WireGuard profile has no PrivateKey. Download a complete Surfshark .conf "
            "or set wireguard.private_key in vpn/config.local.json."
        )
    if not ipv4_addrs:
        raise ValueError("WireGuard profile has no valid IPv4 Address in [Interface].")
    if not peers:
        raise ValueError("WireGuard profile has no [Peer] section.")

    all_addrs = ipv4_addrs + other_addrs
    # Table = off prevents wg-quick from colliding with existing routes;
    # start_wireguard_profile manages our dedicated policy routing table directly.
    out_lines = [
        "[Interface]",
        f"PrivateKey = {private_key}",
        f"Address = {', '.join(all_addrs)}",
        f"MTU = {mtu}",
        "Table = off",
        *extra_iface_lines,
    ]

    for peer_kv in peers:
        out_lines.append("")
        out_lines.append("[Peer]")
        has_allowed = False
        for key, value in peer_kv:
            key_low = key.lower()
            if key_low == "persistentkeepalive":
                continue
            if key_low == "endpoint" and endpoint_override:
                out_lines.append(f"Endpoint = {endpoint_override}")
                continue
            if key_low == "allowedips":
                has_allowed = True
                if disable_ipv6:
                    v4_nets = []
                    for token in (part.strip() for part in value.split(",") if part.strip()):
                        with suppress(ValueError):
                            net = ipaddress.ip_network(token, strict=False)
                            if isinstance(net, ipaddress.IPv4Network):
                                v4_nets.append(str(net))
                    out_lines.append(f"AllowedIPs = {', '.join(v4_nets or ['0.0.0.0/0'])}")
                else:
                    out_lines.append(f"AllowedIPs = {value}")
            else:
                out_lines.append(f"{key} = {value}")
        if not has_allowed:
            out_lines.append("AllowedIPs = 0.0.0.0/0" if disable_ipv6 else "AllowedIPs = 0.0.0.0/0, ::/0")
        if keepalive > 0:
            out_lines.append(f"PersistentKeepalive = {keepalive}")

    dns_servers = extracted_dns or list(config["dns_servers"])
    ipv4_hosts = [addr.split("/")[0] for addr in ipv4_addrs]
    return "\n".join(out_lines) + "\n", ipv4_hosts, dns_servers


def build_openvpn_runtime_config(
    profile_text: str,
    config: dict[str, Any],
    auth_path: Path,
    route_up_command: str,
) -> str:
    """Sanitize a Surfshark .ovpn into a non-interactive policy-routed OpenVPN config."""
    tun_iface = config["openvpn"]["interface"]
    data_ciphers = str(config["openvpn"].get("data_ciphers") or "").strip()

    kept_lines: list[str] = []
    in_inline_block = False
    saw_auth_user_pass = False
    is_udp = True

    stripped_directives = {
        "auth-nocache",
        "auth-user-pass",
        "dev",
        "dev-type",
        "redirect-gateway",
        "route-nopull",
        "script-security",
        "up",
        "down",
        "route-up",
        "data-ciphers",
        "fast-io",
        "sndbuf",
        "rcvbuf",
        "keepalive",
        "persist-key",
        "persist-tun",
        "iproute",
    }

    for raw_line in profile_text.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("<") and not stripped.startswith("</"):
            in_inline_block = True
            kept_lines.append(raw_line)
            continue
        if stripped.startswith("</"):
            in_inline_block = False
            kept_lines.append(raw_line)
            continue
        if in_inline_block:
            kept_lines.append(raw_line)
            continue

        code = stripped.split("#", 1)[0].split(";", 1)[0].strip()
        if not code:
            continue
        parts = code.split()
        directive = parts[0].lower()
        if directive == "auth-user-pass":
            saw_auth_user_pass = True
            continue
        if directive == "proto" and len(parts) >= 2:
            is_udp = not parts[1].lower().startswith("tcp")
        elif directive == "remote" and len(parts) >= 4:
            is_udp = not parts[3].lower().startswith("tcp")
        if directive in stripped_directives:
            continue
        kept_lines.append(code)

    kept_lines.extend(
        [
            "",
            "# Managed by rp-vpn",
            f"dev {tun_iface}",
            "dev-type tun",
            "route-nopull",
            "persist-key",
            "persist-tun",
            "keepalive 10 30",
            "connect-retry 3 15",
            "sndbuf 393216",
            "rcvbuf 393216",
        ]
    )
    if is_udp:
        kept_lines.append("fast-io")
    if data_ciphers:
        kept_lines.append(f"data-ciphers {data_ciphers}")
    if saw_auth_user_pass:
        kept_lines.append(f"auth-user-pass {auth_path}")
    kept_lines.append("script-security 2")
    kept_lines.append(f"route-up {json.dumps(route_up_command)}")
    return "\n".join(kept_lines) + "\n"


def build_hostapd_config(config: dict[str, Any]) -> str:
    """Build an optimized 5 GHz 802.11ac VHT80 (or 2.4 GHz 802.11n) hostapd.conf."""
    hotspot = config["hotspot"]
    passphrase = hotspot["passphrase"]
    if len(passphrase) < 8 or len(passphrase) > 63 or not passphrase.isprintable():
        raise ValueError(
            "Hotspot WPA2 passphrase must be 8 to 63 printable ASCII characters. "
            "Set it with: sudo rp-vpn configure-hotspot --ssid \"RP-UK-VPN\" --passphrase \"your-password\""
        )

    channel = int(hotspot["channel"])
    band = hotspot["band"]

    lines = [
        "# Generated by rp-vpn",
        f"interface={hotspot['interface']}",
        "driver=nl80211",
        f"country_code={hotspot['country_code']}",
        "ieee80211d=1",
        f"ssid={hotspot['ssid']}",
        "auth_algs=1",
        "wpa=2",
        f"wpa_passphrase={passphrase}",
        "wpa_key_mgmt=WPA-PSK",
        "rsn_pairwise=CCMP",
        "wmm_enabled=1",
        "ignore_broadcast_ssid=0",
    ]

    if band == "5g":
        lines.append("hw_mode=a")
        lines.append(f"channel={channel}")
        lines.append("ieee80211n=1")
        if channel in HT40_PLUS_CHANNELS:
            lines.append("ht_capab=[HT40+][SHORT-GI-20][SHORT-GI-40]")
        elif channel in HT40_MINUS_CHANNELS:
            lines.append("ht_capab=[HT40-][SHORT-GI-20][SHORT-GI-40]")
        else:
            lines.append("ht_capab=[SHORT-GI-20]")

        seg0 = VHT80_CENTER_SEG0.get(channel)
        if hotspot.get("vht80", True) and seg0 is not None:
            lines.append("ieee80211ac=1")
            lines.append("vht_oper_chwidth=1")
            lines.append(f"vht_oper_centr_freq_seg0_idx={seg0}")
        else:
            lines.append("ieee80211ac=1")
            lines.append("vht_oper_chwidth=0")
    else:
        lines.append("hw_mode=g")
        lines.append(f"channel={channel}")
        lines.append("ieee80211n=1")
        lines.append("ht_capab=[SHORT-GI-20]")

    return "\n".join(lines) + "\n"


def build_dnsmasq_config(config: dict[str, Any], dns_servers: Optional[list[str]] = None) -> str:
    """Build a DHCP-only dnsmasq config (port=0 avoids conflicts with systemd-resolved)."""
    hotspot = config["hotspot"]
    subnet = ipaddress.IPv4Network(hotspot["subnet_cidr"], strict=False)
    servers = dns_servers or list(config["dns_servers"])
    dns_csv = ",".join(servers[:2])
    lines = [
        "# Generated by rp-vpn (DHCP-only; DNS is routed inside the VPN tunnel)",
        "port=0",
        f"interface={hotspot['interface']}",
        "bind-dynamic",
        "except-interface=lo",
        "dhcp-authoritative",
        f"dhcp-range={hotspot['dhcp_start']},{hotspot['dhcp_end']},{subnet.netmask},{hotspot['dhcp_lease']}",
        f"dhcp-option=option:router,{hotspot['gateway_ip']}",
        f"dhcp-option=option:dns-server,{dns_csv}",
        "quiet-dhcp6",
    ]
    return "\n".join(lines) + "\n"


def build_nftables_ruleset(
    config: dict[str, Any],
    dns_servers: Optional[list[str]] = None,
    wan_ipv4: Optional[str] = None,
) -> str:
    """Build an atomic, leak-proof nftables ruleset in an isolated table."""
    hotspot = config["hotspot"]
    ap_iface = hotspot["interface"]
    wan_iface = config["wan_interface"]
    wg_iface = config["wireguard"]["interface"]
    ovpn_iface = config["openvpn"]["interface"]
    hotspot_enabled = bool(hotspot.get("enabled", True))
    allow_lan_gw = bool(config.get("allow_lan_gateway", True))
    force_dns = bool(config.get("force_vpn_dns", True))
    disable_ipv6 = bool(config.get("disable_ipv6", True))

    servers = dns_servers or list(config["dns_servers"])
    primary_dns = servers[0]

    prerouting_rules: list[str] = []
    if force_dns and hotspot_enabled:
        prerouting_rules.append(f'        iifname "{ap_iface}" meta l4proto {{ udp, tcp }} th dport 53 dnat ip to {primary_dns}:53')
    if force_dns and allow_lan_gw and wan_ipv4:
        prerouting_rules.append(
            f'        iifname "{wan_iface}" ip daddr {wan_ipv4} meta l4proto {{ udp, tcp }} th dport 53 dnat ip to {primary_dns}:53'
        )

    input_rules: list[str] = []
    if disable_ipv6 and hotspot_enabled:
        input_rules.append(f'        iifname "{ap_iface}" meta nfproto ipv6 drop')
    if hotspot_enabled and not hotspot.get("allow_ssh", True):
        input_rules.append(f'        iifname "{ap_iface}" tcp dport 22 drop')

    forward_rules: list[str] = [
        "        tcp flags syn / syn,rst tcp option maxseg size set rt mtu",
    ]
    if hotspot_enabled:
        if disable_ipv6:
            forward_rules.append(f'        iifname "{ap_iface}" meta nfproto ipv6 drop')
            forward_rules.append(f'        oifname "{ap_iface}" meta nfproto ipv6 drop')
        forward_rules.append(f'        iifname {{ "{wg_iface}", "{ovpn_iface}" }} oifname "{ap_iface}" ct state established,related accept')
        forward_rules.append(f'        iifname "{ap_iface}" oifname {{ "{wg_iface}", "{ovpn_iface}" }} accept')
        # Strict kill-switch: never allow Wi-Fi hotspot packets to leak out WAN or LAN
        forward_rules.append(f'        iifname "{ap_iface}" drop')

    if allow_lan_gw:
        if disable_ipv6:
            forward_rules.append(f'        iifname "{wan_iface}" oifname "{wan_iface}" meta nfproto ipv6 drop')
        forward_rules.append(f'        iifname {{ "{wg_iface}", "{ovpn_iface}" }} oifname "{wan_iface}" ct state established,related accept')
        forward_rules.append(f'        iifname "{wan_iface}" oifname {{ "{wg_iface}", "{ovpn_iface}" }} accept')
        # Strict kill-switch for LAN gateway devices: never hairpin unencrypted traffic back to WAN
        forward_rules.append(f'        iifname "{wan_iface}" oifname "{wan_iface}" drop')

    lines = [
        "table inet rp_vpn",
        "delete table inet rp_vpn",
        "table inet rp_vpn {",
        "    chain prerouting {",
        "        type nat hook prerouting priority dstnat; policy accept;",
        *prerouting_rules,
        "    }",
        "    chain input {",
        "        type filter hook input priority filter; policy accept;",
        *input_rules,
        "    }",
        "    chain forward {",
        "        type filter hook forward priority filter; policy accept;",
        *forward_rules,
        "    }",
        "    chain postrouting {",
        "        type nat hook postrouting priority srcnat; policy accept;",
        f'        oifname {{ "{wg_iface}", "{ovpn_iface}" }} masquerade',
        "    }",
        "}",
    ]
    return "\n".join(lines) + "\n"


def build_sysctl_config() -> str:
    """Build Raspberry Pi 4 kernel network tuning for 500+ Mbps WireGuard throughput."""
    return (
        "# Generated by rp-vpn: Raspberry Pi 4 500+ Mbps VPN & router tuning\n"
        "net.ipv4.ip_forward = 1\n"
        "net.ipv4.conf.all.rp_filter = 2\n"
        "net.ipv4.conf.default.rp_filter = 2\n"
        "net.ipv4.conf.all.send_redirects = 0\n"
        "net.ipv4.conf.default.send_redirects = 0\n"
        "net.core.default_qdisc = fq\n"
        "net.ipv4.tcp_congestion_control = bbr\n"
        "net.core.rmem_max = 26214400\n"
        "net.core.wmem_max = 26214400\n"
        "net.core.rmem_default = 1048576\n"
        "net.core.wmem_default = 1048576\n"
        "net.core.netdev_max_backlog = 16384\n"
        "net.core.optmem_max = 65536\n"
        "net.ipv4.udp_rmem_min = 16384\n"
        "net.ipv4.udp_wmem_min = 16384\n"
        "net.ipv4.tcp_rmem = 4096 131072 16777216\n"
        "net.ipv4.tcp_wmem = 4096 65536 16777216\n"
        "net.ipv4.tcp_mtu_probing = 1\n"
    )


# ---------------------------------------------------------------------------
# System execution helpers (Linux runtime)
# ---------------------------------------------------------------------------


def require_root() -> None:
    """Ensure mutating network commands run as root."""
    if os.geteuid() != 0:
        raise PermissionError("This command requires root privileges. Re-run with sudo.")


def run_cmd(
    argv: list[str],
    *,
    input_text: Optional[str] = None,
    check: bool = True,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    """Run an external command without a shell and capture stdout/stderr."""
    LOG.debug("Exec: %s", " ".join(shlex.quote(part) for part in argv))
    return subprocess.run(
        argv,
        input=input_text,
        text=True,
        capture_output=True,
        check=check,
        timeout=timeout,
    )


def interface_exists(iface: str) -> bool:
    """Return True when a network interface is registered in sysfs."""
    return Path(f"/sys/class/net/{iface}").exists()


SURFSHARK_PRIVATE_DNS = {"162.252.172.57", "149.154.159.92"}
PUBLIC_FALLBACK_DNS = ("1.1.1.1", "8.8.8.8", "9.9.9.9")


def _encode_dns_query(hostname: str, txid: int) -> bytes:
    """Build a minimal RFC 1035 UDP DNS A-record query packet."""
    header = struct.pack("!HHHHHH", txid & 0xFFFF, 0x0100, 1, 0, 0, 0)
    qname = b"".join(
        bytes([len(label)]) + label.encode("ascii")
        for label in hostname.strip(".").split(".")
        if label
    ) + b"\x00"
    return header + qname + struct.pack("!HH", 1, 1)


def _skip_dns_name(packet: bytes, offset: int) -> int:
    """Advance offset past a (possibly compressed) DNS name in packet."""
    length = len(packet)
    while offset < length:
        tag = packet[offset]
        if tag == 0:
            return offset + 1
        if (tag & 0xC0) == 0xC0:
            return offset + 2
        offset += 1 + tag
    return length


def _decode_dns_a_response(packet: bytes, expected_txid: int) -> Optional[str]:
    """Extract the first IPv4 A record from a UDP DNS response packet."""
    if len(packet) < 12:
        return None
    txid, flags, qdcount, ancount, _, _ = struct.unpack("!HHHHHH", packet[:12])
    if txid != (expected_txid & 0xFFFF) or (flags & 0x8000) == 0 or (flags & 0x000F) != 0 or ancount == 0:
        return None
    offset = 12
    for _ in range(qdcount):
        offset = _skip_dns_name(packet, offset) + 4
    for _ in range(ancount):
        if offset >= len(packet):
            break
        offset = _skip_dns_name(packet, offset)
        if offset + 10 > len(packet):
            break
        rtype, rclass, _, rdlength = struct.unpack("!HHIH", packet[offset : offset + 10])
        offset += 10
        if offset + rdlength > len(packet):
            break
        if rtype == 1 and rclass == 1 and rdlength == 4:
            return socket.inet_ntoa(packet[offset : offset + 4])
        offset += rdlength
    return None


def query_dns_a_direct(hostname: str, nameservers: tuple[str, ...] = PUBLIC_FALLBACK_DNS, timeout: float = 2.0) -> Optional[str]:
    """Resolve an IPv4 hostname directly via UDP port 53, bypassing broken /etc/resolv.conf."""
    with suppress(ValueError):
        return str(ipaddress.IPv4Address(hostname))
    txid = random.randint(1, 0xFFFE)
    query = _encode_dns_query(hostname, txid)
    for server in nameservers:
        with suppress(OSError):
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(timeout)
                sock.sendto(query, (server, 53))
                reply, _ = sock.recvfrom(512)
            resolved = _decode_dns_a_response(reply, txid)
            if resolved:
                return resolved
    return None


def resolve_endpoint_ipv4(host: str) -> Optional[str]:
    """Resolve endpoint hostname to IPv4, using direct public DNS first to avoid resolv.conf hangs."""
    if not host:
        return None
    direct = query_dns_a_direct(host)
    if direct:
        return direct
    with suppress(OSError):
        infos = socket.getaddrinfo(host, None, family=socket.AF_INET, type=socket.SOCK_DGRAM)
        if infos:
            return str(infos[0][4][0])
    return None


def resolv_conf_needs_repair(resolv_text: str) -> bool:
    """Return True if /etc/resolv.conf has no nameservers or only Surfshark VPN-internal DNS."""
    servers: list[str] = []
    for raw_line in resolv_text.splitlines():
        line = raw_line.split("#", 1)[0].split(";", 1)[0].strip()
        parts = line.split()
        if len(parts) >= 2 and parts[0].lower() == "nameserver":
            servers.append(parts[1])
    if not servers:
        return True
    # Surfshark's 162.252.172.57 / 149.154.159.92 drop queries coming from non-VPN WAN IPs!
    return all(server in SURFSHARK_PRIVATE_DNS for server in servers)


def ensure_host_dns_healthy(resolv_path: Path = Path("/etc/resolv.conf")) -> None:
    """Repair /etc/resolv.conf if legacy openresolv left it pointing only to VPN-only DNS."""
    try:
        current = resolv_path.read_text(encoding="utf-8", errors="replace") if resolv_path.exists() else ""
    except OSError:
        current = ""
    if not resolv_conf_needs_repair(current):
        return

    LOG.warning("Host /etc/resolv.conf has no reachable WAN DNS (left over from openresolv); repairing.")
    if shutil.which("resolvconf"):
        run_cmd(["resolvconf", "-d", "wg0", "-f"], check=False)

    # Discover default WAN gateway (usually home router DNS forwarder) as primary, plus Cloudflare/Google
    router_dns = ""
    route_res = run_cmd(["ip", "-4", "-j", "route", "show", "default"], check=False)
    if route_res.returncode == 0 and route_res.stdout.strip():
        with suppress(ValueError, KeyError, IndexError, TypeError):
            routes = json.loads(route_res.stdout)
            gw = routes[0].get("gateway")
            if gw:
                router_dns = str(ipaddress.IPv4Address(gw))

    lines = ["# Repaired by rp-vpn (WAN DNS for host services)"]
    if router_dns and router_dns not in SURFSHARK_PRIVATE_DNS:
        lines.append(f"nameserver {router_dns}")
    lines.append("nameserver 1.1.1.1")
    lines.append("nameserver 8.8.8.8")
    with suppress(OSError):
        if resolv_path.is_symlink():
            resolv_path.unlink()
        write_text_atomic(resolv_path, "\n".join(lines) + "\n", mode=0o644)


def ensure_networkmanager_unmanaged(config: dict[str, Any]) -> None:
    """Tell NetworkManager to ignore hotspot and VPN tunnel interfaces so it never flushes their IPs."""
    ifaces = [
        config["hotspot"]["interface"],
        config["wireguard"]["interface"],
        config["openvpn"]["interface"],
    ]
    if Path("/etc/NetworkManager/conf.d").is_dir():
        spec = ";".join(f"interface-name:{name}" for name in ifaces)
        nm_conf = f"[keyfile]\nunmanaged-devices={spec}\n"
        current = NM_UNMANAGED_DEST.read_text(encoding="utf-8", errors="replace") if NM_UNMANAGED_DEST.is_file() else ""
        if current != nm_conf:
            write_text_atomic(NM_UNMANAGED_DEST, nm_conf, mode=0o644)
            if shutil.which("nmcli"):
                run_cmd(["nmcli", "general", "reload"], check=False)
    if shutil.which("nmcli"):
        for name in ifaces:
            if interface_exists(name):
                run_cmd(["nmcli", "device", "set", name, "managed", "no"], check=False)


def get_interface_ipv4(iface: str) -> Optional[str]:
    """Return the primary IPv4 address assigned to iface, or None."""
    result = run_cmd(["ip", "-4", "-j", "addr", "show", "dev", iface], check=False)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    with suppress(ValueError, KeyError, IndexError, TypeError):
        payload = json.loads(result.stdout)
        for addr_info in payload[0].get("addr_info", []):
            local_ip = addr_info.get("local")
            if local_ip:
                return str(ipaddress.IPv4Address(local_ip))
    return None


def enable_multicore_rps(iface: str, mask: str = "f") -> None:
    """Distribute RX softirq packet steering across all 4 Raspberry Pi 4 CPU cores."""
    queues_dir = Path(f"/sys/class/net/{iface}/queues")
    if not queues_dir.is_dir():
        return
    for rps_file in queues_dir.glob("rx-*/rps_cpus"):
        with suppress(OSError):
            rps_file.write_text(f"{mask}\n", encoding="utf-8")


def _kill_pidfile(pid_path: Path) -> None:
    """Terminate a daemon tracked by pid_path if still running."""
    if not pid_path.is_file():
        return
    with suppress(OSError, ValueError):
        pid = int(pid_path.read_text(encoding="utf-8").strip())
        if pid > 1:
            os.kill(pid, 15)
            for _ in range(20):
                time.sleep(0.1)
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
            else:
                with suppress(ProcessLookupError):
                    os.kill(pid, 9)
    with suppress(OSError):
        pid_path.unlink()


def _delete_rules_at_priority(priority: int) -> None:
    """Delete all IPv4 policy rules at a specific priority number."""
    for _ in range(10):
        res = run_cmd(["ip", "-4", "rule", "del", "priority", str(priority)], check=False)
        if res.returncode != 0:
            break


def set_vpn_table_blackhole(config: dict[str, Any]) -> None:
    """Flush the dedicated VPN routing table and install a fail-closed blackhole route."""
    table = str(config["route_table"])
    run_cmd(["ip", "-4", "route", "flush", "table", table], check=False)
    run_cmd(["ip", "-4", "route", "add", "blackhole", "default", "table", table], check=False)


def ensure_policy_routing_base(config: dict[str, Any]) -> None:
    """Install policy routing rules and initialize the blackhole route if no tunnel is up."""
    table = str(config["route_table"])
    hotspot = config["hotspot"]
    wg_iface = config["wireguard"]["interface"]
    ovpn_iface = config["openvpn"]["interface"]

    if not interface_exists(wg_iface) and not interface_exists(ovpn_iface):
        set_vpn_table_blackhole(config)

    _delete_rules_at_priority(RULE_PRIO_MULTICAST)
    run_cmd(["ip", "-4", "rule", "add", "to", "224.0.0.0/4", "lookup", "main", "priority", str(RULE_PRIO_MULTICAST)], check=False)

    _delete_rules_at_priority(RULE_PRIO_HOTSPOT)
    if hotspot.get("enabled", True):
        run_cmd(["ip", "-4", "rule", "add", "iif", hotspot["interface"], "lookup", table, "priority", str(RULE_PRIO_HOTSPOT)], check=False)

    _delete_rules_at_priority(RULE_PRIO_LAN_GW)
    if config.get("allow_lan_gateway", True):
        run_cmd(["ip", "-4", "rule", "add", "iif", config["wan_interface"], "lookup", table, "priority", str(RULE_PRIO_LAN_GW)], check=False)


def cleanup_legacy_wg_quick_artifacts() -> None:
    """Remove stale nftables kill-switch tables and ip rules left behind by wg-quick@wg0."""
    tables_res = run_cmd(["nft", "list", "tables"], check=False)
    if tables_res.returncode == 0:
        for line in tables_res.stdout.splitlines():
            # Example line: 'table ip wg-quick-wg0' or 'table inet wg-quick-wg0'
            parts = line.strip().split()
            if len(parts) == 3 and parts[0] == "table" and parts[2].startswith("wg-quick-"):
                LOG.warning("Removing leftover nftables %s %s (blocks tunnel replies!)", parts[1], parts[2])
                run_cmd(["nft", "delete", "table", parts[1], parts[2]], check=False)
    for _ in range(5):
        if run_cmd(["ip", "-4", "rule", "del", "lookup", "51820"], check=False).returncode != 0:
            break
    for _ in range(5):
        if run_cmd(["ip", "-4", "rule", "del", "table", "main", "suppress_prefixlength", "0"], check=False).returncode != 0:
            break


def apply_nftables(config: dict[str, Any], dns_servers: Optional[list[str]] = None) -> None:
    """Atomically load the rp_vpn nftables kill-switch and NAT rules."""
    cleanup_legacy_wg_quick_artifacts()
    wan_ip = get_interface_ipv4(config["wan_interface"])
    ruleset = build_nftables_ruleset(config, dns_servers=dns_servers, wan_ipv4=wan_ip)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    nft_path = RUNTIME_DIR / "rp-vpn.nft"
    write_text_atomic(nft_path, ruleset, mode=0o600)

    # First ensure table exists so 'delete table inet rp_vpn' inside ruleset always succeeds
    run_cmd(["nft", "add", "table", "inet", "rp_vpn"], check=False)
    res = run_cmd(["nft", "-f", str(nft_path)], check=False)
    if res.returncode != 0:
        raise RuntimeError(f"Failed to apply nftables ruleset: {res.stderr.strip()}")


def read_state(config: dict[str, Any]) -> dict[str, Any]:
    """Read persistent VPN state (active profile & desired state)."""
    data = read_json(config["_state_path"], {})
    if not isinstance(data, dict):
        data = {}
    data.setdefault("active_profile", "")
    data.setdefault("desired_state", "running")
    return data


def save_state(config: dict[str, Any], **updates: Any) -> dict[str, Any]:
    """Update persistent VPN state atomically."""
    state = read_state(config)
    state.update(updates)
    write_json_atomic(config["_state_path"], state, mode=0o600)
    return state


def resolve_active_profile(config: dict[str, Any], profiles: list[Profile]) -> Profile:
    """Return the configured active profile, or auto-pick the top preferred profile."""
    if not profiles:
        raise RuntimeError(
            f"No VPN profiles found in {config['_profiles_dir']}.\n"
            "Import Surfshark .conf or .ovpn files first:\n"
            "  sudo rp-vpn import ~/Downloads/uk-lon.conf"
        )
    state = read_state(config)
    wanted = state.get("active_profile") or ""
    by_name = {p.name: p for p in profiles}
    if wanted in by_name:
        return by_name[wanted]

    ranked = select_failover_candidates(
        profiles,
        current_name=None,
        preferred_country=config["preferred_country"],
        prefer_protocol=config["prefer_protocol"],
    )
    chosen = ranked[0] if ranked else profiles[0]
    save_state(config, active_profile=chosen.name)
    LOG.info("Auto-selected initial VPN profile: %s (%s)", chosen.name, chosen.protocol)
    return chosen


def stop_tunnel_processes(config: dict[str, Any]) -> None:
    """Cleanly stop any active WireGuard interface or OpenVPN daemon."""
    wg_iface = config["wireguard"]["interface"]
    ovpn_iface = config["openvpn"]["interface"]

    wg_conf = RUNTIME_DIR / f"{wg_iface}.conf"
    if wg_conf.is_file():
        run_cmd(["wg-quick", "down", str(wg_conf)], check=False)
    if interface_exists(wg_iface):
        run_cmd(["ip", "link", "del", "dev", wg_iface], check=False)
    # Tear down legacy wg0 if still active from old vpn-hotspot setup
    if wg_iface != "wg0" and interface_exists("wg0"):
        LOG.warning("Stopping conflicting legacy wg0 tunnel...")
        run_cmd(["systemctl", "disable", "--now", "wg-quick@wg0.service"], check=False)
        run_cmd(["wg-quick", "down", "wg0"], check=False)
        run_cmd(["ip", "link", "del", "dev", "wg0"], check=False)
        for _ in range(5):
            if run_cmd(["ip", "-4", "rule", "del", "lookup", "51820"], check=False).returncode != 0:
                break

    _kill_pidfile(RUNTIME_DIR / "openvpn.pid")
    if interface_exists(ovpn_iface):
        run_cmd(["ip", "link", "del", "dev", ovpn_iface], check=False)

    _delete_rules_at_priority(RULE_PRIO_VPN_SRC)
    set_vpn_table_blackhole(config)


def start_wireguard_profile(profile: Profile, config: dict[str, Any]) -> list[str]:
    """Bring up a WireGuard profile and install policy routing."""
    wg_iface = config["wireguard"]["interface"]
    table = str(config["route_table"])
    raw_text = profile.path.read_text(encoding="utf-8", errors="replace")

    endpoint_override = None
    if profile.endpoint_host and profile.endpoint_port:
        resolved_ip = resolve_endpoint_ipv4(profile.endpoint_host)
        if resolved_ip:
            endpoint_override = f"{resolved_ip}:{profile.endpoint_port}"
            LOG.info("Resolved %s -> %s", profile.endpoint_host, endpoint_override)

    rendered, ipv4_hosts, dns_servers = build_wireguard_runtime_config(
        raw_text,
        config,
        endpoint_override=endpoint_override,
    )

    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    wg_conf = RUNTIME_DIR / f"{wg_iface}.conf"
    write_text_atomic(wg_conf, rendered, mode=0o600)

    res = run_cmd(["wg-quick", "up", str(wg_conf)], check=False, timeout=30)
    if res.returncode != 0:
        raise RuntimeError(f"wg-quick up failed for '{profile.name}': {res.stderr.strip() or res.stdout.strip()}")

    if shutil.which("nmcli"):
        run_cmd(["nmcli", "device", "set", wg_iface, "managed", "no"], check=False)
    # Re-ensure IPv4 address and UP state in case a network daemon raced wg-quick
    if not get_interface_ipv4(wg_iface) and ipv4_hosts:
        run_cmd(["ip", "-4", "addr", "replace", f"{ipv4_hosts[0]}/16", "dev", wg_iface], check=False)
    run_cmd(["ip", "link", "set", "dev", wg_iface, "mtu", str(config["wireguard"]["mtu"]), "up"], check=False)

    if config.get("disable_ipv6", True):
        run_cmd(["sysctl", "-w", f"net.ipv6.conf.{wg_iface}.disable_ipv6=1"], check=False)
    run_cmd(["ip", "link", "set", "dev", wg_iface, "txqueuelen", "2000"], check=False)
    run_cmd(["ip", "-4", "route", "flush", "table", table], check=False)
    route_res = run_cmd(["ip", "-4", "route", "replace", "default", "dev", wg_iface, "table", table], check=False)
    if route_res.returncode != 0:
        raise RuntimeError(f"Failed to add WireGuard default route in table {table}: {route_res.stderr.strip()}")

    _delete_rules_at_priority(RULE_PRIO_VPN_SRC)
    run_cmd(["ip", "-4", "rule", "add", "oif", wg_iface, "lookup", table, "priority", str(RULE_PRIO_VPN_SRC)], check=False)
    for host_ip in ipv4_hosts:
        run_cmd(["ip", "-4", "rule", "add", "from", f"{host_ip}/32", "lookup", table, "priority", str(RULE_PRIO_VPN_SRC)], check=False)

    # Trigger initial WireGuard handshake immediately
    target = config["watchdog"]["probe_targets"][0]
    run_cmd(["ping", "-4", "-I", wg_iface, "-c", "1", "-W", "2", target], check=False, timeout=5)
    return dns_servers


def start_openvpn_profile(profile: Profile, config: dict[str, Any]) -> list[str]:
    """Bring up an OpenVPN .ovpn profile with policy routing in table 51820."""
    auth_path: Path = config["_auth_path"]
    if profile.requires_auth and not auth_path.is_file():
        raise RuntimeError(
            f"OpenVPN profile '{profile.name}' requires Surfshark service credentials.\n"
            "Save them first with:\n"
            "  sudo rp-vpn set-auth"
        )

    script_path = Path(__file__).resolve()
    route_up_cmd = f"{sys.executable} {script_path} --config {config['_config_path']} internal-openvpn-up"
    raw_text = profile.path.read_text(encoding="utf-8", errors="replace")
    rendered = build_openvpn_runtime_config(raw_text, config, auth_path=auth_path, route_up_command=route_up_cmd)

    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    ovpn_conf = RUNTIME_DIR / "openvpn.conf"
    pid_file = RUNTIME_DIR / "openvpn.pid"
    status_file = RUNTIME_DIR / "openvpn.status"
    log_file = RUNTIME_DIR / "openvpn.log"
    write_text_atomic(ovpn_conf, rendered, mode=0o600)
    with suppress(OSError):
        log_file.unlink()

    res = run_cmd(
        [
            "openvpn",
            "--daemon",
            "rp-vpn-openvpn",
            "--writepid",
            str(pid_file),
            "--status",
            str(status_file),
            "5",
            "--log",
            str(log_file),
            "--config",
            str(ovpn_conf),
        ],
        check=False,
        timeout=15,
    )
    if res.returncode != 0:
        raise RuntimeError(f"Failed to launch openvpn: {res.stderr.strip() or res.stdout.strip()}")

    ovpn_iface = config["openvpn"]["interface"]
    deadline = time.monotonic() + 25.0
    while time.monotonic() < deadline:
        if interface_exists(ovpn_iface) and get_interface_ipv4(ovpn_iface):
            return list(config["dns_servers"])
        if pid_file.is_file():
            with suppress(OSError, ValueError):
                pid = int(pid_file.read_text(encoding="utf-8").strip())
                os.kill(pid, 0)
        time.sleep(0.5)

    tail = ""
    if log_file.is_file():
        lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()[-12:]
        tail = "\n".join(lines)
    stop_tunnel_processes(config)
    raise RuntimeError(f"OpenVPN profile '{profile.name}' timed out waiting for {ovpn_iface}.\n{tail}".strip())


def handle_openvpn_route_up(config: dict[str, Any]) -> int:
    """Hook invoked by OpenVPN --route-up to populate policy routing table 51820."""
    # OpenVPN exports tunnel parameters as lowercase environment variables.
    env = dict(os.environ)
    dev = env.get("dev") or config["openvpn"]["interface"]
    local_ip = env.get("ifconfig_local") or get_interface_ipv4(dev) or ""
    vpn_gateway = env.get("route_vpn_gateway") or ""
    table = str(config["route_table"])

    if config.get("disable_ipv6", True):
        run_cmd(["sysctl", "-w", f"net.ipv6.conf.{dev}.disable_ipv6=1"], check=False)
    run_cmd(["ip", "link", "set", "dev", dev, "txqueuelen", "2000"], check=False)

    run_cmd(["ip", "-4", "route", "flush", "table", table], check=False)
    if vpn_gateway:
        run_cmd(["ip", "-4", "route", "replace", "default", "via", vpn_gateway, "dev", dev, "table", table], check=False)
    else:
        run_cmd(["ip", "-4", "route", "replace", "default", "dev", dev, "table", table], check=False)

    _delete_rules_at_priority(RULE_PRIO_VPN_SRC)
    run_cmd(["ip", "-4", "rule", "add", "oif", dev, "lookup", table, "priority", str(RULE_PRIO_VPN_SRC)], check=False)
    if local_ip:
        run_cmd(["ip", "-4", "rule", "add", "from", f"{local_ip}/32", "lookup", table, "priority", str(RULE_PRIO_VPN_SRC)], check=False)
    return 0


def service_start(config: dict[str, Any]) -> int:
    """Start the active VPN profile, update firewall/DHCP DNS, and enable RPS."""
    require_root()
    ensure_host_dns_healthy()
    ensure_networkmanager_unmanaged(config)
    profiles = discover_profiles(config["_profiles_dir"])
    profile = resolve_active_profile(config, profiles)

    enable_multicore_rps(config["wan_interface"])
    ensure_policy_routing_base(config)
    stop_tunnel_processes(config)

    # Apply kill-switch before bringing up tunnel so traffic never leaks
    apply_nftables(config, dns_servers=config["dns_servers"])

    LOG.info("Starting VPN profile '%s' (%s -> %s)", profile.name, profile.protocol, profile.endpoint_host or "endpoint")
    dns_servers = start_wireguard_profile(profile, config) if profile.protocol == "wireguard" else start_openvpn_profile(profile, config)

    apply_nftables(config, dns_servers=dns_servers)
    _refresh_dnsmasq_if_running(config, dns_servers)
    save_state(
        config,
        active_profile=profile.name,
        active_protocol=profile.protocol,
        desired_state="running",
        connected_at=utc_now_iso(),
    )
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    write_json_atomic(RUNTIME_DIR / "watchdog.json", {"failures": 0, "updated_at": utc_now_iso()}, mode=0o600)
    LOG.info("VPN profile '%s' is UP (DNS: %s)", profile.name, ", ".join(dns_servers))
    return 0


def service_stop(config: dict[str, Any]) -> int:
    """Stop the active VPN tunnel while keeping the kill-switch active."""
    require_root()
    stop_tunnel_processes(config)
    LOG.info("VPN tunnel stopped (kill-switch remains active for hotspot/gateway clients)")
    return 0


def _refresh_dnsmasq_if_running(config: dict[str, Any], dns_servers: list[str]) -> None:
    """Refresh DHCP DNS servers if the hotspot dnsmasq daemon is currently running."""
    pid_file = RUNTIME_DIR / "dnsmasq.pid"
    if not pid_file.is_file() or not config["hotspot"].get("enabled", True):
        return
    dnsmasq_conf = RUNTIME_DIR / "dnsmasq.conf"
    write_text_atomic(dnsmasq_conf, build_dnsmasq_config(config, dns_servers=dns_servers), mode=0o644)
    _kill_pidfile(pid_file)
    run_cmd(["dnsmasq", f"--conf-file={dnsmasq_conf}", f"--pid-file={pid_file}"], check=False)


def hotspot_up(config: dict[str, Any]) -> int:
    """Configure Wi-Fi interface, kill-switch firewall, DHCP server and 5 GHz hostapd AP."""
    require_root()
    ensure_host_dns_healthy()
    ensure_networkmanager_unmanaged(config)
    ensure_policy_routing_base(config)
    apply_nftables(config, dns_servers=config["dns_servers"])

    hotspot = config["hotspot"]
    if not hotspot.get("enabled", True):
        LOG.info("Wi-Fi hotspot is disabled in config; firewall and LAN gateway rules applied.")
        return 0

    ap_iface = hotspot["interface"]
    if not interface_exists(ap_iface):
        raise RuntimeError(f"Hotspot Wi-Fi interface '{ap_iface}' does not exist.")

    # Validate hostapd config before touching network interfaces
    hostapd_text = build_hostapd_config(config)
    dnsmasq_text = build_dnsmasq_config(config, dns_servers=config["dns_servers"])

    # Prevent wpa_supplicant from fighting hostapd over the AP interface
    run_cmd(["systemctl", "stop", f"wpa_supplicant@{ap_iface}.service"], check=False)

    if shutil.which("rfkill"):
        run_cmd(["rfkill", "unblock", "wifi"], check=False)
    if shutil.which("iw"):
        run_cmd(["iw", "reg", "set", hotspot["country_code"]], check=False)

    _kill_pidfile(RUNTIME_DIR / "hostapd.pid")
    _kill_pidfile(RUNTIME_DIR / "dnsmasq.pid")

    subnet = ipaddress.IPv4Network(hotspot["subnet_cidr"], strict=False)
    gateway_cidr = f"{hotspot['gateway_ip']}/{subnet.prefixlen}"

    run_cmd(["ip", "link", "set", "dev", ap_iface, "down"], check=False)
    run_cmd(["ip", "addr", "flush", "dev", ap_iface], check=False)
    run_cmd(["ip", "link", "set", "dev", ap_iface, "up"], check=False)
    run_cmd(["ip", "link", "set", "dev", ap_iface, "txqueuelen", "2000"], check=False)
    run_cmd(["ip", "-4", "addr", "replace", gateway_cidr, "dev", ap_iface])

    if config.get("disable_ipv6", True):
        run_cmd(["sysctl", "-w", f"net.ipv6.conf.{ap_iface}.disable_ipv6=1"], check=False)
    if shutil.which("iw"):
        run_cmd(["iw", "dev", ap_iface, "set", "power_save", "off"], check=False)

    enable_multicore_rps(ap_iface)
    enable_multicore_rps(config["wan_interface"])

    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    hostapd_conf = RUNTIME_DIR / "hostapd.conf"
    dnsmasq_conf = RUNTIME_DIR / "dnsmasq.conf"
    write_text_atomic(hostapd_conf, hostapd_text, mode=0o600)
    write_text_atomic(dnsmasq_conf, dnsmasq_text, mode=0o644)

    dns_res = run_cmd(
        ["dnsmasq", f"--conf-file={dnsmasq_conf}", f"--pid-file={RUNTIME_DIR / 'dnsmasq.pid'}"],
        check=False,
    )
    if dns_res.returncode != 0:
        raise RuntimeError(f"dnsmasq failed to start: {dns_res.stderr.strip()}")

    ap_res = run_cmd(
        ["hostapd", "-B", "-P", str(RUNTIME_DIR / "hostapd.pid"), str(hostapd_conf)],
        check=False,
    )
    if ap_res.returncode != 0:
        _kill_pidfile(RUNTIME_DIR / "dnsmasq.pid")
        raise RuntimeError(
            f"hostapd failed to start on {ap_iface} ({hotspot['band']} ch {hotspot['channel']}): "
            f"{ap_res.stderr.strip() or ap_res.stdout.strip()}"
        )

    # Re-apply power_save off after hostapd switches CYW43455 into AP mode
    if shutil.which("iw"):
        run_cmd(["iw", "dev", ap_iface, "set", "power_save", "off"], check=False)

    LOG.info(
        "Hotspot '%s' is UP on %s (%s channel %s, gateway %s)",
        hotspot["ssid"],
        ap_iface,
        hotspot["band"],
        hotspot["channel"],
        hotspot["gateway_ip"],
    )
    return 0


def hotspot_down(config: dict[str, Any]) -> int:
    """Stop the Wi-Fi hotspot daemons."""
    require_root()
    _kill_pidfile(RUNTIME_DIR / "hostapd.pid")
    _kill_pidfile(RUNTIME_DIR / "dnsmasq.pid")
    LOG.info("Hotspot stopped.")
    return 0


# ---------------------------------------------------------------------------
# Health probing, endpoint DNS refresh & automatic UK failover watchdog
# ---------------------------------------------------------------------------


def active_tunnel_interface(config: dict[str, Any], protocol: str) -> str:
    """Return the tunnel network interface name for protocol."""
    return config["wireguard"]["interface"] if protocol == "wireguard" else config["openvpn"]["interface"]


def probe_interface_connectivity(iface: str, targets: list[str], timeout_sec: int = 3) -> bool:
    """Return True if at least one probe target responds to ICMP through iface."""
    if not interface_exists(iface) or not get_interface_ipv4(iface):
        return False
    for target in targets:
        res = run_cmd(
            ["ping", "-4", "-I", iface, "-c", "2", "-W", str(timeout_sec), target],
            check=False,
            timeout=timeout_sec * 3 + 3,
        )
        if res.returncode == 0:
            return True
    return False


def probe_wan_connectivity(wan_iface: str, targets: list[str], timeout_sec: int = 2) -> bool:
    """Check whether the underlying WAN uplink itself has internet connectivity."""
    for target in targets[:2]:
        res = run_cmd(
            ["ping", "-4", "-I", wan_iface, "-c", "1", "-W", str(timeout_sec), target],
            check=False,
            timeout=timeout_sec + 3,
        )
        if res.returncode == 0:
            return True
    return False


def wireguard_handshake_age(wg_iface: str) -> Optional[int]:
    """Return seconds since the most recent WireGuard peer handshake, or None."""
    res = run_cmd(["wg", "show", wg_iface, "latest-handshakes"], check=False)
    if res.returncode != 0 or not res.stdout.strip():
        return None
    ages: list[int] = []
    now = int(time.time())
    for line in res.stdout.strip().splitlines():
        parts = line.split()
        if len(parts) >= 2:
            with suppress(ValueError):
                ts = int(parts[1])
                if ts > 0:
                    ages.append(max(0, now - ts))
    return min(ages) if ages else None


def refresh_wireguard_endpoint_dns(profile: Profile, wg_iface: str) -> bool:
    """Re-resolve a Surfshark WireGuard hostname over WAN and update peer endpoint in-place."""
    if not profile.endpoint_host or not profile.endpoint_port or not profile.public_key:
        return False
    resolved_ip = resolve_endpoint_ipv4(profile.endpoint_host)
    if not resolved_ip:
        LOG.warning("DNS lookup failed for %s", profile.endpoint_host)
        return False
    res = run_cmd(
        ["wg", "set", wg_iface, "peer", profile.public_key, "endpoint", f"{resolved_ip}:{profile.endpoint_port}"],
        check=False,
    )
    if res.returncode == 0:
        LOG.info("Refreshed WireGuard endpoint %s -> %s:%d", profile.endpoint_host, resolved_ip, profile.endpoint_port)
        return True
    return False


def restart_vpn_unit_or_direct(config: dict[str, Any]) -> None:
    """Restart rp-vpn.service under systemd when installed, else invoke service_start directly."""
    unit_check = run_cmd(["systemctl", "cat", "rp-vpn.service"], check=False)
    if unit_check.returncode == 0:
        run_cmd(["systemctl", "restart", "rp-vpn.service"], check=True, timeout=60)
    else:
        service_start(config)


def run_watchdog(config: dict[str, Any]) -> int:
    """Verify tunnel health, heal stale endpoints, and fail over across UK profiles."""
    require_root()
    if not config["watchdog"].get("enabled", True):
        return 0

    state = read_state(config)
    if state.get("desired_state") == "stopped":
        LOG.debug("VPN desired_state is stopped; skipping watchdog.")
        return 0

    profiles = discover_profiles(config["_profiles_dir"])
    if not profiles:
        LOG.warning("Watchdog: no profiles imported yet.")
        return 0

    profile = resolve_active_profile(config, profiles)
    tun_iface = active_tunnel_interface(config, profile.protocol)
    targets = config["watchdog"]["probe_targets"]
    timeout_sec = int(config["watchdog"]["probe_timeout_seconds"])
    max_age = int(config["watchdog"]["max_handshake_age_seconds"])

    healthy = probe_interface_connectivity(tun_iface, targets, timeout_sec=timeout_sec)
    if healthy and profile.protocol == "wireguard":
        age = wireguard_handshake_age(tun_iface)
        if age is not None and age > max_age:
            LOG.warning("WireGuard handshake age %ds exceeds %ds; verifying tunnel", age, max_age)

    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    wd_path = RUNTIME_DIR / "watchdog.json"
    wd_state = read_json(wd_path, {"failures": 0})
    if not isinstance(wd_state, dict):
        wd_state = {"failures": 0}

    if healthy:
        if wd_state.get("failures", 0) > 0:
            LOG.info("Watchdog: tunnel '%s' recovered.", profile.name)
        write_json_atomic(wd_path, {"failures": 0, "updated_at": utc_now_iso()}, mode=0o600)
        return 0

    # Check if WAN itself is offline so we do not thrash profiles during an ISP outage
    if not probe_wan_connectivity(config["wan_interface"], targets):
        LOG.warning("Watchdog: WAN interface %s has no internet; waiting for ISP recovery.", config["wan_interface"])
        return 0

    # Step 1: For WireGuard, try seamless in-place DNS re-resolution before restarting tunnel
    if (
        profile.protocol == "wireguard"
        and interface_exists(tun_iface)
        and refresh_wireguard_endpoint_dns(profile, tun_iface)
        and probe_interface_connectivity(tun_iface, targets, timeout_sec=timeout_sec)
    ):
        LOG.info("Watchdog: healed '%s' via endpoint DNS refresh.", profile.name)
        write_json_atomic(wd_path, {"failures": 0, "updated_at": utc_now_iso()}, mode=0o600)
        return 0

    failures = int(wd_state.get("failures", 0)) + 1
    threshold = int(config["watchdog"]["failover_threshold"])
    LOG.warning("Watchdog: tunnel '%s' failed health check (%d/%d).", profile.name, failures, threshold)

    if config["watchdog"].get("auto_failover", True) and failures >= threshold:
        candidates = select_failover_candidates(
            profiles,
            current_name=profile.name,
            preferred_country=config["preferred_country"],
            prefer_protocol=config["prefer_protocol"],
        )
        next_profile = next((item for item in candidates if item.name != profile.name), None)
        if next_profile is not None:
            LOG.warning("Watchdog: failing over from '%s' to '%s' (%s).", profile.name, next_profile.name, next_profile.protocol)
            save_state(
                config,
                active_profile=next_profile.name,
                last_failover_at=utc_now_iso(),
                last_failover_from=profile.name,
            )
            failures = 0

    write_json_atomic(wd_path, {"failures": failures, "updated_at": utc_now_iso()}, mode=0o600)
    restart_vpn_unit_or_direct(config)
    return 0


# ---------------------------------------------------------------------------
# User-facing CLI commands (status, benchmark, import, list, use, auth, etc.)
# ---------------------------------------------------------------------------


def _format_bytes(num_bytes: int) -> str:
    """Format byte counters cleanly in KiB, MiB or GiB."""
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024.0:.1f} KiB"
    if num_bytes < 1024 * 1024 * 1024:
        return f"{num_bytes / (1024.0 * 1024.0):.1f} MiB"
    return f"{num_bytes / (1024.0 * 1024.0 * 1024.0):.2f} GiB"


def fetch_exit_ip_info(iface: Optional[str] = None, timeout: int = 5) -> dict[str, str]:
    """Query public IP & geo location, optionally bound to a specific interface via curl."""
    if iface and shutil.which("curl"):
        cmd = ["curl", "-4", "-sS", "--max-time", str(timeout), "--interface", iface]
        resolved = resolve_endpoint_ipv4("ipinfo.io")
        if resolved:
            cmd.extend(["--resolve", f"ipinfo.io:443:{resolved}"])
        cmd.append("https://ipinfo.io/json")
        res = run_cmd(cmd, check=False, timeout=timeout + 3)
        if res.returncode == 0 and res.stdout.strip():
            with suppress(ValueError, TypeError):
                data = json.loads(res.stdout)
                if isinstance(data, dict):
                    return {k: str(v) for k, v in data.items() if isinstance(v, (str, int, float))}
        return {}

    req = urllib.request.Request("https://ipinfo.io/json", headers={"Accept": "application/json", "User-Agent": "rp-vpn/1.0"})
    with suppress(Exception), urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        if isinstance(data, dict):
            return {k: str(v) for k, v in data.items() if isinstance(v, (str, int, float))}
    return {}


def cmd_list(config: dict[str, Any]) -> int:
    """List all imported Surfshark VPN profiles."""
    profiles = discover_profiles(config["_profiles_dir"])
    state = read_state(config)
    active = state.get("active_profile") or ""

    if not profiles:
        print(f"No VPN profiles in {config['_profiles_dir']}")
        print("Import Surfshark .conf (WireGuard) or .ovpn (OpenVPN) files with:")
        print("  sudo rp-vpn import /path/to/configs/")
        return 0

    print(f"{'ACTIVE':<7} {'NAME':<22} {'PROTOCOL':<11} {'COUNTRY':<8} {'ENDPOINT'}")
    print("-" * 78)
    for prof in profiles:
        marker = "  *    " if prof.name == active else "       "
        endpoint = f"{prof.endpoint_host}:{prof.endpoint_port}" if prof.endpoint_port else prof.endpoint_host
        proto = prof.protocol if prof.protocol == "wireguard" else f"openvpn/{prof.transport}"
        print(f"{marker} {prof.name:<22} {proto:<11} {(prof.country or '-').upper():<8} {endpoint}")
    return 0


def cmd_import(config: dict[str, Any], args: argparse.Namespace) -> int:
    """Import one or more WireGuard/OpenVPN configs."""
    if os.name == "posix" and os.geteuid() != 0 and not os.access(config["_profiles_dir"].parent, os.W_OK):
        require_root()
    imported = import_profiles(Path(args.source), config["_profiles_dir"], custom_name=args.name, overwrite=args.force)
    for prof in imported:
        print(f"Imported {prof.name:<22} ({prof.protocol}, {(prof.country or '?').upper()}, {prof.endpoint_host})")

    all_profiles = discover_profiles(config["_profiles_dir"])
    state = read_state(config)
    if not state.get("active_profile"):
        chosen = resolve_active_profile(config, all_profiles)
        print(f"\nDefault active profile set to: {chosen.name}")
    print(f"\nTotal profiles available: {len(all_profiles)}. Switch anytime with: sudo rp-vpn use <name>")
    return 0


def cmd_use(config: dict[str, Any], profile_name: str, no_restart: bool = False) -> int:
    """Switch active VPN profile and restart the tunnel."""
    require_root()
    slug = normalize_profile_name(profile_name)
    profiles = discover_profiles(config["_profiles_dir"])
    by_name = {p.name: p for p in profiles}
    if slug not in by_name:
        available = ", ".join(p.name for p in profiles) or "(none)"
        raise ValueError(f"Unknown profile '{slug}'. Available profiles: {available}")

    chosen = by_name[slug]
    save_state(config, active_profile=chosen.name, desired_state="running", switched_at=utc_now_iso())
    print(f"Selected profile: {chosen.name} ({chosen.protocol} -> {chosen.endpoint_host})")
    if not no_restart:
        restart_vpn_unit_or_direct(config)
        tun_iface = active_tunnel_interface(config, chosen.protocol)
        info = fetch_exit_ip_info(tun_iface, timeout=5)
        if info.get("ip"):
            print(f"Connected! Exit IP: {info.get('ip')} ({info.get('city', '?')}, {info.get('country', '?')} - {info.get('org', '')})")
    return 0


def cmd_next(config: dict[str, Any]) -> int:
    """Switch to the next preferred UK profile."""
    require_root()
    profiles = discover_profiles(config["_profiles_dir"])
    current = resolve_active_profile(config, profiles)
    candidates = select_failover_candidates(
        profiles,
        current_name=current.name,
        preferred_country=config["preferred_country"],
        prefer_protocol=config["prefer_protocol"],
    )
    next_prof = next((p for p in candidates if p.name != current.name), current)
    if next_prof.name == current.name:
        print(f"Only one matching profile available ('{current.name}'); restarting it.")
    return cmd_use(config, next_prof.name)


def cmd_up(config: dict[str, Any]) -> int:
    """Enable desired_state=running and start the VPN tunnel."""
    require_root()
    save_state(config, desired_state="running")
    restart_vpn_unit_or_direct(config)
    print("VPN is UP.")
    return 0


def cmd_down(config: dict[str, Any]) -> int:
    """Stop the VPN tunnel and pause watchdog auto-restart until 'rp-vpn up'."""
    require_root()
    save_state(config, desired_state="stopped")
    unit_check = run_cmd(["systemctl", "cat", "rp-vpn.service"], check=False)
    if unit_check.returncode == 0:
        run_cmd(["systemctl", "stop", "rp-vpn.service"], check=False)
    else:
        service_stop(config)
    print("VPN stopped (Note: kill-switch remains active so hotspot clients will not leak unencrypted traffic).")
    return 0


def cmd_set_auth(config: dict[str, Any], args: argparse.Namespace) -> int:
    """Save Surfshark OpenVPN service credentials to git-ignored vpn/auth.txt."""
    if os.name == "posix" and os.geteuid() != 0 and not os.access(config["_base_dir"], os.W_OK):
        require_root()
    username = (args.username or "").strip()
    password = (args.password or "").strip()
    if not username:
        username = input("Surfshark OpenVPN Service Username: ").strip()
    if not password:
        password = getpass.getpass("Surfshark OpenVPN Service Password: ").strip()
    if not username or not password:
        raise ValueError("Both username and password are required.")
    auth_path: Path = config["_auth_path"]
    write_text_atomic(auth_path, f"{username}\n{password}\n", mode=0o600)
    print(f"Saved OpenVPN credentials to {auth_path} (mode 0600).")
    return 0


def cmd_configure_hotspot(config: dict[str, Any], args: argparse.Namespace) -> int:
    """Save Wi-Fi hotspot SSID, WPA2 passphrase and radio settings into vpn/config.local.json."""
    if os.name == "posix" and os.geteuid() != 0 and not os.access(config["_base_dir"], os.W_OK):
        require_root()
    hotspot_patch: dict[str, Any] = {}
    if args.ssid:
        hotspot_patch["ssid"] = args.ssid.strip()
    passphrase = args.passphrase
    if passphrase is None and not config["hotspot"].get("passphrase"):
        passphrase = getpass.getpass("Hotspot WPA2 Passphrase (8-63 chars): ").strip()
    if passphrase is not None:
        if len(passphrase) < 8 or len(passphrase) > 63 or not passphrase.isprintable():
            raise ValueError("Passphrase must be 8 to 63 printable ASCII characters.")
        hotspot_patch["passphrase"] = passphrase
    if args.country:
        hotspot_patch["country_code"] = args.country.strip().upper()
    if args.band:
        hotspot_patch["band"] = args.band.strip().lower()
    if args.channel is not None:
        hotspot_patch["channel"] = int(args.channel)
    if args.interface:
        hotspot_patch["interface"] = args.interface.strip()

    if not hotspot_patch:
        print("No hotspot changes specified.")
        return 0

    local_path = update_local_config(config["_config_path"], {"hotspot": hotspot_patch})
    print(f"Updated hotspot configuration in {local_path}.")
    if os.geteuid() == 0 and run_cmd(["systemctl", "is-active", "rp-vpn-hotspot.service"], check=False).returncode == 0:
        run_cmd(["systemctl", "restart", "rp-vpn-hotspot.service"], check=False)
        print("Restarted rp-vpn-hotspot.service.")
    return 0


def cmd_status(config: dict[str, Any]) -> int:
    """Display comprehensive VPN tunnel, exit IP, kill-switch and Wi-Fi hotspot status."""
    state = read_state(config)
    profiles = discover_profiles(config["_profiles_dir"])
    active_name = state.get("active_profile") or "(none)"
    by_name = {p.name: p for p in profiles}
    profile = by_name.get(active_name)

    wg_iface = config["wireguard"]["interface"]
    ovpn_iface = config["openvpn"]["interface"]
    tun_iface = wg_iface if interface_exists(wg_iface) else (ovpn_iface if interface_exists(ovpn_iface) else "")

    print("=== rp-inky Always-On VPN & Hotspot Status ===")
    print(f"Desired state  : {state.get('desired_state', 'running')}")
    print(f"Active profile : {active_name}" + (f" ({profile.protocol}, {profile.endpoint_host})" if profile else ""))
    print(f"Profiles count : {len(profiles)} imported in {config['_profiles_dir']}")

    wan_ip = get_interface_ipv4(config["wan_interface"]) or "down"
    print(f"WAN interface  : {config['wan_interface']} ({wan_ip})")

    if tun_iface:
        tun_ip = get_interface_ipv4(tun_iface) or "no-ip"
        print(f"VPN interface  : {tun_iface} UP ({tun_ip})")
        if tun_iface == wg_iface and shutil.which("wg"):
            age = wireguard_handshake_age(wg_iface)
            xfer = run_cmd(["wg", "show", wg_iface, "transfer"], check=False).stdout.strip()
            age_str = f"{age}s ago" if age is not None else "pending"
            print(f"WG handshake   : {age_str}")
            if xfer:
                parts = xfer.splitlines()[0].split()
                if len(parts) >= 3:
                    with suppress(ValueError):
                        print(f"WG transfer    : RX {_format_bytes(int(parts[1]))} / TX {_format_bytes(int(parts[2]))}")
        exit_info = fetch_exit_ip_info(tun_iface, timeout=4)
        if exit_info.get("ip"):
            print(
                f"VPN exit IP    : {exit_info.get('ip')} — "
                f"{exit_info.get('city', '?')}, {exit_info.get('country', '?')} ({exit_info.get('org', '')})"
            )
    else:
        print("VPN interface  : DOWN")

    if shutil.which("nft") and os.geteuid() == 0:
        nft_ok = run_cmd(["nft", "list", "table", "inet", "rp_vpn"], check=False).returncode == 0
        print(f"Kill-switch    : {'ACTIVE (nftables inet rp_vpn)' if nft_ok else 'INACTIVE'}")

    hotspot = config["hotspot"]
    if hotspot.get("enabled", True):
        ap_iface = hotspot["interface"]
        ap_ip = get_interface_ipv4(ap_iface) or "down"
        print(
            f"Wi-Fi hotspot  : {ap_iface} ({ap_ip}) SSID='{hotspot['ssid']}' "
            f"{hotspot['band']} ch {hotspot['channel']}" + (" VHT80" if hotspot.get("vht80") and hotspot["band"] == "5g" else "")
        )
        if shutil.which("iw") and interface_exists(ap_iface):
            stations = run_cmd(["iw", "dev", ap_iface, "station", "dump"], check=False).stdout
            macs = re.findall(r"^Station ([0-9a-f:]{17})", stations, flags=re.MULTILINE)
            tx_rates = re.findall(r"tx bitrate:\s*(.+)", stations)
            print(f"Wi-Fi clients  : {len(macs)} connected" + (f" (PHY rate: {tx_rates[0].strip()})" if tx_rates else ""))
    if config.get("allow_lan_gateway", True) and wan_ip != "down":
        print(f"500M LAN GW    : Ready — set Gateway & DNS to {wan_ip} on any LAN/Wi-Fi 6 device")
    return 0


def _measure_curl_mbps(iface: str, byte_count: int) -> Optional[float]:
    """Download byte_count from Cloudflare speed endpoint bound to iface and return Mbps."""
    host = "speed.cloudflare.com"
    url = f"https://{host}/__down?bytes={byte_count}"
    cmd = [
        "curl",
        "-4",
        "-sS",
        "-o",
        "/dev/null",
        "-w",
        "%{speed_download}",
        "--max-time",
        "25",
        "--interface",
        iface,
    ]
    resolved = resolve_endpoint_ipv4(host)
    if resolved:
        cmd.extend(["--resolve", f"{host}:443:{resolved}"])
    cmd.append(url)
    res = run_cmd(cmd, check=False, timeout=30)
    if res.returncode != 0 or not res.stdout.strip():
        return None
    try:
        bytes_per_sec = float(res.stdout.strip())
    except ValueError:
        return None
    return (bytes_per_sec * 8.0) / 1_000_000.0 if bytes_per_sec > 0 else None


def cmd_benchmark(config: dict[str, Any], megabytes: int = 25) -> int:
    """Measure direct WAN throughput vs VPN tunnel throughput to isolate bottlenecks."""
    if not shutil.which("curl"):
        raise RuntimeError("curl is required for 'rp-vpn benchmark'. Install with: sudo apt install curl")
    if os.geteuid() == 0:
        ensure_host_dns_healthy()

    byte_count = max(5, min(200, megabytes)) * 1_000_000
    wan_iface = config["wan_interface"]
    wg_iface = config["wireguard"]["interface"]
    ovpn_iface = config["openvpn"]["interface"]
    tun_iface = wg_iface if interface_exists(wg_iface) else (ovpn_iface if interface_exists(ovpn_iface) else "")

    print(f"Running {megabytes} MB throughput test...")
    wan_mbps = _measure_curl_mbps(wan_iface, byte_count)
    print(f"  1. Direct WAN ({wan_iface:<7}) : {f'{wan_mbps:.1f} Mbps' if wan_mbps else 'FAILED'}")

    if not tun_iface:
        print("  2. VPN Tunnel          : DOWN (start VPN first with: sudo rp-vpn up)")
        return 1

    vpn_mbps = _measure_curl_mbps(tun_iface, byte_count)
    exit_info = fetch_exit_ip_info(tun_iface, timeout=5)
    loc = f"{exit_info.get('city', '?')}, {exit_info.get('country', '?')}" if exit_info else "?"
    print(f"  2. VPN Tunnel ({tun_iface:<7}) : {f'{vpn_mbps:.1f} Mbps' if vpn_mbps else 'FAILED'} [{loc}]")

    print("\nBottleneck Guide:")
    if tun_iface == ovpn_iface:
        print("  - Active tunnel uses OpenVPN (user-space crypto caps Pi 4 at ~70-130 Mbps).")
        print("    Switch to a Surfshark WireGuard (.conf) profile for 500+ Mbps!")
    if config["hotspot"].get("enabled", True) and config["hotspot"]["interface"] == "wlan0":
        print("  - Note: Raspberry Pi 4's onboard wlan0 chip physically maxes out around 100-180 Mbps in 5GHz AP mode.")
        wan_ip = get_interface_ipv4(wan_iface) or "<Pi-LAN-IP>"
        print(f"    For full 500 Mbps: point a device's Gateway & DNS on your main Wi-Fi 6 router to {wan_ip},")
        print("    or plug a USB 3.0 Wi-Fi 6 AP adapter (wlan1) into a blue USB 3.0 port.")
    return 0


def cmd_write_sysctl() -> int:
    """Write kernel sysctl & BBR module autoload files and apply them."""
    require_root()
    write_text_atomic(MODULES_DEST, "tcp_bbr\n", mode=0o644)
    run_cmd(["modprobe", "tcp_bbr"], check=False)
    write_text_atomic(SYSCTL_DEST, build_sysctl_config(), mode=0o644)
    run_cmd(["sysctl", "--system"], check=False)
    print(f"Installed kernel network tuning at {SYSCTL_DEST}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rp-vpn",
        description="Always-on Surfshark UK VPN manager, multi-profile switcher and 5 GHz Wi-Fi hotspot",
    )
    parser.add_argument(
        "--config",
        default=str(Path(__file__).resolve().parent / "config.json"),
        help="path to vpn/config.json",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="enable debug logging")

    sub = parser.add_subparsers(dest="command", required=True)

    p_import = sub.add_parser("import", help="import a Surfshark .conf/.ovpn file or directory of configs")
    p_import.add_argument("source", help="path to .conf/.ovpn file or folder")
    p_import.add_argument("--name", help="custom profile name (single file only)")
    p_import.add_argument("--force", action="store_true", help="overwrite existing profile with same name")

    sub.add_parser("list", help="list imported VPN profiles and active selection")

    p_use = sub.add_parser("use", aliases=["switch"], help="switch active VPN profile immediately")
    p_use.add_argument("profile", help="profile name from 'rp-vpn list'")
    p_use.add_argument("--no-restart", action="store_true", help="record selection without restarting tunnel now")

    sub.add_parser("next", help="fail over to the next preferred UK profile")
    sub.add_parser("up", help="start Always-On VPN tunnel")
    sub.add_parser("down", help="stop VPN tunnel (kill-switch stays active)")
    sub.add_parser("restart", help="restart active VPN tunnel")
    sub.add_parser("status", help="show VPN tunnel, exit IP, kill-switch and Wi-Fi hotspot status")

    p_bench = sub.add_parser("benchmark", help="benchmark WAN vs VPN tunnel speed on the Pi")
    p_bench.add_argument("--megabytes", type=int, default=25, help="test payload size in MB (default: 25)")

    p_auth = sub.add_parser("set-auth", help="save Surfshark OpenVPN manual service credentials")
    p_auth.add_argument("--username", help="Surfshark service username")
    p_auth.add_argument("--password", help="Surfshark service password")

    p_ap = sub.add_parser("configure-hotspot", help="configure Wi-Fi hotspot SSID, password and band")
    p_ap.add_argument("--ssid", help="Wi-Fi network name (SSID)")
    p_ap.add_argument("--passphrase", help="WPA2 passphrase (8-63 characters)")
    p_ap.add_argument("--country", help="2-letter Wi-Fi regulatory country code (e.g. US, GB)")
    p_ap.add_argument("--band", choices=["5g", "2.4g"], help="Wi-Fi band (default: 5g for speed)")
    p_ap.add_argument("--channel", type=int, help="Wi-Fi channel (e.g. 36 or 149 for 5g, 1/6/11 for 2.4g)")
    p_ap.add_argument("--interface", help="Wi-Fi AP interface (wlan0 onboard, or wlan1 USB3 Wi-Fi 6)")

    # Internal systemd / hook subcommands
    sub.add_parser("service-start", help=argparse.SUPPRESS)
    sub.add_parser("service-stop", help=argparse.SUPPRESS)
    sub.add_parser("hotspot-up", help=argparse.SUPPRESS)
    sub.add_parser("hotspot-down", help=argparse.SUPPRESS)
    sub.add_parser("watchdog", help=argparse.SUPPRESS)
    sub.add_parser("internal-openvpn-up", help=argparse.SUPPRESS)
    sub.add_parser("write-sysctl", help=argparse.SUPPRESS)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    LOG.setLevel(logging.DEBUG if args.verbose else logging.INFO)

    try:
        config = load_vpn_config(Path(args.config))
        cmd = args.command
        if cmd == "list":
            return cmd_list(config)
        if cmd == "import":
            return cmd_import(config, args)
        if cmd in ("use", "switch"):
            return cmd_use(config, args.profile, no_restart=args.no_restart)
        if cmd == "next":
            return cmd_next(config)
        if cmd in ("up", "restart"):
            return cmd_up(config)
        if cmd == "down":
            return cmd_down(config)
        if cmd == "status":
            return cmd_status(config)
        if cmd == "benchmark":
            return cmd_benchmark(config, megabytes=args.megabytes)
        if cmd == "set-auth":
            return cmd_set_auth(config, args)
        if cmd == "configure-hotspot":
            return cmd_configure_hotspot(config, args)
        if cmd == "service-start":
            return service_start(config)
        if cmd == "service-stop":
            return service_stop(config)
        if cmd == "hotspot-up":
            return hotspot_up(config)
        if cmd == "hotspot-down":
            return hotspot_down(config)
        if cmd == "watchdog":
            return run_watchdog(config)
        if cmd == "internal-openvpn-up":
            return handle_openvpn_route_up(config)
        if cmd == "write-sysctl":
            return cmd_write_sysctl()
        raise ValueError(f"Unhandled command: {cmd}")
    except (ValueError, FileNotFoundError, FileExistsError, PermissionError, RuntimeError) as exc:
        LOG.error("%s", exc)
        return 1
    except Exception:
        LOG.exception("Unexpected error")
        return 1


if __name__ == "__main__":
    sys.exit(main())
