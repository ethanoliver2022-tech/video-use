-- Builds the space lobby, lighting and backdrop at server start.
-- Everything is generated in code so the place file stays tiny and asset-free.

local Lighting = game:GetService("Lighting")
local ReplicatedStorage = game:GetService("ReplicatedStorage")
local Workspace = game:GetService("Workspace")

local Config = require(ReplicatedStorage.Shared.Config)

local World = {}

local worldFolder: Folder
World.LobbySpawn = nil :: SpawnLocation?

local function part(props: { [string]: any }): Part
	local p = Instance.new("Part")
	p.Anchored = true
	p.TopSurface = Enum.SurfaceType.Smooth
	p.BottomSurface = Enum.SurfaceType.Smooth
	for k, v in props do
		(p :: any)[k] = v
	end
	return p
end

local function setupLighting()
	for _, child in Lighting:GetChildren() do
		if child:IsA("Sky") or child:IsA("Atmosphere") or child:IsA("PostEffect") then
			child:Destroy()
		end
	end
	Lighting.ClockTime = 0
	Lighting.Brightness = 2
	Lighting.Ambient = Color3.fromRGB(60, 50, 110)
	Lighting.OutdoorAmbient = Color3.fromRGB(90, 80, 150)
	Lighting.EnvironmentDiffuseScale = 0.4
	Lighting.EnvironmentSpecularScale = 0.6
	Lighting.GlobalShadows = true
	Lighting.FogEnd = 100000

	local sky = Instance.new("Sky")
	sky.StarCount = 5000
	sky.CelestialBodiesShown = false
	sky.Parent = Lighting

	local atmosphere = Instance.new("Atmosphere")
	atmosphere.Density = 0.22
	atmosphere.Offset = 0.1
	atmosphere.Color = Color3.fromRGB(70, 40, 130)
	atmosphere.Decay = Color3.fromRGB(20, 10, 50)
	atmosphere.Glare = 0.3
	atmosphere.Haze = 1.2
	atmosphere.Parent = Lighting

	local bloom = Instance.new("BloomEffect")
	bloom.Intensity = 0.8
	bloom.Size = 30
	bloom.Threshold = 1.4
	bloom.Parent = Lighting

	local cc = Instance.new("ColorCorrectionEffect")
	cc.Saturation = 0.15
	cc.Contrast = 0.08
	cc.TintColor = Color3.fromRGB(240, 235, 255)
	cc.Parent = Lighting

	local sun = Instance.new("SunRaysEffect")
	sun.Intensity = 0.04
	sun.Parent = Lighting
end

local function buildLobby()
	local lobby = Instance.new("Model")
	lobby.Name = "Lobby"
	local c = Config.LobbyCenter

	-- Main deck: a big disc with a glowing rim
	part({
		Name = "Deck",
		Shape = Enum.PartType.Cylinder,
		Size = Vector3.new(3, 70, 70),
		CFrame = CFrame.new(c - Vector3.new(0, 1.5, 0)) * CFrame.Angles(0, 0, math.rad(90)),
		Color = Color3.fromRGB(34, 36, 64),
		Material = Enum.Material.Slate,
		Parent = lobby,
	})
	part({
		Name = "Rim",
		Shape = Enum.PartType.Cylinder,
		Size = Vector3.new(2.6, 72, 72),
		CFrame = CFrame.new(c - Vector3.new(0, 1.7, 0)) * CFrame.Angles(0, 0, math.rad(90)),
		Color = Config.Colors.Cyan,
		Material = Enum.Material.Neon,
		Parent = lobby,
	})
	part({
		Name = "Underbelly",
		Shape = Enum.PartType.Ball,
		Size = Vector3.new(60, 60, 60),
		CFrame = CFrame.new(c - Vector3.new(0, 26, 0)),
		Color = Color3.fromRGB(24, 22, 46),
		Material = Enum.Material.Basalt,
		CanCollide = false,
		Parent = lobby,
	})
	-- Inner pattern ring
	part({
		Name = "Inlay",
		Shape = Enum.PartType.Cylinder,
		Size = Vector3.new(0.2, 26, 26),
		CFrame = CFrame.new(c + Vector3.new(0, 0.05, 0)) * CFrame.Angles(0, 0, math.rad(90)),
		Color = Config.Colors.Pink,
		Material = Enum.Material.Neon,
		CanCollide = false,
		Parent = lobby,
	})
	part({
		Name = "InlayCore",
		Shape = Enum.PartType.Cylinder,
		Size = Vector3.new(0.25, 23, 23),
		CFrame = CFrame.new(c + Vector3.new(0, 0.06, 0)) * CFrame.Angles(0, 0, math.rad(90)),
		Color = Color3.fromRGB(34, 36, 64),
		Material = Enum.Material.Slate,
		CanCollide = false,
		Parent = lobby,
	})

	-- Invisible guard wall so nobody tumbles off the lobby
	local segments = 24
	for i = 1, segments do
		local a = (i / segments) * math.pi * 2
		local pos = c + Vector3.new(math.cos(a) * 35.5, 6, math.sin(a) * 35.5)
		part({
			Name = "Guard",
			Size = Vector3.new(1, 14, 9.6),
			CFrame = CFrame.lookAt(pos, Vector3.new(c.X, pos.Y, c.Z)) * CFrame.Angles(0, math.rad(90), 0),
			Transparency = 1,
			Parent = lobby,
		})
	end
	-- Glowing rail posts
	for i = 1, 16 do
		local a = (i / 16) * math.pi * 2
		local pos = c + Vector3.new(math.cos(a) * 34, 2, math.sin(a) * 34)
		part({
			Name = "Post",
			Size = Vector3.new(0.8, 4, 0.8),
			CFrame = CFrame.new(pos),
			Color = Config.Colors.Cyan,
			Material = Enum.Material.Neon,
			Parent = lobby,
		})
	end

	local spawn = Instance.new("SpawnLocation")
	spawn.Name = "LobbySpawn"
	spawn.Anchored = true
	spawn.Neutral = true
	spawn.Duration = 0
	spawn.Size = Vector3.new(12, 0.4, 12)
	spawn.CFrame = CFrame.new(c + Vector3.new(0, 0.2, 8))
	spawn.Color = Config.Colors.Cyan
	spawn.Material = Enum.Material.Neon
	spawn.Transparency = 0.6
	spawn.CanCollide = false
	spawn.TopSurface = Enum.SurfaceType.Smooth
	local decal = spawn:FindFirstChildOfClass("Decal")
	if decal then
		decal:Destroy()
	end
	spawn.Parent = lobby
	World.LobbySpawn = spawn

	-- Title sign facing the arena
	local sign = part({
		Name = "Sign",
		Size = Vector3.new(34, 11, 1),
		CFrame = CFrame.lookAt(c + Vector3.new(0, 12, -24), Config.ArenaCenter + Vector3.new(0, 12, 0)),
		Color = Color3.fromRGB(14, 16, 34),
		Material = Enum.Material.SmoothPlastic,
		Parent = lobby,
	})
	local function signFace(face: Enum.NormalId)
		local gui = Instance.new("SurfaceGui")
		gui.Face = face
		gui.LightInfluence = 0
		gui.Brightness = 2
		gui.PixelsPerStud = 40
		gui.SizingMode = Enum.SurfaceGuiSizingMode.PixelsPerStud
		gui.Parent = sign
		local title = Instance.new("TextLabel")
		title.BackgroundTransparency = 1
		title.Size = UDim2.fromScale(1, 0.7)
		title.Font = Enum.Font.FredokaOne
		title.Text = Config.GameName
		title.TextScaled = true
		title.TextColor3 = Config.Colors.Cyan
		title.Parent = gui
		local stroke = Instance.new("UIStroke")
		stroke.Color = Config.Colors.Pink
		stroke.Thickness = 6
		stroke.Parent = title
		local sub = Instance.new("TextLabel")
		sub.BackgroundTransparency = 1
		sub.Position = UDim2.fromScale(0, 0.68)
		sub.Size = UDim2.fromScale(1, 0.26)
		sub.Font = Enum.Font.GothamBold
		sub.Text = "BONK THEM INTO ORBIT  •  LAST ONE STANDING WINS"
		sub.TextScaled = true
		sub.TextColor3 = Config.Colors.Text
		sub.Parent = gui
	end
	signFace(Enum.NormalId.Front)
	signFace(Enum.NormalId.Back)
	for _, x in { -15, 15 } do
		part({
			Name = "SignLeg",
			Size = Vector3.new(1, 8, 1),
			CFrame = sign.CFrame * CFrame.new(x, -9, 0),
			Color = Config.Colors.Pink,
			Material = Enum.Material.Neon,
			Parent = lobby,
		})
	end

	-- How-to-play board
	local board = part({
		Name = "HowTo",
		Size = Vector3.new(18, 10, 0.6),
		CFrame = CFrame.lookAt(c + Vector3.new(24, 6, 14), c + Vector3.new(0, 6, 14)),
		Color = Color3.fromRGB(14, 16, 34),
		Parent = lobby,
	})
	local gui = Instance.new("SurfaceGui")
	gui.Face = Enum.NormalId.Front
	gui.LightInfluence = 0
	gui.PixelsPerStud = 40
	gui.SizingMode = Enum.SurfaceGuiSizingMode.PixelsPerStud
	gui.Parent = board
	local txt = Instance.new("TextLabel")
	txt.BackgroundTransparency = 1
	txt.Size = UDim2.new(1, -40, 1, -40)
	txt.Position = UDim2.fromOffset(20, 20)
	txt.Font = Enum.Font.GothamBold
	txt.RichText = true
	txt.TextScaled = true
	txt.TextXAlignment = Enum.TextXAlignment.Left
	txt.TextColor3 = Config.Colors.Text
	txt.Text = '<font color="#00E6FF">HOW TO BONK</font>\n'
		.. "Click / Tap  –  Swing hammer\n"
		.. "Q  –  Dash (works mid-air!)\n"
		.. "E  –  Shockwave slam\n"
		.. "Jump twice  –  Double jump\n"
		.. '<font color="#FF46BE">Higher % = you fly further</font>\n'
		.. "Grab glowing orbs for power-ups"
	txt.Parent = gui

	lobby.Parent = worldFolder
end

local function buildBackdrop()
	local backdrop = Instance.new("Folder")
	backdrop.Name = "Backdrop"
	local rng = Random.new(42)

	-- Planets
	local planets = {
		{ Vector3.new(-420, 160, -520), 260, Color3.fromRGB(255, 100, 160), Enum.Material.Sand },
		{ Vector3.new(520, 40, -260), 140, Color3.fromRGB(80, 200, 255), Enum.Material.Glacier },
		{ Vector3.new(200, 320, 560), 90, Color3.fromRGB(255, 200, 90), Enum.Material.Rock },
	}
	for _, pl in planets do
		local p = part({
			Name = "Planet",
			Shape = Enum.PartType.Ball,
			Size = Vector3.one * pl[2],
			CFrame = CFrame.new(pl[1]),
			Color = pl[3],
			Material = pl[4],
			CanCollide = false,
			CastShadow = false,
			Parent = backdrop,
		})
		-- Ring around the first planet
		if pl == planets[1] then
			part({
				Name = "PlanetRing",
				Shape = Enum.PartType.Cylinder,
				Size = Vector3.new(2, pl[2] * 1.9, pl[2] * 1.9),
				CFrame = p.CFrame * CFrame.Angles(math.rad(20), 0, math.rad(80)),
				Color = Color3.fromRGB(255, 190, 230),
				Material = Enum.Material.Neon,
				Transparency = 0.55,
				CanCollide = false,
				CastShadow = false,
				Parent = backdrop,
			})
		end
	end

	-- Drifting asteroids below and around the arena
	for _ = 1, 45 do
		local angle = rng:NextNumber(0, math.pi * 2)
		local dist = rng:NextNumber(90, 300)
		local pos = Config.ArenaCenter
			+ Vector3.new(math.cos(angle) * dist, rng:NextNumber(-110, 70), math.sin(angle) * dist)
		if (pos - Config.LobbyCenter).Magnitude > 70 then
			local s = rng:NextNumber(3, 16)
			part({
				Name = "Asteroid",
				Size = Vector3.new(s, s * rng:NextNumber(0.6, 1), s * rng:NextNumber(0.7, 1.2)),
				CFrame = CFrame.new(pos)
					* CFrame.Angles(rng:NextNumber(0, 6), rng:NextNumber(0, 6), rng:NextNumber(0, 6)),
				Color = Color3.fromRGB(70, 62, 96):Lerp(Color3.fromRGB(110, 90, 130), rng:NextNumber()),
				Material = Enum.Material.Slate,
				CanCollide = false,
				Parent = backdrop,
			})
		end
	end

	-- Energy pillar under the arena
	part({
		Name = "Beam",
		Shape = Enum.PartType.Cylinder,
		Size = Vector3.new(400, 6, 6),
		CFrame = CFrame.new(Config.ArenaCenter - Vector3.new(0, 210, 0)) * CFrame.Angles(0, 0, math.rad(90)),
		Color = Config.Colors.Pink,
		Material = Enum.Material.Neon,
		Transparency = 0.4,
		CanCollide = false,
		CastShadow = false,
		Parent = backdrop,
	})

	backdrop.Parent = worldFolder
end

function World.Init()
	worldFolder = Instance.new("Folder")
	worldFolder.Name = "World"
	worldFolder.Parent = Workspace

	-- Remove any template baseplate so the place is pure space
	for _, name in { "Baseplate", "SpawnLocation" } do
		local obj = Workspace:FindFirstChild(name)
		if obj and obj ~= World.LobbySpawn then
			obj:Destroy()
		end
	end

	setupLighting()
	buildLobby()
	buildBackdrop()
end

function World.LobbyCFrame(index: number?): CFrame
	local i = index or math.random(1, 12)
	local a = (i / 12) * math.pi * 2
	local c = Config.LobbyCenter
	local pos = c + Vector3.new(math.cos(a) * 10, 4, 8 + math.sin(a) * 6)
	return CFrame.lookAt(pos, Vector3.new(Config.ArenaCenter.X, pos.Y, Config.ArenaCenter.Z))
end

return World
