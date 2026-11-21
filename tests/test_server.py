from __future__ import annotations

import json

from mcp.client import Client

from reaper_mcp.server import create_server


def payload(result):
    if result.structured_content is not None:
        sc = result.structured_content
        return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc
    return json.loads(result.content[0].text)


async def test_tools_are_listed(running):
    async with Client(create_server(running.bridge)) as client:
        tools = {t.name for t in (await client.list_tools()).tools}
    assert {"reaper_status", "get_project_state", "call_reaper_api", "add_midi_notes", "set_fx_params"} <= tools
    assert len(tools) <= 40, "keep the tool list small"


async def test_tools_round_trip_through_mcp(running):
    async with Client(create_server(running.bridge)) as client:
        state = payload(await client.call_tool("get_project_state", {"include_items": False}))
        assert [t["name"] for t in state["tracks"]] == ["Drums", "Kick", "Keys"]

        out = payload(
            await client.call_tool(
                "call_reaper_api",
                {"function": "SetMediaTrackInfo_Value", "args": [{"$type": "MediaTrack", "index": 2}, "B_MUTE", 1], "undo_label": "Mute keys"},
            )
        )
        assert out == [True]
    assert running.fake.tracks[2].info["B_MUTE"] == 1.0
    assert running.fake.undo == ["Mute keys"]


async def test_tool_error_is_reported_not_raised(running):
    async with Client(create_server(running.bridge)) as client:
        result = await client.call_tool("call_reaper_api", {"function": "Nope"})
    assert result.is_error
    assert "unknown ReaScript function" in result.content[0].text
