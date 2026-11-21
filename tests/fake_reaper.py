"""A tiny in-memory stand-in for REAPER's ReaScript API.

It runs the real Lua bridge inside Lupa (Lua 5.4), so tests exercise the
actual bridge code and the actual file transport without REAPER.
"""

from __future__ import annotations

import itertools
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from lupa import lua54

from reaper_mcp.paths import bundled_bridge_script

_guid = itertools.count(1)


def new_guid() -> str:
    return "{%08X-0000-0000-0000-000000000000}" % next(_guid)


@dataclass(eq=False)
class Take:
    name: str
    midi: bool = False
    source: str = ""
    guid: str = field(default_factory=new_guid)
    info: dict = field(default_factory=lambda: {"D_STARTOFFS": 0.0, "D_PLAYRATE": 1.0})

    def __str__(self) -> str:
        return f"take:{id(self):x}"


@dataclass(eq=False)
class Item:
    position: float
    length: float
    takes: list[Take] = field(default_factory=list)
    guid: str = field(default_factory=new_guid)
    selected: bool = False
    info: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.info = {
            "D_POSITION": self.position, "D_LENGTH": self.length, "B_MUTE": 0.0,
            "D_FADEINLEN": 0.01, "D_FADEOUTLEN": 0.01, "D_VOL": 1.0, **self.info,
        }

    def __str__(self) -> str:
        return f"item:{id(self):x}"


@dataclass(eq=False)
class Track:
    name: str
    items: list[Item] = field(default_factory=list)
    fx: list[str] = field(default_factory=list)
    guid: str = field(default_factory=new_guid)
    info: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.info = {
            "I_FOLDERDEPTH": 0.0, "D_VOL": 1.0, "D_PAN": 0.0, "B_MUTE": 0.0, "I_SOLO": 0.0,
            "I_RECARM": 0.0, "I_RECINPUT": 0.0, "I_RECMODE": 0.0, "I_RECMON": 0.0,
            "B_MAINSEND": 1.0, "I_CUSTOMCOLOR": 0.0, **self.info,
        }

    def __str__(self) -> str:
        return f"track:{id(self):x}"


class FakeReaper:
    def __init__(self, resource_path: Path) -> None:
        self.resource_path = resource_path
        self.master = Track("MASTER")
        self.tracks: list[Track] = []
        self.markers: list[tuple] = []  # (isrgn, pos, end, name, id, color)
        self.undo: list[str] = []
        self.undo_depth = 0
        self.tempo = 120.0

    # helpers
    def all_items(self) -> list[Item]:
        return [i for t in self.tracks for i in t.items]

    # --- ReaScript API (only what the bridge and tests use) ---
    def GetResourcePath(self): return str(self.resource_path)
    def RecursiveCreateDirectory(self, path, _ignored): os.makedirs(path, exist_ok=True); return 1
    def EnumerateFiles(self, path, index):
        if index < 0:
            return None
        names = sorted(n for n in os.listdir(path) if os.path.isfile(os.path.join(path, n)))
        return names[index] if index < len(names) else None
    def time_precise(self): return time.perf_counter()
    def GetAppVersion(self): return "7.99/fake"
    def GetProjectName(self, _proj, _buf=""): return "test.rpp"
    def EnumProjects(self, _idx, _buf=""): return (self, "/projects/test.rpp")

    def ValidatePtr2(self, _proj, ptr, typename):
        kinds = {"MediaTrack*": Track, "MediaItem*": Item, "MediaItem_Take*": Take}
        cls = kinds.get(typename)
        return cls is not None and isinstance(ptr, cls)
    def ValidatePtr(self, ptr, typename): return self.ValidatePtr2(0, ptr, typename)

    def GetMasterTrack(self, _proj): return self.master
    def CountTracks(self, _proj): return len(self.tracks)
    def GetTrack(self, _proj, idx): return self.tracks[idx] if 0 <= idx < len(self.tracks) else None
    def InsertTrackAtIndex(self, idx, _defaults): self.tracks.insert(idx, Track(""))
    def GetTrackGUID(self, tr): return tr.guid
    def GetTrackName(self, tr): return (True, tr.name)
    def GetMediaTrackInfo_Value(self, tr, key): return tr.info.get(key, 0.0)
    def SetMediaTrackInfo_Value(self, tr, key, value): tr.info[key] = float(value); return True
    def GetSetMediaTrackInfo_String(self, tr, key, value, is_set):
        if key == "P_NAME":
            if is_set:
                tr.name = value
            return (True, tr.name)
        return (False, "")

    def TrackFX_GetCount(self, tr): return len(tr.fx)
    def TrackFX_GetFXName(self, tr, i, _buf=""): return (True, tr.fx[i])
    def TrackFX_GetEnabled(self, _tr, _i): return True
    def TrackFX_GetOffline(self, _tr, _i): return False
    def TrackFX_GetInstrument(self, tr):
        return next((i for i, n in enumerate(tr.fx) if n.startswith("VSTi")), -1)
    def GetTrackNumSends(self, _tr, _cat): return 0
    def CountTrackEnvelopes(self, _tr): return 0
    def CountTrackMediaItems(self, tr): return len(tr.items)
    def GetTrackMediaItem(self, tr, i): return tr.items[i]

    def CountMediaItems(self, _proj): return len(self.all_items())
    def GetMediaItem(self, _proj, i):
        items = self.all_items()
        return items[i] if 0 <= i < len(items) else None
    def GetMediaItemInfo_Value(self, item, key): return item.info.get(key, 0.0)
    def GetSetMediaItemInfo_String(self, item, key, _v, _set):
        return (True, item.guid) if key == "GUID" else (False, "")
    def IsMediaItemSelected(self, item): return item.selected
    def CountTakes(self, item): return len(item.takes)
    def GetTake(self, item, i): return item.takes[i] if 0 <= i < len(item.takes) else None
    def GetActiveTake(self, item): return item.takes[0] if item.takes else None
    def GetSetMediaItemTakeInfo_String(self, take, key, _v, _set):
        return (True, take.guid) if key == "GUID" else (False, "")
    def GetMediaItemTakeInfo_Value(self, take, key): return take.info.get(key, 0.0)
    def GetMediaItemTake_Source(self, take): return take.source or None
    def GetMediaSourceFileName(self, src, _buf=""): return src
    def GetTakeName(self, take): return take.name
    def TakeIsMIDI(self, take): return take.midi

    def TimeMap_GetTimeSigAtTime(self, _proj, _t): return (4, 4, self.tempo)
    def Master_GetTempo(self): return self.tempo
    def GetCursorPosition(self): return 0.0
    def GetPlayPosition(self): return 0.0
    def GetPlayState(self): return 0
    def GetSet_LoopTimeRange(self, _s, _l, _a, _b, _seek): return (0.0, 0.0)
    def GetProjectLength(self, _proj):
        return max((i.position + i.length for i in self.all_items()), default=0.0)
    def GetSetProjectInfo(self, _proj, key, _v, _set): return 48000.0 if key == "PROJECT_SRATE" else 0.0
    def CountProjectMarkers(self, _proj):
        regions = sum(1 for m in self.markers if m[0])
        return (len(self.markers), len(self.markers) - regions, regions)
    def EnumProjectMarkers3(self, _proj, i):
        isrgn, pos, end, name, mid, color = self.markers[i]
        return (i + 1, isrgn, pos, end, name, mid, color)

    def Undo_BeginBlock2(self, _proj): self.undo_depth += 1
    def Undo_EndBlock2(self, _proj, label, _flags):
        self.undo_depth -= 1
        self.undo.append(label)
    def PreventUIRefresh(self, _n): pass
    def UpdateArrange(self): pass
    def ShowConsoleMsg(self, msg): print(msg, end="")
    def defer(self, _fn): pass
    def atexit(self, _fn): pass


API_NAMES = [n for n in dir(FakeReaper) if n[0].isupper() or n in ("time_precise", "defer", "atexit")]


def load_bridge(fake: FakeReaper):
    """Load the real bridge script into a Lua runtime wired to `fake`. Returns (lua, M)."""
    lua = lua54.LuaRuntime(unpack_returned_tuples=True)
    impl = fake
    make = lua.eval(
        """
        function(impl, names)
          local r = {}
          for name in python.iter(names) do
            r[name] = function(...) return impl[name](...) end
          end
          return r
        end
        """
    )
    reaper = make(impl, API_NAMES)
    lua.globals().reaper = reaper
    lua.globals().REAPER_MCP_NO_START = True
    chunk = lua.execute(bundled_bridge_script().read_text(encoding="utf-8"))
    return lua, chunk


def demo_project(fake: FakeReaper) -> None:
    drums = Track("Drums", fx=["VST: ReaEQ (Cockos)"], info={"I_FOLDERDEPTH": 1.0})
    kick = Track(
        "Kick",
        items=[Item(0.0, 4.0, [Take("kick.wav", source="C:/audio/kick.wav")])],
        info={"I_FOLDERDEPTH": -1.0, "D_VOL": 0.5},
    )
    keys = Track("Keys", items=[Item(2.0, 8.0, [Take("Chords", midi=True)])], fx=["VSTi: ReaSynth (Cockos)"])
    fake.tracks = [drums, kick, keys]
    fake.markers = [(False, 0.0, 0.0, "Intro", 1, 0), (True, 8.0, 24.0, "Verse", 1, 0)]
