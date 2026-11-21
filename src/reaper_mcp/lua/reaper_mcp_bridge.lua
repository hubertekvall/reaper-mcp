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
