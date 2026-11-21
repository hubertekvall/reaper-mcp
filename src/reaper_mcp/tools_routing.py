"""Routing and automation tools."""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.mcpserver import MCPServer

from .tools_editing import DELETE, EDIT, READ, Request, Time, Track, _clean

SendMode = Literal["post_fader", "pre_fx", "post_fx"]
Target = str | dict[str, Any]

TARGET_HELP = (
    'target is "volume" (values in dB), "pan" (-1 left .. 1 right), "width" (-1..1), "mute" (true/false), '
    '"trim" (dB), or an FX parameter {"fx": <index or name>, "param": <index or name>} with values 0..1, '
    'or {"display_value": x} to aim for what the plug-in shows (dB, Hz, %).'
)


def register(mcp: MCPServer, request: Request) -> None:
    def automation_tool(annotations):
        def deco(fn):
            fn.__doc__ = (fn.__doc__ or "").rstrip() + "\n\n" + TARGET_HELP
            return mcp.tool(annotations=annotations)(fn)

        return deco

    # --------------------------------------------------------------- routing

    @mcp.tool(annotations=EDIT)
    async def create_send(
        from_track: Track,
        to_track: Track,
        volume_db: float = 0.0,
        pan: float | None = None,
        mode: SendMode = "post_fader",
        source_channels: str | None = None,
        dest_channels: str | None = None,
        midi: bool | None = None,
    ) -> dict[str, Any]:
        """Send audio (and MIDI) from one track to another, e.g. into a bus or effect return.

        mode: post_fader (follows the fader, the usual choice for reverb/delay sends), pre_fx (raw input,
        e.g. for headphone mixes), post_fx (after the FX but before the fader).
        Channels: "1/2" (default stereo pair), "3/4", "1" (mono) or "none" for a MIDI-only send.
        Sending to "3/4" widens the destination track automatically. midi=false sends audio only."""
        return await request(
            "create_send",
            _clean(
                **{"from": from_track, "to": to_track}, volume_db=volume_db, pan=pan, mode=mode,
                source_channels=source_channels, dest_channels=dest_channels, midi=midi,
            ),
        )

    @mcp.tool(annotations=DELETE)
    async def update_send(
        from_track: Track,
        send: int | str,
        volume_db: float | None = None,
        pan: float | None = None,
        mute: bool | None = None,
        mode: SendMode | None = None,
        source_channels: str | None = None,
        dest_channels: str | None = None,
        midi: bool | None = None,
        remove: bool = False,
    ) -> dict[str, Any] | bool:
        """Change or remove a send. send is its index on from_track (see sends in get_project_state)
        or the destination track. remove=true deletes the send."""
        if remove:
            return await request("remove_send", {"from": from_track, "send": send})
        return await request(
            "update_send",
            _clean(
                **{"from": from_track}, send=send, volume_db=volume_db, pan=pan, mute=mute, mode=mode,
                source_channels=source_channels, dest_channels=dest_channels, midi=midi,
            ),
        )

    @mcp.tool(annotations=EDIT)
    async def create_fx_return(
        name: str,
        fx: list[str],
        sources: list[Track] | None = None,
        send_db: float = -12.0,
        mode: SendMode = "post_fader",
        full_wet: bool = True,
        color: str | None = None,
    ) -> dict[str, Any]:
        """Create an effect return track, e.g. "Reverb" with ["ReaVerbate"] or "Parallel Comp" with
        ["ReaComp"], and send each source track to it at send_db. Return-track plug-ins are set to
        100% wet unless full_wet=false. This is the standard way to share one reverb or delay across
        many tracks, or to blend in heavy compression in parallel."""
        return await request(
            "create_fx_return",
            _clean(name=name, fx=fx, sources=sources or [], send_db=send_db, mode=mode, full_wet=full_wet, color=color),
        )

    @mcp.tool(annotations=EDIT)
    async def create_sidechain(
        from_track: Track,
        to_track: Track,
        fx: int | str | None = None,
        send_db: float = 0.0,
        mode: SendMode = "post_fx",
    ) -> dict[str, Any]:
        """Sidechain: send from_track (e.g. the kick) into channels 3/4 of to_track (e.g. the bass), so a
        compressor or gate there can react to it. If fx names that plug-in, its detector/sidechain input
        parameter is switched to the auxiliary input when it has one (ReaComp does)."""
        return await request(
            "create_sidechain",
            _clean(**{"from": from_track, "to": to_track}, fx=fx, send_db=send_db, mode=mode),
        )

    # ------------------------------------------------------------ automation

    @automation_tool(EDIT)
    async def write_automation(
        track: Track,
        target: Target,
        points: list[dict[str, Any]],
        automation_item: bool = False,
        replace: bool = True,
    ) -> dict[str, Any]:
        """Write automation points. Each point: {"time": <time>, "value": <v>, "shape": "linear"|"square"|
        "slow"|"fast_start"|"fast_end"|"bezier", "tension": -1..1}. The shape sets the curve towards the next point.

        Existing points between the first and last new point are replaced unless replace=false.
        automation_item=true puts the points in a new automation item (movable, loopable block) instead.
        The envelope is created if the track does not have it yet.
        """
        return await request(
            "write_automation",
            {"track": track, "target": target, "points": points, "automation_item": automation_item, "replace": replace},
        )

    @automation_tool(EDIT)
    async def draw_automation(
        track: Track,
        target: Target,
        start: Time,
        end: Time,
        shape: Literal["ramp", "sine", "triangle", "square", "saw"] = "ramp",
        from_value: Any = None,
        to_value: Any = None,
        curve: Literal["linear", "slow", "fast_start", "fast_end", "bezier"] = "linear",
        min: Any = None,
        max: Any = None,
        period_beats: float = 1.0,
        automation_item: bool = False,
    ) -> dict[str, Any]:
        """Draw a shape: a ramp from from_value to to_value (fades, swells, filter sweeps), or a repeating
        sine/triangle/square/saw between min and max with one cycle every period_beats quarter notes
        (tremolo, auto-pan, rhythmic gating, wobble). Replaces existing points in the range."""
        return await request(
            "draw_automation",
            _clean(
                track=track, target=target, start=start, end=end, shape=shape, curve=curve,
                period_beats=period_beats, automation_item=automation_item or None,
                **{"from": from_value, "to": to_value, "min": min, "max": max},
            ),
        )

    @automation_tool(READ)
    async def get_automation(
        track: Track, target: Target, start: Time | None = None, end: Time | None = None, limit: int = 500
    ) -> dict[str, Any]:
        """Read the automation points of a track envelope or FX parameter, in the same units as write_automation."""
        return await request("get_automation", _clean(track=track, target=target, start=start, end=end, limit=limit))

    @automation_tool(DELETE)
    async def clear_automation(track: Track, target: Target, start: Time | None = None, end: Time | None = None) -> int:
        """Delete automation points between start and end (the whole envelope if both are omitted)."""
        return await request("clear_automation", _clean(track=track, target=target, start=start, end=end))
