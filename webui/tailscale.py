"""Read-only Tailscale status collector for the rp-inky Web Hub."""

import json
import shutil
import subprocess
from contextlib import suppress
from typing import Any


def parse_tailscale_status_json(raw: str) -> dict[str, Any]:
    """Parse `tailscale status --json` output into a compact Web Hub summary."""
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("Invalid Tailscale status payload")

    backend_state = str(data.get("BackendState") or "Unknown")
    self_node = data.get("Self") if isinstance(data.get("Self"), dict) else {}
    ts_ips = self_node.get("TailscaleIPs") if isinstance(self_node.get("TailscaleIPs"), list) else []
    ipv4 = next((str(ip) for ip in ts_ips if "." in str(ip)), "")
    dns_name = str(self_node.get("DNSName") or "").rstrip(".")
    host_name = str(self_node.get("HostName") or "")

    peers_raw = data.get("Peer") if isinstance(data.get("Peer"), dict) else {}
    peer_count = len(peers_raw)
    online_peers = sum(1 for p in peers_raw.values() if isinstance(p, dict) and p.get("Online"))
    exit_node_option = bool(self_node.get("ExitNodeOption", False))

    return {
        "installed": True,
        "running": backend_state == "Running",
        "backend_state": backend_state,
        "tailscale_ip": ipv4,
        "dns_name": dns_name,
        "hostname": host_name,
        "peer_count": peer_count,
        "online_peers": online_peers,
        "exit_node_advertised": exit_node_option,
    }


def get_tailscale_status(timeout_sec: float = 2.5) -> dict[str, Any]:
    """Query local `tailscale status --json` without requiring root."""
    if not shutil.which("tailscale"):
        return {
            "installed": False,
            "running": False,
            "backend_state": "NotInstalled",
            "tailscale_ip": "",
            "dns_name": "",
            "hostname": "",
            "peer_count": 0,
            "online_peers": 0,
            "exit_node_advertised": False,
        }

    with suppress(OSError, subprocess.TimeoutExpired, ValueError):
        res = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout_sec,
        )
        if res.stdout.strip():
            return parse_tailscale_status_json(res.stdout)

    return {
        "installed": True,
        "running": False,
        "backend_state": "Stopped",
        "tailscale_ip": "",
        "dns_name": "",
        "hostname": "",
        "peer_count": 0,
        "online_peers": 0,
        "exit_node_advertised": False,
    }
