# reaper-mcp

An [MCP](https://modelcontextprotocol.io) server that lets Claude (or any MCP client) work inside the
[REAPER](https://www.reaper.fm) DAW: read the project, edit items and MIDI, set up tracks, routing,
FX and automation.

> **Status: step 2 of 4.** Project reading, editing (tracks, items, MIDI, FX, transport, tempo,
> markers) and a generic ReaScript call. Routing, automation and render/analysis tools come next.

## How it works

```
Claude ──MCP (stdio)──▶ reaper-mcp (Python) ──JSON files──▶ reaper_mcp_bridge.lua (inside REAPER)
```

The Lua script runs in REAPER's background loop. The Python server drops a request file into
`<REAPER resource folder>/reaper_mcp/requests`; the script runs it and writes the answer to
`.../responses`. A heartbeat file tells the server whether the script is running. No ports, no extra
REAPER extensions, works the same on Windows, macOS and Linux.

## Setup (Windows)

Requires REAPER 7 and Python 3.11+ (or [uv](https://docs.astral.sh/uv/)).

1. Install the bridge script into REAPER:
   ```
   uvx --from git+https://github.com/hubertekvall/reaper-mcp reaper-mcp install-bridge
   ```
   This copies `reaper_mcp_bridge.lua` to `%APPDATA%\REAPER\Scripts\reaper_mcp\`. For a portable
   REAPER install, add `--resource-path <folder>` (REAPER shows it under *Options > Show REAPER
   resource path*).
2. In REAPER: *Actions > Show action list > New action > Load ReaScript*, pick that file, and run it.
   The console prints `reaper-mcp bridge 0.1.0 running`.
3. Check the connection:
   ```
   uvx --from git+https://github.com/hubertekvall/reaper-mcp reaper-mcp doctor
   ```
4. Add the server to Claude Desktop (`%APPDATA%\Claude\claude_desktop_config.json`) or Claude Code:
   ```json
   {
     "mcpServers": {
       "reaper": {
         "command": "uvx",
         "args": ["--from", "git+https://github.com/hubertekvall/reaper-mcp", "reaper-mcp"]
       }
     }
   }
   ```
   Claude Code: `claude mcp add reaper -- uvx --from git+https://github.com/hubertekvall/reaper-mcp reaper-mcp`

## Tools

Shared conventions: a **track** is a 0-based index, exact name, GUID or `"master"`. An **item** is its
GUID or `{"track": <track>, "index": <n>}`. A **time** is seconds, `{"beats": <quarter notes>}` or
`{"bar": 5, "beat": 1}`. Every editing tool is one undo step in REAPER.

| Area | Tools |
| --- | --- |
| Project | `reaper_status`, `get_project_state` (also the `reaper://project` resource) |
| Tracks | `create_track`, `update_track` (name, volume, pan, mute, solo, phase, color, folder depth, move), `delete_tracks`, `create_folder`, `setup_recording` (input, monitoring, record mode, arm), `create_instrument_track` |
| Items | `edit_item` (move, trim, change track, fades, volume, pitch, rate, lock, name), `split_item`, `delete_items`, `reverse_items`, `glue_items`, `duplicate_item`, `insert_media_file` |
| MIDI | `create_midi_item`, `get_midi`, `add_midi_notes`, `delete_midi_notes`, `add_midi_cc` (CC, pitch bend, program change) |
| FX | `list_installed_fx`, `add_fx`, `remove_fx`, `update_fx` (bypass, offline, preset, reorder), `get_fx_params`, `set_fx_params` (normalized, or by the value the plug-in displays, such as `-6` dB) |
| Transport | `transport` (play, stop, pause, record, go to), `set_time_selection`, `set_tempo` |
| Markers | `add_marker` (markers and regions), `update_marker`, `delete_marker` |
| Anything else | `call_reaper_api`: any [ReaScript function](https://www.reaper.fm/sdk/reascript/reascripthelp.html), with optional undo |

REAPER objects travel as references such as `{"$type": "MediaTrack", "index": 0}`,
`{"$type": "MediaItem", "guid": "{...}"}` or `{"$type": "MediaTrack", "master": true}`.

## Settings

| Variable | Default |
| --- | --- |
| `REAPER_RESOURCE_PATH` | `%APPDATA%\REAPER` (Windows), `~/Library/Application Support/REAPER` (macOS), `~/.config/REAPER` (Linux) |
| `REAPER_MCP_BRIDGE_DIR` | `<resource path>/reaper_mcp` |

## Development

```
uv sync
uv run pytest
```

The tests load the real Lua bridge into Lua 5.4 (via [Lupa](https://github.com/scoder/lupa)) with
a small fake of the ReaScript API, so the file transport, JSON handling and commands are exercised
without REAPER.
