"""A small in-memory stand-in for REAPER's ReaScript API.

It runs the real Lua bridge inside Lupa (Lua 5.4), so tests exercise the
actual bridge code and the actual file transport without REAPER. Only the
functions the bridge uses are modelled, and only as far as the tests need.
Fixed tempo: 120 BPM in 4/4, so one quarter note is 0.5 s; 960 PPQ.
"""

from __future__ import annotations

import itertools
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from lupa import lua54

from reaper_mcp.paths import bundled_bridge_script

_guid = itertools.count(1)
PPQ = 960
SEC_PER_QN = 0.5


def new_guid() -> str:
    return "{%08X-0000-0000-0000-000000000000}" % next(_guid)


class Obj:
    def __str__(self) -> str:
        return f"{type(self).__name__.lower()}:{id(self):x}"


@dataclass(eq=False)
class FX(Obj):
    name: str
    enabled: bool = True
    offline: bool = False
    preset: str = ""
    # (name, normalized value, display function)
    params: list = field(default_factory=list)
    wet: float = 0.5


def fake_plugin(name: str) -> FX:
    if "ReaComp" in name:
        return FX(name, params=[
            ["Threshold", 1.0, lambda v: f"{-60 + 60 * v:.1f} dB"],
            ["Ratio", 0.1, lambda v: f"{1 + 19 * v:.1f}:1"],
            ["Detector input", 0.0, lambda v: "Main L+R" if v < 0.5 else "Aux L+R"],
        ])
    if "ReaEQ" in name:
        return FX(name, params=[
            ["Gain", 0.5, lambda v: f"{-24 + 48 * v:.1f} dB"],
            ["Frequency", 0.5, lambda v: f"{20 * 1000 ** v:.0f} Hz"],
            ["Mode", 0.0, lambda v: "Low shelf" if v < 0.5 else "Band"],
        ])
    return FX(name, params=[["Volume", 0.8, lambda v: f"{v * 100:.0f} %"]])


@dataclass(eq=False)
class Envelope(Obj):
    name: str
    points: list = field(default_factory=list)  # [time, value, shape, tension, selected]
    items: list = field(default_factory=list)  # [position, length, points]

    def pts(self, ai):
        return self.points if ai < 0 else self.items[ai][2]


@dataclass(eq=False)
class Send:
    src: Track
    dst: Track
    info: dict = field(default_factory=lambda: {
        "D_VOL": 1.0, "D_PAN": 0.0, "B_MUTE": 0.0, "I_SENDMODE": 0.0,
        "I_SRCCHAN": 0.0, "I_DSTCHAN": 0.0, "I_MIDIFLAGS": 0.0,
    })


ENV_DISPLAY = {"VOLENV2": "Volume", "PANENV2": "Pan", "WIDTHENV2": "Width", "MUTEENV": "Mute", "VOLENV3": "Trim Volume"}


@dataclass(eq=False)
class Take(Obj):
    name: str
    midi: bool = False
    source: str = ""
    guid: str = field(default_factory=new_guid)
    info: dict = field(default_factory=lambda: {"D_STARTOFFS": 0.0, "D_PLAYRATE": 1.0, "D_PITCH": 0.0})
    notes: list = field(default_factory=list)  # [sel, muted, start_ppq, end_ppq, chan, pitch, vel]
    cc: list = field(default_factory=list)  # [sel, muted, ppq, chanmsg, chan, msg2, msg3]
    reversed: bool = False
    item: Item | None = None


@dataclass(eq=False)
class Item(Obj):
    position: float
    length: float
    takes: list[Take] = field(default_factory=list)
    guid: str = field(default_factory=new_guid)
    selected: bool = False
    info: dict = field(default_factory=dict)
    track: Track | None = None

    def __post_init__(self) -> None:
        self.info = {
            "D_POSITION": self.position, "D_LENGTH": self.length, "B_MUTE": 0.0,
            "D_FADEINLEN": 0.01, "D_FADEOUTLEN": 0.01, "D_VOL": 1.0, "C_LOCK": 0.0, **self.info,
        }
        for t in self.takes:
            t.item = self

    @property
    def pos(self) -> float:
        return self.info["D_POSITION"]

    @property
    def len(self) -> float:
        return self.info["D_LENGTH"]


@dataclass(eq=False)
class Track(Obj):
    name: str
    items: list[Item] = field(default_factory=list)
    fx: list = field(default_factory=list)
    guid: str = field(default_factory=new_guid)
    info: dict = field(default_factory=dict)
    selected: bool = False
    envs: dict = field(default_factory=dict)  # chunk name -> Envelope
    fx_envs: dict = field(default_factory=dict)  # (fx, param) -> Envelope

    def __post_init__(self) -> None:
        self.info = {
            "I_NCHAN": 2.0, "I_AUTOMODE": 0.0, "I_FOLDERDEPTH": 0.0, "D_VOL": 1.0, "D_PAN": 0.0, "B_MUTE": 0.0, "I_SOLO": 0.0,
            "I_RECARM": 0.0, "I_RECINPUT": 0.0, "I_RECMODE": 0.0, "I_RECMON": 0.0,
            "B_MAINSEND": 1.0, "I_CUSTOMCOLOR": 0.0, "B_PHASE": 0.0, **self.info,
        }
        self.fx = [fake_plugin(f) if isinstance(f, str) else f for f in self.fx]
        for i in self.items:
            i.track = self


WET = 1000  # pseudo parameter index for the ":wet" ident

INSTALLED_FX = [
    ("VST: ReaEQ (Cockos)", "reaeq.dll"),
    ("VST: ReaComp (Cockos)", "reacomp.dll"),
    ("VSTi: ReaSynth (Cockos)", "reasynth.dll"),
    ("JS: Volume Adjustment", "volume"),
    ("VST: ReaVerbate (Cockos)", "reaverbate.dll"),
]


class FakeReaper:
    def __init__(self, resource_path: Path) -> None:
        self.resource_path = resource_path
        self.master = Track("MASTER")
        self.tracks: list[Track] = []
        self.markers: list[list] = []  # [isrgn, pos, end, name, id, color]
        self.undo: list[str] = []
        self.undo_depth = 0
        self.tempo = 120.0
        self.timesig = (4, 4)
        self.tempo_markers: list[list] = []
        self.cursor = 0.0
        self.play_state = 0
        self.repeat = 0
        self.time_sel = (0.0, 0.0)
        self.commands: list[int] = []
        self.sends: list[Send] = []

    # helpers
    def all_items(self) -> list[Item]:
        return [i for t in self.tracks for i in t.items]

    def selected_items(self) -> list[Item]:
        return [i for i in self.all_items() if i.selected]

    @staticmethod
    def qn_to_time(qn: float) -> float:
        return qn * SEC_PER_QN

    @staticmethod
    def time_to_qn(t: float) -> float:
        return t / SEC_PER_QN

    # --- environment ---
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
    def ShowConsoleMsg(self, msg): print(msg, end="")
    def defer(self, _fn): pass
    def atexit(self, _fn): pass
    def Undo_BeginBlock2(self, _proj): self.undo_depth += 1
    def Undo_EndBlock2(self, _proj, label, _flags):
        self.undo_depth -= 1
        self.undo.append(label)
    def PreventUIRefresh(self, _n): pass
    def UpdateArrange(self): pass
    def UpdateTimeline(self): pass
    def ColorToNative(self, r, g, b): return r | (g << 8) | (b << 16)
    def Main_OnCommand(self, cmd, _flag):
        self.commands.append(cmd)
        if cmd == 41051:  # toggle take reverse
            for item in self.selected_items():
                item.takes[0].reversed = not item.takes[0].reversed
        elif cmd == 41588:  # glue
            by_track: dict = {}
            for item in self.selected_items():
                by_track.setdefault(item.track, []).append(item)
            for tr, items in by_track.items():
                start = min(i.pos for i in items)
                end = max(i.pos + i.len for i in items)
                for i in items:
                    tr.items.remove(i)
                glued = Item(start, end - start, [Take("glued")], selected=True)
                glued.track = tr
                tr.items.append(glued)
        elif cmd == 1013:
            self.play_state = 5

    def ValidatePtr2(self, _proj, ptr, typename):
        kinds = {"MediaTrack*": Track, "MediaItem*": Item, "MediaItem_Take*": Take, "TrackEnvelope*": Envelope}
        cls = kinds.get(typename)
        return cls is not None and isinstance(ptr, cls)
    def ValidatePtr(self, ptr, typename): return self.ValidatePtr2(0, ptr, typename)

    # --- tracks ---
    def GetMasterTrack(self, _proj): return self.master
    def CountTracks(self, _proj): return len(self.tracks)
    def GetTrack(self, _proj, idx): return self.tracks[idx] if 0 <= idx < len(self.tracks) else None
    def InsertTrackAtIndex(self, idx, _defaults): self.tracks.insert(idx, Track(""))
    def DeleteTrack(self, tr): self.tracks.remove(tr)
    def GetTrackGUID(self, tr): return tr.guid
    def GetTrackName(self, tr): return (True, tr.name or f"Track {self.tracks.index(tr) + 1}")
    def GetMediaTrackInfo_Value(self, tr, key):
        if key == "IP_TRACKNUMBER":
            return -1.0 if tr is self.master else float(self.tracks.index(tr) + 1)
        return tr.info.get(key, 0.0)
    def SetMediaTrackInfo_Value(self, tr, key, value): tr.info[key] = float(value); return True
    def GetSetMediaTrackInfo_String(self, tr, key, value, is_set):
        if key == "P_NAME":
            if is_set:
                tr.name = value
            return (True, tr.name)
        return (False, "")
    def SetOnlyTrackSelected(self, tr):
        for t in self.tracks:
            t.selected = t is tr
    def ReorderSelectedTracks(self, before, _make_folder):
        sel = [t for t in self.tracks if t.selected]
        anchor = self.tracks[before] if before < len(self.tracks) else None
        for t in sel:
            self.tracks.remove(t)
        at = self.tracks.index(anchor) if anchor in self.tracks else len(self.tracks)
        self.tracks[at:at] = sel
        return True
    def IsProjectDirty(self, _proj): return 0

    # --- routing ---
    def _sends(self, tr, cat):
        if cat == 0:
            return [x for x in self.sends if x.src is tr]
        if cat == -1:
            return [x for x in self.sends if x.dst is tr]
        return []
    def GetTrackNumSends(self, tr, cat): return len(self._sends(tr, cat))
    def CreateTrackSend(self, src, dst):
        self.sends.append(Send(src, dst))
        return len(self._sends(src, 0)) - 1
    def RemoveTrackSend(self, tr, cat, i): self.sends.remove(self._sends(tr, cat)[i]); return True
    def GetTrackSendInfo_Value(self, tr, cat, i, key):
        snd = self._sends(tr, cat)[i]
        if key == "P_DESTTRACK":
            return snd.dst
        if key == "P_SRCTRACK":
            return snd.src
        return snd.info.get(key, 0.0)
    def SetTrackSendInfo_Value(self, tr, cat, i, key, v):
        self._sends(tr, cat)[i].info[key] = float(v)
        return True

    # --- envelopes ---
    def CountTrackEnvelopes(self, tr): return len(tr.envs) + len(tr.fx_envs)
    def GetTrackEnvelopeByChunkName(self, tr, name): return tr.envs.get(name.lstrip("<"))
    def GetTrackStateChunk(self, tr, _buf, _undo):
        envs = "".join(f"<{n}\nACT 1 -1\n>\n" for n in tr.envs)
        return (True, f"<TRACK\nNAME \"{tr.name}\"\n{envs}<FXCHAIN\nSHOW 0\n>\n<ITEM\n>\n>\n")
    def SetTrackStateChunk(self, tr, chunk, _undo):
        assert chunk.index("<FXCHAIN") > max(chunk.rfind("ENV"), 0), "envelopes must come before the FX chain"
        for name in re.findall(r"^<(\w*ENV\d?)$", chunk, re.M):
            tr.envs.setdefault(name, Envelope(ENV_DISPLAY.get(name, name)))
        return True
    def GetFXEnvelope(self, tr, fx, param, create):
        key = (tr.fx[fx], param)
        if key not in tr.fx_envs and create:
            tr.fx_envs[key] = Envelope(f"{tr.fx[fx].params[param][0]} / {tr.fx[fx].name}")
        return tr.fx_envs.get(key)
    def GetEnvelopeName(self, env, _buf=""): return (True, env.name)
    def GetEnvelopeScalingMode(self, env): return 1 if "Volume" in env.name else 0
    # Fake "fader scaling": a square-root curve, so conversions are visible in tests.
    def ScaleToEnvelopeMode(self, mode, v): return v ** 0.5 if mode == 1 else v
    def ScaleFromEnvelopeMode(self, mode, v): return v ** 2 if mode == 1 else v
    def InsertEnvelopePointEx(self, env, ai, t, v, shape, tension, sel, _nosort):
        env.pts(ai).append([t, v, shape, tension, sel])
        return True
    def DeleteEnvelopePointRangeEx(self, env, ai, a, b):
        pts = env.pts(ai)
        pts[:] = [p for p in pts if not (a <= p[0] < b)]
        return True
    def Envelope_SortPointsEx(self, env, ai): env.pts(ai).sort(key=lambda p: p[0]); return True
    def CountEnvelopePointsEx(self, env, ai): return len(env.pts(ai))
    def GetEnvelopePointEx(self, env, ai, i): return (True, *env.pts(ai)[i])
    def InsertAutomationItem(self, env, _pool, pos, length):
        env.items.append([pos, length, []])
        return len(env.items) - 1
    def CountAutomationItems(self, env): return len(env.items)
    def GetSetAutomationItemInfo(self, env, i, key, _v, _set):
        return {"D_POSITION": env.items[i][0], "D_LENGTH": env.items[i][1]}.get(key, 0.0)
    def CountTrackMediaItems(self, tr): return len(tr.items)
    def GetTrackMediaItem(self, tr, i): return tr.items[i] if 0 <= i < len(tr.items) else None

    # --- FX ---
    def TrackFX_GetCount(self, tr): return len(tr.fx)
    def TrackFX_GetFXName(self, tr, i, _buf=""): return (True, tr.fx[i].name)
    def TrackFX_GetEnabled(self, tr, i): return tr.fx[i].enabled
    def TrackFX_SetEnabled(self, tr, i, on): tr.fx[i].enabled = on
    def TrackFX_GetOffline(self, tr, i): return tr.fx[i].offline
    def TrackFX_SetOffline(self, tr, i, off): tr.fx[i].offline = off
    def TrackFX_GetInstrument(self, tr):
        return next((i for i, f in enumerate(tr.fx) if f.name.startswith("VSTi")), -1)
    def TrackFX_AddByName(self, tr, name, _rec, instantiate):
        match = next((n for n, _ in INSTALLED_FX if name.lower() in n.lower()), None)
        if match is None:
            return -1
        fx = fake_plugin(match)
        if instantiate <= -1000:
            pos = -1000 - instantiate
            tr.fx.insert(pos, fx)
            return pos
        tr.fx.append(fx)
        return len(tr.fx) - 1
    def TrackFX_Delete(self, tr, i): del tr.fx[i]; return True
    def TrackFX_CopyToTrack(self, src, i, dest, j, move):
        fx = src.fx.pop(i) if move else src.fx[i]
        dest.fx.insert(j, fx)
    def TrackFX_SetPreset(self, tr, i, name):
        if name == "missing":
            return False
        tr.fx[i].preset = name
        return True
    def TrackFX_GetPreset(self, tr, i, _buf=""): return (bool(tr.fx[i].preset), tr.fx[i].preset)
    def TrackFX_GetNumParams(self, tr, i): return len(tr.fx[i].params)
    def TrackFX_GetParamName(self, tr, i, p, _buf=""): return (True, tr.fx[i].params[p][0])
    def TrackFX_GetParamNormalized(self, tr, i, p): return tr.fx[i].params[p][1]
    def TrackFX_SetParamNormalized(self, tr, i, p, v):
        if p == WET:
            tr.fx[i].wet = float(v)
        else:
            tr.fx[i].params[p][1] = float(v)
        return True
    def TrackFX_GetParam(self, tr, i, p): return (tr.fx[i].params[p][1], 0.0, 1.0)
    def TrackFX_GetParamFromIdent(self, tr, i, ident): return WET if ident == ":wet" else -1
    def TrackFX_GetFormattedParamValue(self, tr, i, p, _buf=""):
        name, v, fmt = tr.fx[i].params[p]
        return (True, fmt(v))
    def TrackFX_FormatParamValueNormalized(self, tr, i, p, v, _buf=""):
        return (True, tr.fx[i].params[p][2](v))
    def EnumInstalledFX(self, i):
        if i >= len(INSTALLED_FX):
            return (False, "", "")
        return (True, *INSTALLED_FX[i])

    # --- items and takes ---
    def CountMediaItems(self, _proj): return len(self.all_items())
    def GetMediaItem(self, _proj, i):
        items = self.all_items()
        return items[i] if 0 <= i < len(items) else None
    def GetMediaItem_Track(self, item): return item.track
    def GetMediaItemInfo_Value(self, item, key): return item.info.get(key, 0.0)
    def SetMediaItemInfo_Value(self, item, key, value): item.info[key] = float(value); return True
    def GetSetMediaItemInfo_String(self, item, key, _v, _set):
        return (True, item.guid) if key == "GUID" else (False, "")
    def IsMediaItemSelected(self, item): return item.selected
    def SelectAllMediaItems(self, _proj, sel):
        for i in self.all_items():
            i.selected = sel
    def SetMediaItemSelected(self, item, sel): item.selected = sel
    def CountSelectedMediaItems(self, _proj): return len(self.selected_items())
    def GetSelectedMediaItem(self, _proj, i):
        sel = self.selected_items()
        return sel[i] if i < len(sel) else None
    def MoveMediaItemToTrack(self, item, tr):
        item.track.items.remove(item)
        tr.items.append(item)
        item.track = tr
        return True
    def DeleteTrackMediaItem(self, tr, item): tr.items.remove(item); return True
    def AddMediaItemToTrack(self, tr):
        item = Item(0.0, 1.0)
        item.track = tr
        tr.items.append(item)
        return item
    def SplitMediaItem(self, item, t):
        if not (item.pos < t < item.pos + item.len):
            return None
        right_len = item.pos + item.len - t
        right = Item(t, right_len, [Take(tk.name, tk.midi, tk.source) for tk in item.takes])
        right.track = item.track
        item.info["D_LENGTH"] = t - item.pos
        item.track.items.insert(item.track.items.index(item) + 1, right)
        return right
    def GetItemStateChunk(self, item, _buf, _undo):
        take = item.takes[0] if item.takes else None
        return (True, f"<ITEM\nPOSITION {item.pos}\nLENGTH {item.len}\nIGUID {item.guid}\nGUID {item.guid}\n"
                      f"NAME {take.name if take else ''}\n>")
    def SetItemStateChunk(self, item, chunk, _undo):
        assert "GUID" not in chunk, "duplicated chunk must not carry GUIDs"
        item.info["D_LENGTH"] = float(re.search(r"LENGTH (\S+)", chunk).group(1))
        name = re.search(r"NAME (.*)", chunk).group(1)
        item.takes = [Take(name)]
        item.takes[0].item = item
        return True
    def CountTakes(self, item): return len(item.takes)
    def GetTake(self, item, i): return item.takes[i] if 0 <= i < len(item.takes) else None
    def GetActiveTake(self, item): return item.takes[0] if item.takes else None
    def GetSetMediaItemTakeInfo_String(self, take, key, value, is_set):
        if key == "GUID":
            return (True, take.guid)
        if key == "P_NAME":
            if is_set:
                take.name = value
            return (True, take.name)
        return (False, "")
    def GetMediaItemTakeInfo_Value(self, take, key): return take.info.get(key, 0.0)
    def SetMediaItemTakeInfo_Value(self, take, key, value): take.info[key] = float(value); return True
    def GetMediaItemTake_Source(self, take): return take.source or None
    def GetMediaSourceFileName(self, src, _buf=""): return src
    def GetTakeName(self, take): return take.name
    def TakeIsMIDI(self, take): return take.midi
    def SetEditCurPos(self, t, _view, _seek): self.cursor = t
    def InsertMedia(self, file, _mode):
        tr = next(t for t in self.tracks if t.selected)
        item = Item(self.cursor, 2.0, [Take(os.path.basename(file), source=file)], selected=True)
        item.track = tr
        tr.items.append(item)
        return 1

    # --- MIDI (ppq counted from the item start) ---
    def CreateNewMIDIItemInProj(self, tr, start, end, _qn):
        item = Item(start, end - start, [Take("", midi=True)])
        item.track = tr
        tr.items.append(item)
        return item
    def _item_qn(self, take): return self.time_to_qn(take.item.pos)
    def MIDI_GetPPQPosFromProjQN(self, take, qn): return (qn - self._item_qn(take)) * PPQ
    def MIDI_GetPPQPosFromProjTime(self, take, t): return self.MIDI_GetPPQPosFromProjQN(take, self.time_to_qn(t))
    def MIDI_GetProjTimeFromPPQPos(self, take, ppq): return self.qn_to_time(ppq / PPQ + self._item_qn(take))
    def MIDI_CountEvts(self, take): return (len(take.notes) + len(take.cc), len(take.notes), len(take.cc), 0)
    def MIDI_GetNote(self, take, i): return (True, *take.notes[i])
    def MIDI_SetNote(self, take, i, sel, muted, a, b, chan, pitch, vel, _nosort):
        take.notes[i] = [sel, muted, a, b, chan, pitch, vel]
        return True
    def MIDI_InsertNote(self, take, sel, muted, a, b, chan, pitch, vel, _nosort):
        take.notes.append([sel, muted, a, b, chan, pitch, vel])
        return True
    def MIDI_DeleteNote(self, take, i): del take.notes[i]; return True
    def MIDI_Sort(self, take): take.notes.sort(key=lambda n: (n[2], n[5])); take.cc.sort(key=lambda c: c[2])
    def MIDI_GetCC(self, take, i): return (True, *take.cc[i])
    def MIDI_InsertCC(self, take, sel, muted, ppq, chanmsg, chan, msg2, msg3):
        take.cc.append([sel, muted, ppq, chanmsg, chan, msg2, msg3])
        return True
    def MIDI_SetItemExtents(self, item, qn_start, qn_end):
        item.info["D_POSITION"] = self.qn_to_time(qn_start)
        item.info["D_LENGTH"] = self.qn_to_time(qn_end) - self.qn_to_time(qn_start)
        return True

    # --- time, tempo, transport ---
    def TimeMap2_QNToTime(self, _proj, qn): return self.qn_to_time(qn)
    def TimeMap2_timeToQN(self, _proj, t): return self.time_to_qn(t)
    def TimeMap_GetMeasureInfo(self, _proj, measure):
        qn = measure * 4.0
        return (measure, qn, qn + 4, 4, 4, self.tempo)
    def TimeMap_GetTimeSigAtTime(self, _proj, _t): return (*self.timesig, self.tempo)
    def Master_GetTempo(self): return self.tempo
    def SetCurrentBPM(self, _proj, bpm, _undo): self.tempo = bpm
    def CountTempoTimeSigMarkers(self, _proj): return len(self.tempo_markers)
    def GetTempoTimeSigMarker(self, _proj, i): return (True, *self.tempo_markers[i])
    def SetTempoTimeSigMarker(self, _proj, idx, t, measure, beat, bpm, num, denom, linear):
        m = [t, measure, beat, bpm, num, denom, linear]
        if idx < 0:
            self.tempo_markers.append(m)
        else:
            self.tempo_markers[idx] = m
        self.tempo, self.timesig = bpm, (num, denom)
        return True
    def GetCursorPosition(self): return self.cursor
    def GetPlayPosition(self): return self.cursor
    def GetPlayState(self): return self.play_state
    def OnPlayButton(self): self.play_state = 1
    def OnStopButton(self): self.play_state = 0
    def OnPauseButton(self): self.play_state = 2
    def GetSetRepeat(self, v):
        if v >= 0:
            self.repeat = v
        return self.repeat
    def GetSet_LoopTimeRange(self, is_set, _loop, a, b, _seek):
        if is_set:
            self.time_sel = (a, b)
        return self.time_sel
    def GetProjectLength(self, _proj):
        return max((i.pos + i.len for i in self.all_items()), default=0.0)
    def GetSetProjectInfo(self, _proj, key, _v, _set): return 48000.0 if key == "PROJECT_SRATE" else 0.0

    # --- markers and regions ---
    def CountProjectMarkers(self, _proj):
        regions = sum(1 for m in self.markers if m[0])
        return (len(self.markers), len(self.markers) - regions, regions)
    def EnumProjectMarkers3(self, _proj, i):
        isrgn, pos, end, name, mid, color = self.markers[i]
        return (i + 1, isrgn, pos, end, name, mid, color)
    def AddProjectMarker2(self, _proj, isrgn, pos, end, name, want_id, color):
        mid = want_id if want_id >= 0 else 1 + max((m[4] for m in self.markers if m[0] == isrgn), default=0)
        self.markers.append([isrgn, pos, end, name, mid, color])
        self.markers.sort(key=lambda m: m[1])
        return mid
    def SetProjectMarker4(self, _proj, mid, isrgn, pos, end, name, color, flags):
        for m in self.markers:
            if m[4] == mid and m[0] == isrgn:
                m[1:4] = [pos, end, "" if flags & 1 else name]
                m[5] = color
                return True
        return False
    def DeleteProjectMarker(self, _proj, mid, isrgn):
        before = len(self.markers)
        self.markers = [m for m in self.markers if not (m[4] == mid and m[0] == isrgn)]
        return len(self.markers) < before


API_NAMES = [n for n in dir(FakeReaper) if n[0].isupper() or n in ("time_precise", "defer", "atexit")]


def load_bridge(fake: FakeReaper):
    """Load the real bridge script into a Lua runtime wired to `fake`. Returns (lua, M)."""
    lua = lua54.LuaRuntime(unpack_returned_tuples=True)
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
    lua.globals().reaper = make(fake, API_NAMES)
    lua.globals().REAPER_MCP_NO_START = True
    module = lua.execute(bundled_bridge_script().read_text(encoding="utf-8"))
    return lua, module


def demo_project(fake: FakeReaper) -> None:
    drums = Track("Drums", fx=["VST: ReaEQ (Cockos)"], info={"I_FOLDERDEPTH": 1.0})
    kick = Track(
        "Kick",
        items=[Item(0.0, 4.0, [Take("kick.wav", source="C:/audio/kick.wav")])],
        info={"I_FOLDERDEPTH": -1.0, "D_VOL": 0.5},
    )
    keys = Track("Keys", items=[Item(2.0, 8.0, [Take("Chords", midi=True)])], fx=["VSTi: ReaSynth (Cockos)"])
    fake.tracks = [drums, kick, keys]
    fake.markers = [[False, 0.0, 0.0, "Intro", 1, 0], [True, 8.0, 24.0, "Verse", 1, 0]]
