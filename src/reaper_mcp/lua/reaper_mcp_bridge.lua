-- reaper_mcp_bridge.lua
-- Runs inside REAPER and executes requests from the reaper-mcp Python server.
--
-- Transport: the server writes <id>.req.json into <resource>/reaper_mcp/requests,
-- this script answers with <id>.res.json in <resource>/reaper_mcp/responses.
-- A heartbeat file tells the server the bridge is alive.
--
-- Load once via Actions > Show action list > New action > Load ReaScript,
-- then run it. It keeps running in the background until stopped.

local BRIDGE_VERSION = "0.1.0"
local HEARTBEAT_INTERVAL = 1.0 -- seconds

local M = { version = BRIDGE_VERSION }

---------------------------------------------------------------------------
-- JSON
---------------------------------------------------------------------------

local json = {}
json.null = setmetatable({}, { __tostring = function() return "null" end })
local OBJECT_MT = { __jsontype = "object" }
local ARRAY_MT = { __jsontype = "array" }

-- Mark a table so it always encodes as an object / array, even when empty.
function json.object(t) return setmetatable(t or {}, OBJECT_MT) end
function json.array(t) return setmetatable(t or {}, ARRAY_MT) end

local escapes = {
  ['"'] = '\\"', ['\\'] = '\\\\', ['\b'] = '\\b', ['\f'] = '\\f',
  ['\n'] = '\\n', ['\r'] = '\\r', ['\t'] = '\\t',
}

local function encode_string(s)
  return '"' .. s:gsub('[%c"\\]', function(c)
    return escapes[c] or string.format("\\u%04x", c:byte())
  end) .. '"'
end

local function is_array(t)
  local mt = getmetatable(t)
  if mt == ARRAY_MT then return true end
  if mt == OBJECT_MT then return false end
  local n = 0
  for _ in pairs(t) do n = n + 1 end
  if n == 0 then return true end
  for i = 1, n do
    if t[i] == nil then return false end
  end
  return true
end

local encode_value

local function encode_table(t, seen)
  if seen[t] then error("cannot encode cyclic table") end
  seen[t] = true
  local out = {}
  if is_array(t) then
    local n = t.n or #t
    for i = 1, n do out[i] = encode_value(t[i], seen) end
    seen[t] = nil
    return "[" .. table.concat(out, ",") .. "]"
  end
  local keys = {}
  for k in pairs(t) do keys[#keys + 1] = tostring(k) end
  table.sort(keys)
  for _, k in ipairs(keys) do
    local v = t[k]
    if v == nil then v = t[tonumber(k)] end
    out[#out + 1] = encode_string(k) .. ":" .. encode_value(v, seen)
  end
  seen[t] = nil
  return "{" .. table.concat(out, ",") .. "}"
end

-- Hook for values JSON cannot represent (REAPER pointers). Set further down.
local encode_userdata = function(u) return encode_string(tostring(u)) end

encode_value = function(v, seen)
  local tv = type(v)
  if v == nil or v == json.null then return "null"
  elseif tv == "boolean" then return v and "true" or "false"
  elseif tv == "number" then
    if v ~= v or v == math.huge or v == -math.huge then return "null" end
    if math.type(v) == "integer" then return string.format("%d", v) end
    if v == math.floor(v) and math.abs(v) < 2^53 then return string.format("%.1f", v) end
    return string.format("%.17g", v)
  elseif tv == "string" then return encode_string(v)
  elseif tv == "table" then return encode_table(v, seen)
  elseif tv == "userdata" then return encode_value(encode_userdata(v), seen)
  end
  return encode_string(tostring(v))
end

function json.encode(v) return encode_value(v, {}) end

local function utf8_char(cp)
  return utf8.char(cp)
end

function json.decode(s)
  local pos = 1
  local function fail(msg) error(string.format("JSON error at %d: %s", pos, msg), 0) end
  local function skip()
    pos = s:find("[^ \t\r\n]", pos) or (#s + 1)
  end
  local parse

  local function parse_string()
    pos = pos + 1 -- opening quote
    local parts = {}
    while true do
      local c = s:sub(pos, pos)
      if c == "" then fail("unterminated string") end
      if c == '"' then pos = pos + 1; break end
      if c == "\\" then
        local e = s:sub(pos + 1, pos + 1)
        local map = { ['"'] = '"', ['\\'] = '\\', ['/'] = '/', b = '\b', f = '\f', n = '\n', r = '\r', t = '\t' }
        if map[e] then
          parts[#parts + 1] = map[e]; pos = pos + 2
        elseif e == "u" then
          local cp = tonumber(s:sub(pos + 2, pos + 5), 16)
          if not cp then fail("bad unicode escape") end
          pos = pos + 6
          if cp >= 0xD800 and cp <= 0xDBFF and s:sub(pos, pos + 1) == "\\u" then
            local lo = tonumber(s:sub(pos + 2, pos + 5), 16)
            if lo and lo >= 0xDC00 and lo <= 0xDFFF then
              cp = 0x10000 + (cp - 0xD800) * 0x400 + (lo - 0xDC00)
              pos = pos + 6
            end
          end
          parts[#parts + 1] = utf8_char(cp)
        else
          fail("bad escape")
        end
      else
        local stop = s:find('["\\]', pos) or (#s + 1)
        parts[#parts + 1] = s:sub(pos, stop - 1)
        pos = stop
      end
    end
    return table.concat(parts)
  end

  local function parse_number()
    local num = s:match("^-?%d+", pos)
    if not num then fail("bad number") end
    local frac = s:match("^%.%d+", pos + #num)
    if frac then num = num .. frac end
    local exp = s:match("^[eE][-+]?%d+", pos + #num)
    if exp then num = num .. exp end
    pos = pos + #num
    local is_int = not num:find("[.eE]")
    local v = is_int and math.tointeger(tonumber(num)) or tonumber(num)
    if v == nil then v = tonumber(num) end
    if v == nil then fail("bad number") end
    return v
  end

  parse = function()
    skip()
    local c = s:sub(pos, pos)
    if c == "{" then
      pos = pos + 1
      local t = json.object()
      skip()
      if s:sub(pos, pos) == "}" then pos = pos + 1; return t end
      while true do
        skip()
        if s:sub(pos, pos) ~= '"' then fail("expected key") end
        local k = parse_string()
        skip()
        if s:sub(pos, pos) ~= ":" then fail("expected ':'") end
        pos = pos + 1
        t[k] = parse()
        skip()
        local d = s:sub(pos, pos)
        pos = pos + 1
        if d == "}" then return t end
        if d ~= "," then fail("expected ',' or '}'") end
      end
    elseif c == "[" then
      pos = pos + 1
      local t = json.array()
      local n = 0
      skip()
      if s:sub(pos, pos) == "]" then pos = pos + 1; t.n = 0; return t end
      while true do
        n = n + 1
        t[n] = parse()
        skip()
        local d = s:sub(pos, pos)
        pos = pos + 1
        if d == "]" then t.n = n; return t end
        if d ~= "," then fail("expected ',' or ']'") end
      end
    elseif c == '"' then return parse_string()
    elseif c == "t" and s:sub(pos, pos + 3) == "true" then pos = pos + 4; return true
    elseif c == "f" and s:sub(pos, pos + 4) == "false" then pos = pos + 5; return false
    elseif c == "n" and s:sub(pos, pos + 3) == "null" then pos = pos + 4; return json.null
    elseif c:match("[-%d]") then return parse_number()
    end
    fail("unexpected character '" .. c .. "'")
  end

  local v = parse()
  skip()
  if pos <= #s then fail("trailing characters") end
  return v
end

M.json = json

---------------------------------------------------------------------------
-- REAPER object handles
--
-- REAPER pointers (tracks, items, takes, envelopes, projects) cannot cross
-- JSON, so they are sent as references:
--   {"$type": "MediaTrack", "$ptr": "<id>", "guid": "{...}"}
-- and accepted back in any of these forms:
--   {"$ptr": "<id>"}                          pointer seen earlier this session
--   {"$type": "MediaTrack", "guid": "{...}"}   track, item or take by GUID
--   {"$type": "MediaTrack", "index": 0}       track by 0-based index
--   {"$type": "MediaTrack", "master": true}   master track
--   {"$type": "MediaItem", "index": 0}        item by 0-based project index
--   {"$type": "ReaProject"}                   current project
---------------------------------------------------------------------------

local POINTER_TYPES = { "MediaTrack", "MediaItem", "MediaItem_Take", "TrackEnvelope", "ReaProject", "PCM_source" }
local handles = {}

local function ptr_id(u)
  return (tostring(u):gsub("^userdata:%s*", ""))
end

local function pointer_type(u)
  for _, t in ipairs(POINTER_TYPES) do
    local ok, valid = pcall(reaper.ValidatePtr2, 0, u, t .. "*")
    if ok and valid then return t end
  end
  for _, t in ipairs(POINTER_TYPES) do
    local ok, valid = pcall(reaper.ValidatePtr, u, t .. "*")
    if ok and valid then return t end
  end
  return nil
end

local function guid_of(u, t)
  if t == "MediaTrack" then return reaper.GetTrackGUID(u) end
  if t == "MediaItem" then
    local _, g = reaper.GetSetMediaItemInfo_String(u, "GUID", "", false); return g
  end
  if t == "MediaItem_Take" then
    local _, g = reaper.GetSetMediaItemTakeInfo_String(u, "GUID", "", false); return g
  end
  return nil
end

local function make_ref(u)
  local id = ptr_id(u)
  handles[id] = u
  local t = pointer_type(u)
  local ref = json.object({ ["$ptr"] = id, ["$type"] = t or "unknown" })
  if t then
    local ok, g = pcall(guid_of, u, t)
    if ok and g and g ~= "" then ref.guid = g end
  end
  return ref
end
encode_userdata = make_ref

local function all_tracks()
  local list = { reaper.GetMasterTrack(0) }
  for i = 0, reaper.CountTracks(0) - 1 do list[#list + 1] = reaper.GetTrack(0, i) end
  return list
end

local function find_by_guid(t, guid)
  if t == "MediaTrack" then
    for _, tr in ipairs(all_tracks()) do
      if reaper.GetTrackGUID(tr) == guid then return tr end
    end
  elseif t == "MediaItem" or t == "MediaItem_Take" then
    for i = 0, reaper.CountMediaItems(0) - 1 do
      local item = reaper.GetMediaItem(0, i)
      if t == "MediaItem" then
        if guid_of(item, "MediaItem") == guid then return item end
      else
        for k = 0, reaper.CountTakes(item) - 1 do
          local take = reaper.GetTake(item, k)
          if take and guid_of(take, "MediaItem_Take") == guid then return take end
        end
      end
    end
  end
  return nil
end

local function resolve_ref(ref)
  local id, t = ref["$ptr"], ref["$type"]
  if id then
    local u = handles[id]
    if u and (not t or t == "unknown" or pointer_type(u) == t) then
      if pointer_type(u) then return u end
    end
    if not ref.guid then error("stale or unknown object handle " .. tostring(id), 0) end
  end
  if ref.guid and t then
    local u = find_by_guid(t, ref.guid)
    if not u then error(t .. " with GUID " .. ref.guid .. " not found", 0) end
    return u
  end
  if t == "MediaTrack" then
    if ref.master then return reaper.GetMasterTrack(0) end
    if ref.index then
      local tr = reaper.GetTrack(0, ref.index)
      if not tr then error("no track at index " .. tostring(ref.index), 0) end
      return tr
    end
  elseif t == "MediaItem" and ref.index then
    local item = reaper.GetMediaItem(0, ref.index)
    if not item then error("no item at index " .. tostring(ref.index), 0) end
    return item
  elseif t == "ReaProject" then
    return reaper.EnumProjects(ref.index or -1)
  end
  error("cannot resolve object reference", 0)
end

-- Turn decoded JSON arguments into Lua call arguments.
local function to_lua_arg(v)
  if v == json.null then return nil end
  if type(v) == "table" then
    if getmetatable(v) == OBJECT_MT and (v["$ptr"] or v["$type"]) then return resolve_ref(v) end
    return v
  end
  return v
end

M.make_ref = make_ref
M.resolve_ref = resolve_ref

---------------------------------------------------------------------------
-- Commands
---------------------------------------------------------------------------

local commands = {}

local function with_undo(label, fn)
  if not label or label == "" then return fn() end
  reaper.Undo_BeginBlock2(0)
  reaper.PreventUIRefresh(1)
  local ok, res = pcall(fn)
  reaper.PreventUIRefresh(-1)
  reaper.UpdateArrange()
  reaper.Undo_EndBlock2(0, label, -1)
  if not ok then error(res, 0) end
  return res
end

function commands.ping()
  local name = reaper.GetProjectName(0, "")
  return json.object({
    bridge_version = BRIDGE_VERSION,
    reaper_version = reaper.GetAppVersion(),
    resource_path = reaper.GetResourcePath(),
    project = name,
  })
end

-- Generic ReaScript call: {"fn": "GetTrack", "args": [0, 0], "undo": "label"}
function commands.call(p)
  local name = p.fn
  if type(name) ~= "string" then error("'fn' must be a function name", 0) end
  name = name:gsub("^reaper%.", "")
  local f = reaper[name]
  if type(f) ~= "function" then error("unknown ReaScript function: " .. name, 0) end
  local raw = p.args or json.array()
  local n = raw.n or #raw
  local args = {}
  for i = 1, n do args[i] = to_lua_arg(raw[i]) end
  return with_undo(p.undo, function()
    local out = table.pack(f(table.unpack(args, 1, n)))
    return json.array(out)
  end)
end

local function db(v)
  if v <= 0 then return json.null end
  return 20 * math.log(v, 10)
end

local function track_info(tr, idx, with_items)
  local function num(k) return reaper.GetMediaTrackInfo_Value(tr, k) end
  local _, name = reaper.GetTrackName(tr)
  local fx = json.array()
  for i = 0, reaper.TrackFX_GetCount(tr) - 1 do
    local _, fxname = reaper.TrackFX_GetFXName(tr, i, "")
    fx[#fx + 1] = json.object({
      index = i, name = fxname,
      enabled = reaper.TrackFX_GetEnabled(tr, i),
      offline = reaper.TrackFX_GetOffline(tr, i),
    })
  end
  local info = json.object({
    index = idx,
    guid = reaper.GetTrackGUID(tr),
    name = name,
    folder_depth = math.tointeger(num("I_FOLDERDEPTH")) or num("I_FOLDERDEPTH"),
    volume_db = db(num("D_VOL")),
    pan = num("D_PAN"),
    mute = num("B_MUTE") == 1,
    solo = num("I_SOLO") ~= 0,
    rec_arm = num("I_RECARM") == 1,
    rec_input = math.tointeger(num("I_RECINPUT")),
    rec_mode = math.tointeger(num("I_RECMODE")),
    rec_monitor = math.tointeger(num("I_RECMON")),
    main_send = num("B_MAINSEND") == 1,
    color = math.tointeger(num("I_CUSTOMCOLOR")),
    instrument_fx = reaper.TrackFX_GetInstrument(tr),
    fx = fx,
    sends = reaper.GetTrackNumSends(tr, 0),
    receives = reaper.GetTrackNumSends(tr, -1),
    hw_outputs = reaper.GetTrackNumSends(tr, 1),
    envelopes = reaper.CountTrackEnvelopes(tr),
    item_count = reaper.CountTrackMediaItems(tr),
  })
  if with_items then
    local items = json.array()
    for i = 0, reaper.CountTrackMediaItems(tr) - 1 do
      local item = reaper.GetTrackMediaItem(tr, i)
      local function inum(k) return reaper.GetMediaItemInfo_Value(item, k) end
      local take = reaper.GetActiveTake(item)
      local entry = json.object({
        index = i,
        guid = guid_of(item, "MediaItem"),
        position = inum("D_POSITION"),
        length = inum("D_LENGTH"),
        mute = inum("B_MUTE") == 1,
        selected = reaper.IsMediaItemSelected(item),
        fade_in = inum("D_FADEINLEN"),
        fade_out = inum("D_FADEOUTLEN"),
        volume_db = db(inum("D_VOL")),
        takes = reaper.CountTakes(item),
      })
      if take then
        local src = reaper.GetMediaItemTake_Source(take)
        entry.take = json.object({
          name = reaper.GetTakeName(take),
          guid = guid_of(take, "MediaItem_Take"),
          midi = reaper.TakeIsMIDI(take),
          start_offset = reaper.GetMediaItemTakeInfo_Value(take, "D_STARTOFFS"),
          playrate = reaper.GetMediaItemTakeInfo_Value(take, "D_PLAYRATE"),
          source = src and reaper.GetMediaSourceFileName(src, "") or json.null,
        })
      end
      items[#items + 1] = entry
    end
    info.items = items
  end
  return info
end

function commands.project_state(p)
  local with_items = p.items ~= false
  local name = reaper.GetProjectName(0, "")
  local _, path = reaper.EnumProjects(-1, "")
  local num, denom, tempo = reaper.TimeMap_GetTimeSigAtTime(0, reaper.GetCursorPosition())
  local ts_start, ts_end = reaper.GetSet_LoopTimeRange(false, false, 0, 0, false)
  local play = reaper.GetPlayState()

  local tracks = json.array()
  for i = 0, reaper.CountTracks(0) - 1 do
    tracks[#tracks + 1] = track_info(reaper.GetTrack(0, i), i, with_items)
  end

  local markers, regions = json.array(), json.array()
  local _, nm, nr = reaper.CountProjectMarkers(0)
  for i = 0, nm + nr - 1 do
    local _, isrgn, pos, rgnend, mname, id, color = reaper.EnumProjectMarkers3(0, i)
    if isrgn then
      regions[#regions + 1] = json.object({ id = id, name = mname, start = pos, ["end"] = rgnend, color = color })
    else
      markers[#markers + 1] = json.object({ id = id, name = mname, position = pos, color = color })
    end
  end

  return json.object({
    project = json.object({
      name = name,
      path = path,
      tempo = tempo,
      time_signature = { num, denom },
      sample_rate = reaper.GetSetProjectInfo(0, "PROJECT_SRATE", 0, false),
      length = reaper.GetProjectLength(0),
      cursor = reaper.GetCursorPosition(),
      play_position = reaper.GetPlayPosition(),
      playing = play & 1 == 1,
      paused = play & 2 == 2,
      recording = play & 4 == 4,
      time_selection = ts_end > ts_start and { ts_start, ts_end } or json.null,
    }),
    master = track_info(reaper.GetMasterTrack(0), -1, false),
    tracks = tracks,
    markers = markers,
    regions = regions,
  })
end

---------------------------------------------------------------------------
-- Lookups shared by the editing commands
---------------------------------------------------------------------------

local function is_null(v) return v == nil or v == json.null end

local function track_name(tr)
  local _, name = reaper.GetTrackName(tr)
  return name
end

-- Track spec: 0-based index, "master", a GUID string, a track name, or an object reference.
local function get_track(spec)
  if is_null(spec) then error("a track is required", 0) end
  if type(spec) == "number" then
    local tr = reaper.GetTrack(0, math.tointeger(spec) or -1)
    if not tr then error("no track at index " .. tostring(spec), 0) end
    return tr
  end
  if type(spec) == "string" then
    if spec:lower() == "master" then return reaper.GetMasterTrack(0) end
    if spec:match("^{.*}$") then return resolve_ref({ ["$type"] = "MediaTrack", guid = spec }) end
    local found
    for i = 0, reaper.CountTracks(0) - 1 do
      local tr = reaper.GetTrack(0, i)
      if track_name(tr) == spec then
        if found then error("more than one track is named '" .. spec .. "'; use its index", 0) end
        found = tr
      end
    end
    if not found then error("no track named '" .. spec .. "'", 0) end
    return found
  end
  if type(spec) == "table" then return resolve_ref(spec) end
  error("bad track reference", 0)
end

local function track_index(tr)
  return math.tointeger(reaper.GetMediaTrackInfo_Value(tr, "IP_TRACKNUMBER")) - 1
end

-- Item spec: GUID string, {"track": t, "index": i}, or an object reference.
local function get_item(spec)
  if type(spec) == "string" then return resolve_ref({ ["$type"] = "MediaItem", guid = spec }) end
  if type(spec) == "table" and not is_null(spec.track) and not spec["$type"] then
    local tr = get_track(spec.track)
    local item = reaper.GetTrackMediaItem(tr, math.tointeger(spec.index or 0))
    if not item then error("track has no item at index " .. tostring(spec.index), 0) end
    return item
  end
  if type(spec) == "table" then return resolve_ref(spec) end
  error("bad item reference: use the item's GUID or {track, index}", 0)
end

local function get_items(specs)
  local out = {}
  for i = 1, (specs.n or #specs) do out[i] = get_item(specs[i]) end
  return out
end

local function get_take(spec)
  local item = get_item(spec)
  local take
  if type(spec) == "table" and spec.take then take = reaper.GetTake(item, spec.take)
  else take = reaper.GetActiveTake(item) end
  if not take then error("item has no take", 0) end
  return take, item
end

local function item_guid(item) return guid_of(item, "MediaItem") end

local function item_summary(item)
  local take = reaper.GetActiveTake(item)
  return json.object({
    guid = item_guid(item),
    track = track_index(reaper.GetMediaItem_Track(item)),
    position = reaper.GetMediaItemInfo_Value(item, "D_POSITION"),
    length = reaper.GetMediaItemInfo_Value(item, "D_LENGTH"),
    name = take and reaper.GetTakeName(take) or json.null,
    midi = take and reaper.TakeIsMIDI(take) or false,
  })
end

-- Time spec: seconds, {"beats": qn from project start}, or {"bar": n, "beat": b} (1-based).
local function to_time(spec)
  if is_null(spec) then return nil end
  if type(spec) == "number" then return spec end
  if type(spec) == "table" then
    if spec.seconds then return spec.seconds end
    if spec.beats then return reaper.TimeMap2_QNToTime(0, spec.beats) end
    if spec.bar then
      local _, qn_start, _, _, denom = reaper.TimeMap_GetMeasureInfo(0, math.tointeger(spec.bar) - 1)
      local beat = (spec.beat or 1) - 1
      return reaper.TimeMap2_QNToTime(0, qn_start + beat * 4 / denom)
    end
  end
  error("bad time: use seconds, {beats} or {bar, beat}", 0)
end

local function to_qn(t) return reaper.TimeMap2_timeToQN(0, t) end

local function db_to_gain(v) return 10 ^ (v / 20) end

local function select_only_items(items)
  reaper.SelectAllMediaItems(0, false)
  for _, item in ipairs(items) do reaper.SetMediaItemSelected(item, true) end
end

local function run(p, label, fn)
  return with_undo(p.undo or label, fn)
end

---------------------------------------------------------------------------
-- Tracks
---------------------------------------------------------------------------

local function apply_track_props(tr, p)
  if not is_null(p.name) then reaper.GetSetMediaTrackInfo_String(tr, "P_NAME", p.name, true) end
  if not is_null(p.volume_db) then reaper.SetMediaTrackInfo_Value(tr, "D_VOL", db_to_gain(p.volume_db)) end
  if not is_null(p.pan) then reaper.SetMediaTrackInfo_Value(tr, "D_PAN", p.pan) end
  if not is_null(p.mute) then reaper.SetMediaTrackInfo_Value(tr, "B_MUTE", p.mute and 1 or 0) end
  if not is_null(p.solo) then reaper.SetMediaTrackInfo_Value(tr, "I_SOLO", p.solo and 2 or 0) end
  if not is_null(p.phase_invert) then reaper.SetMediaTrackInfo_Value(tr, "B_PHASE", p.phase_invert and 1 or 0) end
  if not is_null(p.color) then
    local c = p.color
    if type(c) == "string" then
      local r, g, b = c:match("^#?(%x%x)(%x%x)(%x%x)$")
      if not r then error("color must be '#rrggbb'", 0) end
      reaper.SetMediaTrackInfo_Value(tr, "I_CUSTOMCOLOR",
        reaper.ColorToNative(tonumber(r, 16), tonumber(g, 16), tonumber(b, 16)) | 0x1000000)
    else
      reaper.SetMediaTrackInfo_Value(tr, "I_CUSTOMCOLOR", 0)
    end
  end
  if not is_null(p.folder_depth) then reaper.SetMediaTrackInfo_Value(tr, "I_FOLDERDEPTH", p.folder_depth) end
  if not is_null(p.main_send) then reaper.SetMediaTrackInfo_Value(tr, "B_MAINSEND", p.main_send and 1 or 0) end
end

local function move_track(tr, to_index)
  reaper.SetOnlyTrackSelected(tr)
  local before = math.tointeger(to_index)
  if before > track_index(tr) then before = before + 1 end
  reaper.ReorderSelectedTracks(before, 0)
end

function commands.create_track(p)
  return run(p, "Create track", function()
    local idx = math.tointeger(p.index) or reaper.CountTracks(0)
    reaper.InsertTrackAtIndex(idx, true)
    local tr = reaper.GetTrack(0, idx)
    apply_track_props(tr, p)
    return json.object({ index = idx, guid = reaper.GetTrackGUID(tr) })
  end)
end

function commands.update_track(p)
  local tr = get_track(p.track)
  return run(p, "Edit track", function()
    apply_track_props(tr, p)
    if not is_null(p.move_to) then move_track(tr, p.move_to) end
    return track_info(tr, track_index(tr), false)
  end)
end

function commands.delete_tracks(p)
  local tracks = {}
  for i = 1, (p.tracks.n or #p.tracks) do tracks[i] = get_track(p.tracks[i]) end
  return run(p, "Delete tracks", function()
    for _, tr in ipairs(tracks) do reaper.DeleteTrack(tr) end
    return #tracks
  end)
end

-- Put a contiguous run of tracks under a new folder track.
function commands.create_folder(p)
  local list = {}
  for i = 1, (p.tracks.n or #p.tracks) do list[i] = track_index(get_track(p.tracks[i])) end
  table.sort(list)
  for i = 2, #list do
    if list[i] ~= list[i - 1] + 1 then error("folder children must be adjacent tracks", 0) end
  end
  return run(p, "Create folder", function()
    local first, last = list[1], list[#list]
    reaper.InsertTrackAtIndex(first, true)
    local folder = reaper.GetTrack(0, first)
    apply_track_props(folder, { name = p.name, color = p.color })
    reaper.SetMediaTrackInfo_Value(folder, "I_FOLDERDEPTH", 1)
    local last_child = reaper.GetTrack(0, last + 1)
    local depth = reaper.GetMediaTrackInfo_Value(last_child, "I_FOLDERDEPTH")
    reaper.SetMediaTrackInfo_Value(last_child, "I_FOLDERDEPTH", depth - 1)
    return json.object({ index = first, guid = reaper.GetTrackGUID(folder) })
  end)
end

-- Record input spec: {"type": "audio", "channel": 1, "stereo": false}
--                    {"type": "midi", "device": "all"|n, "channel": 0 (all) .. 16}
--                    {"type": "none"}
local function rec_input_value(spec)
  if spec.type == "none" then return -1 end
  if spec.type == "audio" then
    local ch = math.tointeger(spec.channel or 1) - 1
    if spec.stereo then return 1024 + ch end
    return ch
  end
  if spec.type == "midi" then
    local dev = (spec.device == nil or spec.device == "all") and 63 or math.tointeger(spec.device)
    local chan = math.tointeger(spec.channel or 0)
    return 4096 + (dev << 5) + chan
  end
  error("input type must be audio, midi or none", 0)
end

local MONITOR = { off = 0, on = 1, auto = 2 }
local REC_MODE = { input = 0, output = 1, none = 2, midi_overdub = 7, midi_replace = 8 }

local function apply_recording(tr, p)
  if not is_null(p.input) then reaper.SetMediaTrackInfo_Value(tr, "I_RECINPUT", rec_input_value(p.input)) end
  if not is_null(p.monitor) then
    local m = MONITOR[p.monitor]
    if not m then error("monitor must be off, on or auto", 0) end
    reaper.SetMediaTrackInfo_Value(tr, "I_RECMON", m)
  end
  if not is_null(p.mode) then
    local m = REC_MODE[p.mode]
    if not m then error("mode must be input, output, none, midi_overdub or midi_replace", 0) end
    reaper.SetMediaTrackInfo_Value(tr, "I_RECMODE", m)
  end
  if not is_null(p.arm) then reaper.SetMediaTrackInfo_Value(tr, "I_RECARM", p.arm and 1 or 0) end
end

function commands.setup_recording(p)
  local tr = get_track(p.track)
  return run(p, "Set up track for recording", function()
    apply_recording(tr, p)
    return track_info(tr, track_index(tr), false)
  end)
end

function commands.create_instrument_track(p)
  return run(p, "Create instrument track", function()
    local idx = math.tointeger(p.index) or reaper.CountTracks(0)
    reaper.InsertTrackAtIndex(idx, true)
    local tr = reaper.GetTrack(0, idx)
    apply_track_props(tr, { name = p.name or p.instrument, color = p.color })
    local fx = reaper.TrackFX_AddByName(tr, p.instrument, false, -1)
    if fx < 0 then
      reaper.DeleteTrack(tr)
      error("instrument not found: " .. p.instrument .. ". Use list_installed_fx to find the exact name.", 0)
    end
    apply_recording(tr, {
      input = p.input or { type = "midi", device = "all", channel = 0 },
      monitor = p.monitor or "on",
      arm = p.arm,
    })
    return json.object({ index = idx, guid = reaper.GetTrackGUID(tr), fx_index = fx })
  end)
end

---------------------------------------------------------------------------
-- Media items
---------------------------------------------------------------------------

function commands.split_item(p)
  local item = get_item(p.item)
  local times = p.at
  if getmetatable(times) ~= ARRAY_MT then times = { times } end
  local list = {}
  for i = 1, (times.n or #times) do list[i] = to_time(times[i]) end
  table.sort(list, function(a, b) return a > b end)
  return run(p, "Split item", function()
    local pieces = {}
    for _, t in ipairs(list) do
      local right = reaper.SplitMediaItem(item, t)
      if right then table.insert(pieces, 1, right) end
    end
    table.insert(pieces, 1, item)
    local out = json.array()
    for i, it in ipairs(pieces) do out[i] = item_summary(it) end
    return out
  end)
end

function commands.edit_item(p)
  local item = get_item(p.item)
  return run(p, "Edit item", function()
    local function set(k, v) reaper.SetMediaItemInfo_Value(item, k, v) end
    if not is_null(p.track) then reaper.MoveMediaItemToTrack(item, get_track(p.track)) end
    -- Trimming keeps the audio in place: moving the left edge shifts the take offset too.
    if not is_null(p.start) or not is_null(p["end"]) then
      local pos = reaper.GetMediaItemInfo_Value(item, "D_POSITION")
      local len = reaper.GetMediaItemInfo_Value(item, "D_LENGTH")
      local new_start = to_time(p.start) or pos
      local new_end = to_time(p["end"]) or (pos + len)
      if new_end <= new_start then error("end must be after start", 0) end
      local delta = new_start - pos
      for k = 0, reaper.CountTakes(item) - 1 do
        local take = reaper.GetTake(item, k)
        if take then
          local rate = reaper.GetMediaItemTakeInfo_Value(take, "D_PLAYRATE")
          local offs = reaper.GetMediaItemTakeInfo_Value(take, "D_STARTOFFS")
          reaper.SetMediaItemTakeInfo_Value(take, "D_STARTOFFS", offs + delta * rate)
        end
      end
      set("D_POSITION", new_start)
      set("D_LENGTH", new_end - new_start)
    end
    if not is_null(p.position) then set("D_POSITION", to_time(p.position)) end
    if not is_null(p.mute) then set("B_MUTE", p.mute and 1 or 0) end
    if not is_null(p.volume_db) then set("D_VOL", db_to_gain(p.volume_db)) end
    if not is_null(p.fade_in) then set("D_FADEINLEN", p.fade_in) end
    if not is_null(p.fade_out) then set("D_FADEOUTLEN", p.fade_out) end
    if not is_null(p.lock) then set("C_LOCK", p.lock and 1 or 0) end
    local take = reaper.GetActiveTake(item)
    if take then
      if not is_null(p.name) then reaper.GetSetMediaItemTakeInfo_String(take, "P_NAME", p.name, true) end
      if not is_null(p.pitch) then reaper.SetMediaItemTakeInfo_Value(take, "D_PITCH", p.pitch) end
      if not is_null(p.playrate) then reaper.SetMediaItemTakeInfo_Value(take, "D_PLAYRATE", p.playrate) end
    end
    return item_summary(item)
  end)
end

function commands.delete_items(p)
  local items = get_items(p.items)
  return run(p, "Delete items", function()
    for _, item in ipairs(items) do
      reaper.DeleteTrackMediaItem(reaper.GetMediaItem_Track(item), item)
    end
    return #items
  end)
end

local CMD_TAKE_REVERSE = 41051 -- Item properties: Toggle take reverse
local CMD_GLUE = 41588         -- Item: Glue items

local function reverse_midi(take, item)
  local _, notes = reaper.MIDI_CountEvts(take)
  local item_start = reaper.GetMediaItemInfo_Value(item, "D_POSITION")
  local item_end = item_start + reaper.GetMediaItemInfo_Value(item, "D_LENGTH")
  local s = reaper.MIDI_GetPPQPosFromProjTime(take, item_start)
  local e = reaper.MIDI_GetPPQPosFromProjTime(take, item_end)
  for i = 0, notes - 1 do
    local _, sel, muted, a, b, chan, pitch, vel = reaper.MIDI_GetNote(take, i)
    reaper.MIDI_SetNote(take, i, sel, muted, s + e - b, s + e - a, chan, pitch, vel, true)
  end
  reaper.MIDI_Sort(take)
end

function commands.reverse_items(p)
  local items = get_items(p.items)
  return run(p, "Reverse items", function()
    local audio = {}
    for _, item in ipairs(items) do
      local take = reaper.GetActiveTake(item)
      if take and reaper.TakeIsMIDI(take) then reverse_midi(take, item)
      elseif take then audio[#audio + 1] = item end
    end
    if #audio > 0 then
      select_only_items(audio)
      reaper.Main_OnCommand(CMD_TAKE_REVERSE, 0)
    end
    return #items
  end)
end

function commands.glue_items(p)
  local items = get_items(p.items)
  return run(p, "Glue items", function()
    select_only_items(items)
    reaper.Main_OnCommand(CMD_GLUE, 0)
    local out = json.array()
    for i = 0, reaper.CountSelectedMediaItems(0) - 1 do
      out[#out + 1] = item_summary(reaper.GetSelectedMediaItem(0, i))
    end
    return out
  end)
end

function commands.duplicate_item(p)
  local item = get_item(p.item)
  return run(p, "Duplicate item", function()
    local tr = is_null(p.track) and reaper.GetMediaItem_Track(item) or get_track(p.track)
    local _, chunk = reaper.GetItemStateChunk(item, "", false)
    chunk = chunk:gsub("\n%s*[IP]?GUID {[^}]*}", "")
    local count = math.tointeger(p.count or 1)
    local pos = reaper.GetMediaItemInfo_Value(item, "D_POSITION")
    local len = reaper.GetMediaItemInfo_Value(item, "D_LENGTH")
    local out = json.array()
    for i = 1, count do
      local copy = reaper.AddMediaItemToTrack(tr)
      reaper.SetItemStateChunk(copy, chunk, false)
      local at = to_time(p.position)
      if at then at = at + (i - 1) * len else at = pos + i * len end
      reaper.SetMediaItemInfo_Value(copy, "D_POSITION", at)
      out[#out + 1] = item_summary(copy)
    end
    return out
  end)
end

function commands.insert_media(p)
  local tr = get_track(p.track)
  return run(p, "Insert media", function()
    reaper.SetOnlyTrackSelected(tr)
    reaper.SelectAllMediaItems(0, false)
    reaper.SetEditCurPos(to_time(p.position) or 0, false, false)
    local ok = reaper.InsertMedia(p.file, 0)
    local item = reaper.GetSelectedMediaItem(0, 0)
    if ok == 0 or not item then error("could not insert " .. tostring(p.file), 0) end
    return item_summary(item)
  end)
end

---------------------------------------------------------------------------
-- MIDI
---------------------------------------------------------------------------

function commands.create_midi_item(p)
  local tr = get_track(p.track)
  return run(p, "Create MIDI item", function()
    local s, e = to_time(p.start), to_time(p["end"])
    if not s or not e or e <= s then error("start and end are required, with end after start", 0) end
    local item = reaper.CreateNewMIDIItemInProj(tr, s, e, false)
    local take = reaper.GetActiveTake(item)
    if not is_null(p.name) then reaper.GetSetMediaItemTakeInfo_String(take, "P_NAME", p.name, true) end
    return item_summary(item)
  end)
end

local function note_out(take, i)
  local _, sel, muted, a, b, chan, pitch, vel = reaper.MIDI_GetNote(take, i)
  local ta = reaper.MIDI_GetProjTimeFromPPQPos(take, a)
  local tb = reaper.MIDI_GetProjTimeFromPPQPos(take, b)
  return json.object({
    index = i, pitch = pitch, velocity = vel, channel = chan + 1,
    start_beats = to_qn(ta), length_beats = to_qn(tb) - to_qn(ta),
    start = ta, ["end"] = tb, muted = muted, selected = sel,
  })
end

function commands.get_midi(p)
  local take, item = get_take(p.item)
  if not reaper.TakeIsMIDI(take) then error("item is not MIDI", 0) end
  local _, notes, ccs = reaper.MIDI_CountEvts(take)
  local out_notes, out_cc = json.array(), json.array()
  for i = 0, notes - 1 do out_notes[#out_notes + 1] = note_out(take, i) end
  if p.cc ~= false then
    for i = 0, ccs - 1 do
      local _, _, muted, ppq, chanmsg, chan, msg2, msg3 = reaper.MIDI_GetCC(take, i)
      local t = reaper.MIDI_GetProjTimeFromPPQPos(take, ppq)
      local e = json.object({ index = i, channel = chan + 1, time = t, beats = to_qn(t), muted = muted })
      if chanmsg == 0xB0 then e.type = "cc"; e.cc = msg2; e.value = msg3
      elseif chanmsg == 0xE0 then e.type = "pitchbend"; e.value = (msg3 << 7 | msg2) - 8192
      elseif chanmsg == 0xC0 then e.type = "program"; e.value = msg2
      elseif chanmsg == 0xD0 then e.type = "aftertouch"; e.value = msg2
      else e.type = string.format("0x%02X", chanmsg); e.msg2 = msg2; e.msg3 = msg3 end
      out_cc[#out_cc + 1] = e
    end
  end
  local summary = item_summary(item)
  summary.start_beats = to_qn(summary.position)
  summary.notes = out_notes
  summary.cc = out_cc
  return summary
end

-- Note time: start_beats/length_beats (quarter notes, from item start when relative=true,
-- else from project start), or start/end in seconds.
local function note_ppq(take, item, n, relative)
  local base = relative and to_qn(reaper.GetMediaItemInfo_Value(item, "D_POSITION")) or 0
  local a, b
  if not is_null(n.start_beats) then
    a = reaper.MIDI_GetPPQPosFromProjQN(take, base + n.start_beats)
    b = reaper.MIDI_GetPPQPosFromProjQN(take, base + n.start_beats + (n.length_beats or 1))
  elseif not is_null(n.start) then
    a = reaper.MIDI_GetPPQPosFromProjTime(take, to_time(n.start))
    b = reaper.MIDI_GetPPQPosFromProjTime(take, to_time(n["end"]))
  else
    error("each note needs start_beats or start", 0)
  end
  if b <= a then error("note end must be after start", 0) end
  return a, b
end

function commands.add_midi_notes(p)
  local take, item = get_take(p.item)
  if not reaper.TakeIsMIDI(take) then error("item is not MIDI", 0) end
  local relative = p.relative ~= false
  return run(p, "Add MIDI notes", function()
    local latest = 0
    for i = 1, (p.notes.n or #p.notes) do
      local n = p.notes[i]
      local a, b = note_ppq(take, item, n, relative)
      local pitch = math.tointeger(n.pitch)
      if not pitch or pitch < 0 or pitch > 127 then error("pitch must be 0-127", 0) end
      reaper.MIDI_InsertNote(take, false, n.muted == true, a, b,
        math.tointeger(n.channel or 1) - 1, pitch, math.tointeger(n.velocity or 100), true)
      latest = math.max(latest, reaper.MIDI_GetProjTimeFromPPQPos(take, b))
    end
    reaper.MIDI_Sort(take)
    -- Grow the item so no note is cut off.
    local pos = reaper.GetMediaItemInfo_Value(item, "D_POSITION")
    if p.extend_item ~= false and latest > pos + reaper.GetMediaItemInfo_Value(item, "D_LENGTH") then
      reaper.MIDI_SetItemExtents(item, to_qn(pos), to_qn(latest))
    end
    local _, count = reaper.MIDI_CountEvts(take)
    return json.object({ added = p.notes.n or #p.notes, total_notes = count })
  end)
end

function commands.delete_midi_notes(p)
  local take = get_take(p.item)
  return run(p, "Delete MIDI notes", function()
    local _, count = reaper.MIDI_CountEvts(take)
    local doomed = {}
    if p.all then
      for i = 0, count - 1 do doomed[#doomed + 1] = i end
    elseif p.indices then
      for i = 1, (p.indices.n or #p.indices) do doomed[#doomed + 1] = math.tointeger(p.indices[i]) end
    else
      error("pass indices or all=true", 0)
    end
    table.sort(doomed, function(a, b) return a > b end)
    for _, i in ipairs(doomed) do reaper.MIDI_DeleteNote(take, i) end
    reaper.MIDI_Sort(take)
    return #doomed
  end)
end

function commands.add_midi_cc(p)
  local take, item = get_take(p.item)
  local relative = p.relative ~= false
  local base = relative and to_qn(reaper.GetMediaItemInfo_Value(item, "D_POSITION")) or 0
  return run(p, "Add MIDI CC", function()
    for i = 1, (p.events.n or #p.events) do
      local e = p.events[i]
      local ppq
      if not is_null(e.beats) then ppq = reaper.MIDI_GetPPQPosFromProjQN(take, base + e.beats)
      else ppq = reaper.MIDI_GetPPQPosFromProjTime(take, to_time(e.time)) end
      local chan = math.tointeger(e.channel or 1) - 1
      local kind = e.type or "cc"
      if kind == "cc" then
        reaper.MIDI_InsertCC(take, false, false, ppq, 0xB0, chan, math.tointeger(e.cc), math.tointeger(e.value))
      elseif kind == "pitchbend" then
        local v = math.tointeger(e.value) + 8192
        reaper.MIDI_InsertCC(take, false, false, ppq, 0xE0, chan, v & 0x7F, (v >> 7) & 0x7F)
      elseif kind == "program" then
        reaper.MIDI_InsertCC(take, false, false, ppq, 0xC0, chan, math.tointeger(e.value), 0)
      else
        error("event type must be cc, pitchbend or program", 0)
      end
    end
    reaper.MIDI_Sort(take)
    return p.events.n or #p.events
  end)
end

---------------------------------------------------------------------------
-- FX
---------------------------------------------------------------------------

local function fx_target(p)
  local tr = get_track(p.track)
  local fx = math.tointeger(p.fx)
  if fx == nil and type(p.fx) == "string" then
    for i = 0, reaper.TrackFX_GetCount(tr) - 1 do
      local _, name = reaper.TrackFX_GetFXName(tr, i, "")
      if name == p.fx or name:lower():find(p.fx:lower(), 1, true) then fx = i; break end
    end
    if fx == nil then error("no FX matching '" .. p.fx .. "' on this track", 0) end
  end
  if fx and (fx < 0 or fx >= reaper.TrackFX_GetCount(tr)) then error("no FX at index " .. fx, 0) end
  return tr, fx
end

local function param_info(tr, fx, i)
  local _, name = reaper.TrackFX_GetParamName(tr, fx, i, "")
  local _, shown = reaper.TrackFX_GetFormattedParamValue(tr, fx, i, "")
  return json.object({
    index = i, name = name,
    value = reaper.TrackFX_GetParamNormalized(tr, fx, i),
    display = shown,
  })
end

local function find_param(tr, fx, spec)
  local n = reaper.TrackFX_GetNumParams(tr, fx)
  local i = math.tointeger(spec)
  if i then
    if i < 0 or i >= n then error("no parameter " .. i, 0) end
    return i
  end
  local want, partial = tostring(spec):lower(), nil
  for k = 0, n - 1 do
    local _, name = reaper.TrackFX_GetParamName(tr, fx, k, "")
    if name:lower() == want then return k end
    if not partial and name:lower():find(want, 1, true) then partial = k end
  end
  if partial then return partial end
  error("no parameter matching '" .. tostring(spec) .. "'", 0)
end

function commands.add_fx(p)
  local tr = get_track(p.track)
  return run(p, "Add FX", function()
    local pos = is_null(p.position) and -1 or (-1000 - math.tointeger(p.position))
    local fx = reaper.TrackFX_AddByName(tr, p.name, false, pos)
    if fx < 0 then error("FX not found: " .. p.name .. ". Use list_installed_fx to find the exact name.", 0) end
    if not is_null(p.preset) then reaper.TrackFX_SetPreset(tr, fx, p.preset) end
    local _, name = reaper.TrackFX_GetFXName(tr, fx, "")
    return json.object({ index = fx, name = name, params = reaper.TrackFX_GetNumParams(tr, fx) })
  end)
end

function commands.remove_fx(p)
  local tr, fx = fx_target(p)
  return run(p, "Remove FX", function()
    reaper.TrackFX_Delete(tr, fx)
    return true
  end)
end

function commands.update_fx(p)
  local tr, fx = fx_target(p)
  return run(p, "Edit FX", function()
    if not is_null(p.enabled) then reaper.TrackFX_SetEnabled(tr, fx, p.enabled) end
    if not is_null(p.offline) then reaper.TrackFX_SetOffline(tr, fx, p.offline) end
    if not is_null(p.preset) then
      if not reaper.TrackFX_SetPreset(tr, fx, p.preset) then error("preset not found: " .. p.preset, 0) end
    end
    if not is_null(p.move_to) then reaper.TrackFX_CopyToTrack(tr, fx, tr, math.tointeger(p.move_to), true) end
    return true
  end)
end

function commands.get_fx_params(p)
  local tr, fx = fx_target(p)
  local _, name = reaper.TrackFX_GetFXName(tr, fx, "")
  local _, preset = reaper.TrackFX_GetPreset(tr, fx, "")
  local params = json.array()
  local filter = not is_null(p.filter) and p.filter:lower() or nil
  for i = 0, reaper.TrackFX_GetNumParams(tr, fx) - 1 do
    local info = param_info(tr, fx, i)
    if not filter or info.name:lower():find(filter, 1, true) then params[#params + 1] = info end
  end
  return json.object({ index = fx, name = name, preset = preset, enabled = reaper.TrackFX_GetEnabled(tr, fx), params = params })
end

-- Find the normalized value whose display text reads `target` (e.g. -6 for "-6.0 dB").
-- Assumes the display rises or falls monotonically with the value, as it does for most plug-ins.
local function solve_display(tr, fx, i, target)
  local function shown(v)
    local _, s = reaper.TrackFX_FormatParamValueNormalized(tr, fx, i, v, "")
    local num = tonumber((s or ""):match("[-+]?%d+%.?%d*"))
    if s and s:lower():find("inf") then num = s:find("-") and -math.huge or math.huge end
    return num
  end
  local lo, hi = 0.0, 1.0
  local vlo, vhi = shown(lo), shown(hi)
  if not vlo or not vhi then error("this parameter's display is not numeric; set value (0-1) instead", 0) end
  local rising = vhi >= vlo
  for _ = 1, 40 do
    local mid = (lo + hi) / 2
    local v = shown(mid)
    if v == nil then break end
    if (v < target) == rising then lo = mid else hi = mid end
  end
  -- The display is rounded, so pick whichever end of the bracket reads closest to the target.
  local dlo, dhi = math.abs((shown(lo) or math.huge) - target), math.abs((shown(hi) or math.huge) - target)
  return dhi <= dlo and hi or lo
end

function commands.set_fx_params(p)
  local tr, fx = fx_target(p)
  return run(p, "Set FX parameters", function()
    local out = json.array()
    for k = 1, (p.params.n or #p.params) do
      local s = p.params[k]
      local i = find_param(tr, fx, s.param)
      if not is_null(s.display_value) then
        reaper.TrackFX_SetParamNormalized(tr, fx, i, solve_display(tr, fx, i, s.display_value))
      elseif not is_null(s.value) then
        reaper.TrackFX_SetParamNormalized(tr, fx, i, s.value)
      else
        error("each parameter needs value (0-1) or display_value", 0)
      end
      out[#out + 1] = param_info(tr, fx, i)
    end
    return out
  end)
end

function commands.list_installed_fx(p)
  local filter = not is_null(p.filter) and p.filter:lower() or nil
  local out = json.array()
  local i = 0
  while true do
    local ok, name, ident = reaper.EnumInstalledFX(i)
    if not ok then break end
    if not filter or name:lower():find(filter, 1, true) then
      out[#out + 1] = json.object({ name = name, ident = ident })
      if #out >= (p.limit or 200) then break end
    end
    i = i + 1
  end
  return out
end

---------------------------------------------------------------------------
-- Transport, tempo, markers and regions
---------------------------------------------------------------------------

local CMD_RECORD = 1013

function commands.transport(p)
  local a = p.action
  if not is_null(p.position) then reaper.SetEditCurPos(to_time(p.position), true, true) end
  if a == "play" then reaper.OnPlayButton()
  elseif a == "stop" then reaper.OnStopButton()
  elseif a == "pause" then reaper.OnPauseButton()
  elseif a == "record" then reaper.Main_OnCommand(CMD_RECORD, 0)
  elseif a ~= "goto" and a ~= nil then error("action must be play, stop, pause, record or goto", 0) end
  if not is_null(p["repeat"]) then reaper.GetSetRepeat(p["repeat"] and 1 or 0) end
  local play = reaper.GetPlayState()
  return json.object({
    playing = play & 1 == 1, paused = play & 2 == 2, recording = play & 4 == 4,
    cursor = reaper.GetCursorPosition(),
  })
end

function commands.set_time_selection(p)
  local s, e = to_time(p.start), to_time(p["end"])
  if not s or not e then
    reaper.GetSet_LoopTimeRange(true, false, 0, 0, false)
    return json.null
  end
  reaper.GetSet_LoopTimeRange(true, p.loop ~= false, s, e, false)
  return { s, e }
end

function commands.set_tempo(p)
  return run(p, "Set tempo", function()
    local count = reaper.CountTempoTimeSigMarkers(0)
    local bpm = p.bpm
    local num = math.tointeger(p.numerator)
    local denom = math.tointeger(p.denominator)
    if count == 0 then
      if num or denom then
        reaper.SetTempoTimeSigMarker(0, -1, 0, -1, -1, bpm or reaper.Master_GetTempo(), num or 4, denom or 4, false)
      elseif bpm then
        reaper.SetCurrentBPM(0, bpm, false)
      end
    else
      local _, t, measure, beat, old_bpm, old_num, old_denom, linear = reaper.GetTempoTimeSigMarker(0, 0)
      reaper.SetTempoTimeSigMarker(0, 0, t, measure, beat, bpm or old_bpm,
        num or old_num, denom or old_denom, linear)
    end
    reaper.UpdateTimeline()
    local n, d, tempo = reaper.TimeMap_GetTimeSigAtTime(0, 0)
    return json.object({ bpm = tempo, time_signature = { n, d }, tempo_markers = reaper.CountTempoTimeSigMarkers(0) })
  end)
end

local function hex_color(c)
  if is_null(c) then return 0 end
  local r, g, b = tostring(c):match("^#?(%x%x)(%x%x)(%x%x)$")
  if not r then error("color must be '#rrggbb'", 0) end
  return reaper.ColorToNative(tonumber(r, 16), tonumber(g, 16), tonumber(b, 16)) | 0x1000000
end

function commands.add_marker(p)
  return run(p, p.region and "Add region" or "Add marker", function()
    local s = to_time(p.position or p.start)
    if not s then error("position (or start for a region) is required", 0) end
    local e = to_time(p["end"]) or s
    local is_region = p.region == true or not is_null(p["end"])
    if is_region and e <= s then error("region end must be after start", 0) end
    local id = reaper.AddProjectMarker2(0, is_region, s, e, p.name or "", math.tointeger(p.id) or -1, hex_color(p.color))
    return json.object({ id = id, region = is_region })
  end)
end

local function find_marker(id, is_region)
  local _, nm, nr = reaper.CountProjectMarkers(0)
  for i = 0, nm + nr - 1 do
    local _, isrgn, pos, rgnend, name, mid, color = reaper.EnumProjectMarkers3(0, i)
    if mid == id and isrgn == is_region then return pos, rgnend, name, color end
  end
  error((is_region and "region " or "marker ") .. tostring(id) .. " not found", 0)
end

function commands.update_marker(p)
  local id, is_region = math.tointeger(p.id), p.region == true
  local pos, rgnend, name, color = find_marker(id, is_region)
  return run(p, "Edit marker", function()
    local s = to_time(p.position or p.start) or pos
    local e = to_time(p["end"]) or rgnend
    local c = is_null(p.color) and color or hex_color(p.color)
    reaper.SetProjectMarker4(0, id, is_region, s, e, is_null(p.name) and name or p.name, c,
      (not is_null(p.name) and p.name == "") and 1 or 0)
    return true
  end)
end

function commands.delete_marker(p)
  local id, is_region = math.tointeger(p.id), p.region == true
  find_marker(id, is_region)
  return run(p, "Delete marker", function()
    return reaper.DeleteProjectMarker(0, id, is_region)
  end)
end

M.commands = commands

---------------------------------------------------------------------------
-- Request handling and file transport
---------------------------------------------------------------------------

function M.handle(raw)
  local id = json.null
  local ok, result = xpcall(function()
    local req = json.decode(raw)
    id = req.id or json.null
    local cmd = commands[req.cmd]
    if not cmd then error("unknown command: " .. tostring(req.cmd), 0) end
    return cmd(req.params or json.object())
  end, function(e)
    if type(e) == "string" and not e:find("\n") and not e:find("^[^:]+%.lua:%d+:") then return e end
    return debug.traceback(tostring(e), 2)
  end)
  if ok then
    return json.encode(json.object({ id = id, ok = true, result = result }))
  end
  return json.encode(json.object({ id = id, ok = false, error = tostring(result) }))
end

local sep = package.config:sub(1, 1)

local function read_file(path)
  local f = io.open(path, "rb")
  if not f then return nil end
  local data = f:read("a")
  f:close()
  return data
end

local function write_atomic(path, data)
  local tmp = path .. ".tmp"
  local f = assert(io.open(tmp, "wb"))
  f:write(data)
  f:close()
  os.remove(path)
  assert(os.rename(tmp, path))
end

function M.setup(base)
  M.base = base or (reaper.GetResourcePath() .. sep .. "reaper_mcp")
  M.requests = M.base .. sep .. "requests"
  M.responses = M.base .. sep .. "responses"
  M.heartbeat = M.base .. sep .. "heartbeat.json"
  reaper.RecursiveCreateDirectory(M.requests, 0)
  reaper.RecursiveCreateDirectory(M.responses, 0)
end

local last_beat = -math.huge

function M.beat(force)
  local now = reaper.time_precise()
  if force or now - last_beat >= HEARTBEAT_INTERVAL then
    last_beat = now
    write_atomic(M.heartbeat, json.encode(json.object({
      bridge_version = BRIDGE_VERSION, time = os.time(),
    })))
  end
end

-- Process every pending request once. Returns the number handled.
function M.poll()
  reaper.EnumerateFiles(M.requests, -1) -- force REAPER to rescan the folder
  local names, i = {}, 0
  while true do
    local fname = reaper.EnumerateFiles(M.requests, i)
    if not fname then break end
    if fname:match("%.req%.json$") then names[#names + 1] = fname end
    i = i + 1
  end
  table.sort(names)
  for _, fname in ipairs(names) do
    local path = M.requests .. sep .. fname
    local raw = read_file(path)
    os.remove(path)
    if raw then
      local id = fname:gsub("%.req%.json$", "")
      write_atomic(M.responses .. sep .. id .. ".res.json", M.handle(raw))
    end
  end
  return #names
end

local function loop()
  local ok, err = pcall(function()
    M.poll()
    M.beat(false)
  end)
  if not ok then reaper.ShowConsoleMsg("reaper-mcp bridge error: " .. tostring(err) .. "\n") end
  reaper.defer(loop)
end

function M.start()
  M.setup()
  if reaper.set_action_options then pcall(reaper.set_action_options, 1 | 4) end
  reaper.atexit(function()
    os.remove(M.heartbeat)
    if reaper.set_action_options then pcall(reaper.set_action_options, 8) end
  end)
  M.beat(true)
  reaper.ShowConsoleMsg("reaper-mcp bridge " .. BRIDGE_VERSION .. " running. Folder: " .. M.base .. "\n")
  loop()
end

-- Tests load this file with REAPER_MCP_NO_START set and drive M directly.
if not REAPER_MCP_NO_START then M.start() end

return M
