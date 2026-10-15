-- Orbit Bonk round manager.
-- Lobby -> intermission -> countdown -> brawl on a collapsing hex arena -> results.

local Players = game:GetService("Players")
local ReplicatedStorage = game:GetService("ReplicatedStorage")
local Workspace = game:GetService("Workspace")

local Config = require(ReplicatedStorage.Shared.Config)
local Remotes = ReplicatedStorage.Remotes

local Data = require(script.Parent.Data)
local World = require(script.Parent.World)
local Arena = require(script.Parent.Arena)
local Combat = require(script.Parent.Combat)
local Bots = require(script.Parent.Bots)
local Hammer = require(script.Parent.Hammer)
local PowerUps = require(script.Parent.PowerUps)

Players.RespawnTime = 3

World.Init()
Data.Init()
Combat.Init()
Bots.Init()

local function setState(state: string, timeLeft: number?, status: string?)
	ReplicatedStorage:SetAttribute("State", state)
	ReplicatedStorage:SetAttribute("TimeLeft", timeLeft or 0)
	if status then
		ReplicatedStorage:SetAttribute("Status", status)
	end
end

local function notifyAll(kind: string, data: { [string]: any }?)
	Remotes.Notify:FireAllClients(kind, data or {})
end

local function rootOf(model: Model?): BasePart?
	if not model then
		return nil
	end
	local hrp = model:FindFirstChild("HumanoidRootPart")
	return hrp and hrp:IsA("BasePart") and hrp or nil
end

local function removeHammers(player: Player)
	for _, container in { player.Character, player:FindFirstChildOfClass("Backpack") } do
		if container then
			for _, item in container:GetChildren() do
				if item:IsA("Tool") and item.Name == "Bonk Hammer" then
					item:Destroy()
				end
			end
		end
	end
end

local function sendToLobby(player: Player)
	removeHammers(player)
	player:SetAttribute("Fighting", false)
	local character = player.Character
	local hrp = rootOf(character)
	local hum = character and character:FindFirstChildOfClass("Humanoid")
	if character and hrp and hum and hum.Health > 0 then
		hrp.Anchored = false
		hrp.AssemblyLinearVelocity = Vector3.zero
		character:PivotTo(World.LobbyCFrame())
		character:SetAttribute("Percent", 0)
		character:SetAttribute("PowerUp", nil)
		local bubble = character:FindFirstChild("BubbleShield")
		if bubble then
			bubble:Destroy()
		end
		hum.WalkSpeed = game:GetService("StarterPlayer").CharacterWalkSpeed
		hum.JumpHeight = game:GetService("StarterPlayer").CharacterJumpHeight
		local head = character:FindFirstChild("Head")
		local tag = head and head:FindFirstChild("BonkTag")
		if tag then
			tag:Destroy()
		end
		hum.DisplayDistanceType = Enum.HumanoidDisplayDistanceType.Viewer
	end
	Remotes.Notify:FireClient(player, "Reset", {})
end

local function giveHammer(player: Player, f: Combat.Fighter)
	removeHammers(player)
	local character = player.Character
	local hum = character and character:FindFirstChildOfClass("Humanoid")
	local backpack = player:FindFirstChildOfClass("Backpack")
	if not character or not hum or not backpack then
		return
	end
	local tool = Hammer.MakeTool(f.Skin)
	tool.Parent = backpack
	hum:EquipTool(tool)

	tool.Activated:Connect(function()
		if not f.Alive or os.clock() - f.LastSwing < Config.SwingCooldown then
			return
		end
		-- Triggers the default character "slash" animation
		local anim = Instance.new("StringValue")
		anim.Name = "toolanim"
		anim.Value = "Slash"
		anim.Parent = tool
		Combat.Swing(f)
	end)
	-- The hammer is your only weapon; keep it in hand
	tool.Unequipped:Connect(function()
		task.defer(function()
			if f.Alive and tool.Parent == backpack and hum.Parent and hum.Health > 0 then
				hum:EquipTool(tool)
			end
		end)
	end)
end

local function eligiblePlayers(): { Player }
	local list = {}
	for _, player in Players:GetPlayers() do
		local hum = player.Character and player.Character:FindFirstChildOfClass("Humanoid")
		if hum and hum.Health > 0 and rootOf(player.Character) then
			table.insert(list, player)
		end
	end
	return list
end

local function shuffle<T>(t: { T })
	for i = #t, 2, -1 do
		local j = math.random(1, i)
		t[i], t[j] = t[j], t[i]
	end
end

local function aliveHumans(): number
	local n = 0
	for _, f in Combat.Fighters do
		if f.Alive and f.Player then
			n += 1
		end
	end
	return n
end

-- Round bookkeeping
local roundCoins: { [Player]: number } = {}

local function award(player: Player?, amount: number)
	if player and player.Parent then
		roundCoins[player] = (roundCoins[player] or 0) + amount
		Data.AddCoins(player, amount)
	end
end

local function eliminate(f: Combat.Fighter, silent: boolean?)
	if not f.Alive then
		return
	end
	local aliveBefore = #Combat.Alive()
	f.Alive = false
	f.Placement = aliveBefore
	local hrp = rootOf(f.Model)
	local credit = Combat.KOCredit(f)
	if credit then
		credit.KOs += 1
		if credit.Player then
			award(credit.Player, Config.CoinsPerKO)
			Data.AddStat(credit.Player, "KOs", 1)
		end
	end
	if not silent then
		notifyAll("KO", {
			Victim = f.Name,
			By = credit and credit.Name or nil,
			Left = aliveBefore - 1,
		})
		if hrp then
			Combat.FireEffect("KO", { Position = hrp.Position, Center = Config.ArenaCenter })
		end
	end
	if f.Player then
		local player = f.Player
		Remotes.Notify:FireClient(player, "Eliminated", { Placement = f.Placement, By = credit and credit.Name or nil })
		task.delay(1.4, function()
			if player.Parent and player.Character == f.Model then
				sendToLobby(player)
			else
				player:SetAttribute("Fighting", false)
			end
		end)
	else
		task.delay(1.5, function()
			Bots.Remove(f)
		end)
	end
end

local function outOfBounds(f: Combat.Fighter): boolean
	if f.Player and not f.Player.Parent then
		return true
	end
	if f.Player and f.Player.Character ~= f.Model then
		return true
	end
	if not f.Model.Parent then
		return true
	end
	local hum = f.Model:FindFirstChildOfClass("Humanoid")
	if not hum or hum.Health <= 0 then
		return true
	end
	local hrp = rootOf(f.Model)
	if not hrp then
		return true
	end
	local c = Config.ArenaCenter
	local p = hrp.Position
	if p.Y < c.Y - Config.FallKillDepth or p.Y > c.Y + Config.TopKillHeight then
		return true
	end
	if Vector3.new(p.X - c.X, 0, p.Z - c.Z).Magnitude > Config.SideKillDistance then
		return true
	end
	return false
end

local function setFrozen(frozen: boolean)
	Workspace:SetAttribute("Frozen", frozen)
	for _, f in Combat.Fighters do
		local hrp = rootOf(f.Model)
		if hrp then
			hrp.Anchored = frozen
		end
	end
end

local function runRound()
	local players = eligiblePlayers()
	if #players == 0 then
		return
	end
	shuffle(players)
	table.clear(roundCoins)

	setState("Starting", 0, "Get ready...")
	Arena.Build()
	Combat.Clear()
	Bots.Clear()

	local total = math.max(#players, Config.BotFillTo)
	local points = Arena.SpawnPoints(total)
	for i, player in players do
		local character = player.Character :: Model
		character:PivotTo(points[i])
		local hrp = rootOf(character)
		if hrp then
			hrp.AssemblyLinearVelocity = Vector3.zero
		end
		local f = Combat.Register(character, player, player.DisplayName, Data.GetEquipped(player))
		player:SetAttribute("Fighting", true)
		giveHammer(player, f)
	end
	Bots.Spawn(total - #players, points, #players + 1)

	-- Countdown with everyone locked in place
	setFrozen(true)
	for i = Config.CountdownTime, 1, -1 do
		setState("Countdown", i, "Round starting")
		notifyAll("Countdown", { Value = tostring(i) })
		task.wait(1)
	end
	setFrozen(false)
	notifyAll("Countdown", { Value = "BONK!" })
	local shieldEnd = os.clock() + Config.SpawnShieldTime
	for _, f in Combat.Fighters do
		f.ShieldUntil = shieldEnd
	end

	PowerUps.Start()
	local startTime = os.clock()
	local nextCollapse = Config.CollapseStart
	local nextPowerUp = 4
	local warned = false
	local humansStarted = #players

	while true do
		task.wait(0.1)
		local elapsed = os.clock() - startTime
		local timeLeft = math.max(0, math.ceil(Config.RoundTime - elapsed))

		for _, f in Combat.Fighters do
			if f.Alive and outOfBounds(f) then
				local quietly = f.Player ~= nil and not f.Player.Parent
				eliminate(f, quietly)
			end
		end

		if not warned and elapsed >= nextCollapse - Config.CollapseWarning - 2 then
			warned = true
			notifyAll("Banner", { Text = "THE ARENA IS COLLAPSING!", Color = Config.Colors.Danger })
		end
		if elapsed >= nextCollapse then
			nextCollapse += Config.CollapseInterval
			if Arena.CollapseOuterRing() then
				notifyAll("Collapse", {})
				task.delay(Config.CollapseWarning + 0.2, PowerUps.Prune)
			end
		end
		if elapsed >= nextPowerUp then
			nextPowerUp = elapsed + Config.PowerUpInterval
			PowerUps.SpawnOne()
		end

		local alive = Combat.Alive()
		ReplicatedStorage:SetAttribute("Alive", #alive)
		setState("Round", timeLeft, #alive .. " fighters left")

		if #alive <= 1 then
			break
		end
		if humansStarted > 0 and aliveHumans() == 0 then
			break
		end
		if elapsed >= Config.RoundTime then
			break
		end
	end

	-- Decide the winner
	PowerUps.Stop()
	local alive = Combat.Alive()
	table.sort(alive, function(a, b)
		return a.Percent < b.Percent
	end)
	local winner = alive[1]
	for i, f in alive do
		f.Placement = i
	end

	-- Rewards
	for _, f in Combat.Fighters do
		if f.Player and f.Player.Parent then
			award(f.Player, Config.CoinsForPlaying)
			if f.Placement and f.Placement <= 3 and f ~= winner then
				award(f.Player, Config.CoinsPerSurvivalBonus)
			end
		end
	end
	if winner and winner.Player then
		award(winner.Player, Config.CoinsForWin)
		Data.AddStat(winner.Player, "Wins", 1)
	end

	-- Best brawler by KOs
	local mvp: Combat.Fighter? = nil
	for _, f in Combat.Fighters do
		if f.KOs > 0 and (not mvp or f.KOs > mvp.KOs) then
			mvp = f
		end
	end

	setState("Ended", Config.EndScreenTime, winner and (winner.Name .. " wins!") or "No winner")
	notifyAll("RoundEnd", {
		Winner = winner and winner.Name or nil,
		WinnerIsBot = winner ~= nil and winner.Player == nil,
		WinnerModel = winner and winner.Model or nil,
		MVP = mvp and mvp.Name or nil,
		MVPKOs = mvp and mvp.KOs or 0,
	})
	for _, f in Combat.Fighters do
		if f.Player and f.Player.Parent then
			Remotes.Notify:FireClient(f.Player, "Rewards", {
				Coins = roundCoins[f.Player] or 0,
				Placement = f.Placement or #Combat.Fighters,
				KOs = f.KOs,
				Won = f == winner,
			})
		end
	end
	if winner then
		Combat.FireEffect("Victory", { Model = winner.Model })
	end

	task.wait(Config.EndScreenTime)

	-- Clean up
	for _, f in Combat.Fighters do
		f.Alive = false
		if f.Player and f.Player.Parent then
			sendToLobby(f.Player)
		end
	end
	Bots.Clear()
	Combat.Clear()
	Arena.Clear()
	ReplicatedStorage:SetAttribute("Alive", 0)
end

-- Keep lobby players safe and handle new characters
local function onCharacter(player: Player, character: Model)
	player:SetAttribute("Fighting", false)
	local hum = character:WaitForChild("Humanoid", 5) :: Humanoid?
	if hum then
		hum.BreakJointsOnDeath = false
	end
end

Players.PlayerAdded:Connect(function(player)
	player:SetAttribute("Fighting", false)
	player.CharacterAdded:Connect(function(character)
		onCharacter(player, character)
	end)
end)
for _, player in Players:GetPlayers() do
	player.CharacterAdded:Connect(function(character)
		onCharacter(player, character)
	end)
end

task.spawn(function()
	while true do
		task.wait(0.5)
		for _, player in Players:GetPlayers() do
			if not player:GetAttribute("Fighting") then
				local hrp = rootOf(player.Character)
				if hrp and hrp.Position.Y < Config.LobbyCenter.Y - 40 then
					hrp.AssemblyLinearVelocity = Vector3.zero;
					(player.Character :: Model):PivotTo(World.LobbyCFrame())
				end
			end
		end
	end
end)

-- Main loop
setState("Waiting", 0, "Waiting for players")
while true do
	if #Players:GetPlayers() < Config.MinPlayers then
		setState("Waiting", 0, "Waiting for players")
		repeat
			task.wait(1)
		until #Players:GetPlayers() >= Config.MinPlayers
	end

	for t = Config.IntermissionTime, 1, -1 do
		setState("Intermission", t, "Next round in")
		task.wait(1)
		if #Players:GetPlayers() < Config.MinPlayers then
			break
		end
	end

	if #Players:GetPlayers() >= Config.MinPlayers then
		local ok, err = pcall(runRound)
		if not ok then
			warn("[OrbitBonk] Round error:", err)
			-- Recover cleanly so one bad round never stalls the server
			PowerUps.Stop()
			Workspace:SetAttribute("Frozen", false)
			for _, f in Combat.Fighters do
				if f.Player and f.Player.Parent then
					pcall(sendToLobby, f.Player)
				end
			end
			Bots.Clear()
			Combat.Clear()
			Arena.Clear()
		end
	end
end
