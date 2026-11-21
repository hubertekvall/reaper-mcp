"""Editing tools, end to end: MCP client -> Python server -> files -> real Lua bridge -> fake REAPER."""

from __future__ import annotations

import pytest
from mcp.client import Client

from reaper_mcp.server import create_server

from .test_server import payload


class Caller:
    """Calls tools through a real MCP client session."""

    def __init__(self, running):
        self.server = create_server(running.bridge)
        self.fake = running.fake

    async def raw(self, tool, args):
        # A fresh in-process session per call keeps anyio scopes inside the test's own task.
        async with Client(self.server) as client:
            return await client.call_tool(tool, args)

    async def __call__(self, tool, **args):
        result = await self.raw(tool, args)
        if result.is_error:
            raise AssertionError(result.content[0].text)
        return payload(result)


@pytest.fixture
def call(running):
    return Caller(running)


async def error_text(call, tool, **args):
    result = await call.raw(tool, args)
    assert result.is_error, "expected an error"
    return result.content[0].text


# ------------------------------------------------------------------ tracks

async def test_create_and_update_track(call):
    out = await call("create_track", name="Vox", volume_db=-6, pan=-0.5, color="#ff8000")
    fake = call.fake
    vox = fake.tracks[out["index"]]
    assert out["index"] == 3 and vox.name == "Vox"
    assert vox.info["D_VOL"] == pytest.approx(0.501, abs=1e-3)
    assert vox.info["D_PAN"] == -0.5
    assert int(vox.info["I_CUSTOMCOLOR"]) == (0xFF | 0x80 << 8) | 0x1000000

    info = await call("update_track", track="Vox", mute=True, solo=True, move_to=0)
    assert fake.tracks[0] is vox and info["index"] == 0
    assert info["mute"] is True and info["solo"] is True
    assert fake.undo[-1] == "Edit track"


async def test_track_lookup_errors(call):
    assert "no track named 'Nope'" in await error_text(call, "update_track", track="Nope", mute=True)
    await call("create_track", name="Kick")
    assert "more than one track" in await error_text(call, "update_track", track="Kick", mute=True)


async def test_delete_tracks(call):
    assert await call("delete_tracks", tracks=["Keys", 0]) == 2
    assert [t.name for t in call.fake.tracks] == ["Kick"]


async def test_create_folder(call):
    await call("create_track", name="Bass DI")
    await call("create_track", name="Bass Amp")
    out = await call("create_folder", tracks=["Bass DI", "Bass Amp"], name="Bass")
    names = [t.name for t in call.fake.tracks]
    assert names == ["Drums", "Kick", "Keys", "Bass", "Bass DI", "Bass Amp"]
    depths = [t.info["I_FOLDERDEPTH"] for t in call.fake.tracks]
    assert depths == [1, -1, 0, 1, 0, -1]
    assert out["index"] == 3
    assert "adjacent" in await error_text(call, "create_folder", tracks=["Drums", "Keys"], name="x")


async def test_setup_recording(call):
    await call("setup_recording", track="Drums", input={"type": "audio", "channel": 3, "stereo": True}, monitor="auto", arm=True)
    info = call.fake.tracks[0].info
    assert info["I_RECINPUT"] == 1024 + 2
    assert info["I_RECMON"] == 2 and info["I_RECARM"] == 1

    await call("setup_recording", track="Keys", input={"type": "midi", "device": "all", "channel": 0}, mode="midi_overdub")
    info = call.fake.tracks[2].info
    assert info["I_RECINPUT"] == 4096 + (63 << 5)
    assert info["I_RECMODE"] == 7


async def test_create_instrument_track(call):
    out = await call("create_instrument_track", instrument="ReaSynth", name="Lead", arm=True)
    tr = call.fake.tracks[out["index"]]
    assert tr.name == "Lead" and tr.fx[0].name == "VSTi: ReaSynth (Cockos)"
    assert tr.info["I_RECINPUT"] == 4096 + (63 << 5) and tr.info["I_RECMON"] == 1 and tr.info["I_RECARM"] == 1
    count = len(call.fake.tracks)
    assert "instrument not found" in await error_text(call, "create_instrument_track", instrument="Nonexistent")
    assert len(call.fake.tracks) == count  # the empty track was removed again


# ------------------------------------------------------------------- items

def kick_item(call):
    return call.fake.tracks[1].items[0]


async def test_split_item_at_several_points(call):
    item = kick_item(call)
    pieces = await call("split_item", item=item.guid, at=[3.0, 1.0, {"beats": 4}])
    assert [(p["position"], p["length"]) for p in pieces] == [(0, 1), (1, 1), (2, 1), (3, 1)]
    assert len(call.fake.tracks[1].items) == 4


async def test_edit_item_trim_keeps_audio_in_place(call):
    item = kick_item(call)
    out = await call("edit_item", item={"track": "Kick", "index": 0}, start=1.0, end={"bar": 2, "beat": 1})
    assert out["position"] == 1 and out["length"] == 1
    assert item.takes[0].info["D_STARTOFFS"] == 1.0

    await call("edit_item", item=item.guid, position={"bar": 3}, track="Keys", fade_in=0.5, name="Kick 2", volume_db=0)
    assert item.track.name == "Keys" and item.pos == 4.0
    assert item.info["D_FADEINLEN"] == 0.5 and item.takes[0].name == "Kick 2"


async def test_delete_reverse_glue_duplicate(call):
    fake = call.fake
    item = kick_item(call)
    copies = await call("duplicate_item", item=item.guid, count=3)
    assert [c["position"] for c in copies] == [4, 8, 12]
    assert all(c["name"] == "kick.wav" for c in copies)
    assert len({i.guid for i in fake.tracks[1].items}) == 4

    await call("reverse_items", items=[item.guid])
    assert item.takes[0].reversed and 41051 in fake.commands

    glued = await call("glue_items", items=[c["guid"] for c in copies])
    assert len(glued) == 1 and glued[0]["position"] == 4 and glued[0]["length"] == 12

    assert await call("delete_items", items=[item.guid]) == 1
    assert item not in fake.tracks[1].items


async def test_insert_media_file(call):
    out = await call("insert_media_file", track="Keys", file="C:/samples/pad.wav", position=6.0)
    assert out["position"] == 6 and out["name"] == "pad.wav" and out["track"] == 2


# -------------------------------------------------------------------- MIDI

async def test_compose_and_read_midi(call):
    item = await call("create_midi_item", track="Keys", start={"bar": 2}, end={"bar": 3}, name="Chords")
    assert item["position"] == 2 and item["length"] == 2 and item["midi"] is True

    chord = [{"pitch": p, "start_beats": 0, "length_beats": 2, "velocity": 90} for p in (60, 64, 67)]
    run = [{"pitch": 72 + i, "start_beats": 2 + i * 0.5, "length_beats": 0.5} for i in range(6)]
    out = await call("add_midi_notes", item=item["guid"], notes=chord + run)
    assert out == {"added": 9, "total_notes": 9}

    midi = await call("get_midi", item=item["guid"])
    assert midi["length"] == 2.5  # grown to fit the run (ends at beat 5 of the item)
    first = midi["notes"][0]
    assert first["pitch"] == 60 and first["start_beats"] == 4 and first["length_beats"] == 2 and first["channel"] == 1
    assert midi["notes"][-1]["pitch"] == 77

    await call("add_midi_cc", item=item["guid"], events=[
        {"type": "cc", "cc": 64, "value": 127, "beats": 0},
        {"type": "pitchbend", "value": -8192, "beats": 1},
        {"type": "program", "value": 5, "beats": 0},
    ])
    midi = await call("get_midi", item=item["guid"])
    kinds = sorted((e["type"], e["value"]) for e in midi["cc"])
    assert kinds == [("cc", 127), ("pitchbend", -8192), ("program", 5)]

    assert await call("delete_midi_notes", item=item["guid"], indices=[0, 1]) == 2
    assert await call("delete_midi_notes", item=item["guid"], all=True) == 7
    assert (await call("get_midi", item=item["guid"]))["notes"] == []


async def test_reverse_midi_mirrors_notes(call):
    item = await call("create_midi_item", track="Keys", start=0.0, end=2.0)  # 4 beats
    await call("add_midi_notes", item=item["guid"], notes=[{"pitch": 60, "start_beats": 0, "length_beats": 1}])
    await call("reverse_items", items=[item["guid"]])
    note = (await call("get_midi", item=item["guid"]))["notes"][0]
    assert note["start_beats"] == 3 and note["length_beats"] == 1


async def test_midi_tools_reject_audio_and_bad_pitch(call):
    assert "not MIDI" in await error_text(call, "get_midi", item=kick_item(call).guid)
    item = await call("create_midi_item", track="Keys", start=0.0, end=2.0)
    assert "0-127" in await error_text(call, "add_midi_notes", item=item["guid"], notes=[{"pitch": 200, "start_beats": 0}])


# ---------------------------------------------------------------------- FX

async def test_fx_chain_and_parameters(call):
    found = await call("list_installed_fx", filter="rea")
    assert [f["name"] for f in found][:2] == ["VST: ReaEQ (Cockos)", "VST: ReaComp (Cockos)"]

    added = await call("add_fx", track="Keys", name="ReaComp", preset="Vocal")
    assert added["index"] == 1 and added["params"] == 3
    fx = call.fake.tracks[2].fx[1]
    assert fx.preset == "Vocal"

    params = await call("get_fx_params", track="Keys", fx="ReaComp", filter="gain")
    assert params["params"] == [{"index": 0, "name": "Gain", "value": 0.5, "display": "0.0 dB"}]

    out = await call("set_fx_params", track="Keys", fx=1, params=[
        {"param": "gain", "display_value": -6},
        {"param": "Frequency", "display_value": 2000},
        {"param": 2, "value": 1.0},
    ])
    assert [p["display"] for p in out] == ["-6.0 dB", "2000 Hz", "Band"]
    assert "not numeric" in await error_text(call, "set_fx_params", track="Keys", fx=1, params=[{"param": "Mode", "display_value": 3}])

    await call("update_fx", track="Keys", fx="ReaComp", enabled=False, move_to=0)
    assert call.fake.tracks[2].fx[0] is fx and fx.enabled is False

    assert await call("remove_fx", track="Keys", fx=0) is True
    assert [f.name for f in call.fake.tracks[2].fx] == ["VSTi: ReaSynth (Cockos)"]
    assert "FX not found" in await error_text(call, "add_fx", track="Keys", name="Nope")
    assert "no FX matching" in await error_text(call, "remove_fx", track="Keys", fx="Nope")


# -------------------------------------------------- transport, tempo, markers

async def test_transport_and_time_selection(call):
    state = await call("transport", action="play", position={"bar": 5})
    assert state["playing"] is True and state["cursor"] == 8
    assert (await call("transport", action="stop"))["playing"] is False
    await call("transport", action="goto", position=1.5, repeat=True)
    assert call.fake.cursor == 1.5 and call.fake.repeat == 1
    assert await call("set_time_selection", start={"bar": 1}, end={"bar": 3}) == [0, 4]
    assert await call("set_time_selection") is None


async def test_set_tempo(call):
    out = await call("set_tempo", bpm=96)
    assert out["bpm"] == 96
    out = await call("set_tempo", numerator=6, denominator=8)
    assert out["time_signature"] == [6, 8] and out["bpm"] == 96
    out = await call("set_tempo", bpm=140)
    assert out == {"bpm": 140, "time_signature": [6, 8], "tempo_markers": 1}


async def test_markers_and_regions(call):
    m = await call("add_marker", position={"bar": 9}, name="Drop")
    assert m == {"id": 2, "region": False}
    r = await call("add_marker", position={"bar": 13}, end={"bar": 21}, name="Chorus", color="#00ff00")
    assert r == {"id": 2, "region": True}

    await call("update_marker", id=2, region=True, name="Chorus 1", end={"bar": 17})
    state = await call("get_project_state", include_items=False)
    chorus = next(x for x in state["regions"] if x["id"] == 2)
    assert chorus["name"] == "Chorus 1" and chorus["start"] == 24 and chorus["end"] == 32

    assert await call("delete_marker", id=2) is True
    assert [x["name"] for x in (await call("get_project_state", include_items=False))["markers"]] == ["Intro"]
    assert "not found" in await error_text(call, "delete_marker", id=99)


async def test_every_edit_is_one_closed_undo_step(call):
    await call("create_track", name="A")
    await call("add_fx", track="A", name="ReaEQ")
    await call("add_marker", position=1.0, name="x")
    assert call.fake.undo == ["Create track", "Add FX", "Add marker"]
    assert call.fake.undo_depth == 0
