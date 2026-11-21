"""Step 3: richer project reading, routing and automation."""

from __future__ import annotations

import pytest

from .test_editing import Caller, error_text


@pytest.fixture
def call(running):
    return Caller(running)


def track(call, name):
    return next(t for t in call.fake.tracks if t.name == name)


# ------------------------------------------------------------ reading fixes

async def test_project_state_lists_routing_and_readable_record_settings(call):
    await call("create_send", from_track="Kick", to_track="Drums", volume_db=-6, mode="pre_fx")
    await call("setup_recording", track="Keys", input={"type": "midi", "device": "all", "channel": 0}, mode="midi_overdub", monitor="on")
    await call("setup_recording", track="Kick", input={"type": "audio", "channel": 3, "stereo": True})
    state = await call("get_project_state")
    drums, kick, keys = state["tracks"]

    (send,) = kick["sends"]
    assert send["to"]["name"] == "Drums" and send["to"]["index"] == 0
    assert send["volume_db"] == pytest.approx(-6, abs=1e-6) and send["mode"] == "pre_fx"
    assert send["source_channels"] == "1/2" and send["dest_channels"] == "1/2"
    assert drums["receives"][0]["from"]["name"] == "Kick"

    assert keys["rec_input"] == {"type": "midi", "device": "all", "channel": "all"}
    assert keys["rec_mode"] == "midi_overdub" and keys["rec_monitor"] == "on"
    assert kick["rec_input"] == {"type": "audio", "channel": 3, "stereo": True, "channels": "3/4"}
    assert keys["items"][0]["take"]["source"] == "in-project MIDI"
    assert drums["automation_mode"] == "trim/read"


async def test_get_midi_pages_and_summarizes_dense_items(call):
    item = await call("create_midi_item", track="Keys", start=0.0, end=8.0)
    notes = [{"pitch": 36 + (i % 3) * 2, "start_beats": i * 0.25, "length_beats": 0.25, "velocity": 60 + i % 40} for i in range(64)]
    await call("add_midi_notes", item=item["guid"], notes=notes)

    summary = await call("get_midi", item=item["guid"], summary=True)
    assert summary["total_notes"] == 64 and "notes" not in summary
    assert summary["pitch_range"] == [36, 40] and summary["pitch_counts"] == {"36": 22, "38": 21, "40": 21}
    assert summary["velocity_range"] == [60, 99]

    page1 = await call("get_midi", item=item["guid"], limit=50)
    assert len(page1["notes"]) == 50 and page1["next_offset"] == 50
    page2 = await call("get_midi", item=item["guid"], offset=50, limit=50)
    assert len(page2["notes"]) == 14 and "next_offset" not in page2
    assert page2["notes"][0]["index"] == 50


async def test_status_flags_an_outdated_bridge(call, running):
    assert "warning" not in await call("reaper_status")
    running.M.commands.ping = running.lua.eval('function() return {bridge_version = "0.1.0"} end')
    status = await call("reaper_status")
    assert "install-bridge" in status["warning"]


# ------------------------------------------------------------------ routing

async def test_send_lifecycle(call):
    out = await call("create_send", from_track="Keys", to_track="Drums", volume_db=-3, pan=0.25, midi=False)
    assert out["to"]["name"] == "Drums" and out["pan"] == 0.25
    snd = call.fake.sends[0]
    assert snd.info["I_MIDIFLAGS"] == 31

    out = await call("update_send", from_track="Keys", send="Drums", mute=True, mode="post_fx", source_channels="1", dest_channels="3/4")
    assert out["mute"] is True and out["mode"] == "post_fx"
    assert out["source_channels"] == "1" and out["dest_channels"] == "3/4"
    assert track(call, "Drums").info["I_NCHAN"] == 4  # widened for channels 3/4

    assert "no send to that track" in await error_text(call, "update_send", from_track="Keys", send="Kick", mute=True)
    assert "neighbouring" in await error_text(call, "update_send", from_track="Keys", send=0, dest_channels="3/5")
    assert await call("update_send", from_track="Keys", send=0, remove=True) is True
    assert call.fake.sends == []
    assert "itself" in await error_text(call, "create_send", from_track="Keys", to_track="Keys")


async def test_fx_return(call):
    out = await call("create_fx_return", name="Reverb", fx=["ReaVerbate"], sources=["Kick", "Keys"], send_db=-15)
    ret = call.fake.tracks[out["index"]]
    assert ret.name == "Reverb" and ret.fx[0].name == "VST: ReaVerbate (Cockos)"
    assert ret.fx[0].wet == 1.0
    assert ret.fx[0].params[1][1] == 0.0  # ReaVerbate's own Dry at -inf
    assert ret.fx[0].params[0][1] == 0.5  # Wet untouched
    assert out["fx"][0]["set_to_wet"] == ["Dry"]
    assert [s["to"]["name"] for s in out["sends"]] == ["Reverb", "Reverb"]
    assert all(s["volume_db"] == pytest.approx(-15) and s["mode"] == "post_fader" for s in out["sends"])
    assert call.fake.undo[-1] == "Create FX return"

    count = len(call.fake.tracks)
    assert "FX not found" in await error_text(call, "create_fx_return", name="x", fx=["Nope"])
    assert len(call.fake.tracks) == count


async def test_sidechain_sets_detector_input(call):
    await call("create_track", name="Bass")
    await call("add_fx", track="Bass", name="ReaComp")
    out = await call("create_sidechain", from_track="Kick", to_track="Bass", fx="ReaComp")
    assert out["send"]["dest_channels"] == "3/4" and out["send"]["mode"] == "post_fx"
    assert out["target_channels"] == 4
    assert out["detector"] == "SignIn = 2 (detector: auxiliary inputs 3/4)"
    assert call.fake.sends[0].info["I_MIDIFLAGS"] == 31

    plain = await call("create_sidechain", from_track="Kick", to_track="Keys", fx="ReaSynth")
    assert plain["detector"] is None and "sidechain" in plain["note"]


# --------------------------------------------------------------- automation

async def test_write_and_read_volume_automation(call):
    out = await call("write_automation", track="Kick", target="volume", points=[
        {"time": 0, "value": -60, "shape": "slow"},
        {"time": {"bar": 2}, "value": 0},
        {"time": {"bar": 3}, "value": -6, "shape": "square"},
    ])
    assert out["envelope"] == "Volume" and out["points_written"] == 3
    env = track(call, "Kick").envs["VOLENV2"]
    assert env.active and env.visible  # switched on after REAPER creates it hidden
    # stored with REAPER's fader scaling (the fake uses a square-root curve)
    assert env.points[1][1] == pytest.approx(1.0) and env.points[2][1] == pytest.approx(0.5011872 ** 0.5)

    read = await call("get_automation", track="Kick", target="volume")
    assert read["units"] == "dB" and read["total_points"] == 3
    assert [round(p["value"], 3) for p in read["points"]] == [-60, 0, -6]
    assert [p["shape"] for p in read["points"]] == ["slow", "linear", "square"]
    assert read["points"][2]["beats"] == 8

    # writing over a range replaces what was there
    await call("write_automation", track="Kick", target="volume", points=[{"time": 1.0, "value": -3}, {"time": 2.5, "value": -3}])
    times = [p["time"] for p in (await call("get_automation", track="Kick", target="volume"))["points"]]
    assert times == [0, 1, 2.5, 4]


async def test_pan_mute_and_fx_parameter_automation(call):
    await call("write_automation", track="Keys", target="pan", points=[{"time": 0, "value": -1}, {"time": 1, "value": 1}])
    env = track(call, "Keys").envs["PANENV2"]
    assert [p[1] for p in env.points] == [1, -1]  # REAPER's pan envelopes are inverted
    read = await call("get_automation", track="Keys", target="pan")
    assert [p["value"] for p in read["points"]] == [-1, 1]

    await call("write_automation", track="Keys", target="mute", points=[{"time": 0, "value": True, "shape": "square"}])
    mute_env = track(call, "Keys").envs["MUTEENV"]
    assert mute_env.points[0][1] == 0  # REAPER: 0 = muted, 1 = unmuted
    assert mute_env.active and mute_env.visible
    assert (await call("get_automation", track="Keys", target="mute"))["points"][0]["value"] is True

    await call("write_automation", track="Drums", target={"fx": "ReaEQ", "param": "Gain"}, points=[
        {"time": 0, "value": 0.5}, {"time": 2, "value": {"display_value": -12}},
    ])
    read = await call("get_automation", track="Drums", target={"fx": 0, "param": "gain"})
    assert read["envelope"] == "Gain / VST: ReaEQ (Cockos)"
    assert read["points"][1]["value"] == pytest.approx(0.25, abs=0.01)  # -12 dB on a -24..+24 scale


async def test_draw_shapes(call):
    ramp = await call("draw_automation", track="Kick", target="volume", start={"bar": 1}, end={"bar": 3},
                      shape="ramp", from_value=-30, to_value=0, curve="fast_start")
    assert ramp["points_written"] == 2

    lfo = await call("draw_automation", track="Keys", target="pan", start={"bar": 1}, end={"bar": 2},
                     shape="sine", min=-0.5, max=0.5, period_beats=1)
    assert lfo["points_written"] == 9  # 4 cycles x 2 + closing point
    pts = (await call("get_automation", track="Keys", target="pan"))["points"]
    assert [p["value"] for p in pts[:3]] == [-0.5, 0.5, -0.5]
    assert pts[1]["beats"] == 0.5 and pts[0]["shape"] == "slow"

    sq = await call("draw_automation", track="Drums", target={"fx": "ReaEQ", "param": "Frequency"}, start=0.0, end=2.0,
                    shape="square", min=0.2, max=0.8, period_beats=2, automation_item=True)
    assert sq["automation_item"] == 0
    env = next(iter(track(call, "Drums").fx_envs.values()))
    assert env.points == [] and len(env.items[0][2]) == 5

    assert "period" in await error_text(call, "draw_automation", track="Kick", target="volume", start=0.0, end=0.1, shape="saw", min=-10, max=0)
    assert "from and to" in await error_text(call, "draw_automation", track="Kick", target="volume", start=0.0, end=1.0)


async def test_clear_automation_and_errors(call):
    await call("draw_automation", track="Kick", target="width", start=0.0, end=4.0, shape="triangle", min=0, max=1, period_beats=2)
    # period 2 beats = 1 s: points every 0.5 s from 0 to 4
    assert await call("clear_automation", track="Kick", target="width", start=0.0, end=2.0) == 4
    assert (await call("get_automation", track="Kick", target="width"))["total_points"] == 5
    assert await call("clear_automation", track="Kick", target="width") == 5

    assert "no trim automation" in await error_text(call, "get_automation", track="Keys", target="trim")
    assert "target must be" in await error_text(call, "write_automation", track="Keys", target="cutoff", points=[])
    assert "needs a value" in await error_text(call, "write_automation", track="Keys", target="volume", points=[{"time": 0}])


async def test_automation_mode(call):
    info = await call("update_track", track="Keys", automation_mode="latch")
    assert info["automation_mode"] == "latch"
