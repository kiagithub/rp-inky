"""Raspberry Pi System Health collector (CPU temp, RAM, SD disk, uptime & throttling)."""

import os
import re
import shutil
import subprocess
from contextlib import suppress
from pathlib import Path
from typing import Any, Optional

THROTTLE_BITS: tuple[tuple[int, str, bool], ...] = (
    (0x1, "Undervoltage now", True),
    (0x2, "ARM freq capped now", True),
    (0x4, "Throttled now", True),
    (0x8, "Soft temp limit now", True),
    (0x10000, "Undervoltage occurred", False),
    (0x20000, "Freq capped occurred", False),
    (0x40000, "Throttling occurred", False),
    (0x80000, "Soft temp limit occurred", False),
)


def parse_throttled_mask(mask: int) -> dict[str, Any]:
    """Decode Raspberry Pi `vcgencmd get_throttled` hexadecimal bitmask."""
    active: list[str] = []
    historical: list[str] = []
    for bit, label, is_active in THROTTLE_BITS:
        if mask & bit:
            if is_active:
                active.append(label)
            else:
                historical.append(label)
    if active:
        status = "crit"
        summary = ", ".join(active)
    elif historical:
        status = "warn"
        summary = ", ".join(historical)
    else:
        status = "ok"
        summary = "OK"
    return {
        "mask": hex(mask),
        "status": status,
        "summary": summary,
        "active": active,
        "historical": historical,
    }


def read_cpu_temp_c() -> Optional[float]:
    """Read SoC temperature in Celsius from Linux thermal zone or vcgencmd."""
    thermal = Path("/sys/class/thermal/thermal_zone0/temp")
    if thermal.is_file():
        with suppress(OSError, ValueError):
            raw = thermal.read_text(encoding="utf-8").strip()
            val = float(raw)
            return round(val / 1000.0 if val > 200 else val, 1)

    if shutil.which("vcgencmd"):
        with suppress(OSError, subprocess.TimeoutExpired, ValueError):
            res = subprocess.run(
                ["vcgencmd", "measure_temp"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=2,
            )
            if res.returncode == 0:
                m = re.search(r"([0-9]+(?:\.[0-9]+)?)", res.stdout)
                if m:
                    return round(float(m.group(1)), 1)
    return None


def read_throttled_info() -> dict[str, Any]:
    """Query `vcgencmd get_throttled` on Raspberry Pi OS (returns N/A on non-Pi hosts)."""
    if not shutil.which("vcgencmd"):
        return {"available": False, "mask": "", "status": "ok", "summary": "N/A", "active": [], "historical": []}
    with suppress(OSError, subprocess.TimeoutExpired, ValueError):
        res = subprocess.run(
            ["vcgencmd", "get_throttled"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=2,
        )
        if res.returncode == 0:
            m = re.search(r"0x([0-9a-fA-F]+)", res.stdout)
            if m:
                parsed = parse_throttled_mask(int(m.group(1), 16))
                parsed["available"] = True
                return parsed
    return {"available": False, "mask": "", "status": "ok", "summary": "N/A", "active": [], "historical": []}


def read_memory_info() -> dict[str, Any]:
    """Read total & used RAM from /proc/meminfo on Linux."""
    meminfo = Path("/proc/meminfo")
    if meminfo.is_file():
        values: dict[str, int] = {}
        with suppress(OSError):
            for line in meminfo.read_text(encoding="utf-8", errors="ignore").splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    values[parts[0].rstrip(":")] = int(parts[1])  # kB
        total_kb = values.get("MemTotal", 0)
        avail_kb = values.get("MemAvailable", values.get("MemFree", 0))
        if total_kb > 0:
            used_kb = max(0, total_kb - avail_kb)
            return {
                "available": True,
                "total_mb": round(total_kb / 1024.0),
                "used_mb": round(used_kb / 1024.0),
                "pct": round((used_kb * 100.0) / total_kb, 1),
            }
    return {"available": False, "total_mb": 0, "used_mb": 0, "pct": None}


def read_disk_info(path: str = "/") -> dict[str, Any]:
    """Read root filesystem / SD card usage."""
    try:
        usage = shutil.disk_usage(path)
        pct = round((usage.used * 100.0) / usage.total, 1) if usage.total > 0 else 0.0
        return {
            "available": True,
            "total_gb": round(usage.total / (1024**3), 1),
            "used_gb": round(usage.used / (1024**3), 1),
            "free_gb": round(usage.free / (1024**3), 1),
            "pct": pct,
        }
    except OSError:
        return {"available": False, "total_gb": 0.0, "used_gb": 0.0, "free_gb": 0.0, "pct": None}


def read_uptime_seconds() -> Optional[int]:
    """Read system uptime in seconds from /proc/uptime."""
    up_path = Path("/proc/uptime")
    if up_path.is_file():
        with suppress(OSError, ValueError):
            first = up_path.read_text(encoding="utf-8").split()[0]
            return int(float(first))
    return None


def format_uptime(seconds: Optional[int]) -> str:
    """Format uptime seconds into a compact string like '3d 14h' or '42m'."""
    if seconds is None or seconds < 0:
        return "—"
    if seconds < 60:
        return f"{seconds}s"
    mins = seconds // 60
    if mins < 60:
        return f"{mins}m"
    hrs = mins // 60
    if hrs < 24:
        return f"{hrs}h {mins % 60}m"
    days = hrs // 24
    return f"{days}d {hrs % 24}h"


def parse_log2ram_mounts(mounts_text: str) -> bool:
    """Return True if a log2ram mount is active on /var/log."""
    for line in mounts_text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "/var/log" and "log2ram" in parts[0].lower():
            return True
    return False


def read_log2ram_info() -> dict[str, Any]:
    """Check whether log2ram is mounted on /var/log and report its RAM buffer usage."""
    mounts_path = Path("/proc/mounts")
    active = False
    if mounts_path.is_file():
        with suppress(OSError):
            active = parse_log2ram_mounts(mounts_path.read_text(encoding="utf-8", errors="ignore"))
    if not active:
        return {"active": False, "used_mb": 0, "total_mb": 0, "pct": None}
    try:
        usage = shutil.disk_usage("/var/log")
        total_mb = round(usage.total / (1024**2))
        used_mb = round(usage.used / (1024**2))
        pct = round((usage.used * 100.0) / usage.total, 1) if usage.total > 0 else 0.0
        return {"active": True, "used_mb": used_mb, "total_mb": total_mb, "pct": pct}
    except OSError:
        return {"active": True, "used_mb": 0, "total_mb": 0, "pct": None}


def get_system_health(disk_path: str = "/") -> dict[str, Any]:
    """Collect a full, fast snapshot of Raspberry Pi system health."""
    cpu_temp_c = read_cpu_temp_c()
    memory = read_memory_info()
    disk = read_disk_info(disk_path)
    throttled = read_throttled_info()
    log2ram = read_log2ram_info()
    uptime_sec = read_uptime_seconds()

    load_1m: Optional[float] = None
    with suppress(OSError):
        load_1m = round(os.getloadavg()[0], 2)

    status = "ok"
    if (
        (cpu_temp_c is not None and cpu_temp_c >= 80.0)
        or (disk["pct"] is not None and disk["pct"] >= 90.0)
        or throttled["status"] == "crit"
    ):
        status = "crit"
    elif (
        (cpu_temp_c is not None and cpu_temp_c >= 70.0)
        or (disk["pct"] is not None and disk["pct"] >= 80.0)
        or (memory["pct"] is not None and memory["pct"] >= 85.0)
        or throttled["status"] == "warn"
    ):
        status = "warn"

    return {
        "status": status,
        "cpu_temp_c": cpu_temp_c,
        "load_1m": load_1m,
        "uptime_seconds": uptime_sec,
        "uptime_human": format_uptime(uptime_sec),
        "memory": memory,
        "disk": disk,
        "throttled": throttled,
        "log2ram": log2ram,
    }
