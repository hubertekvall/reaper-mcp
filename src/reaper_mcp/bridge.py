"""Client for the Lua bridge running inside REAPER.

Each request is a JSON file dropped into ``<bridge>/requests``; the bridge
answers with a file of the same id in ``<bridge>/responses``. Files are
written to a temporary name and renamed, so neither side reads half a file.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from .paths import bridge_dir


class BridgeError(Exception):
    """Base class for bridge failures."""


class BridgeNotRunning(BridgeError):
    pass


class BridgeTimeout(BridgeError):
    pass


class ReaperError(BridgeError):
    """REAPER (or the bridge script) reported an error for this request."""


NOT_RUNNING_HELP = (
    "The REAPER bridge is not running. Start REAPER, then run the action "
    "'Script: reaper_mcp_bridge.lua' (install it with `reaper-mcp install-bridge`)."
)


class Bridge:
    def __init__(
        self,
        base: Path | None = None,
        *,
        timeout: float = 10.0,
        poll_interval: float = 0.005,
        heartbeat_max_age: float = 5.0,
    ) -> None:
        self.base = Path(base) if base else bridge_dir()
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.heartbeat_max_age = heartbeat_max_age

    @property
    def requests_dir(self) -> Path:
        return self.base / "requests"

    @property
    def responses_dir(self) -> Path:
        return self.base / "responses"

    @property
    def heartbeat_file(self) -> Path:
        return self.base / "heartbeat.json"

    def heartbeat_age(self) -> float | None:
        """Seconds since the bridge last wrote its heartbeat, or None if absent."""
        try:
            return max(0.0, time.time() - self.heartbeat_file.stat().st_mtime)
        except FileNotFoundError:
            return None

    def is_running(self) -> bool:
        age = self.heartbeat_age()
        return age is not None and age <= self.heartbeat_max_age

    async def request(self, cmd: str, params: dict[str, Any] | None = None, *, timeout: float | None = None) -> Any:
        if not self.is_running():
            raise BridgeNotRunning(NOT_RUNNING_HELP)

        req_id = f"{time.time_ns():020d}-{uuid.uuid4().hex[:8]}"
        payload = json.dumps({"id": req_id, "cmd": cmd, "params": params or {}}, allow_nan=False)
        self.requests_dir.mkdir(parents=True, exist_ok=True)
        req_path = self.requests_dir / f"{req_id}.req.json"
        res_path = self.responses_dir / f"{req_id}.res.json"
        _write_atomic(req_path, payload)

        deadline = time.monotonic() + (timeout if timeout is not None else self.timeout)
        while True:
            raw = _try_read(res_path)
            if raw is not None:
                _unlink(res_path)
                break
            if time.monotonic() > deadline:
                picked_up = not req_path.exists()
                _unlink(req_path)
                if picked_up:
                    raise BridgeTimeout(f"REAPER took longer than the timeout to answer '{cmd}'.")
                raise BridgeNotRunning(NOT_RUNNING_HELP) if not self.is_running() else BridgeTimeout(
                    f"The bridge did not pick up '{cmd}' in time. REAPER may be busy or showing a dialog."
                )
            await asyncio.sleep(self.poll_interval)

        try:
            response = json.loads(raw)
        except json.JSONDecodeError as e:
            raise BridgeError(f"Bridge sent invalid JSON: {e}") from e
        if not response.get("ok"):
            raise ReaperError(response.get("error") or "unknown error")
        return response.get("result")


def _write_atomic(path: Path, data: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(data, encoding="utf-8")
    os.replace(tmp, path)


def _try_read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, PermissionError):
        # PermissionError: on Windows the bridge may still be renaming it.
        return None


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except (FileNotFoundError, PermissionError):
        pass
