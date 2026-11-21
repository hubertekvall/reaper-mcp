"""Where REAPER keeps its resources, and where the bridge exchanges files."""

from __future__ import annotations

import os
import sys
from pathlib import Path

BRIDGE_SCRIPT_NAME = "reaper_mcp_bridge.lua"


def reaper_resource_path() -> Path:
    """REAPER's resource folder (Options > Show REAPER resource path).

    Override with REAPER_RESOURCE_PATH, for example for a portable install.
    """
    override = os.environ.get("REAPER_RESOURCE_PATH")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "REAPER"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "REAPER"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "REAPER"


def bridge_dir() -> Path:
    """Folder shared with the Lua bridge. Override with REAPER_MCP_BRIDGE_DIR."""
    override = os.environ.get("REAPER_MCP_BRIDGE_DIR")
    if override:
        return Path(override).expanduser()
    return reaper_resource_path() / "reaper_mcp"


def bundled_bridge_script() -> Path:
    return Path(__file__).parent / "lua" / BRIDGE_SCRIPT_NAME
