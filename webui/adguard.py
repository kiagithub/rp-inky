"""Lightweight AdGuard Home local status detector for the rp-inky Web Hub."""

import json
import socket
import urllib.error
import urllib.request
from contextlib import suppress
from typing import Any


def get_adguard_status(host: str = "127.0.0.1", port: int = 3000, timeout_sec: float = 1.5) -> dict[str, Any]:
    """Check whether AdGuard Home is running locally and read basic status/stats if accessible."""
    port = int(port or 3000)
    dns_port_open = False
    web_port_open = False

    with suppress(OSError), socket.create_connection((host, port), timeout=timeout_sec):
        web_port_open = True

    with suppress(OSError), socket.create_connection((host, 53), timeout=0.6):
        dns_port_open = True

    result: dict[str, Any] = {
        "installed": web_port_open,
        "running": web_port_open,
        "dns_listening": dns_port_open,
        "port": port,
        "auth_required": False,
        "protection_enabled": None,
        "version": "",
        "dns_queries": None,
        "blocked_filtering": None,
        "blocked_pct": None,
    }
    if not web_port_open:
        return result

    base_url = f"http://{host}:{port}"
    try:
        req = urllib.request.Request(f"{base_url}/control/status", headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            if isinstance(data, dict):
                result["protection_enabled"] = bool(data.get("protection_enabled", True))
                result["version"] = str(data.get("version") or "")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            result["auth_required"] = True
            return result
    except (OSError, ValueError):
        return result

    try:
        req = urllib.request.Request(f"{base_url}/control/stats", headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            stats = json.loads(resp.read().decode("utf-8", errors="replace"))
            if isinstance(stats, dict):
                queries = int(stats.get("num_dns_queries") or 0)
                blocked = int(stats.get("num_blocked_filtering") or 0)
                result["dns_queries"] = queries
                result["blocked_filtering"] = blocked
                result["blocked_pct"] = round((blocked * 100.0) / queries, 1) if queries > 0 else 0.0
    except (OSError, ValueError, urllib.error.URLError):
        pass

    return result
