"""MCP server exposing REAPER through the Lua bridge."""

from __future__ import annotations

import json
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError, ToolError
from mcp.types import ToolAnnotations

from . import __version__
from .bridge import Bridge, BridgeError

INSTRUCTIONS = """\
Controls the REAPER DAW that is running on the user's computer.

Start with get_project_state to see tracks, items, FX, markers and regions.
Track indices are 0-based; the master track is reported separately.
Times are in seconds unless a tool says otherwise.

call_reaper_api runs any ReaScript function (https://www.reaper.fm/sdk/reascript/reascripthelp.html,
Lua signatures). REAPER objects are passed and returned as references, for example
{"$type": "MediaTrack", "index": 0} or {"$type": "MediaTrack", "guid": "{...}"}.
Pass undo_label whenever the call changes the project, so the user can undo it.
"""


def create_server(bridge: Bridge | None = None) -> MCPServer:
    bridge = bridge or Bridge()
    mcp = MCPServer("reaper", instructions=INSTRUCTIONS, version=__version__)

    async def request(cmd: str, params: dict[str, Any] | None = None) -> Any:
        # Bridge and REAPER errors are expected failures: pass their message to the model.
        try:
            return await bridge.request(cmd, params)
        except BridgeError as e:
            raise ToolError(str(e)) from e

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def reaper_status() -> dict[str, Any]:
        """Check that REAPER and the bridge are reachable; returns versions and the open project."""
        return await request("ping")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def get_project_state(include_items: bool = True) -> dict[str, Any]:
        """Read the open project: tempo, time signature, transport, time selection, every track
        (name, folder depth, volume, pan, mute/solo, record arm/input/monitoring, FX chain,
        send counts) with its media items, plus markers and regions.

        Set include_items to false for a lighter result on large projects.
        """
        return await request("project_state", {"items": include_items})

    @mcp.tool(annotations=ToolAnnotations(destructiveHint=True, openWorldHint=False))
    async def call_reaper_api(
        function: str,
        args: list[Any] | None = None,
        undo_label: str | None = None,
    ) -> list[Any]:
        """Call any ReaScript API function and return all of its return values as a list.

        Use it for anything the other tools do not cover. Arguments follow the Lua
        signature from the ReaScript docs, for example
        function="GetMediaTrackInfo_Value", args=[{"$type": "MediaTrack", "index": 0}, "D_VOL"].

        Object references: {"$type": "MediaTrack"|"MediaItem"|"MediaItem_Take", "guid": "..."},
        {"$type": "MediaTrack", "index": n}, {"$type": "MediaTrack", "master": true},
        {"$type": "MediaItem", "index": n} or a {"$ptr": ...} reference returned earlier.

        Set undo_label (for example "Set vocal volume") when the call changes the project; the
        change then becomes one undo step. Leave it empty for read-only calls.
        """
        params: dict[str, Any] = {"fn": function, "args": args or []}
        if undo_label:
            params["undo"] = undo_label
        return await request("call", params)

    @mcp.resource("reaper://project", mime_type="application/json")
    async def project_resource() -> str:
        """The open REAPER project, as returned by get_project_state."""
        try:
            return json.dumps(await bridge.request("project_state", {"items": True}))
        except BridgeError as e:
            raise ResourceError(str(e)) from e

    return mcp
