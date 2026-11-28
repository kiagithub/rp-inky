"""Structured JSON bridge between the Web UI and vpn/manager.py."""

import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

from vpn import manager as vm

COUNTRY_NAMES: dict[str, str] = {
    "GB": "United Kingdom",
    "UK": "United Kingdom",
    "US": "United States",
    "CH": "Switzerland",
    "CZ": "Czechia",
    "ES": "Spain",
    "IS": "Iceland",
    "MX": "Mexico",
    "SE": "Sweden",
    "CA": "Canada",
    "DE": "Germany",
    "FR": "France",
    "NL": "Netherlands",
    "IT": "Italy",
    "JP": "Japan",
    "AU": "Australia",
    "NO": "Norway",
    "DK": "Denmark",
    "FI": "Finland",
    "IE": "Ireland",
    "PT": "Portugal",
    "AT": "Austria",
    "BE": "Belgium",
    "PL": "Poland",
    "SG": "Singapore",
    "KR": "South Korea",
    "BR": "Brazil",
    "IN": "India",
    "TR": "Turkey",
}


def country_meta(code: str) -> tuple[str, str, str]:
    """Return (iso2_code, readable_name, flag_emoji) for a 2-letter country code."""
    raw = (code or "").strip().upper()
    if raw == "UK":
        iso = "GB"
    elif len(raw) == 2 and raw.isalpha():
        iso = raw
    else:
        return ("", "Unknown", "🌐")
    flag = "".join(chr(0x1F1E6 + ord(ch) - ord("A")) for ch in iso)
    name = COUNTRY_NAMES.get(iso, iso)
    return (iso, name, flag)


class VpnBridge:
    """Thread-safe controller for reading VPN status, switching configs and benchmarking."""

    def __init__(self, vpn_config_path: Path):
        self.vpn_config_path = Path(vpn_config_path).expanduser().resolve()
        self._exit_cache: dict[str, Any] = {"fetched_at": 0.0, "iface": "", "profile": "", "info": {}}
        self._bench_lock = threading.Lock()
        self._bench_state: dict[str, Any] = {
            "running": False,
            "wan_mbps": None,
            "vpn_mbps": None,
            "exit_location": "",
            "updated_at": "",
            "error": "",
        }

    def _rp_vpn_cmd(self) -> list[str]:
        """Build command prefix for invoking privileged rp-vpn operations."""
        cli = Path("/usr/local/bin/rp-vpn")
        script = self.vpn_config_path.parent / "manager.py"
        if os.geteuid() == 0:
            if cli.is_file():
                return [str(cli)]
            return ["/usr/bin/python3", str(script), "--config", str(self.vpn_config_path)]
        if cli.is_file():
            return ["sudo", "-n", str(cli)]
        return ["sudo", "-n", "/usr/bin/python3", str(script), "--config", str(self.vpn_config_path)]

    def _run_privileged(self, args: list[str], timeout: int = 45) -> str:
        cmd = [*self._rp_vpn_cmd(), *args]
        res = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=timeout)
        if res.returncode != 0:
            detail = (res.stderr or res.stdout or "").strip()
            raise RuntimeError(detail or f"rp-vpn {' '.join(args)} failed (code {res.returncode})")
        return res.stdout.strip()

    def get_status(self, refresh_exit_ip: bool = False) -> dict[str, Any]:
        """Return structured JSON status for VPN tunnel, profiles, hotspot & benchmark."""
        cfg = vm.load_vpn_config(self.vpn_config_path)
        state = vm.read_state(cfg)
        profiles = vm.discover_profiles(cfg["_profiles_dir"])
        active_name = state.get("active_profile") or ""

        wg_iface = cfg["wireguard"]["interface"]
        ovpn_iface = cfg["openvpn"]["interface"]
        tun_iface = wg_iface if vm.interface_exists(wg_iface) else (ovpn_iface if vm.interface_exists(ovpn_iface) else "")
        tun_ip = vm.get_interface_ipv4(tun_iface) if tun_iface else None
        wan_ip = vm.get_interface_ipv4(cfg["wan_interface"])

        handshake_age: Optional[int] = None
        rx_bytes = 0
        tx_bytes = 0
        if tun_iface == wg_iface and shutil.which("wg"):
            # Try direct or passwordless sudo wg show via rp-vpn status fallback
            wg_cmd = ["wg", "show", wg_iface, "dump"] if os.geteuid() == 0 else ["sudo", "-n", "wg", "show", wg_iface, "dump"]
            res = subprocess.run(wg_cmd, capture_output=True, text=True, check=False, timeout=4)
            if res.returncode == 0:
                lines = res.stdout.strip().splitlines()
                if len(lines) >= 2:
                    parts = lines[1].split("\t")
                    if len(parts) >= 7:
                        ts = int(parts[4]) if parts[4].isdigit() else 0
                        if ts > 0:
                            handshake_age = max(0, int(time.time()) - ts)
                        rx_bytes = int(parts[5]) if parts[5].isdigit() else 0
                        tx_bytes = int(parts[6]) if parts[6].isdigit() else 0

        exit_info: dict[str, str] = {}
        if tun_iface and tun_ip:
            now = time.monotonic()
            cached = self._exit_cache
            if (
                not refresh_exit_ip
                and cached["iface"] == tun_iface
                and cached["profile"] == active_name
                and (now - cached["fetched_at"]) < 45.0
            ):
                exit_info = cached["info"]
            else:
                exit_info = vm.fetch_exit_ip_info(tun_iface, timeout=4)
                self._exit_cache = {
                    "fetched_at": now,
                    "iface": tun_iface,
                    "profile": active_name,
                    "info": exit_info,
                }

        hotspot = cfg["hotspot"]
        ap_iface = hotspot["interface"]
        ap_up = hotspot.get("enabled", True) and vm.interface_exists(ap_iface)
        wifi_clients = 0
        wifi_phy_rate = ""
        if ap_up and shutil.which("iw"):
            st = subprocess.run(["iw", "dev", ap_iface, "station", "dump"], capture_output=True, text=True, check=False, timeout=3)
            if st.returncode == 0:
                wifi_clients = len(re.findall(r"^Station ([0-9a-f:]{17})", st.stdout, flags=re.MULTILINE))
                rates = re.findall(r"tx bitrate:\s*(.+)", st.stdout)
                if rates:
                    wifi_phy_rate = rates[0].strip()

        active_prof = next((p for p in profiles if p.name == active_name), None)
        raw_country = exit_info.get("country") or (active_prof.country if active_prof else "") or ""
        act_iso, act_cname, act_flag = country_meta(raw_country)

        # Group imported profiles by country for 1-tap country switching
        by_country: dict[str, dict[str, Any]] = {}
        for p in profiles:
            p_iso, p_cname, p_flag = country_meta(p.country or "")
            if not p_iso:
                continue
            grp = by_country.get(p_iso)
            if grp is None:
                by_country[p_iso] = {
                    "code": p_iso,
                    "name": p_cname,
                    "flag": p_flag,
                    "active": p_iso == act_iso and bool(active_name),
                    "profile_count": 1,
                    "primary_profile": p.name,
                    "_primary_proto": p.protocol,
                }
            else:
                grp["profile_count"] += 1
                if p.name == active_name:
                    grp["active"] = True
                if grp["_primary_proto"] != "wireguard" and p.protocol == "wireguard":
                    grp["primary_profile"] = p.name
                    grp["_primary_proto"] = p.protocol

        countries_list = sorted(
            ({k: v for k, v in g.items() if not k.startswith("_")} for g in by_country.values()),
            key=lambda g: (0 if g["active"] else 1, g["name"]),
        )

        profile_rows = []
        for p in profiles:
            p_iso, p_cname, p_flag = country_meta(p.country or "")
            profile_rows.append(
                {
                    "name": p.name,
                    "protocol": p.protocol,
                    "transport": p.transport,
                    "country": p_iso or (p.country or "").upper(),
                    "country_name": p_cname,
                    "country_flag": p_flag,
                    "endpoint": f"{p.endpoint_host}:{p.endpoint_port}" if p.endpoint_port else p.endpoint_host,
                    "active": p.name == active_name,
                }
            )

        return {
            "desired_state": state.get("desired_state", "running"),
            "active_profile": active_name,
            "active_country": act_iso,
            "active_country_name": act_cname,
            "active_country_flag": act_flag,
            "connected": bool(tun_iface and tun_ip),
            "tunnel_interface": tun_iface or "",
            "tunnel_ip": tun_ip or "",
            "wan_interface": cfg["wan_interface"],
            "wan_ip": wan_ip or "",
            "handshake_age_seconds": handshake_age,
            "rx_bytes": rx_bytes,
            "tx_bytes": tx_bytes,
            "rx_human": vm._format_bytes(rx_bytes),
            "tx_human": vm._format_bytes(tx_bytes),
            "exit_ip": exit_info.get("ip", ""),
            "exit_city": exit_info.get("city", ""),
            "exit_country": exit_info.get("country", ""),
            "exit_org": exit_info.get("org", ""),
            "hotspot": {
                "enabled": bool(hotspot.get("enabled", True)),
                "up": ap_up,
                "interface": ap_iface,
                "ssid": hotspot["ssid"],
                "has_passphrase": bool(hotspot.get("passphrase")),
                "band": hotspot["band"],
                "channel": hotspot["channel"],
                "vht80": bool(hotspot.get("vht80", True)),
                "gateway_ip": hotspot["gateway_ip"],
                "clients": wifi_clients,
                "phy_rate": wifi_phy_rate,
            },
            "lan_gateway_ip": wan_ip if cfg.get("allow_lan_gateway", True) and wan_ip else "",
            "countries": countries_list,
            "profiles": profile_rows,
            "benchmark": dict(self._bench_state),
        }

    def switch_profile(self, profile_name: str) -> dict[str, Any]:
        slug = vm.normalize_profile_name(profile_name)
        self._run_privileged(["use", slug], timeout=50)
        self._exit_cache["fetched_at"] = 0.0
        return self.get_status(refresh_exit_ip=True)

    def switch_country(self, country_code: str) -> dict[str, Any]:
        """Switch VPN to the best available profile in the requested country."""
        iso, _, _ = country_meta(country_code)
        if not iso:
            raise ValueError(f"Invalid country code: {country_code!r}")
        cfg = vm.load_vpn_config(self.vpn_config_path)
        profiles = vm.discover_profiles(cfg["_profiles_dir"])
        matches = [p for p in profiles if country_meta(p.country or "")[0] == iso]
        if not matches:
            raise ValueError(f"No imported VPN profiles found for country '{iso}'.")
        matches.sort(key=lambda p: (0 if p.protocol == "wireguard" else 1, p.name))
        return self.switch_profile(matches[0].name)

    def update_hotspot(self, ssid: str = "", passphrase: str = "") -> dict[str, Any]:
        """Update Wi-Fi hotspot network name (SSID) and/or WPA2 passphrase via rp-vpn."""
        clean_ssid = ssid.strip()
        clean_pass = passphrase.strip()
        if not clean_ssid and not clean_pass:
            raise ValueError("Provide a new Wi-Fi network name (SSID) or password.")
        args = ["configure-hotspot"]
        if clean_ssid:
            if len(clean_ssid) > 32 or not clean_ssid.isprintable():
                raise ValueError("Wi-Fi name (SSID) must be 1 to 32 printable characters.")
            args.extend(["--ssid", clean_ssid])
        if clean_pass:
            if len(clean_pass) < 8 or len(clean_pass) > 63 or not clean_pass.isprintable():
                raise ValueError("Wi-Fi password must be 8 to 63 printable ASCII characters.")
            args.extend(["--passphrase", clean_pass])
        self._run_privileged(args, timeout=30)
        return self.get_status()

    def action(self, action_name: str) -> dict[str, Any]:
        if action_name not in ("up", "down", "restart", "next"):
            raise ValueError(f"Unsupported VPN action: {action_name}")
        self._run_privileged([action_name], timeout=50)
        self._exit_cache["fetched_at"] = 0.0
        return self.get_status(refresh_exit_ip=(action_name != "down"))

    def upload_profile(self, filename: str, content_bytes: bytes, custom_name: str = "") -> dict[str, Any]:
        ext = Path(filename).suffix.lower()
        if ext not in (".conf", ".ovpn"):
            raise ValueError("Profile file must end with .conf (WireGuard) or .ovpn (OpenVPN).")
        if not content_bytes or len(content_bytes) > 512 * 1024:
            raise ValueError("Invalid profile file size.")

        derived_name = vm.normalize_profile_name(custom_name or Path(filename).name)
        with tempfile.NamedTemporaryFile(prefix="rp-vpn-upload-", suffix=ext, delete=False) as handle:
            tmp_path = Path(handle.name)
            os.chmod(tmp_path, 0o600)
            handle.write(content_bytes)
        try:
            args = ["import", str(tmp_path), "--name", derived_name, "--force"]
            self._run_privileged(args, timeout=20)
        finally:
            tmp_path.unlink(missing_ok=True)
        return self.get_status()

    def start_benchmark(self, megabytes: int = 25) -> dict[str, Any]:
        """Launch a non-blocking WAN vs VPN speed benchmark in a background thread."""
        with self._bench_lock:
            if self._bench_state["running"]:
                return dict(self._bench_state)
            self._bench_state = {
                "running": True,
                "wan_mbps": None,
                "vpn_mbps": None,
                "exit_location": "",
                "updated_at": vm.utc_now_iso(),
                "error": "",
            }

        thread = threading.Thread(target=self._run_benchmark_worker, args=(megabytes,), daemon=True)
        thread.start()
        return dict(self._bench_state)

    def _run_benchmark_worker(self, megabytes: int) -> None:
        try:
            cfg = vm.load_vpn_config(self.vpn_config_path)
            byte_count = max(5, min(100, int(megabytes))) * 1_000_000
            wan_mbps = vm._measure_curl_mbps(cfg["wan_interface"], byte_count)

            wg_iface = cfg["wireguard"]["interface"]
            ovpn_iface = cfg["openvpn"]["interface"]
            tun_iface = wg_iface if vm.interface_exists(wg_iface) else (ovpn_iface if vm.interface_exists(ovpn_iface) else "")
            vpn_mbps = vm._measure_curl_mbps(tun_iface, byte_count) if tun_iface else None
            exit_info = vm.fetch_exit_ip_info(tun_iface, timeout=4) if tun_iface else {}
            loc = f"{exit_info.get('city', '?')}, {exit_info.get('country', '?')}" if exit_info.get("ip") else ""

            with self._bench_lock:
                self._bench_state = {
                    "running": False,
                    "wan_mbps": round(wan_mbps, 1) if wan_mbps else None,
                    "vpn_mbps": round(vpn_mbps, 1) if vpn_mbps else None,
                    "exit_location": loc,
                    "updated_at": vm.utc_now_iso(),
                    "error": "" if (wan_mbps or vpn_mbps) else "Benchmark download failed",
                }
        except Exception as exc:
            with self._bench_lock:
                self._bench_state = {
                    "running": False,
                    "wan_mbps": None,
                    "vpn_mbps": None,
                    "exit_location": "",
                    "updated_at": vm.utc_now_iso(),
                    "error": str(exc),
                }
