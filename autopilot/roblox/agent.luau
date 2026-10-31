--!nocheck
-- Autopilot's agent inside a Roblox Studio playtest. The bridge (autopilot/roblox/plugin.py) installs it with Studio
-- MCP's execute_luau into the Client datamodel (and a log tap into the Server one) once per play session, then calls
-- _G.__AP.call(op, argsJson) for each read the brain asks. It only reads, except: `turn` (the camera, as a mouse would),
-- `rc` writes and calls the brain asks for, and `ui_target` (which only says where an element is).
--
-- Everything it answers is in the kit's frame: X forward, Y right, Z up, centimetres (1 stud = 28 cm), yaw turning
-- right, pitch up. Roblox's: X right, Y up, -Z forward (a CFrame's LookVector), studs. kit = 28 * (-z, x, y).
-- Instances are named by paths from `game`, parts joined with "/": "Workspace/Arena/Platform". Siblings with the same
-- name get "~2", "~3"... after the first; "/" "~" "%" inside a name are written %2F %7E %25.

local VERSION = "__AP_VERSION__"
local ROLE = "__AP_ROLE__" -- "client" or "server"
if _G.__AP and _G.__AP.version == VERSION then
	return "ready"
end
if _G.__AP and _G.__AP.stop then
	pcall(_G.__AP.stop)
end

local HS = game:GetService("HttpService")
local Players = game:GetService("Players")
local LogService = game:GetService("LogService")
local CollectionService = game:GetService("CollectionService")

local S = 28 -- centimetres per stud
local AP = { version = VERSION, role = ROLE, pages = {}, nextPage = 0, ui = {}, logs = {}, logDropped = 0, conns = {} }

-- -- the frame -----------------------------------------------------------------------------------------------

local function r2(x)
	return math.floor(x * 100 + 0.5) / 100
end
local function kit(v)
	return { X = r2(-v.Z * S), Y = r2(v.X * S), Z = r2(v.Y * S) }
end
local function kitSize(v)
	return { X = r2(math.abs(v.Z) * S), Y = r2(math.abs(v.X) * S), Z = r2(math.abs(v.Y) * S) }
end
local function kitDir(v)
	return { X = r2(-v.Z), Y = r2(v.X), Z = r2(v.Y) }
end
local function yawOf(look)
	return math.deg(math.atan2(look.X, -look.Z))
end
local function pitchOf(look)
	return math.deg(math.asin(math.clamp(look.Y, -1, 1)))
end
local function wrap180(a)
	return (a + 180) % 360 - 180
end
local function dirOfYaw(yaw)
	local r = math.rad(yaw)
	return Vector3.new(math.sin(r), 0, -math.cos(r))
end
local function rotOf(cf)
	local look = cf.LookVector
	local _, _, rz = cf:ToEulerAnglesYXZ()
	return { Pitch = r2(pitchOf(look)), Yaw = r2(yawOf(look)), Roll = r2(-math.deg(rz)) }
end

-- -- paths -------------------------------------------------------------------------------------------------

local pathCache = nil

local function escape(name)
	return (string.gsub(string.gsub(string.gsub(name, "%%", "%%25"), "/", "%%2F"), "~", "%%7E"))
end
local function unescape(s)
	return (string.gsub(s, "%%(%x%x)", function(h)
		return string.char(tonumber(h, 16))
	end))
end

local function segOf(inst)
	local parent = inst.Parent
	local name = inst.Name
	if parent == nil or parent == game then
		return escape(name)
	end
	local k = 0
	for _, c in ipairs(parent:GetChildren()) do
		if c.Name == name then
			k += 1
			if c == inst then
				break
			end
		end
	end
	if k > 1 then
		return escape(name) .. "~" .. k
	end
	return escape(name)
end

local function pathOf(inst)
	if inst == nil then
		return ""
	end
	if inst == game then
		return "game"
	end
	if pathCache and pathCache[inst] then
		return pathCache[inst]
	end
	local parent = inst.Parent
	local p
	if parent == nil or parent == game then
		p = segOf(inst)
	else
		p = pathOf(parent) .. "/" .. segOf(inst)
	end
	if pathCache then
		pathCache[inst] = p
	end
	return p
end

local function resolve(path)
	if type(path) ~= "string" or path == "" then
		return nil
	end
	if path == "game" then
		return game
	end
	local cur = game
	for seg in string.gmatch(path, "[^/]+") do
		local name, k = string.match(seg, "^(.*)~(%d+)$")
		if name == nil then
			name, k = seg, 1
		else
			k = tonumber(k)
		end
		name = unescape(name)
		local n, nxt = 0, nil
		local ok, kids = pcall(function()
			return cur:GetChildren()
		end)
		if not ok then
			return nil
		end
		for _, c in ipairs(kids) do
			if c.Name == name then
				n += 1
				if n == k then
					nxt = c
					break
				end
			end
		end
		if nxt == nil and cur == game and k == 1 then
			local okS, svc = pcall(function()
				return game:FindService(name)
			end)
			nxt = okS and svc or nil
		end
		if nxt == nil then
			return nil
		end
		cur = nxt
	end
	return cur
end

local function fail(status, msg)
	error({ status = status, error = msg }, 0)
end

-- -- the player --------------------------------------------------------------------------------------------

local function char()
	local p = Players.LocalPlayer
	local c = p and p.Character
	local hrp = c and c:FindFirstChild("HumanoidRootPart")
	local hum = c and c:FindFirstChildOfClass("Humanoid")
	if c and hrp and hum and hum.Health > 0 then
		return c, hrp, hum
	end
	return nil
end

local function halfHeight(hrp, hum)
	if hum.RigType == Enum.HumanoidRigType.R6 then
		return hrp.Size.Y / 2 + 2 + hum.HipHeight
	end
	return hrp.Size.Y / 2 + hum.HipHeight
end

local function onGround(hum)
	return hum.FloorMaterial ~= Enum.Material.Air
end

-- -- boxes and things --------------------------------------------------------------------------------------

local function aabb(cf, s)
	local _, _, _, r00, r01, r02, r10, r11, r12, r20, r21, r22 = cf:GetComponents()
	local hx, hy, hz = s.X / 2, s.Y / 2, s.Z / 2
	local ex = math.abs(r00) * hx + math.abs(r01) * hy + math.abs(r02) * hz
	local ey = math.abs(r10) * hx + math.abs(r11) * hy + math.abs(r12) * hz
	local ez = math.abs(r20) * hx + math.abs(r21) * hy + math.abs(r22) * hz
	return cf.Position, Vector3.new(ex * 2, ey * 2, ez * 2)
end

local function boxOf(inst)
	if inst:IsA("BasePart") then
		return aabb(inst.CFrame, inst.Size)
	elseif inst:IsA("Model") then
		local ok, cf, s = pcall(function()
			return inst:GetBoundingBox()
		end)
		if ok then
			return aabb(cf, s)
		end
	elseif inst:IsA("Attachment") then
		return inst.WorldPosition, Vector3.zero
	end
	-- a folder (or anything else): the box of the parts in it
	local mn, mx
	local n = 0
	for _, d in ipairs(inst:GetDescendants()) do
		if d:IsA("BasePart") then
			local c, s = aabb(d.CFrame, d.Size)
			local a, b = c - s / 2, c + s / 2
			mn = mn and mn:Min(a) or a
			mx = mx and mx:Max(b) or b
			n += 1
			if n > 5000 then
				break
			end
		end
	end
	if mn then
		return (mn + mx) / 2, mx - mn
	end
	return nil
end

local BIG = 60 -- studs: a model bigger than this, with no Humanoid, is a place (an area, a level), not one thing
local thingCache, sizeCache = nil, nil

local function modelSize(m)
	local s = sizeCache and sizeCache[m]
	if s == nil then
		local ok, _, size = pcall(function()
			return m:GetBoundingBox()
		end)
		s = ok and math.max(size.X, size.Y, size.Z) or math.huge
		if sizeCache then
			sizeCache[m] = s
		end
	end
	return s
end

-- The thing a part belongs to: the biggest Model around it that is still one thing (has a Humanoid, or is small),
-- below the folders and big models a level is organised in; else the part itself.
local function thingOf(part)
	if thingCache and thingCache[part] then
		return thingCache[part]
	end
	local best = part
	local p = part.Parent
	while p and p ~= workspace and p ~= game and not p:IsA("Folder") do
		if p:IsA("Model") then
			if p:FindFirstChildOfClass("Humanoid") or modelSize(p) <= BIG then
				best = p
			else
				break
			end
		end
		p = p.Parent
	end
	if thingCache then
		thingCache[part] = best
	end
	return best
end

local function velocityOf(inst)
	if inst:IsA("BasePart") then
		return inst.AssemblyLinearVelocity
	elseif inst:IsA("Model") then
		local pp = inst.PrimaryPart or inst:FindFirstChild("HumanoidRootPart") or inst:FindFirstChildWhichIsA("BasePart")
		if pp then
			return pp.AssemblyLinearVelocity
		end
	end
	return Vector3.zero
end

local function ignored(inst, c)
	local cam = workspace.CurrentCamera
	return (c and inst:IsDescendantOf(c)) or (cam and inst:IsDescendantOf(cam))
end

-- -- values (rc) -------------------------------------------------------------------------------------------

local function toJson(v)
	local t = typeof(v)
	if t == "Vector3" then
		return { x = v.X, y = v.Y, z = v.Z }
	elseif t == "Vector2" then
		return { x = v.X, y = v.Y }
	elseif t == "CFrame" then
		local rx, ry, rz = v:ToEulerAnglesYXZ()
		return { x = v.X, y = v.Y, z = v.Z, rx = math.deg(rx), ry = math.deg(ry), rz = math.deg(rz) }
	elseif t == "Color3" then
		return { r = v.R, g = v.G, b = v.B }
	elseif t == "EnumItem" then
		return v.Name
	elseif t == "Instance" then
		return pathOf(v)
	elseif t == "UDim2" then
		return { xs = v.X.Scale, xo = v.X.Offset, ys = v.Y.Scale, yo = v.Y.Offset }
	elseif t == "BrickColor" then
		return v.Name
	elseif t == "number" or t == "string" or t == "boolean" or t == "nil" then
		return v
	elseif t == "table" then
		local out = {}
		for k, x in pairs(v) do
			out[tostring(k)] = toJson(x)
		end
		return out
	end
	return tostring(v)
end

local function fromJson(v, like)
	local t = typeof(like)
	if type(v) == "table" then
		if t == "CFrame" or v.rx ~= nil then
			local pos = Vector3.new(v.x or like.X, v.y or like.Y, v.z or like.Z)
			if v.rx ~= nil then
				return CFrame.new(pos) * CFrame.fromEulerAnglesYXZ(math.rad(v.rx), math.rad(v.ry or 0), math.rad(v.rz or 0))
			end
			return CFrame.new(pos) * (like - like.Position)
		elseif t == "Color3" or v.r ~= nil then
			return Color3.new(v.r or 0, v.g or 0, v.b or 0)
		elseif t == "Vector2" then
			return Vector2.new(v.x or 0, v.y or 0)
		elseif t == "UDim2" then
			return UDim2.new(v.xs or 0, v.xo or 0, v.ys or 0, v.yo or 0)
		elseif v.x ~= nil or v.X ~= nil then
			return Vector3.new(v.x or v.X or 0, v.y or v.Y or 0, v.z or v.Z or 0)
		end
	elseif type(v) == "string" then
		if t == "EnumItem" then
			return like.EnumType[v]
		elseif t == "Instance" or (t == "nil" and string.find(v, "/")) then
			return resolve(v) or v
		elseif t == "BrickColor" then
			return BrickColor.new(v)
		end
	end
	return v
end

-- Properties worth listing in describe (Roblox has no reflection in Luau): the ones of the classes it is
local COMMON = {
	Instance = { "Name", "ClassName", "Parent" },
	PVInstance = {},
	BasePart = { "Position", "Orientation", "Size", "CFrame", "Anchored", "CanCollide", "CanTouch", "Transparency",
		"Color", "Material", "AssemblyLinearVelocity", "Massless" },
	Model = { "PrimaryPart", "WorldPivot" },
	Humanoid = { "Health", "MaxHealth", "WalkSpeed", "JumpPower", "JumpHeight", "UseJumpPower", "HipHeight",
		"MaxSlopeAngle", "MoveDirection", "FloorMaterial", "Sit", "PlatformStand", "AutoRotate", "RigType" },
	ValueBase = { "Value" },
	GuiObject = { "Visible", "Position", "Size", "AbsolutePosition", "AbsoluteSize", "BackgroundTransparency" },
	TextLabel = { "Text" },
	TextButton = { "Text" },
	TextBox = { "Text", "PlaceholderText" },
	LayerCollector = { "Enabled" },
	Sound = { "SoundId", "Playing", "Volume", "TimePosition" },
	ProximityPrompt = { "ActionText", "ObjectText", "Enabled", "KeyboardKeyCode", "HoldDuration", "MaxActivationDistance" },
	Light = { "Enabled", "Brightness", "Range", "Color" },
	Camera = { "CFrame", "Focus", "FieldOfView", "CameraType", "CameraSubject" },
}

local function describe(inst)
	local props = {}
	local seen = {}
	for cls, names in pairs(COMMON) do
		local ok, isA = pcall(function()
			return inst:IsA(cls)
		end)
		if ok and isA then
			for _, n in ipairs(names) do
				if not seen[n] then
					seen[n] = true
					local okV, v = pcall(function()
						return inst[n]
					end)
					if okV then
						table.insert(props, { Name = n, Type = typeof(v), Description = "" })
					end
				end
			end
		end
	end
	for name, v in pairs(inst:GetAttributes()) do
		table.insert(props, { Name = "@" .. name, Type = typeof(v), Description = "attribute" })
	end
	local kids = {}
	for i, c in ipairs(inst:GetChildren()) do
		if i > 60 then
			table.insert(kids, "...")
			break
		end
		table.insert(kids, segOf(c) .. " (" .. c.ClassName .. ")")
	end
	local tags = CollectionService:GetTags(inst)
	return { Name = inst.Name, Class = inst.ClassName, Properties = props, Functions = {}, Children = kids, Tags = tags }
end

-- A property by name: "Position", "@Attr" (an attribute), "Position:Y" (one part), "Pivot" (GetPivot / PivotTo)
local function readProp(inst, name)
	local sub
	name, sub = string.match(name, "^([^:]+):?(.*)$")
	local v
	if string.sub(name, 1, 1) == "@" then
		v = inst:GetAttribute(string.sub(name, 2))
	elseif name == "Pivot" then
		v = inst:GetPivot()
	else
		local ok, x = pcall(function()
			return inst[name]
		end)
		if not ok then
			local a = inst:GetAttribute(name)
			if a == nil then
				local child = inst:FindFirstChild(name)
				if child and child:IsA("ValueBase") then
					return child.Value
				end
				fail(400, "Property " .. name .. " does not exist on object " .. pathOf(inst))
			end
			v = a
		elseif typeof(x) == "Instance" and x.Parent == inst and x:IsA("ValueBase") then
			v = x.Value -- (a child's name: leaderstats.Coins is its Value)
		else
			v = x
		end
	end
	if sub ~= nil and sub ~= "" then
		local ok, x = pcall(function()
			return v[sub]
		end)
		if not ok then
			fail(400, "Property " .. name .. ":" .. sub .. " does not exist on object " .. pathOf(inst))
		end
		v = x
	end
	return v
end

local function writeProp(inst, name, value)
	if string.sub(name, 1, 1) == "@" then
		inst:SetAttribute(string.sub(name, 2), fromJson(value, inst:GetAttribute(string.sub(name, 2))))
	elseif name == "Pivot" then
		local cur = inst:GetPivot()
		inst:PivotTo(fromJson(value, cur))
	else
		local ok, cur = pcall(function()
			return inst[name]
		end)
		if ok and typeof(cur) == "Instance" and cur.Parent == inst and cur:IsA("ValueBase") then
			cur.Value = fromJson(value, cur.Value)
			return
		end
		if not ok then
			local child = inst:FindFirstChild(name)
			if child and child:IsA("ValueBase") then
				child.Value = fromJson(value, child.Value)
				return
			end
			fail(400, "Property " .. name .. " does not exist on object " .. pathOf(inst))
		end
		inst[name] = fromJson(value, cur)
	end
end

-- -- GUI ---------------------------------------------------------------------------------------------------

local function guiInset()
	local ok, a = pcall(function()
		return game:GetService("GuiService"):GetGuiInset()
	end)
	return ok and a.Y or 0
end

-- The ScreenGui a GUI object is shown in, when it and everything above it is visible; else nil
local function shownIn(g)
	local x = g
	while x and not x:IsA("LayerCollector") do
		if x:IsA("GuiObject") and not x.Visible then
			return nil
		end
		x = x.Parent
	end
	if x and x:IsA("ScreenGui") and x.Enabled then
		return x
	end
	return nil
end

local function onScreen(g, vp)
	local p, s = g.AbsolutePosition, g.AbsoluteSize
	return s.X > 1 and s.Y > 1 and p.X + s.X > 0 and p.Y + s.Y > -60 and p.X < vp.X and p.Y < vp.Y
end

local function textOf(g)
	if (g:IsA("TextLabel") or g:IsA("TextButton") or g:IsA("TextBox")) and g.TextTransparency < 1 then
		local ok, t = pcall(function()
			return g.ContentText
		end)
		t = ok and t or g.Text
		if t ~= "" then
			return t
		end
	end
	return nil
end

local function playerGui()
	local p = Players.LocalPlayer
	return p and p:FindFirstChildOfClass("PlayerGui")
end

-- -- ops -----------------------------------------------------------------------------------------------------

local ops = {}

function ops.ping()
	return { game_time = workspace.DistributedGameTime, wall_time = os.time() }
end

function ops.context(a)
	local c = char()
	local cam = workspace.CurrentCamera
	local controls = {}
	local okC, info = pcall(function()
		return game:GetService("ContextActionService"):GetAllBoundActionInfo()
	end)
	if okC and info then
		for name, i in pairs(info) do
			local keys = {}
			for _, it in ipairs(i.inputTypes or {}) do
				table.insert(keys, typeof(it) == "EnumItem" and it.Name or tostring(it))
			end
			table.insert(controls, { name = name, action = name, keys = keys, digital = true, roblox = true })
		end
	end
	return {
		world = "Workspace",
		scene = game.Name,
		character = c and pathOf(c) or "",
		controller = "",
		input_subsystem = "",
		camera_manager = "",
		camera = cam and pathOf(cam) or "",
		dimension = "3d",
		game_name = game.Name,
		player = Players.LocalPlayer and Players.LocalPlayer.Name or "",
		game_time = workspace.DistributedGameTime,
		controls = controls,
		camera_type = cam and cam.CameraType.Name or "",
	}
end

function ops.pose()
	local c, hrp, hum = char()
	if not c then
		fail(400, "No player")
	end
	local cam = workspace.CurrentCamera
	local look = cam.CFrame.LookVector
	local hh = halfHeight(hrp, hum)
	return {
		path = pathOf(c),
		at = kit(hrp.Position),
		feet = kit(hrp.Position - Vector3.new(0, hh, 0)),
		eye = kit(cam.CFrame.Position),
		yaw = r2(yawOf(look)),
		pitch = r2(pitchOf(look)),
		velocity = kit(hrp.AssemblyLinearVelocity),
		on_ground = onGround(hum),
		health = hum.Health,
		state = hum:GetState().Name,
	}
end

function ops.movement()
	local c, hrp, hum = char()
	if not c then
		fail(400, "No player")
	end
	local g = workspace.Gravity
	local jumpV = hum.UseJumpPower and hum.JumpPower or math.sqrt(2 * g * hum.JumpHeight)
	return {
		walk_speed = r2(hum.WalkSpeed * S),
		jump_z = r2(jumpV * S),
		gravity = r2(g * S),
		half_height = r2(halfHeight(hrp, hum) * S),
		radius = r2(math.max(hrp.Size.X, hrp.Size.Z) / 2 * S),
		step = nil,
		max_slope = hum.MaxSlopeAngle,
		air_control = 1,
		sources = {
			walk_speed = "Humanoid.WalkSpeed",
			jump_z = hum.UseJumpPower and "Humanoid.JumpPower" or "Humanoid.JumpHeight + Workspace.Gravity",
			gravity = "Workspace.Gravity",
			half_height = "HumanoidRootPart and Humanoid.HipHeight",
			max_slope = "Humanoid.MaxSlopeAngle",
		},
	}
end

local SEARCH_ROOTS = { "Workspace", "Players", "ReplicatedStorage", "Lighting", "SoundService", "Teams" }

function ops.find(a)
	local limit = tonumber(a.limit) or 1000
	local out, seen = {}, {}
	local nameL = a.name and string.lower(tostring(a.name)) or nil
	local c = char()
	local function add(inst)
		if seen[inst] then
			return false
		end
		if nameL and not string.find(string.lower(inst.Name), nameL, 1, true) then
			return false
		end
		seen[inst] = true
		table.insert(out, pathOf(inst))
		return #out >= limit
	end
	if a.things then
		for _, d in ipairs(workspace:GetDescendants()) do
			if d:IsA("BasePart") and not ignored(d, nil) and not d:IsA("Terrain") then
				if add(thingOf(d)) then
					break
				end
			end
		end
		return { objects = out }
	end
	local cls = a.class
	if cls == nil or cls == "" then
		for _, d in ipairs(workspace:GetChildren()) do
			if add(d) then
				break
			end
		end
		return { objects = out }
	end
	cls = tostring(cls)
	local tag = string.match(cls, "^group:(.+)$") or string.match(cls, "^tag:(.+)$")
	if tag then
		for _, inst in ipairs(CollectionService:GetTagged(tag)) do
			if add(inst) then
				break
			end
		end
		return { objects = out }
	end
	local isClass = pcall(function()
		return game:IsA(cls)
	end)
	for _, root in ipairs(SEARCH_ROOTS) do
		local r = game:FindFirstChild(root) or game:FindService(root)
		if r then
			for _, d in ipairs(r:GetDescendants()) do
				local match
				if isClass then
					match = d:IsA(cls)
				else
					match = d.Name == cls or CollectionService:HasTag(d, cls)
				end
				if match and add(d) then
					return { objects = out }
				end
			end
		end
	end
	return { objects = out }
end

function ops.transforms(a)
	local res = {}
	for _, p in ipairs(a.objects or {}) do
		local inst = resolve(p)
		if inst ~= nil then
			local center, size = boxOf(inst)
			local pos, rot = center, nil
			if inst:IsA("PVInstance") then
				local ok, cf = pcall(function()
					return inst:GetPivot()
				end)
				if ok then
					pos, rot = cf.Position, rotOf(cf)
				end
			elseif inst:IsA("Attachment") then
				rot = rotOf(inst.WorldCFrame)
			end
			if pos ~= nil then
				res[p] = {
					location = kit(pos),
					rotation = rot or { Pitch = 0, Yaw = 0, Roll = 0 },
					velocity = kit(velocityOf(inst)),
					center = center and kit(center) or kit(pos),
					extent = size and kitSize(size / 2) or { X = 0, Y = 0, Z = 0 },
				}
			end
		end
	end
	return { objects = res }
end

-- How far the body could walk each way: box sweeps of its size above the knees (walls), rays down (drops)
local function sonar(c, hrp, hum, a, viewYaw)
	local reach = math.clamp(tonumber(a.sonar_reach) or 1500, 50, 5000) / S
	local turns = a.sonar_turns or { 0, -30, 30, -60, 60, -90, 90, -135, 135, 180 }
	local hh = halfHeight(hrp, hum)
	local cosSlope = math.cos(math.rad(hum.MaxSlopeAngle))
	local params = RaycastParams.new()
	params.FilterType = Enum.RaycastFilterType.Exclude
	params.FilterDescendantsInstances = { c, workspace.CurrentCamera }
	params.RespectCanCollide = true
	local width = math.max(hrp.Size.X, 1.5)
	local knee = 1.0 -- studs above the feet the box starts at (lower things: a rise, felt by the rays down)
	local height = math.max(hh * 2 - knee, 1)
	local step = 1.5
	local dropAt = 2.2 -- studs: a floor this far below the feet is a drop (60 cm, as Godot)
	local ways = {}
	for i, turn in ipairs(turns) do
		if i > 16 then
			break
		end
		local dir = dirOfYaw(viewYaw + turn)
		local feet = hrp.Position - Vector3.new(0, hh, 0)
		local startY = feet.Y
		local travelled = 0
		local way = { turn = turn, by = "reach" }
		local guard = 0
		while travelled < reach and guard < 200 do
			guard += 1
			local len = math.min(step, reach - travelled)
			local center = feet + Vector3.new(0, knee + height / 2, 0)
			local hit = workspace:Blockcast(CFrame.lookAt(center, center + dir), Vector3.new(width, height, 0.5), dir * len, params)
			if hit then
				if hit.Normal.Y >= cosSlope and guard < 199 then
					feet += Vector3.new(0, 0.5, 0) -- a walkable rise: go up it
					continue
				end
				travelled += hit.Distance
				way.by = "wall"
				local t = thingOf(hit.Instance)
				way.thing = segOf(t)
				way.path = pathOf(t)
				way.normal = { X = r2(-hit.Normal.Z), Y = r2(hit.Normal.X) }
				break
			end
			feet += dir * len
			travelled += len
			local mid = feet + Vector3.new(0, hh, 0)
			local down = workspace:Raycast(mid, Vector3.new(0, -(hh + 107), 0), params)
			local below = down and (feet.Y - down.Position.Y) or 108
			if below > dropAt then
				way.by = "drop"
				way.drop = below < 107 and r2(below * S) or nil
				local lo, hi = 0, len
				local back = mid - dir * len
				for _ = 1, 5 do
					local m = (lo + hi) / 2
					local at = back + dir * m
					if workspace:Raycast(at, Vector3.new(0, -(hh + dropAt), 0), params) then
						lo = m
					else
						hi = m
					end
				end
				travelled -= len - lo
				break
			end
			feet = Vector3.new(feet.X, down.Position.Y, feet.Z)
		end
		way.clear = r2(math.max(travelled, 0) * S)
		local rise = feet.Y - startY
		if math.abs(rise) > 0.2 then
			way.rise = r2(rise * S)
		end
		table.insert(ways, way)
	end
	return { ways = ways, reach = r2(reach * S) }
end

local function lookOf(t)
	local look = {}
	local lights, bright, texts = 0, 0, {}
	local desc = t:IsA("BasePart") and t:GetChildren() or t:GetDescendants()
	if #desc > 800 then
		return look
	end
	for _, d in ipairs(desc) do
		if d:IsA("Light") and d.Enabled and d.Brightness > 0 then
			lights += 1
			bright = math.max(bright, d.Brightness)
		elseif (d:IsA("TextLabel") or d:IsA("TextButton")) and #texts < 4 then
			local tx = textOf(d)
			if tx then
				table.insert(texts, string.sub(tx, 1, 40))
			end
		end
	end
	if lights > 0 then
		look.lights = { on = lights, brightness = r2(bright) }
	end
	if #texts > 0 then
		look.texts = texts
	end
	local animator = t:FindFirstChildWhichIsA("Humanoid", true) or t:FindFirstChildWhichIsA("AnimationController", true)
	animator = animator and animator:FindFirstChildOfClass("Animator")
	if animator then
		local tracks = animator:GetPlayingAnimationTracks()
		if #tracks > 0 then
			look.animation = tracks[1].Name
		end
	end
	return look
end

local OMITTED = { "held", "openings", "ways_up", "slope ends", "lanes", "under", "overhead", "highlights", "effects", "touching" }

function ops.view(a)
	local cam = workspace.CurrentCamera
	if not cam then
		fail(400, "No camera")
	end
	local maxD = (tonumber(a.max_distance) or 5000) / S
	local maxCount = tonumber(a.max_count) or 24
	local onlySet = nil
	if type(a.only) == "table" then
		onlySet = {}
		for _, x in ipairs(a.only) do
			onlySet[tostring(x)] = true
		end
	end
	local c, hrp, hum = char()
	local camCF = cam.CFrame
	local camPos = camCF.Position
	local vp = cam.ViewportSize
	local excl = { cam }
	if c then
		table.insert(excl, c)
	end
	local params = RaycastParams.new()
	params.FilterType = Enum.RaycastFilterType.Exclude
	params.FilterDescendantsInstances = excl
	local body = {}
	local feetY = camPos.Y
	local standing = nil
	local viewYaw = yawOf(camCF.LookVector)
	if c then
		local hh = halfHeight(hrp, hum)
		feetY = hrp.Position.Y - hh
		local vel = hrp.AssemblyLinearVelocity
		local g = onGround(hum)
		body = { velocity = kit(vel), feet_z = r2(feetY * S), crouched = false, on_ground = g, falling = (not g) and vel.Y < -1 }
		local down = workspace:Raycast(hrp.Position, Vector3.new(0, -(hh + 1.5), 0), params)
		if down then
			standing = thingOf(down.Instance)
			body.standing_on = segOf(standing)
			body.floor_slope_degrees = r2(math.deg(math.acos(math.clamp(down.Normal.Y, -1, 1))))
		end
	end
	local op = OverlapParams.new()
	op.FilterType = Enum.RaycastFilterType.Exclude
	op.FilterDescendantsInstances = excl
	local parts = workspace:GetPartBoundsInRadius(camPos, maxD, op)
	local things, order = {}, {}
	for _, part in ipairs(parts) do
		if part.Transparency < 0.95 and not part:IsA("Terrain") then
			local t = thingOf(part)
			if onlySet == nil or onlySet[t.Name] or onlySet[segOf(t)] or onlySet[pathOf(t)] then
				local center, size = aabb(part.CFrame, part.Size)
				local mn, mx = center - size / 2, center + size / 2
				local e = things[t]
				if e then
					e.mn = e.mn:Min(mn)
					e.mx = e.mx:Max(mx)
				else
					things[t] = { mn = mn, mx = mx }
					table.insert(order, t)
				end
			end
		end
	end
	local hit = workspace:Raycast(camPos, camCF.LookVector * maxD, params)
	local crossT = hit and hit.Instance and thingOf(hit.Instance) or nil
	-- nearest first, then the expensive checks until max_count are found
	local cand = {}
	for _, t in ipairs(order) do
		local e = things[t]
		local mn, mx = e.mn, e.mx
		local size = mx - mn
		if not (t == standing and size.X > 36 and size.Z > 36) then
			local nearest = Vector3.new(math.clamp(camPos.X, mn.X, mx.X), math.clamp(camPos.Y, mn.Y, mx.Y), math.clamp(camPos.Z, mn.Z, mx.Z))
			local dist = (nearest - camPos).Magnitude
			if dist <= maxD then
				table.insert(cand, { t = t, mn = mn, mx = mx, nearest = nearest, dist = dist })
			end
		end
	end
	table.sort(cand, function(x, y)
		return x.dist < y.dist
	end)
	local out = {}
	for _, e in ipairs(cand) do
		if #out >= maxCount then
			break
		end
		local t, mn, mx = e.t, e.mn, e.mx
		local center = (mn + mx) / 2
		local sp, inView = cam:WorldToViewportPoint(center)
		local anyIn = inView
		if not anyIn then
			for i = 0, 7 do
				local corner = Vector3.new(i % 2 == 0 and mn.X or mx.X, (i // 2) % 2 == 0 and mn.Y or mx.Y, (i // 4) == 0 and mn.Z or mx.Z)
				local _, cin = cam:WorldToViewportPoint(corner)
				if cin then
					anyIn = true
					break
				end
			end
		end
		if anyIn then
			local seen = false
			for _, target in ipairs({ center, Vector3.new(center.X, mx.Y - 0.05, center.Z), e.nearest }) do
				local r = workspace:Raycast(camPos, target - camPos, params)
				if r == nil or r.Instance == t or r.Instance:IsDescendantOf(t) or (r.Position - target).Magnitude < 0.2 then
					seen = true
					break
				end
			end
			if seen then
				local size = mx - mn
				local item = {
					name = segOf(t),
					path = pathOf(t),
					class = t.ClassName,
					center = kit(center),
					size = kitSize(size),
					yaw = t:IsA("PVInstance") and r2(yawOf(t:GetPivot().LookVector)) or 0,
					distance = r2(e.dist * S),
					top_above_feet = r2((mx.Y - feetY) * S),
					bottom_above_feet = r2((mn.Y - feetY) * S),
					crosshair_on = t == crossT,
				}
				if sp.Z > 0 then
					item.screen_x = r2(sp.X / vp.X * 2 - 1)
					item.screen_y = r2(1 - sp.Y / vp.Y * 2)
				end
				if not inView then
					item.partly_in_view = true
				end
				local v = velocityOf(t)
				if v.Magnitude > 0.05 then
					item.speed = r2(v.Magnitude * S)
				end
				local look = lookOf(t)
				if next(look) then
					item.look = look
					if look.texts then
						item.label = look.texts[1]
					end
				end
				table.insert(out, item)
			end
		end
	end
	local vfov = cam.FieldOfView
	local hfov = math.deg(2 * math.atan(math.tan(math.rad(vfov) / 2) * vp.X / math.max(vp.Y, 1)))
	local result = {
		visible = out,
		camera = { location = kit(camPos), yaw = r2(viewYaw), pitch = r2(pitchOf(camCF.LookVector)), fov = r2(hfov), vfov = r2(vfov) },
		crosshair_tested = true,
		oriented = true,
		effects_play = true,
		body = body,
		omitted = OMITTED,
	}
	if onlySet then
		result.only = a.only
	end
	if a.sonar and c then
		body.sonar = sonar(c, hrp, hum, a, viewYaw)
	end
	return result
end

function ops.turn(a)
	local cam = workspace.CurrentCamera
	if not cam then
		fail(409, "No camera")
	end
	local dyaw, dpitch = tonumber(a.yaw) or 0, tonumber(a.pitch) or 0
	local f = cam.Focus.Position
	local rel = CFrame.new(f):ToObjectSpace(cam.CFrame)
	local cf = CFrame.new(f) * CFrame.Angles(0, -math.rad(dyaw), 0) * rel
	if dpitch ~= 0 then
		local cur = pitchOf(cf.LookVector)
		local d = math.clamp(cur + dpitch, -80, 80) - cur
		cf = CFrame.new(f) * CFrame.fromAxisAngle(cf.RightVector, math.rad(d)) * CFrame.new(f):ToObjectSpace(cf)
	end
	cam.CFrame = cf
	return { yaw = r2(yawOf(cf.LookVector)), pitch = r2(pitchOf(cf.LookVector)) }
end

function ops.screen(a)
	local pg = playerGui()
	local texts, count = {}, 0
	if pg then
		local cam = workspace.CurrentCamera
		local vp = cam and cam.ViewportSize or Vector2.new(1920, 1080)
		for _, g in ipairs(pg:GetDescendants()) do
			local tx = g:IsA("GuiObject") and textOf(g)
			if tx then
				local sg = shownIn(g)
				if sg and onScreen(g, vp) then
					local key = sg.Name .. "." .. g.Name
					if texts[key] ~= nil then
						local rel = {}
						local x = g
						while x and x ~= sg do
							table.insert(rel, 1, x.Name)
							x = x.Parent
						end
						key = sg.Name .. "." .. table.concat(rel, ".")
					end
					if texts[key] == nil then
						texts[key] = string.sub(tx, 1, 200)
						count += 1
						if count >= 400 then
							break
						end
					end
				end
			end
		end
	end
	return { texts = texts }
end

function ops.ui(a)
	local pg = playerGui()
	local els = {}
	AP.ui = {}
	if not pg then
		return { elements = els }
	end
	local cam = workspace.CurrentCamera
	local vp = cam and cam.ViewportSize or Vector2.new(1920, 1080)
	local inset = guiInset()
	for _, g in ipairs(pg:GetDescendants()) do
		if g:IsA("GuiButton") or g:IsA("TextBox") then
			local sg = shownIn(g)
			if sg and onScreen(g, vp) then
				local label = textOf(g)
				if not label then
					for _, d in ipairs(g:GetDescendants()) do
						if d:IsA("GuiObject") and shownIn(d) then
							label = textOf(d)
							if label then
								break
							end
						end
					end
				end
				local okI, interactable = pcall(function()
					return g.Interactable
				end)
				local enabled = (not okI or interactable ~= false) and g.Active ~= false
				local center = g.AbsolutePosition + g.AbsoluteSize / 2
				local y = center.Y + inset -- (on the screenshot: AbsolutePosition starts below the top bar)
				local id = #AP.ui + 1
				AP.ui[id] = g
				table.insert(els, {
					id = id,
					kind = g:IsA("TextBox") and "text_input" or "button",
					name = g.Name,
					owner = sg.Name,
					label = label and string.sub(label, 1, 80) or g.Name,
					enabled = enabled,
					path = pathOf(g),
					class = g.ClassName,
					screen = { x = r2(center.X), y = r2(y) },
					placeholder = g:IsA("TextBox") and g.PlaceholderText or nil,
					value = g:IsA("TextBox") and g.Text or nil,
				})
				if #els >= 300 then
					break
				end
			end
		end
	end
	return { elements = els }
end

-- Where to click an element of the last `ui` listing, in Studio MCP's mouse coordinates (below the top bar)
function ops.ui_target(a)
	local g = AP.ui[tonumber(a.id) or -1]
	if g == nil or g.Parent == nil then
		fail(400, "no element " .. tostring(a.id) .. " on screen")
	end
	local sg = shownIn(g)
	if not sg then
		fail(400, "no element " .. tostring(a.id) .. " on screen")
	end
	local okI, interactable = pcall(function()
		return g.Interactable
	end)
	if okI and interactable == false then
		fail(400, tostring(a.id) .. " is disabled")
	end
	-- (AbsolutePosition is measured from below the top bar for every ScreenGui, IgnoreGuiInset or not: a
	-- full-screen one sits at y = -inset. Studio MCP's mouse is measured from there too)
	local center = g.AbsolutePosition + g.AbsoluteSize / 2
	local y = center.Y
	local to = nil
	if a.to ~= nil then
		local d = AP.ui[tonumber(a.to) or -1]
		if d then
			local dc = d.AbsolutePosition + d.AbsoluteSize / 2
			to = { x = dc.X, y = dc.Y }
		end
	end
	-- Studio MCP's own way to name it ("LocalPlayer.PlayerGui.HUD.Button"), when every name is a plain identifier
	local mcpPath = nil
	local names = {}
	local x = g
	local pg = playerGui()
	local plain = true
	while x and x ~= pg do
		table.insert(names, 1, x.Name)
		if x.Parent and x.Parent:FindFirstChild(x.Name) ~= x then
			plain = false -- (a sibling before it has its name: Studio would find that one)
		end
		x = x.Parent
	end
	if x == pg and plain then
		for _, n in ipairs(names) do
			if not string.match(n, "^[%a_][%w_]*$") then
				plain = false
				break
			end
		end
		if plain then
			mcpPath = "LocalPlayer.PlayerGui." .. table.concat(names, ".")
		end
	end
	return {
		x = math.floor(center.X + 0.5),
		y = math.floor(y + 0.5),
		kind = g:IsA("TextBox") and "text_input" or "button",
		to = to,
		mcp_path = mcpPath,
	}
end

function ops.rc(a)
	local verb, b = a.verb, a.body
	if verb == "probe" then
		return { ok = true }
	end
	if type(b) ~= "table" then
		fail(400, "rc needs a body")
	end
	local inst = resolve(b.objectPath)
	if inst == nil then
		fail(400, "Object " .. tostring(b.objectPath) .. " does not exist")
	end
	if verb == "describe" then
		return describe(inst)
	elseif verb == "property" then
		if b.access == "WRITE_ACCESS" or b.propertyValue ~= nil then
			for name, v in pairs(b.propertyValue or {}) do
				writeProp(inst, name, v)
			end
			return {}
		end
		if b.propertyName == nil or b.propertyName == "" then
			local all = {}
			for _, p in ipairs(describe(inst).Properties) do
				local ok, v = pcall(readProp, inst, p.Name)
				if ok then
					all[p.Name] = toJson(v)
				end
			end
			return all
		end
		return { [b.propertyName] = toJson(readProp(inst, b.propertyName)) }
	elseif verb == "call" then
		local fname = b.functionName
		local args = {}
		if type(b.parameters) == "table" then
			if #b.parameters > 0 then
				for _, v in ipairs(b.parameters) do
					table.insert(args, fromJson(v, nil))
				end
			else
				for _, v in pairs(b.parameters) do
					table.insert(args, fromJson(v, nil))
				end
			end
		end
		local ok, f = pcall(function()
			return inst[fname]
		end)
		if not ok or type(f) ~= "function" then
			fail(400, "Function: " .. tostring(fname) .. " does not exist on object: " .. pathOf(inst))
		end
		local okC, ret = pcall(f, inst, table.unpack(args))
		if not okC then
			fail(400, tostring(fname) .. " failed: " .. tostring(ret))
		end
		return { ReturnValue = toJson(ret) }
	end
	fail(400, "unknown rc verb " .. tostring(verb))
end

-- The game's log since the last drain (LogService.MessageOut in this datamodel)
function ops.logs()
	local out = AP.logs
	AP.logs = {}
	local dropped = AP.logDropped
	AP.logDropped = 0
	return { logs = out, dropped = dropped }
end

-- Scripts and their sources (the codex): every LuaSourceContainer under the roots, without third-party packages
-- (Wally's Packages / ServerPackages / DevPackages folders) and with sources over MAX_SOURCE left out
local PACKAGES = { Packages = true, ServerPackages = true, DevPackages = true, _Index = true }
local MAX_SOURCE = 400000
local function skipped(d, root)
	local x = d.Parent
	while x and x ~= root do
		if PACKAGES[x.Name] then
			return true
		end
		x = x.Parent
	end
	return false
end

function ops.scripts(a)
	local roots = a.roots or { "ServerScriptService", "ReplicatedStorage", "ReplicatedFirst", "StarterPlayer", "StarterGui", "StarterPack", "ServerStorage", "Workspace" }
	local out = {}
	for _, rn in ipairs(roots) do
		local r = game:FindFirstChild(rn) or game:FindService(rn)
		if r then
			for _, d in ipairs(r:GetDescendants()) do
				if d:IsA("LuaSourceContainer") and not skipped(d, r) then
					local ok, src = pcall(function()
						return d.Source
					end)
					src = ok and src or ""
					if #src > MAX_SOURCE then
						table.insert(out, { path = pathOf(d), class = d.ClassName, too_big = #src,
							source = "-- (" .. #src .. " bytes: too big for the codex; a data module, most likely)\n" })
					else
						table.insert(out, { path = pathOf(d), class = d.ClassName, source = src })
					end
				end
			end
		end
	end
	return { scripts = out }
end

-- A summary of the game (the codex's summary.txt)
function ops.summary()
	local lines = {}
	local function tree(inst, depth, maxDepth)
		if depth > maxDepth then
			return
		end
		local kids = inst:GetChildren()
		local counts = {}
		for i, c in ipairs(kids) do
			if i > 40 then
				table.insert(lines, string.rep("  ", depth) .. "... (" .. (#kids - 40) .. " more)")
				break
			end
			if c:IsA("BasePart") and #kids > 20 then
				counts[c.ClassName] = (counts[c.ClassName] or 0) + 1
			else
				table.insert(lines, string.rep("  ", depth) .. pathOf(c) .. " (" .. c.ClassName .. ")")
				tree(c, depth + 1, maxDepth)
			end
		end
		for cls, n in pairs(counts) do
			table.insert(lines, string.rep("  ", depth) .. n .. " x " .. cls)
		end
	end
	table.insert(lines, "# The game's instance tree (Workspace and ReplicatedStorage, top levels)")
	tree(workspace, 0, 2)
	tree(game:GetService("ReplicatedStorage"), 0, 1)
	table.insert(lines, "")
	table.insert(lines, "# Remotes (client <-> server messages)")
	for _, d in ipairs(game:GetService("ReplicatedStorage"):GetDescendants()) do
		if d:IsA("RemoteEvent") or d:IsA("RemoteFunction") or d:IsA("UnreliableRemoteEvent") then
			table.insert(lines, pathOf(d) .. " (" .. d.ClassName .. ")")
		end
	end
	table.insert(lines, "")
	table.insert(lines, "# Tags (CollectionService) on things in Workspace")
	local tags = {}
	for _, d in ipairs(workspace:GetDescendants()) do
		for _, t in ipairs(CollectionService:GetTags(d)) do
			tags[t] = (tags[t] or 0) + 1
		end
	end
	for t, n in pairs(tags) do
		table.insert(lines, t .. ": " .. n)
	end
	table.insert(lines, "")
	table.insert(lines, "# Proximity prompts (hold their key near them)")
	for _, d in ipairs(workspace:GetDescendants()) do
		if d:IsA("ProximityPrompt") then
			table.insert(lines, pathOf(d) .. ": " .. d.ActionText .. " " .. d.ObjectText .. " [" .. d.KeyboardKeyCode.Name .. "]")
		end
	end
	return { text = table.concat(lines, "\n") }
end

-- -- calls ---------------------------------------------------------------------------------------------------

local PAGE = 60000

function AP.call(op, argsJson)
	local f = ops[op]
	local env
	if f == nil then
		env = { ok = false, status = 400, error = "unknown op " .. tostring(op) }
	else
		local okA, a = pcall(function()
			return (argsJson and argsJson ~= "") and HS:JSONDecode(argsJson) or {}
		end)
		if not okA then
			a = {}
		end
		pathCache, thingCache, sizeCache = {}, {}, {}
		local ok, res = pcall(f, a)
		pathCache, thingCache, sizeCache = nil, nil, nil
		if ok then
			env = { ok = true, result = res }
		elseif type(res) == "table" then
			env = { ok = false, status = res.status or 400, error = res.error or "error" }
		else
			env = { ok = false, status = 500, error = tostring(res) }
		end
	end
	env.t = workspace.DistributedGameTime
	local s = HS:JSONEncode(env)
	if #s <= PAGE then
		return s
	end
	AP.nextPage += 1
	local id = tostring(AP.nextPage)
	local chunks = {}
	for i = 1, #s, PAGE do
		table.insert(chunks, string.sub(s, i, i + PAGE - 1))
	end
	AP.pages[id] = chunks
	return HS:JSONEncode({ paged = id, count = #chunks })
end

function AP.page(id, i)
	local chunks = AP.pages[id]
	if chunks == nil then
		return ""
	end
	local s = chunks[i] or ""
	if i >= #chunks then
		AP.pages[id] = nil
	end
	return s
end

function AP.stop()
	for _, c in ipairs(AP.conns) do
		c:Disconnect()
	end
end

table.insert(AP.conns, LogService.MessageOut:Connect(function(msg, kind)
	if kind == Enum.MessageType.MessageError or kind == Enum.MessageType.MessageWarning then
		if #AP.logs < 500 then
			table.insert(AP.logs, {
				t = workspace.DistributedGameTime,
				verbosity = kind == Enum.MessageType.MessageError and "Error" or "Warning",
				message = string.sub(msg, 1, 2000),
				side = ROLE,
			})
		else
			AP.logDropped += 1
		end
	end
end))

_G.__AP = AP
return "installed"
