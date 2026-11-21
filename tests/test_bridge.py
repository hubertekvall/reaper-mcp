from __future__ import annotations

import json
import os
import time

import pytest

from reaper_mcp.bridge import BridgeNotRunning, BridgeTimeout, ReaperError

TRACK0 = {"$type": "MediaTrack", "index": 0}


def test_lua_json_round_trip(env):
    value = {
        "s": 'quote " backslash \\ newline \n tab \t å ü 🎛',
        "i": 42,
        "f": -1.5e-3,
        "whole_float": 2.0,
        "b": [True, False, None],
        "nested": {"empty_list": [], "empty_obj": {}},
    }
    J = env.M.json
    assert json.loads(J.encode(J.decode(json.dumps(value)))) == value
    assert json.loads(J.encode(J.decode('"\\u00e5\\ud83c\\udf9b"'))) == "å🎛"
    assert J.decode("[1,-2,3.5e2]")[2] == -2
    # UTF-8 must pass through unescaped whatever the C locale says about bytes >= 0x80.
    assert J.encode("åü🎛 \x01") == '"åü🎛 \\u0001"'


def test_lua_json_rejects_garbage(env):
    J = env.M.json
    for bad in ["{", "[1,]", "1-2", '{"a" 1}', "nul"]:
        with pytest.raises(Exception):
            J.decode(bad)


async def test_ping(running):
    info = await running.bridge.request("ping")
    assert info["bridge_version"] == "0.3.1"
    assert info["saved"] is True and info["unsaved_changes"] is False
    assert info["project"] == "test.rpp"


async def test_project_state(running):
    state = await running.bridge.request("project_state")
    assert state["project"]["tempo"] == 120
    assert state["project"]["time_signature"] == [4, 4]
    assert state["project"]["time_selection"] is None
    names = [t["name"] for t in state["tracks"]]
    assert names == ["Drums", "Kick", "Keys"]
    drums, kick, keys = state["tracks"]
    assert drums["folder_depth"] == 1 and kick["folder_depth"] == -1
    assert kick["volume_db"] == pytest.approx(-6.0206, abs=1e-3)
    assert drums["fx"] == [{"index": 0, "name": "VST: ReaEQ (Cockos)", "enabled": True, "offline": False}]
    assert keys["instrument_fx"] == 0
    assert kick["items"][0]["take"]["source"] == "C:/audio/kick.wav"
    assert keys["items"][0]["take"]["midi"] is True
    assert state["markers"] == [{"id": 1, "name": "Intro", "position": 0, "color": 0}]
    assert state["regions"][0]["name"] == "Verse"
    assert state["regions"][0]["end"] == 24

    light = await running.bridge.request("project_state", {"items": False})
    assert "items" not in light["tracks"][0]


async def test_call_reads_and_writes_with_undo(running):
    (vol,) = await running.bridge.request("call", {"fn": "GetMediaTrackInfo_Value", "args": [{"$type": "MediaTrack", "index": 1}, "D_VOL"]})
    assert vol == 0.5
    assert running.fake.undo == []

    result = await running.bridge.request(
        "call", {"fn": "reaper.SetMediaTrackInfo_Value", "args": [TRACK0, "D_PAN", -0.25], "undo": "Pan drums"}
    )
    assert result == [True]
    assert running.fake.tracks[0].info["D_PAN"] == -0.25
    assert running.fake.undo == ["Pan drums"]
    assert running.fake.undo_depth == 0


async def test_object_references_round_trip(running):
    (ref,) = await running.bridge.request("call", {"fn": "GetTrack", "args": [0, 2]})
    assert ref["$type"] == "MediaTrack"
    assert ref["guid"] == running.fake.tracks[2].guid

    # by pointer handle
    (_, name) = await running.bridge.request("call", {"fn": "GetTrackName", "args": [{"$ptr": ref["$ptr"]}]})
    assert name == "Keys"
    # by GUID
    (_, name) = await running.bridge.request("call", {"fn": "GetTrackName", "args": [{"$type": "MediaTrack", "guid": ref["guid"]}]})
    assert name == "Keys"
    # items and takes by GUID
    item_guid = running.fake.tracks[1].items[0].guid
    (take_ref,) = await running.bridge.request("call", {"fn": "GetActiveTake", "args": [{"$type": "MediaItem", "guid": item_guid}]})
    assert take_ref["$type"] == "MediaItem_Take"
    (tname,) = await running.bridge.request("call", {"fn": "GetTakeName", "args": [take_ref]})
    assert tname == "kick.wav"
    # master
    (_, name) = await running.bridge.request("call", {"fn": "GetTrackName", "args": [{"$type": "MediaTrack", "master": True}]})
    assert name == "MASTER"


async def test_null_arguments_and_nil_returns(running):
    result = await running.bridge.request("call", {"fn": "GetTrack", "args": [None, 99]})
    assert result == [None]


async def test_errors_come_back_as_reaper_errors(running):
    with pytest.raises(ReaperError, match="unknown ReaScript function: NoSuchThing"):
        await running.bridge.request("call", {"fn": "NoSuchThing"})
    with pytest.raises(ReaperError, match="no track at index 7"):
        await running.bridge.request("call", {"fn": "GetTrackName", "args": [{"$type": "MediaTrack", "index": 7}]})
    with pytest.raises(ReaperError, match="not found"):
        await running.bridge.request("call", {"fn": "GetTrackName", "args": [{"$type": "MediaTrack", "guid": "{nope}"}]})
    with pytest.raises(ReaperError, match="unknown command"):
        await running.bridge.request("explode")
    # the bridge keeps working afterwards
    assert (await running.bridge.request("ping"))["project"] == "test.rpp"


async def test_failed_write_still_closes_undo_block(running):
    with pytest.raises(ReaperError):
        await running.bridge.request("call", {"fn": "SetMediaTrackInfo_Value", "args": [{"$type": "MediaTrack", "index": 9}, "D_VOL", 1], "undo": "x"})
    # resolving the reference fails before the undo block opens
    assert running.fake.undo_depth == 0


async def test_not_running_without_heartbeat(env):
    env.bridge.heartbeat_file.unlink()
    with pytest.raises(BridgeNotRunning):
        await env.bridge.request("ping")


async def test_stale_heartbeat_counts_as_not_running(env):
    old = time.time() - 60
    os.utime(env.bridge.heartbeat_file, (old, old))
    with pytest.raises(BridgeNotRunning):
        await env.bridge.request("ping")


async def test_timeout_cleans_up_request(env):
    env.bridge.timeout = 0.1
    with pytest.raises(BridgeTimeout, match="did not pick up"):
        await env.bridge.request("ping")
    assert list(env.bridge.requests_dir.iterdir()) == []


async def test_many_requests_in_parallel(running):
    import asyncio

    results = await asyncio.gather(*(running.bridge.request("call", {"fn": "CountTracks", "args": [0]}) for _ in range(25)))
    assert results == [[3]] * 25
    assert list(running.bridge.responses_dir.iterdir()) == []
