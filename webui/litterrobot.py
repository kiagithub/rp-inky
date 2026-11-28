"""Whisker Litter-Robot 4 dashboard bridge & alert monitor built on pylitterbot."""

import asyncio
import importlib.util
import logging
import threading
import time
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

LOG = logging.getLogger("webui.litterrobot")


def is_pylitterbot_installed() -> bool:
    """Return True if the optional `pylitterbot` package is importable."""
    return importlib.util.find_spec("pylitterbot") is not None


def _run_async(coro: Any) -> Any:
    """Execute an async coroutine safely from a synchronous HTTP/monitor worker thread."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        with suppress(Exception):
            loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()


def _safe_float(val: Any, digits: int = 1) -> Optional[float]:
    if val is None or val == "":
        return None
    try:
        return round(float(val), digits)
    except (TypeError, ValueError):
        return None


def _safe_int(val: Any) -> Optional[int]:
    if val is None or val == "":
        return None
    try:
        return round(float(val))
    except (TypeError, ValueError):
        return None


def _format_ts(ts: Any) -> str:
    if isinstance(ts, datetime):
        return ts.astimezone().strftime("%b %d, %I:%M %p")
    if ts:
        return str(ts)[:24]
    return ""


def serialize_robot(
    robot: Any,
    history: Optional[list[Any]] = None,
    insight: Any = None,
) -> dict[str, Any]:
    """Convert a pylitterbot Robot / LitterRobot4 object into clean JSON for the Web Hub."""
    status_obj = getattr(robot, "status", None)
    status_text = getattr(status_obj, "text", None) or (str(status_obj) if status_obj is not None else "Unknown")
    status_code = str(getattr(robot, "status_code", "") or getattr(status_obj, "value", "") or "")

    waste_pct = _safe_int(getattr(robot, "waste_drawer_level", None))
    litter_pct = _safe_int(getattr(robot, "litter_level", None))
    litter_state = getattr(robot, "litter_level_state", None)
    litter_state_str = getattr(litter_state, "name", None) or (str(litter_state) if litter_state is not None else "")

    pet_weight = _safe_float(getattr(robot, "pet_weight", None) or getattr(robot, "last_weight", None), digits=2)
    wait_time = _safe_int(getattr(robot, "clean_cycle_wait_time_minutes", None))

    hist_rows: list[dict[str, str]] = []
    for item in (history or [])[:12]:
        action_obj = getattr(item, "action", None)
        action_txt = getattr(action_obj, "text", None) or (str(action_obj) if action_obj is not None else str(item))
        hist_rows.append(
            {
                "timestamp": _format_ts(getattr(item, "timestamp", None)),
                "action": action_txt,
            }
        )

    insight_data: dict[str, Any] = {"total_cycles": None, "average_cycles": None, "days": []}
    if insight is not None:
        insight_data["total_cycles"] = _safe_int(getattr(insight, "total_cycles", None))
        insight_data["average_cycles"] = _safe_float(getattr(insight, "average_cycles", None), digits=1)
        cycle_hist = getattr(insight, "cycle_history", None) or []
        days_list: list[dict[str, Any]] = []
        for entry in cycle_hist[-7:]:
            if isinstance(entry, (tuple, list)) and len(entry) >= 2:
                day_obj, count = entry[0], entry[1]
                day_label = day_obj.strftime("%a") if hasattr(day_obj, "strftime") else str(day_obj)[-5:]
                days_list.append({"label": day_label, "cycles": int(count or 0)})
        insight_data["days"] = days_list

    return {
        "id": str(getattr(robot, "id", "") or getattr(robot, "serial", "")),
        "name": str(getattr(robot, "name", "Litter-Robot")),
        "serial": str(getattr(robot, "serial", "")),
        "model": str(getattr(robot, "model", None) or robot.__class__.__name__),
        "firmware": str(getattr(robot, "firmware", "") or ""),
        "is_online": bool(getattr(robot, "is_online", True)),
        "status_text": str(status_text),
        "status_code": status_code,
        "waste_drawer_level": waste_pct,
        "is_waste_drawer_full": bool(getattr(robot, "is_waste_drawer_full", False)),
        "litter_level": litter_pct,
        "litter_level_state": litter_state_str,
        "pet_weight": pet_weight,
        "night_light_enabled": bool(getattr(robot, "night_light_mode_enabled", False)),
        "panel_lock_enabled": bool(getattr(robot, "panel_lock_enabled", False)),
        "sleep_mode_enabled": bool(getattr(robot, "sleep_mode_enabled", False)),
        "wait_time_minutes": wait_time,
        "cycle_count": _safe_int(getattr(robot, "cycle_count", None)),
        "history": hist_rows,
        "insight": insight_data,
    }


def serialize_pet(pet: Any) -> dict[str, Any]:
    """Serialize a Whisker pet profile if available on the account."""
    return {
        "id": str(getattr(pet, "id", "")),
        "name": str(getattr(pet, "name", "Pet")),
        "weight": _safe_float(getattr(pet, "weight", None), digits=2),
        "last_weight": _safe_float(getattr(pet, "last_weight_reading", None) or getattr(pet, "weight", None), digits=2),
    }


class LitterRobotBridge:
    """Thread-safe controller for Whisker Litter-Robot 4 status, controls & alerts."""

    def __init__(
        self,
        webui_config_getter: Callable[[], dict[str, Any]],
        local_config_updater: Callable[[dict[str, Any]], Path],
    ):
        self._get_cfg = webui_config_getter
        self._update_local = local_config_updater
        self._lock = threading.Lock()
        self._cache: Optional[dict[str, Any]] = None
        self._cache_mono: float = 0.0
        self._alert_state: dict[str, dict[str, bool]] = {}

    def _lr_cfg(self) -> dict[str, Any]:
        cfg = self._get_cfg()
        raw = cfg.get("litterrobot")
        return raw if isinstance(raw, dict) else {}

    def _base_payload(self) -> dict[str, Any]:
        lr = self._lr_cfg()
        username = str(lr.get("username") or "").strip()
        has_pw = bool(str(lr.get("password") or "").strip())
        return {
            "installed": is_pylitterbot_installed(),
            "configured": bool(username and has_pw),
            "username": username,
            "has_password": has_pw,
            "alerts_enabled": bool(lr.get("alerts_enabled", True)),
            "alert_drawer_pct": int(lr.get("alert_drawer_pct", 85) or 85),
            "updated_at": "",
            "error": "",
            "robots": [],
            "pets": [],
        }

    async def _fetch_cloud_snapshot(self, username: str, password: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        from pylitterbot import Account  # type: ignore[import-not-found]

        account = Account()
        try:
            await account.connect(username=username, password=password, load_robots=True)
            robots_out: list[dict[str, Any]] = []
            for robot in getattr(account, "robots", []) or []:
                history: list[Any] = []
                insight: Any = None
                with suppress(Exception):
                    if hasattr(robot, "get_activity_history"):
                        history = await robot.get_activity_history(limit=12)
                with suppress(Exception):
                    if hasattr(robot, "get_insight"):
                        insight = await robot.get_insight(days=7)
                robots_out.append(serialize_robot(robot, history=history, insight=insight))

            pets_out = [serialize_pet(pet) for pet in (getattr(account, "pets", []) or [])]
            return robots_out, pets_out
        finally:
            with suppress(Exception):
                await account.disconnect()

    async def _execute_robot_action(
        self,
        username: str,
        password: str,
        robot_id: str,
        action: str,
        value: Any,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        from pylitterbot import Account  # type: ignore[import-not-found]

        account = Account()
        try:
            await account.connect(username=username, password=password, load_robots=True)
            robots = list(getattr(account, "robots", []) or [])
            target = next(
                (r for r in robots if str(getattr(r, "id", "")) == robot_id or str(getattr(r, "serial", "")) == robot_id),
                None,
            )
            if target is None:
                raise ValueError(f"Robot '{robot_id}' not found on Whisker account.")

            if action == "clean":
                await target.start_cleaning()
            elif action == "night_light":
                await target.set_night_light(bool(value))
            elif action == "panel_lock":
                await target.set_panel_lockout(bool(value))
            elif action == "sleep_mode":
                await target.set_sleep_mode(bool(value))
            elif action == "wait_time":
                mins = int(value)
                if mins not in (3, 7, 15, 25, 30):
                    raise ValueError("Wait time must be 3, 7, 15, 25, or 30 minutes.")
                await target.set_wait_time(mins)
            elif action == "power":
                await target.set_power_status(bool(value))
            else:
                raise ValueError(f"Unsupported Litter-Robot action: {action!r}")

            with suppress(Exception):
                await target.refresh()

            robots_out: list[dict[str, Any]] = []
            for robot in robots:
                history: list[Any] = []
                insight: Any = None
                with suppress(Exception):
                    if hasattr(robot, "get_activity_history"):
                        history = await robot.get_activity_history(limit=12)
                with suppress(Exception):
                    if hasattr(robot, "get_insight"):
                        insight = await robot.get_insight(days=7)
                robots_out.append(serialize_robot(robot, history=history, insight=insight))
            pets_out = [serialize_pet(p) for p in (getattr(account, "pets", []) or [])]
            return robots_out, pets_out
        finally:
            with suppress(Exception):
                await account.disconnect()

    def get_status(self, force: bool = False) -> dict[str, Any]:
        """Return Litter-Robot status (cached 45s unless force=True)."""
        base = self._base_payload()
        if not base["installed"] or not base["configured"]:
            return base

        with self._lock:
            if not force and self._cache is not None and (time.monotonic() - self._cache_mono) < 45.0:
                return dict(self._cache)

        lr = self._lr_cfg()
        username = str(lr.get("username") or "").strip()
        password = str(lr.get("password") or "").strip()
        try:
            robots, pets = _run_async(self._fetch_cloud_snapshot(username, password))
            base["robots"] = robots
            base["pets"] = pets
            base["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            with self._lock:
                self._cache = dict(base)
                self._cache_mono = time.monotonic()
            return base
        except Exception as exc:
            LOG.warning("Litter-Robot cloud fetch failed: %s", exc)
            base["error"] = str(exc)
            return base

    def save_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Save Whisker credentials and alert preferences to webui/config.local.json."""
        existing = self._lr_cfg()
        patch: dict[str, Any] = {}
        if "username" in payload:
            patch["username"] = str(payload.get("username") or "").strip()
        if payload.get("clear_credentials"):
            patch["username"] = ""
            patch["password"] = ""
        elif "password" in payload and str(payload.get("password") or "").strip():
            patch["password"] = str(payload["password"]).strip()
        elif "password" not in existing:
            patch["password"] = ""

        if "alerts_enabled" in payload:
            patch["alerts_enabled"] = bool(payload["alerts_enabled"])
        if "alert_drawer_pct" in payload:
            pct = max(50, min(100, int(payload["alert_drawer_pct"] or 85)))
            patch["alert_drawer_pct"] = pct

        self._update_local({"litterrobot": patch})
        with self._lock:
            self._cache = None
            self._cache_mono = 0.0
        return self.get_status(force=True)

    def send_action(self, robot_id: str, action: str, value: Any = None) -> dict[str, Any]:
        """Trigger a control command on a specific Litter-Robot and refresh status."""
        base = self._base_payload()
        if not base["installed"]:
            raise RuntimeError("pylitterbot is not installed. Run: ~/.virtualenvs/pimoroni/bin/pip install pylitterbot")
        if not base["configured"]:
            raise ValueError("Configure your Whisker account email and password first.")
        if not robot_id:
            raise ValueError("robot_id is required.")

        lr = self._lr_cfg()
        robots, pets = _run_async(
            self._execute_robot_action(
                str(lr["username"]).strip(),
                str(lr["password"]).strip(),
                robot_id.strip(),
                action.strip().lower(),
                value,
            )
        )
        base["robots"] = robots
        base["pets"] = pets
        base["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock:
            self._cache = dict(base)
            self._cache_mono = time.monotonic()
        return base

    def check_alerts(self, on_alert: Callable[[str, str, str, str], None]) -> None:
        """Background alert check for full waste drawer or offline Litter-Robot."""
        lr = self._lr_cfg()
        if not lr.get("alerts_enabled", True):
            return
        if not is_pylitterbot_installed() or not (lr.get("username") and lr.get("password")):
            return

        snap = self.get_status(force=True)
        if snap.get("error"):
            return
        threshold = int(lr.get("alert_drawer_pct", 85) or 85)

        for r in snap.get("robots") or []:
            rid = r["id"]
            name = r["name"]
            state = self._alert_state.setdefault(rid, {"drawer": False, "offline": False})

            # Waste drawer alert with 10% hysteresis
            lvl = r.get("waste_drawer_level")
            is_full = bool(r.get("is_waste_drawer_full")) or (lvl is not None and lvl >= threshold)
            if is_full and not state["drawer"]:
                state["drawer"] = True
                on_alert(
                    name,
                    "down",
                    f"[ALERT] {name} Waste Drawer {lvl if lvl is not None else 'Full'}%",
                    f"{name} waste drawer is at {lvl}% (threshold {threshold}%). Time to empty!",
                )
            elif not is_full and (lvl is None or lvl <= threshold - 10) and state["drawer"]:
                state["drawer"] = False
                on_alert(
                    name,
                    "recovered",
                    f"[RECOVERED] {name} Waste Drawer Emptied",
                    f"{name} waste drawer is now at {lvl if lvl is not None else 0}%.",
                )

            # Offline alert
            online = bool(r.get("is_online", True))
            if not online and not state["offline"]:
                state["offline"] = True
                on_alert(name, "down", f"[DOWN] {name} Offline", f"{name} lost Wi-Fi connection to Whisker cloud.")
            elif online and state["offline"]:
                state["offline"] = False
                on_alert(name, "recovered", f"[RECOVERED] {name} Online", f"{name} is back online ({r.get('status_text')}).")
