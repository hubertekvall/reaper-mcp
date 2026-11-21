"""Render and listen: bounce audio from REAPER and measure it."""

from __future__ import annotations

import asyncio
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import analysis
from .bridge import Bridge, BridgeError
from .tools_editing import EDIT, READ, Time, Track, _clean

RENDER_TIMEOUT = 900.0  # seconds; long projects render in real time with some plug-ins


def register(mcp: MCPServer, bridge: Bridge) -> None:
    @mcp.tool(annotations=EDIT)
    async def render(
        start: Time | None = None,
        end: Time | None = None,
        tracks: list[Track] | None = None,
        through_master: bool = False,
        name: str | None = None,
        directory: str | None = None,
        analyze: bool = True,
    ) -> dict[str, Any]:
        """Render audio to 24-bit WAV and (by default) measure it, so you can check the result of your edits.

        Without tracks: the full master mix (with master FX) from start to end (default: whole project).
        With tracks: one file per track, each track's own output (through_master=true sends it through
        the master FX chain instead). Files go to REAPER's resource folder under reaper_mcp/renders unless
        directory is given. The project's own render settings and track selection are left as they were.

        The analysis reports integrated/short-term/momentary loudness (LUFS) and loudness range, sample and
        true peak, RMS, crest factor, clipping, stereo correlation and width, and how the energy is spread
        across sub/bass/low-mid/mid/high-mid/presence/air bands."""
        try:
            result = await bridge.request(
                "render",
                _clean(start=start, end=end, tracks=tracks, through_master=through_master or None, name=name, directory=directory),
                timeout=RENDER_TIMEOUT,
            )
        except BridgeError as e:
            raise ToolError(str(e)) from e
        if analyze:
            reports = []
            for f in result["files"]:
                try:
                    reports.append(await asyncio.to_thread(analysis.analyze, f))
                except (OSError, analysis.AudioFileError) as e:
                    reports.append({"file": f, "error": str(e)})
            result["analysis"] = reports
        return result

    @mcp.tool(annotations=READ)
    async def analyze_audio_file(path: str) -> dict[str, Any]:
        """Measure any WAV file on this computer (loudness, peaks, dynamics, stereo image, tonal balance),
        e.g. a reference track to compare a mix against."""
        try:
            return await asyncio.to_thread(analysis.analyze, path)
        except (OSError, analysis.AudioFileError) as e:
            raise ToolError(str(e)) from e
