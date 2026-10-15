-- Player profiles: coins, stats and hammer skins, saved with DataStoreService.
-- Works without API access too (Studio): data just isn't persisted.

local DataStoreService = game:GetService("DataStoreService")
local Players = game:GetService("Players")
local ReplicatedStorage = game:GetService("ReplicatedStorage")
local RunService = game:GetService("RunService")

local Config = require(ReplicatedStorage.Shared.Config)
local Remotes = ReplicatedStorage.Remotes

local Data = {}

local STORE_NAME = "OrbitBonk_Profiles_v1"
local AUTOSAVE_INTERVAL = 120

local store: DataStore? = nil
do
	local ok, result = pcall(function()
		return DataStoreService:GetDataStore(STORE_NAME)
	end)
	if ok then
		store = result
	else
		warn("[OrbitBonk] DataStore unavailable, progress will not save:", result)
	end
end

type Profile = {
	Coins: number,
	Wins: number,
	KOs: number,
	Owned: { [string]: boolean },
	Equipped: string,
}

local profiles: { [Player]: Profile } = {}
local loaded: { [Player]: boolean } = {}

local function defaultProfile(): Profile
	return {
		Coins = 0,
		Wins = 0,
		KOs = 0,
		Owned = { [Config.Skins[1].Id] = true },
		Equipped = Config.Skins[1].Id,
	}
end

local function sanitize(raw: any): Profile
	local p = defaultProfile()
	if type(raw) ~= "table" then
		return p
	end
	p.Coins = tonumber(raw.Coins) or 0
	p.Wins = tonumber(raw.Wins) or 0
	p.KOs = tonumber(raw.KOs) or 0
	if type(raw.Owned) == "table" then
		for id, v in raw.Owned do
			if v == true and type(id) == "string" then
				p.Owned[id] = true
			end
		end
	end
	if type(raw.Equipped) == "string" and p.Owned[raw.Equipped] then
		p.Equipped = raw.Equipped
	end
	return p
end

local function retry<T>(fn: () -> T): (boolean, T | string)
	local ok, result
	for attempt = 1, 3 do
		ok, result = pcall(fn)
		if ok then
			return true, result
		end
		if attempt < 3 then
			task.wait(attempt)
		end
	end
	return false, result
end

local function key(player: Player): string
	return "u_" .. player.UserId
end

local function syncLeaderstats(player: Player)
	local p = profiles[player]
	local ls = player:FindFirstChild("leaderstats")
	if not p or not ls then
		return
	end
	(ls :: any).Coins.Value = p.Coins;
	(ls :: any).Wins.Value = p.Wins;
	(ls :: any).KOs.Value = p.KOs
end

function Data.Push(player: Player)
	local p = profiles[player]
	if not p then
		return
	end
	syncLeaderstats(player)
	Remotes.DataUpdate:FireClient(player, {
		Coins = p.Coins,
		Wins = p.Wins,
		KOs = p.KOs,
		Owned = p.Owned,
		Equipped = p.Equipped,
	})
end

function Data.Get(player: Player): Profile?
	return profiles[player]
end

function Data.IsLoaded(player: Player): boolean
	return loaded[player] == true
end

function Data.AddCoins(player: Player, amount: number)
	local p = profiles[player]
	if p then
		p.Coins += amount
		Data.Push(player)
	end
end

function Data.AddStat(player: Player, stat: "Wins" | "KOs", amount: number)
	local p = profiles[player]
	if p then
		p[stat] += amount
		Data.Push(player)
	end
end

function Data.GetEquipped(player: Player): string
	local p = profiles[player]
	return p and p.Equipped or Config.Skins[1].Id
end

local function save(player: Player)
	local p = profiles[player]
	if not p or not store or not loaded[player] then
		return
	end
	local ok, err = retry(function()
		return (store :: DataStore):UpdateAsync(key(player), function()
			return p
		end)
	end)
	if not ok then
		warn("[OrbitBonk] Save failed for", player.Name, err)
	end
end

local function onPlayerAdded(player: Player)
	local ls = Instance.new("Folder")
	ls.Name = "leaderstats"
	for _, stat in { "Coins", "Wins", "KOs" } do
		local v = Instance.new("IntValue")
		v.Name = stat
		v.Parent = ls
	end
	ls.Parent = player

	profiles[player] = defaultProfile()
	if store then
		local ok, raw = retry(function()
			return (store :: DataStore):GetAsync(key(player))
		end)
		if not player.Parent then
			return
		end
		if ok then
			profiles[player] = sanitize(raw)
			loaded[player] = true
		else
			-- Don't mark loaded so we never overwrite real data with a blank profile.
			warn("[OrbitBonk] Load failed for", player.Name, raw)
		end
	end
	Data.Push(player)
end

local function onShop(player: Player, action: any, skinId: any)
	local p = profiles[player]
	if not p then
		return { ok = false, msg = "Profile still loading" }
	end
	if action == "Get" then
		Data.Push(player)
		return { ok = true }
	end
	if type(skinId) ~= "string" then
		return { ok = false, msg = "Bad request" }
	end
	local skin = Config.GetSkin(skinId)
	if skin.Id ~= skinId then
		return { ok = false, msg = "Unknown skin" }
	end
	if action == "Buy" then
		if p.Owned[skinId] then
			return { ok = false, msg = "Already owned" }
		end
		if p.Coins < skin.Price then
			return { ok = false, msg = "Need " .. (skin.Price - p.Coins) .. " more coins" }
		end
		p.Coins -= skin.Price
		p.Owned[skinId] = true
		p.Equipped = skinId
		Data.Push(player)
		task.spawn(save, player)
		return { ok = true, msg = "Unlocked " .. skin.Name .. "!" }
	elseif action == "Equip" then
		if not p.Owned[skinId] then
			return { ok = false, msg = "Not owned" }
		end
		p.Equipped = skinId
		Data.Push(player)
		return { ok = true, msg = "Equipped " .. skin.Name }
	end
	return { ok = false, msg = "Bad request" }
end

function Data.Init()
	Players.PlayerAdded:Connect(onPlayerAdded)
	for _, player in Players:GetPlayers() do
		task.spawn(onPlayerAdded, player)
	end
	Players.PlayerRemoving:Connect(function(player)
		save(player)
		profiles[player] = nil
		loaded[player] = nil
	end)
	Remotes.Shop.OnServerInvoke = onShop

	task.spawn(function()
		while true do
			task.wait(AUTOSAVE_INTERVAL)
			for _, player in Players:GetPlayers() do
				task.spawn(save, player)
			end
		end
	end)

	game:BindToClose(function()
		if RunService:IsStudio() and not store then
			return
		end
		local pending = 0
		for _, player in Players:GetPlayers() do
			pending += 1
			task.spawn(function()
				save(player)
				pending -= 1
			end)
		end
		local start = os.clock()
		while pending > 0 and os.clock() - start < 25 do
			task.wait(0.1)
		end
	end)
end

return Data
