"""Editing tools: tracks, items, MIDI, FX, transport, tempo, markers and regions.

Shared argument conventions (also explained in the server instructions):

* track: 0-based index, exact track name, track GUID, or "master".
* item: the item's GUID (from get_project_state), or {"track": <track>, "index": <0-based item on that track>}.
* time: seconds, {"beats": <quarter notes from project start>}, or {"bar": <n>, "beat": <b>} (both 1-based).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

Track = int | str
Item = str | dict[str, Any]
Time = float | dict[str, float]
Request = Callable[[str, dict[str, Any]], Awaitable[Any]]

READ = ToolAnnotations(readOnlyHint=True)
EDIT = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
DELETE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)


def _clean(**kwargs: Any) -> dict[str, Any]:
    return {k: v for k, v in kwargs.items() if v is not None}


def register(mcp: MCPServer, request: Request) -> None:
    # ---------------------------------------------------------------- tracks

    @mcp.tool(annotations=EDIT)
    async def create_track(
        name: str | None = None,
        index: int | None = None,
        volume_db: float | None = None,
        pan: float | None = None,
        color: str | None = None,
    ) -> dict[str, Any]:
        """Insert a new track (at the end unless index is given). color is '#rrggbb'. pan is -1 (left) to 1 (right)."""
        return await request("create_track", _clean(name=name, index=index, volume_db=volume_db, pan=pan, color=color))

    @mcp.tool(annotations=EDIT)
    async def update_track(
        track: Track,
        name: str | None = None,
        volume_db: float | None = None,
        pan: float | None = None,
        mute: bool | None = None,
        solo: bool | None = None,
        phase_invert: bool | None = None,
        color: str | None = None,
        folder_depth: int | None = None,
        main_send: bool | None = None,
        move_to: int | None = None,
    ) -> dict[str, Any]:
        """Change a track: name, volume (dB), pan (-1..1), mute, solo, phase, color ('#rrggbb', '' to clear),
        folder_depth (1 = starts a folder, 0 = normal, -1 = closes one folder level), main_send (send to
        parent/master) and move_to (new 0-based position). Only the given fields change."""
        return await request(
            "update_track",
            _clean(
                track=track, name=name, volume_db=volume_db, pan=pan, mute=mute, solo=solo,
                phase_invert=phase_invert, color=color, folder_depth=folder_depth,
                main_send=main_send, move_to=move_to,
            ),
        )

    @mcp.tool(annotations=DELETE)
    async def delete_tracks(tracks: list[Track]) -> int:
        """Delete tracks and everything on them."""
        return await request("delete_tracks", {"tracks": tracks})

    @mcp.tool(annotations=EDIT)
    async def create_folder(tracks: list[Track], name: str, color: str | None = None) -> dict[str, Any]:
        """Group adjacent tracks under a new folder track (for example a Drums folder over the drum tracks).
        The folder track is inserted above the first of them; its children are mixed through it."""
        return await request("create_folder", _clean(tracks=tracks, name=name, color=color))

    @mcp.tool(annotations=EDIT)
    async def setup_recording(
        track: Track,
        input: dict[str, Any] | None = None,
        monitor: Literal["off", "on", "auto"] | None = None,
        mode: Literal["input", "output", "none", "midi_overdub", "midi_replace"] | None = None,
        arm: bool | None = None,
    ) -> dict[str, Any]:
        """Prepare a track for recording.

        input: {"type": "audio", "channel": 1, "stereo": false} (channel is the first hardware input, 1-based),
        {"type": "midi", "device": "all" or device index, "channel": 0 for all or 1-16}, or {"type": "none"}.
        monitor: 'on' to hear the input, 'auto' to hear it only while armed or recording (tape style).
        """
        return await request("setup_recording", _clean(track=track, input=input, monitor=monitor, mode=mode, arm=arm))

    @mcp.tool(annotations=EDIT)
    async def create_instrument_track(
        instrument: str,
        name: str | None = None,
        index: int | None = None,
        input: dict[str, Any] | None = None,
        arm: bool | None = None,
        color: str | None = None,
    ) -> dict[str, Any]:
        """Create a track with a virtual instrument, MIDI input from all devices and monitoring on, ready to
        play. instrument is the plug-in name as REAPER lists it, e.g. 'ReaSynth' or 'VSTi: Serum (Xfer Records)'.
        Use list_installed_fx(filter=...) to find the exact name."""
        return await request(
            "create_instrument_track",
            _clean(instrument=instrument, name=name, index=index, input=input, arm=arm, color=color),
        )

    # ----------------------------------------------------------------- items

    @mcp.tool(annotations=EDIT)
    async def edit_item(
        item: Item,
        position: Time | None = None,
        start: Time | None = None,
        end: Time | None = None,
        track: Track | None = None,
        mute: bool | None = None,
        volume_db: float | None = None,
        fade_in: float | None = None,
        fade_out: float | None = None,
        name: str | None = None,
        pitch: float | None = None,
        playrate: float | None = None,
        lock: bool | None = None,
    ) -> dict[str, Any]:
        """Move, trim or change a media item (audio or MIDI).

        position moves the whole item. start/end trim its edges while the audio stays in place.
        track moves it to another track. fade_in/fade_out are lengths in seconds; pitch is in semitones."""
        return await request(
            "edit_item",
            _clean(
                item=item, position=position, start=start, end=end, track=track, mute=mute,
                volume_db=volume_db, fade_in=fade_in, fade_out=fade_out, name=name, pitch=pitch,
                playrate=playrate, lock=lock,
            ),
        )

    @mcp.tool(annotations=EDIT)
    async def split_item(item: Item, at: Time | list[Time]) -> list[dict[str, Any]]:
        """Split an item at one or more times. Returns the resulting pieces from left to right."""
        return await request("split_item", {"item": item, "at": at})

    @mcp.tool(annotations=DELETE)
    async def delete_items(items: list[Item]) -> int:
        """Remove media items from the project."""
        return await request("delete_items", {"items": items})

    @mcp.tool(annotations=EDIT)
    async def reverse_items(items: list[Item]) -> int:
        """Reverse items. Audio takes play backwards; MIDI notes are mirrored within the item."""
        return await request("reverse_items", {"items": items})

    @mcp.tool(annotations=EDIT)
    async def glue_items(items: list[Item]) -> list[dict[str, Any]]:
        """Glue items into one new item per track (renders audio, merges MIDI)."""
        return await request("glue_items", {"items": items})

    @mcp.tool(annotations=EDIT)
    async def duplicate_item(
        item: Item, count: int = 1, position: Time | None = None, track: Track | None = None
    ) -> list[dict[str, Any]]:
        """Copy an item. Copies go right after the original (or from position on) back to back,
        on the same track unless track is given. Good for repeating a loop or a MIDI pattern."""
        return await request("duplicate_item", _clean(item=item, count=count, position=position, track=track))

    @mcp.tool(annotations=EDIT)
    async def insert_media_file(track: Track, file: str, position: Time | None = None) -> dict[str, Any]:
        """Import an audio or MIDI file from disk onto a track at position (default 0)."""
        return await request("insert_media", _clean(track=track, file=file, position=position))

    # ------------------------------------------------------------------ MIDI

    @mcp.tool(annotations=EDIT)
    async def create_midi_item(track: Track, start: Time, end: Time, name: str | None = None) -> dict[str, Any]:
        """Create an empty MIDI item, for example from {"bar": 1} to {"bar": 5}. Then add notes with add_midi_notes."""
        return await request("create_midi_item", _clean(track=track, start=start, end=end, name=name))

    @mcp.tool(annotations=READ)
    async def get_midi(item: Item, include_cc: bool = True) -> dict[str, Any]:
        """Read the notes (pitch, velocity, channel, start and length in beats and seconds) and CC,
        pitch bend and program events of a MIDI item. Beats are quarter notes from project start."""
        return await request("get_midi", {"item": item, "cc": include_cc})

    @mcp.tool(annotations=EDIT)
    async def add_midi_notes(item: Item, notes: list[dict[str, Any]], relative: bool = True) -> dict[str, Any]:
        """Write notes into a MIDI item.

        Each note: {"pitch": 60, "start_beats": 0, "length_beats": 1, "velocity": 100, "channel": 1}.
        Beats are quarter notes. With relative=true (default) start_beats counts from the item start;
        with relative=false from project start. Alternatively give "start"/"end" as times.
        The item is lengthened if notes run past its end."""
        return await request("add_midi_notes", {"item": item, "notes": notes, "relative": relative})

    @mcp.tool(annotations=DELETE)
    async def delete_midi_notes(item: Item, indices: list[int] | None = None, all: bool = False) -> int:
        """Delete notes by index (from get_midi), or every note with all=true."""
        return await request("delete_midi_notes", _clean(item=item, indices=indices, all=all or None))

    @mcp.tool(annotations=EDIT)
    async def add_midi_cc(item: Item, events: list[dict[str, Any]], relative: bool = True) -> int:
        """Write controller events into a MIDI item.

        Each event: {"type": "cc", "cc": 1, "value": 64, "beats": 0, "channel": 1},
        {"type": "pitchbend", "value": -8192..8191, "beats": 2} or {"type": "program", "value": 5, "beats": 0}.
        Beats are quarter notes from the item start (relative=true) or project start."""
        return await request("add_midi_cc", {"item": item, "events": events, "relative": relative})

    # -------------------------------------------------------------------- FX

    @mcp.tool(annotations=READ)
    async def list_installed_fx(filter: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        """Search the plug-ins installed in REAPER (VST, VST3, CLAP, JS, AU). filter matches part of the name."""
        return await request("list_installed_fx", _clean(filter=filter, limit=limit))

    @mcp.tool(annotations=EDIT)
    async def add_fx(track: Track, name: str, position: int | None = None, preset: str | None = None) -> dict[str, Any]:
        """Add a plug-in to a track's FX chain (at the end unless position is given), optionally loading a preset.
        name can be partial, e.g. 'ReaEQ', 'ReaComp', 'Pro-Q 3'."""
        return await request("add_fx", _clean(track=track, name=name, position=position, preset=preset))

    @mcp.tool(annotations=DELETE)
    async def remove_fx(track: Track, fx: int | str) -> bool:
        """Remove a plug-in from a track. fx is its index in the chain or part of its name."""
        return await request("remove_fx", {"track": track, "fx": fx})

    @mcp.tool(annotations=EDIT)
    async def update_fx(
        track: Track,
        fx: int | str,
        enabled: bool | None = None,
        offline: bool | None = None,
        preset: str | None = None,
        move_to: int | None = None,
    ) -> bool:
        """Bypass or enable a plug-in, take it offline, load a preset, or move it within the chain."""
        return await request(
            "update_fx", _clean(track=track, fx=fx, enabled=enabled, offline=offline, preset=preset, move_to=move_to)
        )

    @mcp.tool(annotations=READ)
    async def get_fx_params(track: Track, fx: int | str, filter: str | None = None) -> dict[str, Any]:
        """List a plug-in's parameters with their normalized value (0-1) and the value as the plug-in shows it
        (e.g. '-6.0 dB', '2.5 kHz'). filter matches part of the parameter name."""
        return await request("get_fx_params", _clean(track=track, fx=fx, filter=filter))

    @mcp.tool(annotations=EDIT)
    async def set_fx_params(track: Track, fx: int | str, params: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Set plug-in parameters. Each entry: {"param": <index or name>, "value": 0.0-1.0} or
        {"param": "Threshold", "display_value": -18} to aim for what the plug-in displays
        (dB, Hz, ms, %, ratio). Returns the resulting values."""
        return await request("set_fx_params", {"track": track, "fx": fx, "params": params})

    # ------------------------------------------------------ transport, tempo

    @mcp.tool(annotations=EDIT)
    async def transport(
        action: Literal["play", "stop", "pause", "record", "goto"],
        position: Time | None = None,
        repeat: bool | None = None,
    ) -> dict[str, Any]:
        """Control playback. position moves the edit cursor first (use action 'goto' to only move it).
        repeat turns loop playback of the time selection on or off."""
        return await request("transport", _clean(action=action, position=position, repeat=repeat))

    @mcp.tool(annotations=EDIT)
    async def set_time_selection(
        start: Time | None = None, end: Time | None = None, loop: bool = True
    ) -> list[float] | None:
        """Set the time selection (and loop points, unless loop=false). Call with no arguments to clear it."""
        return await request("set_time_selection", _clean(start=start, end=end, loop=loop))

    @mcp.tool(annotations=EDIT)
    async def set_tempo(bpm: float | None = None, numerator: int | None = None, denominator: int | None = None) -> dict[str, Any]:
        """Set the project tempo and/or time signature (from the start of the project)."""
        return await request("set_tempo", _clean(bpm=bpm, numerator=numerator, denominator=denominator))

    # ------------------------------------------------------ markers, regions

    @mcp.tool(annotations=EDIT)
    async def add_marker(
        position: Time | None = None,
        name: str | None = None,
        end: Time | None = None,
        color: str | None = None,
    ) -> dict[str, Any]:
        """Add a marker at position, or a region (song section such as Verse or Chorus) when end is given."""
        return await request("add_marker", _clean(position=position, name=name, end=end, color=color))

    @mcp.tool(annotations=EDIT)
    async def update_marker(
        id: int,
        region: bool = False,
        name: str | None = None,
        position: Time | None = None,
        end: Time | None = None,
        color: str | None = None,
    ) -> bool:
        """Rename, move or recolor a marker or region, by the id shown in get_project_state."""
        return await request(
            "update_marker", _clean(id=id, region=region, name=name, position=position, end=end, color=color)
        )

    @mcp.tool(annotations=DELETE)
    async def delete_marker(id: int, region: bool = False) -> bool:
        """Delete a marker (or a region with region=true) by its id."""
        return await request("delete_marker", {"id": id, "region": region})
