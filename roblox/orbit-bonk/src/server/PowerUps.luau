-- Glowing orbs that spawn on live tiles during a round.

local ReplicatedStorage = game:GetService("ReplicatedStorage")
local TweenService = game:GetService("TweenService")
local Workspace = game:GetService("Workspace")

local Config = require(ReplicatedStorage.Shared.Config)
local Combat = require(script.Parent.Combat)
local Arena = require(script.Parent.Arena)

local PowerUps = {}

local folder: Folder? = nil
local KINDS = { "Mega", "Zoom", "Bubble", "Feather" }

local function apply(f: Combat.Fighter, kind: string)
	local hum = Combat.Humanoid(f)
	local duration = Config.PowerUpDuration
	if kind == "Mega" then
		f.MegaUntil = os.clock() + duration
	elseif kind == "Bubble" then
		Combat.GiveBubble(f)
	elseif kind == "Zoom" and hum then
		local base = f.Player and game:GetService("StarterPlayer").CharacterWalkSpeed or 16
		hum.WalkSpeed = Config.ZoomSpeed
		task.delay(duration, function()
			if hum.Parent then
				hum.WalkSpeed = base
			end
		end)
	elseif kind == "Feather" and hum then
		Combat.SetPercent(f, f.Percent - 15)
		local base = f.Player and game:GetService("StarterPlayer").CharacterJumpHeight or 7.5
		hum.JumpHeight = Config.FeatherJump
		task.delay(duration, function()
			if hum.Parent then
				hum.JumpHeight = base
			end
		end)
	end
	if kind ~= "Bubble" then
		f.Model:SetAttribute("PowerUp", kind)
		f.Model:SetAttribute("PowerUpEnds", Workspace:GetServerTimeNow() + duration)
		task.delay(duration, function()
			if f.Model:GetAttribute("PowerUp") == kind then
				f.Model:SetAttribute("PowerUp", nil)
			end
		end)
	end
	local hrp = Combat.Root(f)
	Combat.FireEffect("PowerUp", {
		Position = hrp and hrp.Position or Vector3.zero,
		Kind = kind,
		Model = f.Model,
	})
end

function PowerUps.SpawnOne()
	if not folder then
		return
	end
	if #folder:GetChildren() >= Config.PowerUpMax then
		return
	end
	local pos = Arena.RandomTileTop()
	if not pos then
		return
	end
	local kind = KINDS[math.random(1, #KINDS)]
	local info = Config.PowerUps[kind]

	local orb = Instance.new("Part")
	orb.Name = kind
	orb.Shape = Enum.PartType.Ball
	orb.Size = Vector3.new(2.6, 2.6, 2.6)
	orb.Anchored = true
	orb.CanCollide = false
	orb.Material = Enum.Material.Neon
	orb.Color = info.Color
	orb.CFrame = CFrame.new(pos + Vector3.new(0, 2.6, 0))

	local shell = Instance.new("Part")
	shell.Name = "Shell"
	shell.Shape = Enum.PartType.Ball
	shell.Size = Vector3.new(3.6, 3.6, 3.6)
	shell.Anchored = true
	shell.CanCollide = false
	shell.CanTouch = false
	shell.CanQuery = false
	shell.Material = Enum.Material.ForceField
	shell.Color = info.Color
	shell.CFrame = orb.CFrame
	shell.Parent = orb

	local light = Instance.new("PointLight")
	light.Color = info.Color
	light.Range = 12
	light.Brightness = 2
	light.Parent = orb

	local gui = Instance.new("BillboardGui")
	gui.Size = UDim2.fromOffset(120, 30)
	gui.StudsOffset = Vector3.new(0, 2.8, 0)
	gui.LightInfluence = 0
	gui.MaxDistance = 120
	local label = Instance.new("TextLabel")
	label.BackgroundTransparency = 1
	label.Size = UDim2.fromScale(1, 1)
	label.Font = Enum.Font.FredokaOne
	label.TextScaled = true
	label.Text = info.Label
	label.TextColor3 = info.Color
	label.Parent = gui
	local stroke = Instance.new("UIStroke")
	stroke.Thickness = 2
	stroke.Parent = label
	gui.Parent = orb

	-- Bob up and down
	TweenService:Create(
		orb,
		TweenInfo.new(1, Enum.EasingStyle.Sine, Enum.EasingDirection.InOut, -1, true),
		{ CFrame = orb.CFrame + Vector3.new(0, 1.2, 0) }
	):Play()
	TweenService:Create(
		shell,
		TweenInfo.new(1, Enum.EasingStyle.Sine, Enum.EasingDirection.InOut, -1, true),
		{ CFrame = orb.CFrame + Vector3.new(0, 1.2, 0) }
	):Play()

	local taken = false
	orb.Touched:Connect(function(hit)
		if taken then
			return
		end
		local f = nil
		local node: Instance? = hit.Parent
		while node and node ~= Workspace and not f do
			f = Combat.FromModel(node)
			node = node.Parent
		end
		if not f or not f.Alive then
			return
		end
		taken = true
		orb:Destroy()
		apply(f, kind)
	end)

	orb.Parent = folder
	-- Orbs expire if nobody grabs them
	task.delay(20, function()
		if orb.Parent then
			orb:Destroy()
		end
	end)
end

function PowerUps.Start()
	PowerUps.Stop()
	local f = Instance.new("Folder")
	f.Name = "PowerUps"
	f.Parent = Workspace
	folder = f
end

function PowerUps.Stop()
	if folder then
		folder:Destroy()
		folder = nil
	end
end

-- Remove orbs that are hovering over a tile that fell away
function PowerUps.Prune()
	if not folder then
		return
	end
	for _, orb in folder:GetChildren() do
		if orb:IsA("BasePart") and not Arena.IsOnArena(orb.Position) then
			orb:Destroy()
		end
	end
end

return PowerUps
