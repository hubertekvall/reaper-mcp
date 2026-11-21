"""Command line entry point: `reaper-mcp [serve|install-bridge|doctor]`."""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import time
from pathlib import Path

from .bridge import Bridge, BridgeError
from .paths import BRIDGE_SCRIPT_NAME, bridge_dir, bundled_bridge_script, reaper_resource_path


STARTUP_BEGIN = "-- >>> reaper-mcp bridge (added by `reaper-mcp install-bridge --autostart`) >>>"
STARTUP_END = "-- <<< reaper-mcp bridge <<<"
STARTUP_BLOCK = f"""{STARTUP_BEGIN}
do
  local bridge = reaper.GetResourcePath() .. "/Scripts/reaper_mcp/reaper_mcp_bridge.lua"
  local f = io.open(bridge, "r")
  if f then f:close(); dofile(bridge) end
end
{STARTUP_END}
"""


def set_autostart(resource_path: Path, enabled: bool) -> Path:
    """Add or remove the block in Scripts/__startup.lua that starts the bridge with REAPER."""
    startup = resource_path / "Scripts" / "__startup.lua"
    text = startup.read_text(encoding="utf-8") if startup.exists() else ""
    if STARTUP_BEGIN in text:
        before, rest = text.split(STARTUP_BEGIN, 1)
        after = rest.split(STARTUP_END, 1)[1] if STARTUP_END in rest else ""
        text = before.rstrip("\n") + ("\n" if before.strip() else "") + after.lstrip("\n")
    if enabled:
        text = (text.rstrip("\n") + "\n\n" if text.strip() else "") + STARTUP_BLOCK
    startup.parent.mkdir(parents=True, exist_ok=True)
    startup.write_text(text, encoding="utf-8")
    return startup


def install_bridge(resource_path: Path) -> Path:
    if not resource_path.is_dir():
        raise SystemExit(
            f"REAPER resource folder not found: {resource_path}\n"
            "Find it in REAPER under Options > Show REAPER resource path, then pass --resource-path."
        )
    target_dir = resource_path / "Scripts" / "reaper_mcp"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / BRIDGE_SCRIPT_NAME
    shutil.copyfile(bundled_bridge_script(), target)
    return target


def doctor(bridge: Bridge) -> int:
    print(f"Bridge folder: {bridge.base}")
    age = bridge.heartbeat_age()
    if age is None:
        print("Heartbeat: missing. The bridge script is not running in REAPER.")
        return 1
    print(f"Heartbeat: {age:.1f} s ago")
    try:
        start = time.perf_counter()
        info = asyncio.run(bridge.request("ping"))
        elapsed = (time.perf_counter() - start) * 1000
    except BridgeError as e:
        print(f"Ping failed: {e}")
        return 1
    print(f"Ping: {elapsed:.0f} ms round trip")
    for key, value in info.items():
        print(f"  {key}: {value}")
    timings = []
    for _ in range(20):
        start = time.perf_counter()
        asyncio.run(bridge.request("ping"))
        timings.append((time.perf_counter() - start) * 1000)
    timings.sort()
    print(f"20 pings: median {timings[10]:.0f} ms, worst {timings[-1]:.0f} ms")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="reaper-mcp", description="MCP server for the REAPER DAW")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the MCP server over stdio (default)")
    inst = sub.add_parser("install-bridge", help="copy the Lua bridge script into REAPER's Scripts folder")
    inst.add_argument("--resource-path", type=Path, default=None, help="REAPER resource folder")
    auto = inst.add_mutually_exclusive_group()
    auto.add_argument("--autostart", action="store_true", help="start the bridge automatically with REAPER")
    auto.add_argument("--no-autostart", action="store_true", help="stop starting the bridge with REAPER")
    sub.add_parser("doctor", help="check that the bridge is running and measure its speed")
    args = parser.parse_args(argv)

    if args.command == "install-bridge":
        resource = args.resource_path or reaper_resource_path()
        target = install_bridge(resource)
        print(f"Installed {target}")
        if args.autostart or args.no_autostart:
            startup = set_autostart(resource, enabled=args.autostart)
            state = "will now start" if args.autostart else "no longer starts"
            print(f"The bridge {state} automatically with REAPER ({startup}).")
        if args.autostart:
            print("Restart REAPER (or run the script once from the action list) to start it now.")
        else:
            print(
                "In REAPER: Actions > Show action list > New action > Load ReaScript, pick that file,\n"
                "then run it. Tip: add --autostart to start it with REAPER every time."
            )
        print("If an older bridge is already running, stop it and run the script again to load this version.")
        print("Run `reaper-mcp doctor` to check the connection.")
        return 0
    if args.command == "doctor":
        return doctor(Bridge(bridge_dir()))

    from .server import create_server

    create_server().run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
