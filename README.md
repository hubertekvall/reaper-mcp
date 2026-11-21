# reaper-mcp

An [MCP](https://modelcontextprotocol.io) server that lets Claude (or any MCP client) work inside the
[REAPER](https://www.reaper.fm) DAW: read the project, edit items and MIDI, set up tracks, routing,
FX and automation.

> **Status: step 1 of 4.** The bridge, project reading and a generic ReaScript call work. Dedicated
> editing, routing, automation and render tools come next.

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

| Tool | What it does |
| --- | --- |
| `reaper_status` | Checks that REAPER and the bridge are reachable. |
| `get_project_state` | Tempo, time signature, transport, every track with FX, items, routing counts, markers and regions. Also available as the `reaper://project` resource. |
| `call_reaper_api` | Calls any [ReaScript function](https://www.reaper.fm/sdk/reascript/reascripthelp.html). With `undo_label`, the change becomes one undo step. |

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
