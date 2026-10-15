-- Client-side juice: hit bursts, BONK popups, shockwaves, KO blasts, camera shake.

local Players = game:GetService("Players")
local ReplicatedStorage = game:GetService("ReplicatedStorage")
local RunService = game:GetService("RunService")
local TweenService = game:GetService("TweenService")
local Debris = game:GetService("Debris")
local Workspace = game:GetService("Workspace")

local Config = require(ReplicatedStorage.Shared.Config)

local player = Players.LocalPlayer
local camera = Workspace.CurrentCamera

local Effects = {}

local fxFolder = Instance.new("Folder")
fxFolder.Name = "ClientFX"
fxFolder.Parent = Workspace

local SPARK = "rbxasset://textures/particles/sparkles_main.dds"
local BONK_WORDS = { "BONK!", "BOINK!", "WHAM!", "POW!", "THWACK!", "BAM!" }

---------------------------------------------------------------------------
-- Camera shake
---------------------------------------------------------------------------
local shakeAmount = 0
function Effects.Shake(amount: number)
	shakeAmount = math.max(shakeAmount, amount)
end

RunService:BindToRenderStep("OrbitBonkShake", Enum.RenderPriority.Camera.Value + 1, function(dt)
	if shakeAmount > 0.001 then
		local a = shakeAmount
		camera.CFrame = camera.CFrame
			* CFrame.Angles(
				math.rad((math.random() - 0.5) * a * 2),
				math.rad((math.random() - 0.5) * a * 2),
				math.rad((math.random() - 0.5) * a)
			)
		shakeAmount = math.max(0, shakeAmount - dt * 6 * math.max(1, a))
	end
end)

---------------------------------------------------------------------------
-- Helpers
---------------------------------------------------------------------------
function Effects.Sound(id: string, position: Vector3?, volume: number?, pitch: number?)
	local s = Instance.new("Sound")
	s.SoundId = id
	s.Volume = volume or 0.6
	s.PlaybackSpeed = pitch or 1
	if position then
		local holder = Instance.new("Part")
		holder.Anchored = true
		holder.CanCollide = false
		holder.CanQuery = false
		holder.CanTouch = false
		holder.Transparency = 1
		holder.Size = Vector3.one
		holder.CFrame = CFrame.new(position)
		holder.Parent = fxFolder
		s.RollOffMinDistance = 20
		s.RollOffMaxDistance = 250
		s.Parent = holder
		Debris:AddItem(holder, 4)
	else
		s.Parent = fxFolder
		Debris:AddItem(s, 4)
	end
	s:Play()
end

local function anchoredPart(props: { [string]: any }): Part
	local p = Instance.new("Part")
	p.Anchored = true
	p.CanCollide = false
	p.CanQuery = false
	p.CanTouch = false
	p.CastShadow = false
	p.Material = Enum.Material.Neon
	for k, v in props do
		(p :: any)[k] = v
	end
	p.Parent = fxFolder
	return p
end

local function burst(position: Vector3, color: Color3, count: number, speed: number)
	local holder = anchoredPart({ Size = Vector3.one, Transparency = 1, CFrame = CFrame.new(position) })
	local att = Instance.new("Attachment")
	att.Parent = holder
	local pe = Instance.new("ParticleEmitter")
	pe.Texture = SPARK
	pe.Color = ColorSequence.new(color)
	pe.LightEmission = 1
	pe.Size = NumberSequence.new({ NumberSequenceKeypoint.new(0, 1.4), NumberSequenceKeypoint.new(1, 0) })
	pe.Transparency = NumberSequence.new(0, 1)
	pe.Lifetime = NumberRange.new(0.3, 0.7)
	pe.Speed = NumberRange.new(speed * 0.5, speed)
	pe.SpreadAngle = Vector2.new(180, 180)
	pe.Drag = 4
	pe.Rate = 0
	pe.Parent = att
	pe:Emit(count)
	Debris:AddItem(holder, 1.5)
end

local function ring(position: Vector3, color: Color3, fromSize: number, toSize: number, time: number, thickness: number?)
	local r = anchoredPart({
		Shape = Enum.PartType.Cylinder,
		Size = Vector3.new(thickness or 0.4, fromSize, fromSize),
		CFrame = CFrame.new(position) * CFrame.Angles(0, 0, math.rad(90)),
		Color = color,
		Transparency = 0.1,
	})
	TweenService:Create(r, TweenInfo.new(time, Enum.EasingStyle.Quad, Enum.EasingDirection.Out), {
		Size = Vector3.new(thickness or 0.4, toSize, toSize),
		Transparency = 1,
	}):Play()
	Debris:AddItem(r, time + 0.1)
end

local function sphere(position: Vector3, color: Color3, fromSize: number, toSize: number, time: number)
	local s = anchoredPart({
		Shape = Enum.PartType.Ball,
		Size = Vector3.one * fromSize,
		CFrame = CFrame.new(position),
		Color = color,
		Transparency = 0.2,
	})
	TweenService:Create(s, TweenInfo.new(time, Enum.EasingStyle.Quad, Enum.EasingDirection.Out), {
		Size = Vector3.one * toSize,
		Transparency = 1,
	}):Play()
	Debris:AddItem(s, time + 0.1)
end

local function popupText(position: Vector3, text: string, color: Color3, size: number)
	local holder = anchoredPart({ Size = Vector3.one, Transparency = 1, CFrame = CFrame.new(position) })
	local gui = Instance.new("BillboardGui")
	gui.Size = UDim2.fromOffset(200 * size, 70 * size)
	gui.AlwaysOnTop = true
	gui.LightInfluence = 0
	gui.MaxDistance = 300
	gui.Parent = holder
	local l = Instance.new("TextLabel")
	l.BackgroundTransparency = 1
	l.Size = UDim2.fromScale(1, 1)
	l.Font = Enum.Font.FredokaOne
	l.TextScaled = true
	l.Text = text
	l.TextColor3 = color
	l.Rotation = math.random(-14, 14)
	l.Parent = gui
	local st = Instance.new("UIStroke")
	st.Thickness = 3
	st.Color = Color3.fromRGB(20, 10, 40)
	st.Parent = l
	local scale = Instance.new("UIScale")
	scale.Scale = 0.3
	scale.Parent = l
	TweenService:Create(scale, TweenInfo.new(0.25, Enum.EasingStyle.Back, Enum.EasingDirection.Out), { Scale = 1 }):Play()
	TweenService:Create(gui, TweenInfo.new(0.8, Enum.EasingStyle.Quad, Enum.EasingDirection.Out), {
		StudsOffset = Vector3.new(0, 4, 0),
	}):Play()
	task.delay(0.5, function()
		TweenService:Create(l, TweenInfo.new(0.3), { TextTransparency = 1 }):Play()
		TweenService:Create(st, TweenInfo.new(0.3), { Transparency = 1 }):Play()
	end)
	Debris:AddItem(holder, 1)
end

local function isLocal(model: Instance?): boolean
	return model ~= nil and model == player.Character
end

---------------------------------------------------------------------------
-- Effect handlers
---------------------------------------------------------------------------
local handlers = {}

function handlers.Swing(data)
	Effects.Sound(Config.Sounds.Swing, data.Position, 0.45, 0.9 + math.random() * 0.2)
end

function handlers.Hit(data)
	local pos: Vector3 = data.Position
	local strength: number = data.Strength or 0.5
	local color = Config.PercentColor(data.Percent or 0)
	sphere(pos, color, 1, 6 + strength * 5, 0.25)
	burst(pos, color, math.floor(14 + strength * 20), 30 + strength * 30)
	popupText(pos + Vector3.new(0, 2, 0), BONK_WORDS[math.random(1, #BONK_WORDS)], color, 0.7 + strength * 0.5)
	Effects.Sound(Config.Sounds.Hit, pos, 0.7, 1.15 - math.min(strength, 1) * 0.35)
	if isLocal(data.Victim) then
		Effects.Shake(0.8 + strength * 1.5)
	elseif isLocal(data.Attacker) then
		Effects.Shake(0.35 + strength * 0.5)
		-- Hit-stop: a tiny freeze-frame feel for the attacker
		local hum = player.Character and player.Character:FindFirstChildOfClass("Humanoid")
		if hum then
			local speed = hum.WalkSpeed
			hum.WalkSpeed = speed * 0.3
			task.delay(0.06, function()
				if hum.Parent and hum.WalkSpeed == speed * 0.3 then
					hum.WalkSpeed = speed
				end
			end)
		end
	end
end

function handlers.Blocked(data)
	sphere(data.Position, Color3.new(1, 1, 1), 4, 8, 0.25)
	popupText(data.Position + Vector3.new(0, 2, 0), "SHIELD", Config.Colors.Cyan, 0.7)
end

function handlers.BubblePop(data)
	sphere(data.Position, Config.PowerUps.Bubble.Color, 7, 13, 0.35)
	burst(data.Position, Config.PowerUps.Bubble.Color, 30, 40)
	popupText(data.Position + Vector3.new(0, 2, 0), "POP!", Config.PowerUps.Bubble.Color, 0.9)
	Effects.Sound(Config.Sounds.PowerUp, data.Position, 0.6, 0.7)
end

function handlers.Slam(data)
	local pos: Vector3 = data.Position - Vector3.new(0, 2.5, 0)
	ring(pos, Config.Colors.Pink, 2, data.Radius * 2, 0.45, 0.6)
	ring(pos + Vector3.new(0, 0.3, 0), Config.Colors.Cyan, 2, data.Radius * 1.5, 0.35, 0.4)
	burst(pos, Config.Colors.Pink, 40, 50)
	Effects.Sound(Config.Sounds.Hit, pos, 0.9, 0.6)
	local cam = camera.CFrame.Position
	local dist = (cam - pos).Magnitude
	if dist < 60 then
		Effects.Shake(0.6 * (1 - dist / 60) + 0.2)
	end
end

function handlers.Dash(data)
	local model: Model? = data.Model
	local hrp = model and model:FindFirstChild("HumanoidRootPart")
	if not hrp then
		return
	end
	local a0 = Instance.new("Attachment")
	a0.Position = Vector3.new(0, 1.2, 0)
	a0.Parent = hrp
	local a1 = Instance.new("Attachment")
	a1.Position = Vector3.new(0, -1.6, 0)
	a1.Parent = hrp
	local trail = Instance.new("Trail")
	trail.Attachment0 = a0
	trail.Attachment1 = a1
	trail.Color = ColorSequence.new(data.Color or Config.Colors.Cyan)
	trail.Transparency = NumberSequence.new(0.1, 1)
	trail.Lifetime = 0.3
	trail.LightEmission = 1
	trail.Parent = hrp
	Debris:AddItem(trail, 0.6)
	Debris:AddItem(a0, 0.6)
	Debris:AddItem(a1, 0.6)
	burst((hrp :: BasePart).Position, data.Color or Config.Colors.Cyan, 12, 15)
	Effects.Sound(Config.Sounds.Jump, (hrp :: BasePart).Position, 0.5, 1.4)
end

function handlers.PowerUp(data)
	local info = Config.PowerUps[data.Kind]
	if not info then
		return
	end
	sphere(data.Position, info.Color, 2, 10, 0.4)
	burst(data.Position, info.Color, 30, 30)
	popupText(data.Position + Vector3.new(0, 3, 0), info.Label, info.Color, 0.9)
	Effects.Sound(Config.Sounds.PowerUp, data.Position, 0.8, 1.2)
end

function handlers.KO(data)
	-- Smash-style blast line from the edge of the arena
	local pos: Vector3 = data.Position
	local center: Vector3 = data.Center
	local dir = (pos - center)
	if dir.Magnitude < 1 then
		dir = Vector3.new(0, -1, 0)
	end
	local beam = anchoredPart({
		Size = Vector3.new(6, 6, 80),
		CFrame = CFrame.lookAt(pos - dir.Unit * 40, pos),
		Color = Config.Colors.Gold,
		Transparency = 0.1,
	})
	TweenService:Create(beam, TweenInfo.new(0.7, Enum.EasingStyle.Quad, Enum.EasingDirection.Out), {
		Size = Vector3.new(0.2, 0.2, 140),
		Transparency = 1,
	}):Play()
	Debris:AddItem(beam, 0.8)
	sphere(pos, Config.Colors.Gold, 4, 30, 0.6)
	burst(pos, Config.Colors.Pink, 60, 80)
	Effects.Sound(Config.Sounds.KO, pos, 1, 1)
	Effects.Shake(0.5)
end

function handlers.Victory(data)
	local model: Model? = data.Model
	local hrp = model and model:FindFirstChild("HumanoidRootPart") :: BasePart?
	if not hrp then
		return
	end
	for i = 1, 6 do
		task.delay(i * 0.25, function()
			if hrp.Parent then
				local colors = { Config.Colors.Cyan, Config.Colors.Pink, Config.Colors.Gold }
				burst(hrp.Position + Vector3.new(0, 6, 0), colors[(i % 3) + 1], 50, 45)
				ring(hrp.Position - Vector3.new(0, 2.5, 0), colors[(i % 3) + 1], 2, 24, 0.6, 0.3)
			end
		end)
	end
end

function Effects.Handle(kind: string, data: { [string]: any })
	local fn = handlers[kind]
	if fn then
		local ok, err = pcall(fn, data)
		if not ok then
			warn("[OrbitBonk] effect error", kind, err)
		end
	end
end

-- Local-only effects used by controls
function Effects.DoubleJump(position: Vector3)
	ring(position - Vector3.new(0, 2.8, 0), Config.Colors.Cyan, 2, 9, 0.3, 0.3)
	Effects.Sound(Config.Sounds.Jump, position, 0.5, 1.25)
end

return Effects
