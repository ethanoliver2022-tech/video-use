-- Fighters, hits, knockback and KO credit. Server authoritative.

local Players = game:GetService("Players")
local ReplicatedStorage = game:GetService("ReplicatedStorage")
local Workspace = game:GetService("Workspace")

local Config = require(ReplicatedStorage.Shared.Config)
local Remotes = ReplicatedStorage.Remotes

local Combat = {}

export type Fighter = {
	Model: Model,
	Player: Player?,
	Name: string,
	Alive: boolean,
	Percent: number,
	LastHitBy: Fighter?,
	LastHitTime: number,
	ShieldUntil: number, -- spawn protection
	Bubble: boolean, -- power-up shield, blocks one hit
	MegaUntil: number,
	LastSwing: number,
	LastSlam: number,
	LastDash: number,
	KOs: number,
	Placement: number?,
	Skin: string,
}

Combat.Fighters = {} :: { Fighter }

-- Hooks filled in by the round manager / bots
Combat.OnBotKnocked = nil :: ((Fighter, Vector3, number) -> ())?

local function root(f: Fighter): BasePart?
	local hrp = f.Model:FindFirstChild("HumanoidRootPart")
	return hrp and hrp:IsA("BasePart") and hrp or nil
end
Combat.Root = root

function Combat.Humanoid(f: Fighter): Humanoid?
	return f.Model:FindFirstChildOfClass("Humanoid")
end

function Combat.Register(model: Model, player: Player?, name: string, skin: string): Fighter
	local f: Fighter = {
		Model = model,
		Player = player,
		Name = name,
		Alive = true,
		Percent = 0,
		LastHitBy = nil,
		LastHitTime = 0,
		ShieldUntil = 0,
		Bubble = false,
		MegaUntil = 0,
		LastSwing = 0,
		LastSlam = 0,
		LastDash = 0,
		KOs = 0,
		Placement = nil,
		Skin = skin,
	}
	model:SetAttribute("Percent", 0)
	model:SetAttribute("Fighter", true)
	table.insert(Combat.Fighters, f)
	Combat.AttachBillboard(f)
	return f
end

function Combat.Clear()
	table.clear(Combat.Fighters)
end

function Combat.FromModel(model: Instance?): Fighter?
	if not model then
		return nil
	end
	for _, f in Combat.Fighters do
		if f.Model == model then
			return f
		end
	end
	return nil
end

function Combat.FromPlayer(player: Player): Fighter?
	for _, f in Combat.Fighters do
		if f.Player == player and f.Alive then
			return f
		end
	end
	return nil
end

function Combat.Alive(): { Fighter }
	local list = {}
	for _, f in Combat.Fighters do
		if f.Alive then
			table.insert(list, f)
		end
	end
	return list
end

-- Floating name + % tag above each fighter
function Combat.AttachBillboard(f: Fighter)
	local head = f.Model:FindFirstChild("Head") :: BasePart?
	if not head then
		return
	end
	local old = head:FindFirstChild("BonkTag")
	if old then
		old:Destroy()
	end
	local gui = Instance.new("BillboardGui")
	gui.Name = "BonkTag"
	gui.Size = UDim2.fromOffset(160, 64)
	gui.StudsOffset = Vector3.new(0, 3.2, 0)
	gui.AlwaysOnTop = true
	gui.MaxDistance = 220
	gui.LightInfluence = 0

	local name = Instance.new("TextLabel")
	name.Name = "NameLabel"
	name.BackgroundTransparency = 1
	name.Size = UDim2.fromScale(1, 0.38)
	name.Font = Enum.Font.GothamBold
	name.TextScaled = true
	name.Text = f.Name
	name.TextColor3 = f.Player and Config.Colors.Text or Config.Colors.Muted
	name.Parent = gui
	local ns = Instance.new("UIStroke")
	ns.Thickness = 1.5
	ns.Parent = name

	local pct = Instance.new("TextLabel")
	pct.Name = "PercentLabel"
	pct.BackgroundTransparency = 1
	pct.Position = UDim2.fromScale(0, 0.36)
	pct.Size = UDim2.fromScale(1, 0.64)
	pct.Font = Enum.Font.FredokaOne
	pct.TextScaled = true
	pct.Text = "0%"
	pct.TextColor3 = Color3.new(1, 1, 1)
	pct.Parent = gui
	local ps = Instance.new("UIStroke")
	ps.Thickness = 2.5
	ps.Parent = pct

	gui.Parent = head

	-- Hide the default Roblox name so ours is the only tag
	local hum = Combat.Humanoid(f)
	if hum then
		hum.DisplayDistanceType = Enum.HumanoidDisplayDistanceType.None
	end
end

function Combat.SetPercent(f: Fighter, value: number)
	f.Percent = math.clamp(math.floor(value + 0.5), 0, 999)
	f.Model:SetAttribute("Percent", f.Percent)
	local head = f.Model:FindFirstChild("Head")
	local tag = head and head:FindFirstChild("BonkTag")
	local label = tag and tag:FindFirstChild("PercentLabel") :: TextLabel?
	if label then
		label.Text = f.Percent .. "%"
		label.TextColor3 = Config.PercentColor(f.Percent)
	end
end

local function fireEffect(kind: string, data: { [string]: any })
	Remotes.Effect:FireAllClients(kind, data)
end
Combat.FireEffect = fireEffect

-- Launch a fighter. `direction` is horizontal; power scales everything.
function Combat.Knock(attacker: Fighter?, victim: Fighter, direction: Vector3, damage: number, power: number)
	if not victim.Alive then
		return
	end
	local hrp = root(victim)
	if not hrp then
		return
	end
	local now = os.clock()
	if now < victim.ShieldUntil then
		fireEffect("Blocked", { Position = hrp.Position })
		return
	end
	if victim.Bubble then
		victim.Bubble = false
		victim.Model:SetAttribute("Bubble", false)
		local bubble = victim.Model:FindFirstChild("BubbleShield")
		if bubble then
			bubble:Destroy()
		end
		fireEffect("BubblePop", { Position = hrp.Position })
		return
	end

	Combat.SetPercent(victim, victim.Percent + damage)
	if attacker then
		victim.LastHitBy = attacker
		victim.LastHitTime = now
	end

	local flat = Vector3.new(direction.X, 0, direction.Z)
	if flat.Magnitude < 0.01 then
		flat = Vector3.new(math.random() - 0.5, 0, math.random() - 0.5)
	end
	flat = flat.Unit
	local p = victim.Percent
	local horizontal = (Config.KnockBase + p * Config.KnockScale) * power
	local lift = (Config.LiftBase + p * Config.LiftScale) * math.sqrt(power)
	local velocity = flat * horizontal + Vector3.new(0, lift, 0)
	local stun = math.min(Config.StunBase + p * Config.StunScale, Config.StunMax)

	if victim.Player then
		-- The client owns its character physics, so it applies the launch.
		Remotes.Knockback:FireClient(victim.Player, velocity, stun)
	elseif Combat.OnBotKnocked then
		Combat.OnBotKnocked(victim, velocity, stun)
	end

	fireEffect("Hit", {
		Position = hrp.Position,
		Strength = math.clamp(horizontal / 120, 0.3, 1.5),
		Percent = victim.Percent,
		Victim = victim.Model,
		Attacker = attacker and attacker.Model or nil,
	})
end

local function isMega(f: Fighter): boolean
	return os.clock() < f.MegaUntil
end

-- Hammer swing: hits every fighter in a forward cone.
function Combat.Swing(f: Fighter): boolean
	local now = os.clock()
	if Workspace:GetAttribute("Frozen") then
		return false
	end
	if not f.Alive or now - f.LastSwing < Config.SwingCooldown then
		return false
	end
	f.LastSwing = now
	local hrp = root(f)
	if not hrp then
		return false
	end
	local look = hrp.CFrame.LookVector
	local flatLook = Vector3.new(look.X, 0, look.Z)
	flatLook = flatLook.Magnitude > 0 and flatLook.Unit or Vector3.new(0, 0, -1)
	local origin = hrp.Position

	fireEffect("Swing", { Position = origin + flatLook * 3, Model = f.Model })

	local hitAny = false
	for _, other in Combat.Fighters do
		if other ~= f and other.Alive then
			local ohrp = root(other)
			if ohrp then
				local offset = ohrp.Position - origin
				local flat = Vector3.new(offset.X, 0, offset.Z)
				local dist = offset.Magnitude
				if dist <= Config.SwingRange and math.abs(offset.Y) < 7 then
					local dot = flat.Magnitude > 0.01 and flatLook:Dot(flat.Unit) or 1
					if dot >= Config.SwingConeDot then
						local dmg = math.random(Config.SwingDamage[1], Config.SwingDamage[2])
						local power = isMega(f) and Config.MegaMultiplier or 1
						-- Blend facing and offset so hits feel aimed but fair
						local dir = (flatLook * 0.6 + (flat.Magnitude > 0.01 and flat.Unit or flatLook) * 0.4)
						Combat.Knock(f, other, dir, dmg, power)
						hitAny = true
					end
				end
			end
		end
	end
	return hitAny
end

-- Shockwave slam: radial knockback around the user.
function Combat.Slam(f: Fighter): boolean
	local now = os.clock()
	if Workspace:GetAttribute("Frozen") then
		return false
	end
	if not f.Alive or now - f.LastSlam < Config.SlamCooldown then
		return false
	end
	f.LastSlam = now
	local hrp = root(f)
	if not hrp then
		return false
	end
	local origin = hrp.Position
	fireEffect("Slam", { Position = origin, Radius = Config.SlamRadius, Model = f.Model })
	for _, other in Combat.Fighters do
		if other ~= f and other.Alive then
			local ohrp = root(other)
			if ohrp then
				local offset = ohrp.Position - origin
				if offset.Magnitude <= Config.SlamRadius and math.abs(offset.Y) < 10 then
					local falloff = 1 - (offset.Magnitude / Config.SlamRadius) * 0.5
					local power = (isMega(f) and Config.MegaMultiplier or 1) * 0.85 * falloff
					Combat.Knock(f, other, offset, Config.SlamDamage, power)
				end
			end
		end
	end
	return true
end

function Combat.Dash(f: Fighter): boolean
	local now = os.clock()
	if Workspace:GetAttribute("Frozen") then
		return false
	end
	if not f.Alive or now - f.LastDash < Config.DashCooldown - 0.15 then
		return false
	end
	f.LastDash = now
	local hrp = root(f)
	if hrp then
		fireEffect("Dash", { Model = f.Model, Color = Config.GetSkin(f.Skin).Trail })
	end
	return true
end

-- Who gets KO credit for this fighter's fall?
function Combat.KOCredit(victim: Fighter): Fighter?
	local by = victim.LastHitBy
	if by and by ~= victim and os.clock() - victim.LastHitTime <= Config.KOCreditWindow then
		return by
	end
	return nil
end

function Combat.Init()
	Remotes.Ability.OnServerEvent:Connect(function(player: Player, ability: any)
		local f = Combat.FromPlayer(player)
		if not f then
			return
		end
		if ability == "Slam" then
			Combat.Slam(f)
		elseif ability == "Dash" then
			Combat.Dash(f)
		end
	end)
	Players.PlayerRemoving:Connect(function(player)
		for _, f in Combat.Fighters do
			if f.Player == player then
				f.Alive = false
			end
		end
	end)
end

-- Simple visual for the bubble power-up
function Combat.GiveBubble(f: Fighter)
	f.Bubble = true
	f.Model:SetAttribute("Bubble", true)
	local hrp = root(f)
	if not hrp or f.Model:FindFirstChild("BubbleShield") then
		return
	end
	local b = Instance.new("Part")
	b.Name = "BubbleShield"
	b.Shape = Enum.PartType.Ball
	b.Size = Vector3.new(7.5, 7.5, 7.5)
	b.Material = Enum.Material.ForceField
	b.Color = Config.PowerUps.Bubble.Color
	b.CanCollide = false
	b.CanQuery = false
	b.CanTouch = false
	b.Massless = true
	b.CFrame = hrp.CFrame
	local weld = Instance.new("WeldConstraint")
	weld.Part0 = hrp
	weld.Part1 = b
	weld.Parent = b
	b.Parent = f.Model
end

return Combat
