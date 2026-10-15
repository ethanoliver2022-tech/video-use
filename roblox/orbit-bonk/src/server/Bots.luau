-- BonkBots: fill empty slots so a round is always fun, even solo.

local Players = game:GetService("Players")
local ReplicatedStorage = game:GetService("ReplicatedStorage")
local Workspace = game:GetService("Workspace")

local Config = require(ReplicatedStorage.Shared.Config)
local Combat = require(script.Parent.Combat)
local Arena = require(script.Parent.Arena)
local Hammer = require(script.Parent.Hammer)

local Bots = {}

-- Roblox's public default R15 animations
local ANIMS = {
	Idle = "rbxassetid://507766388",
	Run = "rbxassetid://507767714",
	Fall = "rbxassetid://507767968",
	Slash = "rbxassetid://522635514",
}

local botFolder: Folder? = nil
local stunToken: { [Model]: number } = {}

local function makeRig(name: string, color: Color3): Model?
	local desc = Instance.new("HumanoidDescription")
	desc.HeadColor = color
	desc.TorsoColor = color:Lerp(Color3.new(0, 0, 0), 0.25)
	desc.LeftArmColor = color
	desc.RightArmColor = color
	desc.LeftLegColor = Color3.fromRGB(40, 40, 70)
	desc.RightLegColor = Color3.fromRGB(40, 40, 70)
	local ok, rig = pcall(function()
		return Players:CreateHumanoidModelFromDescription(desc, Enum.HumanoidRigType.R15)
	end)
	if not ok or not rig then
		warn("[OrbitBonk] Could not build bot rig:", rig)
		return nil
	end
	rig.Name = name
	local animate = rig:FindFirstChild("Animate")
	if animate then
		animate:Destroy()
	end
	local hum = rig:FindFirstChildOfClass("Humanoid")
	if hum then
		hum.WalkSpeed = 16
		hum.UseJumpPower = false
		hum.JumpHeight = 7.5
		hum.BreakJointsOnDeath = false
	end
	-- Visor: a glowing strip across the face so bots read as robots
	local head = rig:FindFirstChild("Head") :: BasePart?
	if head then
		local face = head:FindFirstChild("face")
		if face then
			face:Destroy()
		end
		local visor = Instance.new("Part")
		visor.Name = "Visor"
		visor.Size = Vector3.new(1.05, 0.32, 0.3)
		visor.Material = Enum.Material.Neon
		visor.Color = Config.Colors.Cyan
		visor.CanCollide = false
		visor.CanQuery = false
		visor.Massless = true
		visor.CFrame = head.CFrame * CFrame.new(0, 0.1, -0.5)
		local w = Instance.new("WeldConstraint")
		w.Part0 = head
		w.Part1 = visor
		w.Parent = visor
		visor.Parent = rig
		local antenna = Instance.new("Part")
		antenna.Name = "Antenna"
		antenna.Shape = Enum.PartType.Ball
		antenna.Size = Vector3.new(0.45, 0.45, 0.45)
		antenna.Material = Enum.Material.Neon
		antenna.Color = Config.Colors.Pink
		antenna.CanCollide = false
		antenna.CanQuery = false
		antenna.Massless = true
		antenna.CFrame = head.CFrame * CFrame.new(0, 1.1, 0)
		local w2 = Instance.new("WeldConstraint")
		w2.Part0 = head
		w2.Part1 = antenna
		w2.Parent = antenna
		antenna.Parent = rig
	end
	return rig
end

local function loadAnims(hum: Humanoid): { [string]: AnimationTrack }
	local animator = hum:FindFirstChildOfClass("Animator") or Instance.new("Animator")
	animator.Parent = hum
	local tracks = {}
	for key, id in ANIMS do
		local anim = Instance.new("Animation")
		anim.AnimationId = id
		local ok, track = pcall(function()
			return animator:LoadAnimation(anim)
		end)
		if ok and track then
			tracks[key] = track
			if key == "Slash" then
				track.Priority = Enum.AnimationPriority.Action
			elseif key ~= "Idle" then
				track.Priority = Enum.AnimationPriority.Movement
			end
		end
	end
	return tracks
end

local function nearestTarget(self: Combat.Fighter, pos: Vector3): (Combat.Fighter?, number)
	local best, bestDist = nil, math.huge
	for _, f in Combat.Fighters do
		if f ~= self and f.Alive then
			local hrp = Combat.Root(f)
			if hrp then
				local d = (hrp.Position - pos).Magnitude
				-- Mild preference for real players so bots don't only fight each other
				if f.Player then
					d *= 0.85
				end
				if d < bestDist then
					best, bestDist = f, d
				end
			end
		end
	end
	return best, bestDist
end

local function think(f: Combat.Fighter, tracks: { [string]: AnimationTrack }, rng: Random)
	local model = f.Model
	local hum = model:FindFirstChildOfClass("Humanoid")
	local hrp = Combat.Root(f)
	if not hum or not hrp then
		return
	end
	local skill = rng:NextNumber(0.55, 0.9) -- each bot has its own personality
	local lastRecover = 0
	local center = Config.ArenaCenter

	hum.Running:Connect(function(speed)
		if not tracks.Run or not tracks.Idle then
			return
		end
		if speed > 1 then
			if not tracks.Run.IsPlaying then
				tracks.Run:Play(0.15)
				tracks.Idle:Stop(0.15)
			end
		else
			if not tracks.Idle.IsPlaying then
				tracks.Idle:Play(0.2)
				tracks.Run:Stop(0.2)
			end
		end
	end)
	if tracks.Idle then
		tracks.Idle:Play()
	end

	while f.Alive and model.Parent do
		task.wait(rng:NextNumber(0.12, 0.25))
		if not f.Alive or not model.Parent or Workspace:GetAttribute("Frozen") then
			continue
		end
		if hum.PlatformStand then
			continue
		end
		local pos = hrp.Position
		local onArena = Arena.IsOnArena(pos)

		-- Recovery: if launched off the edge, dash back toward the middle
		if not onArena and pos.Y < center.Y + 6 and os.clock() - lastRecover > Config.DashCooldown then
			if rng:NextNumber() < skill then
				lastRecover = os.clock()
				local toCenter = Vector3.new(center.X - pos.X, 0, center.Z - pos.Z)
				if toCenter.Magnitude > 0.1 then
					hrp.AssemblyLinearVelocity = toCenter.Unit * Config.DashSpeed * 0.75 + Vector3.new(0, 45, 0)
					Combat.Dash(f)
				end
			end
			continue
		end

		local target, dist = nearestTarget(f, pos)
		if not target then
			hum:MoveTo(center)
			continue
		end
		local thrp = Combat.Root(target)
		if not thrp then
			continue
		end
		local tpos = thrp.Position

		-- Don't chase someone off a cliff
		if Arena.IsOnArena(tpos) then
			hum:MoveTo(tpos + Vector3.new(rng:NextNumber(-1, 1), 0, rng:NextNumber(-1, 1)))
		else
			hum:MoveTo(center)
		end

		-- Avoid walking toward the edge when low on ground
		local ahead = pos + hum.MoveDirection * 5
		if hum.MoveDirection.Magnitude > 0.1 and not Arena.IsOnArena(ahead) then
			hum:MoveTo(center)
		end

		if dist < Config.SwingRange - 1.5 and rng:NextNumber() < skill then
			hrp.CFrame = CFrame.lookAt(pos, Vector3.new(tpos.X, pos.Y, tpos.Z))
			if os.clock() - f.LastSwing >= Config.SwingCooldown then
				Combat.Swing(f)
				if tracks.Slash then
					tracks.Slash:Play(0.05)
				end
			end
		end

		-- Slam when crowded
		if os.clock() - f.LastSlam > Config.SlamCooldown then
			local near = 0
			for _, other in Combat.Fighters do
				local ohrp = other ~= f and other.Alive and Combat.Root(other)
				if ohrp and (ohrp.Position - pos).Magnitude < Config.SlamRadius * 0.7 then
					near += 1
				end
			end
			if near >= 2 and rng:NextNumber() < skill * 0.5 then
				Combat.Slam(f)
			end
		end

		-- Random hop to look alive
		if rng:NextNumber() < 0.03 then
			hum.Jump = true
		end
	end
end

function Bots.Spawn(count: number, spawnPoints: { CFrame }, startIndex: number): { Combat.Fighter }
	if not botFolder or not botFolder.Parent then
		local folder = Instance.new("Folder")
		folder.Name = "Bots"
		folder.Parent = Workspace
		botFolder = folder
	end
	local fighters = {}
	local names = table.clone(Config.BotNames)
	local rng = Random.new()
	for i = 1, count do
		local name = table.remove(names, rng:NextInteger(1, #names)) or ("Bot" .. i)
		local color = Config.BotColors[rng:NextInteger(1, #Config.BotColors)]
		local rig = makeRig("BonkBot " .. name, color)
		if rig then
			local cf = spawnPoints[((startIndex + i - 2) % #spawnPoints) + 1]
			rig:PivotTo(cf)
			rig.Parent = botFolder
			local hrp = rig:FindFirstChild("HumanoidRootPart") :: BasePart?
			if hrp then
				pcall(function()
					hrp:SetNetworkOwner(nil)
				end)
			end
			local skin = Config.Skins[rng:NextInteger(1, math.min(4, #Config.Skins))].Id
			Hammer.AttachToRig(rig, skin)
			local f = Combat.Register(rig, nil, "🤖 " .. name, skin)
			table.insert(fighters, f)
			local hum = rig:FindFirstChildOfClass("Humanoid") :: Humanoid
			local tracks = loadAnims(hum)
			task.spawn(think, f, tracks, rng)
		end
	end
	return fighters
end

function Bots.Knocked(f: Combat.Fighter, velocity: Vector3, stun: number)
	local hum = f.Model:FindFirstChildOfClass("Humanoid")
	local hrp = Combat.Root(f)
	if not hum or not hrp then
		return
	end
	local token = (stunToken[f.Model] or 0) + 1
	stunToken[f.Model] = token
	hum.PlatformStand = true
	hrp.AssemblyLinearVelocity = velocity
	hrp.AssemblyAngularVelocity = Vector3.new(math.random(-8, 8), math.random(-4, 4), math.random(-8, 8))
	task.delay(stun, function()
		if stunToken[f.Model] == token and hum.Parent then
			hum.PlatformStand = false
			hum:ChangeState(Enum.HumanoidStateType.GettingUp)
		end
	end)
end

function Bots.Remove(f: Combat.Fighter)
	stunToken[f.Model] = nil
	f.Model:Destroy()
end

function Bots.Clear()
	table.clear(stunToken)
	if botFolder then
		botFolder:ClearAllChildren()
	end
end

function Bots.Init()
	Combat.OnBotKnocked = Bots.Knocked
end

return Bots
