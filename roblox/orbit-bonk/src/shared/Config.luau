--!strict
-- Orbit Bonk: central tuning. Every gameplay number lives here.

local Config = {}

Config.GameName = "ORBIT BONK"

-- Match flow
Config.MinPlayers = 1 -- one human is enough; bots fill the rest
Config.BotFillTo = 6 -- top up each round to this many fighters
Config.IntermissionTime = 15
Config.CountdownTime = 3
Config.RoundTime = 150
Config.EndScreenTime = 6

-- World layout
Config.ArenaCenter = Vector3.new(0, 60, 0)
Config.ArenaRings = 5 -- hex rings around the centre tile
Config.HexSide = 6.5
Config.LobbyCenter = Vector3.new(0, 95, 190)

-- Blast zones (relative to arena centre), Smash-style
Config.FallKillDepth = 45
Config.SideKillDistance = 260
Config.TopKillHeight = 320

-- Collapse: outer ring drops away every CollapseInterval after CollapseStart
Config.CollapseStart = 35
Config.CollapseInterval = 14
Config.CollapseWarning = 3

-- Combat
Config.SwingCooldown = 0.45
Config.SwingRange = 9.5
Config.SwingConeDot = 0.15 -- 1 = straight ahead only, -1 = all around
Config.SwingDamage = { 8, 12 } -- random % added per hit
Config.SlamCooldown = 7
Config.SlamRadius = 15
Config.SlamDamage = 7
Config.DashCooldown = 2.5
Config.DashSpeed = 95
Config.DoubleJumpPower = 58

Config.KnockBase = 38 -- horizontal launch at 0%
Config.KnockScale = 0.95 -- extra launch per %
Config.LiftBase = 30
Config.LiftScale = 0.22
Config.StunBase = 0.25
Config.StunScale = 0.006
Config.StunMax = 1.1
Config.KOCreditWindow = 7 -- seconds a hit counts for KO credit
Config.SpawnShieldTime = 2.5

-- Power-ups
Config.PowerUpInterval = 9
Config.PowerUpMax = 3
Config.PowerUpDuration = 10
Config.PowerUps = {
	Mega = { Color = Color3.fromRGB(255, 70, 90), Label = "MEGA BONK", Desc = "+70% launch power" },
	Zoom = { Color = Color3.fromRGB(255, 220, 60), Label = "ZOOM", Desc = "Super speed" },
	Bubble = { Color = Color3.fromRGB(80, 200, 255), Label = "BUBBLE", Desc = "Blocks the next hit" },
	Feather = { Color = Color3.fromRGB(190, 255, 140), Label = "FEATHER", Desc = "Huge jumps, % heals 15" },
}
Config.MegaMultiplier = 1.7
Config.ZoomSpeed = 30
Config.FeatherJump = 18

-- Economy
Config.CoinsPerKO = 15
Config.CoinsForWin = 60
Config.CoinsForPlaying = 5
Config.CoinsPerSurvivalBonus = 10 -- bonus for making the final three

-- Hammer skins (cosmetic only, so the shop never breaks balance)
export type Skin = {
	Id: string,
	Name: string,
	Price: number,
	Head: Color3,
	Accent: Color3,
	Material: Enum.Material,
	Trail: Color3,
}

Config.Skins = {
	{ Id = "Comet", Name = "Comet", Price = 0, Head = Color3.fromRGB(70, 90, 140), Accent = Color3.fromRGB(0, 230, 255), Material = Enum.Material.SmoothPlastic, Trail = Color3.fromRGB(0, 230, 255) },
	{ Id = "Nova", Name = "Nova", Price = 120, Head = Color3.fromRGB(255, 120, 40), Accent = Color3.fromRGB(255, 240, 120), Material = Enum.Material.Neon, Trail = Color3.fromRGB(255, 160, 40) },
	{ Id = "Nebula", Name = "Nebula", Price = 200, Head = Color3.fromRGB(140, 60, 255), Accent = Color3.fromRGB(255, 90, 220), Material = Enum.Material.ForceField, Trail = Color3.fromRGB(200, 90, 255) },
	{ Id = "Glacier", Name = "Glacier", Price = 300, Head = Color3.fromRGB(170, 230, 255), Accent = Color3.fromRGB(255, 255, 255), Material = Enum.Material.Glass, Trail = Color3.fromRGB(170, 230, 255) },
	{ Id = "Toxic", Name = "Toxic", Price = 450, Head = Color3.fromRGB(60, 255, 90), Accent = Color3.fromRGB(20, 40, 20), Material = Enum.Material.Neon, Trail = Color3.fromRGB(60, 255, 90) },
	{ Id = "Solar", Name = "Solar Crown", Price = 750, Head = Color3.fromRGB(255, 200, 40), Accent = Color3.fromRGB(255, 255, 220), Material = Enum.Material.Foil, Trail = Color3.fromRGB(255, 220, 80) },
	{ Id = "Void", Name = "Void Breaker", Price = 1200, Head = Color3.fromRGB(10, 10, 16), Accent = Color3.fromRGB(255, 40, 90), Material = Enum.Material.Basalt, Trail = Color3.fromRGB(255, 40, 90) },
} :: { Skin }

function Config.GetSkin(id: string?): Skin
	for _, skin in Config.Skins do
		if skin.Id == id then
			return skin
		end
	end
	return Config.Skins[1]
end

-- Bot names and looks
Config.BotNames = { "Zorp", "Blip", "Kix", "Nubo", "Quasar", "Pix", "Mochi", "Vex", "Tumble", "Orbo", "Glim", "Rocket" }
Config.BotColors = {
	Color3.fromRGB(255, 120, 160),
	Color3.fromRGB(120, 220, 255),
	Color3.fromRGB(255, 210, 90),
	Color3.fromRGB(160, 255, 140),
	Color3.fromRGB(200, 140, 255),
	Color3.fromRGB(255, 160, 90),
}

-- Visual palette
Config.Colors = {
	Panel = Color3.fromRGB(14, 16, 34),
	PanelLight = Color3.fromRGB(30, 34, 66),
	Cyan = Color3.fromRGB(0, 230, 255),
	Pink = Color3.fromRGB(255, 70, 190),
	Gold = Color3.fromRGB(255, 205, 60),
	Text = Color3.fromRGB(240, 244, 255),
	Muted = Color3.fromRGB(150, 160, 200),
	Danger = Color3.fromRGB(255, 70, 90),
}

Config.Sounds = {
	Swing = "rbxasset://sounds/swordslash.wav",
	Hit = "rbxasset://sounds/swordlunge.wav",
	PowerUp = "rbxasset://sounds/electronicpingshort.wav",
	KO = "rbxasset://sounds/uuhhh.mp3",
	Jump = "rbxasset://sounds/action_jump.mp3",
}

-- Colour for the % meter: white -> yellow -> orange -> red
function Config.PercentColor(p: number): Color3
	if p < 40 then
		return Color3.fromRGB(255, 255, 255):Lerp(Color3.fromRGB(255, 230, 80), p / 40)
	elseif p < 90 then
		return Color3.fromRGB(255, 230, 80):Lerp(Color3.fromRGB(255, 130, 40), (p - 40) / 50)
	else
		return Color3.fromRGB(255, 130, 40):Lerp(Color3.fromRGB(255, 30, 60), math.clamp((p - 90) / 60, 0, 1))
	end
end

return Config
