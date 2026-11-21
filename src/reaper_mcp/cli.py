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
    sub.add_parser("doctor", help="check that the bridge is running and measure its speed")
    args = parser.parse_args(argv)

    if args.command == "install-bridge":
        target = install_bridge(args.resource_path or reaper_resource_path())
        print(f"Installed {target}")
        print(
            "In REAPER: Actions > Show action list > New action > Load ReaScript, pick that file,\n"
            "then run it. Run `reaper-mcp doctor` to check the connection."
        )
        return 0
    if args.command == "doctor":
        return doctor(Bridge(bridge_dir()))

    from .server import create_server

    create_server().run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
