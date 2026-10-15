-- The battle arena: a floating disc of hexagon tiles that collapses ring by ring.

local ReplicatedStorage = game:GetService("ReplicatedStorage")
local TweenService = game:GetService("TweenService")
local Workspace = game:GetService("Workspace")

local Config = require(ReplicatedStorage.Shared.Config)

local Arena = {}

type Tile = { Model: Model, Ring: number, Center: Vector3, Alive: boolean }

local folder: Model? = nil
local tiles: { Tile } = {}

local SQRT3 = math.sqrt(3)
local THICKNESS = 2.5
local GAP = 0.35

-- Ring colours: centre is hottest, outside is cool
local RING_COLORS = {
	[0] = Color3.fromRGB(255, 90, 200),
	Color3.fromRGB(190, 90, 255),
	Color3.fromRGB(120, 110, 255),
	Color3.fromRGB(70, 140, 255),
	Color3.fromRGB(40, 190, 255),
	Color3.fromRGB(30, 230, 230),
	Color3.fromRGB(40, 255, 180),
}

local function hexDistance(q: number, r: number): number
	return math.max(math.abs(q), math.abs(r), math.abs(q + r))
end

-- A flat-top hexagon is three rectangles rotated 60° apart.
local function makeHex(center: Vector3, side: number, height: number, props: { [string]: any }, parent: Instance)
	for i = 0, 2 do
		local p = Instance.new("Part")
		p.Anchored = true
		p.Size = Vector3.new(side, height, side * SQRT3)
		p.CFrame = CFrame.new(center) * CFrame.Angles(0, math.rad(60 * i), 0)
		p.TopSurface = Enum.SurfaceType.Smooth
		p.BottomSurface = Enum.SurfaceType.Smooth
		for k, v in props do
			(p :: any)[k] = v
		end
		p.Parent = parent
	end
end

function Arena.Build()
	Arena.Clear()
	local model = Instance.new("Model")
	model.Name = "Arena"
	folder = model

	local s = Config.HexSide
	local c = Config.ArenaCenter
	local rings = Config.ArenaRings
	for q = -rings, rings do
		for r = -rings, rings do
			local ring = hexDistance(q, r)
			if ring <= rings then
				local center = c + Vector3.new(s * 1.5 * q, 0, s * SQRT3 * (r + q / 2))
				local tile = Instance.new("Model")
				tile.Name = "Tile"
				tile:SetAttribute("Ring", ring)
				local color = RING_COLORS[ring] or RING_COLORS[#RING_COLORS]
				-- Glowing base under the gaps (also catches feet so the gaps are safe)
				makeHex(center - Vector3.new(0, 0.6, 0), s, THICKNESS, {
					Name = "Glow",
					Color = color,
					Material = Enum.Material.Neon,
				}, tile)
				-- Top plate, slightly smaller so the neon seams show
				makeHex(center + Vector3.new(0, 0.35, 0), s - GAP, THICKNESS - 0.2, {
					Name = "Top",
					Color = Color3.fromRGB(28, 30, 58):Lerp(color, 0.12),
					Material = Enum.Material.Glass,
					Reflectance = 0.05,
				}, tile)
				tile.Parent = model
				table.insert(tiles, { Model = tile, Ring = ring, Center = center + Vector3.new(0, 1.6, 0), Alive = true })
			end
		end
	end

	-- Decorative under-structure: inverted crystal
	local crystal = Instance.new("Part")
	crystal.Name = "Crystal"
	crystal.Anchored = true
	crystal.CanCollide = false
	crystal.Shape = Enum.PartType.Ball
	crystal.Size = Vector3.new(30, 30, 30)
	crystal.CFrame = CFrame.new(c - Vector3.new(0, 22, 0))
	crystal.Color = Color3.fromRGB(40, 30, 80)
	crystal.Material = Enum.Material.Glass
	crystal.Transparency = 0.25
	crystal.Parent = model
	local core = crystal:Clone()
	core.Name = "Core"
	core.Size = Vector3.new(12, 12, 12)
	core.Color = Config.Colors.Pink
	core.Material = Enum.Material.Neon
	core.Transparency = 0
	core.Parent = model
	local light = Instance.new("PointLight")
	light.Color = Config.Colors.Pink
	light.Range = 40
	light.Brightness = 3
	light.Parent = core

	model.Parent = Workspace
	return model
end

function Arena.Clear()
	if folder then
		folder:Destroy()
		folder = nil
	end
	table.clear(tiles)
end

-- Spawn points spread evenly around a middle ring
function Arena.SpawnPoints(count: number): { CFrame }
	local c = Config.ArenaCenter
	local radius = Config.HexSide * 1.5 * 3
	local points = {}
	for i = 1, count do
		local a = (i / count) * math.pi * 2
		local pos = c + Vector3.new(math.cos(a) * radius, 4.6, math.sin(a) * radius)
		table.insert(points, CFrame.lookAt(pos, Vector3.new(c.X, pos.Y, c.Z)))
	end
	return points
end

function Arena.RandomTileTop(): Vector3?
	local alive = {}
	for _, t in tiles do
		if t.Alive then
			table.insert(alive, t)
		end
	end
	if #alive == 0 then
		return nil
	end
	return alive[math.random(1, #alive)].Center
end

function Arena.OuterRing(): number
	local outer = -1
	for _, t in tiles do
		if t.Alive and t.Ring > outer then
			outer = t.Ring
		end
	end
	return outer
end

-- Warn then drop the outermost ring. Never removes the centre tile.
function Arena.CollapseOuterRing(): number?
	local ring = Arena.OuterRing()
	if ring <= 0 then
		return nil
	end
	local doomed = {}
	for _, t in tiles do
		if t.Alive and t.Ring == ring then
			t.Alive = false
			table.insert(doomed, t)
		end
	end

	-- Warning flash
	for _, t in doomed do
		for _, p in t.Model:GetChildren() do
			if p:IsA("BasePart") and p.Name == "Glow" then
				TweenService:Create(
					p,
					TweenInfo.new(0.25, Enum.EasingStyle.Sine, Enum.EasingDirection.InOut, -1, true),
					{ Color = Config.Colors.Danger }
				):Play()
			end
		end
	end

	task.delay(Config.CollapseWarning, function()
		for _, t in doomed do
			if t.Model.Parent then
				for _, p in t.Model:GetChildren() do
					if p:IsA("BasePart") then
						p.Anchored = false
						p.CanCollide = false
						p.AssemblyLinearVelocity = Vector3.new(0, -math.random(5, 20), 0)
						p.AssemblyAngularVelocity = Vector3.new(math.random(-3, 3), math.random(-3, 3), math.random(-3, 3))
					end
				end
				task.delay(4, function()
					t.Model:Destroy()
				end)
			end
		end
	end)
	return ring
end

function Arena.IsOnArena(position: Vector3): boolean
	-- Is there a living tile roughly underneath this point?
	local s = Config.HexSide
	for _, t in tiles do
		if t.Alive then
			local d = Vector3.new(position.X - t.Center.X, 0, position.Z - t.Center.Z).Magnitude
			if d < s * 0.85 then
				return true
			end
		end
	end
	return false
end

return Arena
