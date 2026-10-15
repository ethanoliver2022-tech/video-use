-- Orbit Bonk client: controls, knockback physics, HUD wiring and spectating.

local ContextActionService = game:GetService("ContextActionService")
local Players = game:GetService("Players")
local ReplicatedStorage = game:GetService("ReplicatedStorage")
local RunService = game:GetService("RunService")
local StarterGui = game:GetService("StarterGui")
local UserInputService = game:GetService("UserInputService")
local Workspace = game:GetService("Workspace")

local Config = require(ReplicatedStorage.Shared.Config)
local Remotes = ReplicatedStorage:WaitForChild("Remotes")
local UI = require(script.Parent.UI)
local Effects = require(script.Parent.Effects)

local player = Players.LocalPlayer
local camera = Workspace.CurrentCamera
local C = Config.Colors

-- The hammer is auto-equipped, so the hotbar is just clutter
task.spawn(function()
	for _ = 1, 10 do
		local ok = pcall(function()
			StarterGui:SetCoreGuiEnabled(Enum.CoreGuiType.Backpack, false)
		end)
		if ok then
			break
		end
		task.wait(0.5)
	end
end)

UI.Build()

---------------------------------------------------------------------------
-- Character state
---------------------------------------------------------------------------
local character: Model? = nil
local humanoid: Humanoid? = nil
local root: BasePart? = nil

local stunToken = 0
local canDoubleJump = false
local lastJumpTime = 0
local lastDash = -100
local lastSlam = -100
local lastSwing = -100

local function fighting(): boolean
	return player:GetAttribute("Fighting") == true
end

local function bindCharacter(char: Model)
	character = char
	humanoid = char:WaitForChild("Humanoid", 10) :: Humanoid?
	root = char:WaitForChild("HumanoidRootPart", 10) :: BasePart?
	stunToken += 1
	canDoubleJump = false
	local hum = humanoid
	if not hum then
		return
	end
	hum.StateChanged:Connect(function(_, new)
		if new == Enum.HumanoidStateType.Landed or new == Enum.HumanoidStateType.Running then
			canDoubleJump = true
		elseif new == Enum.HumanoidStateType.Jumping then
			lastJumpTime = os.clock()
		end
	end)
	char.ChildAdded:Connect(function(child)
		if child:IsA("Tool") then
			child.Activated:Connect(function()
				if os.clock() - lastSwing >= Config.SwingCooldown then
					lastSwing = os.clock()
				end
			end)
		end
	end)
	char:GetAttributeChangedSignal("Percent"):Connect(function()
		UI.SetPercent(char:GetAttribute("Percent") or 0)
	end)
	UI.SetPercent(char:GetAttribute("Percent") or 0)
end

if player.Character then
	task.spawn(bindCharacter, player.Character)
end
player.CharacterAdded:Connect(bindCharacter)

---------------------------------------------------------------------------
-- Knockback (the server tells us; we own our physics)
---------------------------------------------------------------------------
Remotes.Knockback.OnClientEvent:Connect(function(velocity: Vector3, stun: number)
	local hum, hrp = humanoid, root
	if not hum or not hrp or hum.Health <= 0 then
		return
	end
	stunToken += 1
	local token = stunToken
	hum.PlatformStand = true
	hrp.AssemblyLinearVelocity = velocity
	hrp.AssemblyAngularVelocity = Vector3.new(math.random(-6, 6), math.random(-3, 3), math.random(-6, 6))
	task.delay(stun, function()
		if token == stunToken and hum.Parent then
			hum.PlatformStand = false
			hum:ChangeState(Enum.HumanoidStateType.GettingUp)
			canDoubleJump = true -- reward recovery: you always get a jump back after a launch
		end
	end)
end)

local function clearStun()
	stunToken += 1
	if humanoid then
		humanoid.PlatformStand = false
	end
end

---------------------------------------------------------------------------
-- Abilities
---------------------------------------------------------------------------
local function stunned(): boolean
	return humanoid ~= nil and humanoid.PlatformStand
end

local function doDash()
	local hum, hrp = humanoid, root
	if not hum or not hrp or hum.Health <= 0 or stunned() or Workspace:GetAttribute("Frozen") then
		return
	end
	if os.clock() - lastDash < Config.DashCooldown then
		return
	end
	lastDash = os.clock()
	local dir = hum.MoveDirection
	if dir.Magnitude < 0.1 then
		dir = hrp.CFrame.LookVector
	end
	dir = Vector3.new(dir.X, 0, dir.Z)
	if dir.Magnitude < 0.01 then
		return
	end
	dir = dir.Unit
	local airborne = hum.FloorMaterial == Enum.Material.Air
	hrp.AssemblyLinearVelocity = dir * Config.DashSpeed + Vector3.new(0, airborne and 26 or 14, 0)
	Remotes.Ability:FireServer("Dash")
	if not fighting() then
		Effects.Handle("Dash", { Model = character, Color = C.Cyan })
	end
end

local function doSlam()
	local hum = humanoid
	if not fighting() or not hum or hum.Health <= 0 or stunned() or Workspace:GetAttribute("Frozen") then
		return
	end
	if os.clock() - lastSlam < Config.SlamCooldown then
		return
	end
	lastSlam = os.clock()
	Remotes.Ability:FireServer("Slam")
end

ContextActionService:BindAction("OrbitDash", function(_, state)
	if state == Enum.UserInputState.Begin then
		doDash()
	end
	return Enum.ContextActionResult.Sink
end, true, Enum.KeyCode.Q, Enum.KeyCode.ButtonX)
ContextActionService:SetTitle("OrbitDash", "DASH")
ContextActionService:SetPosition("OrbitDash", UDim2.new(1, -170, 1, -150))

ContextActionService:BindAction("OrbitSlam", function(_, state)
	if state == Enum.UserInputState.Begin then
		doSlam()
	end
	return Enum.ContextActionResult.Sink
end, true, Enum.KeyCode.E, Enum.KeyCode.ButtonY)
ContextActionService:SetTitle("OrbitSlam", "SLAM")
ContextActionService:SetPosition("OrbitSlam", UDim2.new(1, -230, 1, -90))

-- Double jump
UserInputService.JumpRequest:Connect(function()
	local hum, hrp = humanoid, root
	if not hum or not hrp or stunned() or hum.Health <= 0 then
		return
	end
	local state = hum:GetState()
	if (state == Enum.HumanoidStateType.Freefall or state == Enum.HumanoidStateType.Jumping)
		and canDoubleJump
		and os.clock() - lastJumpTime > 0.2
		and hum.FloorMaterial == Enum.Material.Air
	then
		canDoubleJump = false
		local v = hrp.AssemblyLinearVelocity
		hrp.AssemblyLinearVelocity = Vector3.new(v.X, Config.DoubleJumpPower, v.Z)
		Effects.DoubleJump(hrp.Position)
	end
end)

---------------------------------------------------------------------------
-- Spectating
---------------------------------------------------------------------------
local spectating = false
local specIndex = 1

local function fightersAlive(): { Model }
	local list = {}
	for _, container in { Workspace, Workspace:FindFirstChild("Bots") } do
		if container then
			for _, m in container:GetChildren() do
				if m:IsA("Model") and m:GetAttribute("Fighter") and m ~= character then
					local hum = m:FindFirstChildOfClass("Humanoid")
					if hum and hum.Health > 0 then
						table.insert(list, m)
					end
				end
			end
		end
	end
	return list
end

local function setSpectate(on: boolean)
	spectating = on
	UI.SpectateBar.Visible = on
	UI.SpectateButton.Text = on and "STOP WATCHING" or "SPECTATE"
	if not on and humanoid then
		camera.CameraSubject = humanoid
	end
end

local function updateSpectate(step: number?)
	if not spectating then
		return
	end
	local list = fightersAlive()
	if #list == 0 then
		setSpectate(false)
		return
	end
	specIndex = ((specIndex - 1 + (step or 0)) % #list) + 1
	local target = list[specIndex]
	local hum = target:FindFirstChildOfClass("Humanoid")
	if hum then
		camera.CameraSubject = hum
	end
	local head = target:FindFirstChild("Head")
	local tag = head and head:FindFirstChild("BonkTag")
	local name = tag and tag:FindFirstChild("NameLabel") :: TextLabel?
	UI.SpectateName.Text = name and name.Text or target.Name
end

UI.SpectateButton.Activated:Connect(function()
	setSpectate(not spectating)
	updateSpectate(0)
end)
UI.SpectatePrev.Activated:Connect(function()
	updateSpectate(-1)
end)
UI.SpectateNext.Activated:Connect(function()
	updateSpectate(1)
end)

-- Keep the spectate target valid as people get knocked out
task.spawn(function()
	while true do
		task.wait(0.5)
		if spectating then
			local subject = camera.CameraSubject
			local hum = subject and subject:IsA("Humanoid") and subject or nil
			local model = hum and hum.Parent
			if not hum or not model or not model.Parent or hum.Health <= 0 or not model:GetAttribute("Fighter") then
				updateSpectate(1)
			end
		end
	end
end)

---------------------------------------------------------------------------
-- HUD wiring
---------------------------------------------------------------------------
local function refreshStatus()
	local state = ReplicatedStorage:GetAttribute("State") or "Waiting"
	local timeLeft = ReplicatedStorage:GetAttribute("TimeLeft") or 0
	local status = ReplicatedStorage:GetAttribute("Status") or ""
	UI.SetStatus(state, timeLeft, status)
	local inRound = state == "Round" or state == "Countdown" or state == "Starting"
	UI.Fight.Visible = fighting() and inRound
	UI.SpectateButton.Visible = not fighting() and state == "Round"
	if spectating and not (state == "Round" or state == "Ended") then
		setSpectate(false)
	end
end
for _, attr in { "State", "TimeLeft", "Status" } do
	ReplicatedStorage:GetAttributeChangedSignal(attr):Connect(refreshStatus)
end
player:GetAttributeChangedSignal("Fighting"):Connect(function()
	if fighting() then
		setSpectate(false)
		lastDash, lastSlam, lastSwing = -100, -100, -100
	end
	refreshStatus()
end)
refreshStatus()

RunService.Heartbeat:Connect(function()
	if not UI.Fight.Visible then
		return
	end
	local now = os.clock()
	UI.SetCooldown("Swing", 1 - (now - lastSwing) / Config.SwingCooldown)
	UI.SetCooldown("Dash", 1 - (now - lastDash) / Config.DashCooldown)
	UI.SetCooldown("Slam", 1 - (now - lastSlam) / Config.SlamCooldown)

	local char = character
	local kind = char and char:GetAttribute("PowerUp")
	local ends = char and char:GetAttribute("PowerUpEnds")
	local bubble = char and char:GetAttribute("Bubble")
	local text = ""
	if kind and ends then
		local left = math.max(0, math.ceil(ends - Workspace:GetServerTimeNow()))
		local info = Config.PowerUps[kind]
		if info and left > 0 then
			text = info.Label .. "  " .. left .. "s"
			UI.PowerText.TextColor3 = info.Color
		end
	end
	if bubble then
		text = (text ~= "" and text .. "   •   " or "") .. "BUBBLE"
		if not kind then
			UI.PowerText.TextColor3 = Config.PowerUps.Bubble.Color
		end
	end
	UI.PowerText.Text = text
end)

local function color(c: Color3): string
	return string.format("#%02X%02X%02X", c.R * 255, c.G * 255, c.B * 255)
end

local function escape(s: string): string
	return (s:gsub("&", "&amp;"):gsub("<", "&lt;"):gsub(">", "&gt;"))
end

Remotes.Notify.OnClientEvent:Connect(function(kind: string, data: { [string]: any })
	if kind == "Countdown" then
		local v = data.Value
		UI.ShowCenter(v, v == "BONK!" and C.Pink or C.Gold, v == "BONK!" and 1 or 0.8)
		Effects.Sound(Config.Sounds.PowerUp, nil, 0.5, v == "BONK!" and 1.5 or 1)
	elseif kind == "Banner" then
		UI.ShowBanner(data.Text, data.Color)
	elseif kind == "Collapse" then
		UI.ShowBanner("OUTER RING DROPPING!", C.Danger, 2.5)
	elseif kind == "KO" then
		local victim = escape(data.Victim)
		if data.By then
			UI.AddFeed(string.format('<font color="%s">%s</font>  bonked  <font color="%s">%s</font>', color(C.Cyan), escape(data.By), color(C.Pink), victim))
		else
			UI.AddFeed(string.format('<font color="%s">%s</font>  fell into the void', color(C.Pink), victim))
		end
		if data.Left and data.Left > 1 and data.Left <= 3 then
			UI.ShowBanner(data.Left .. " FIGHTERS LEFT!", C.Gold, 2)
		end
	elseif kind == "Eliminated" then
		clearStun()
		UI.ShowCenter("KNOCKED OUT  •  " .. UI.Ordinal(data.Placement), C.Danger, 1.8)
	elseif kind == "Reset" then
		clearStun()
		UI.SetPercent(0)
		if humanoid then
			camera.CameraSubject = humanoid
		end
	elseif kind == "RoundEnd" then
		if data.Winner then
			UI.ShowCenter(data.Winner .. " WINS!", C.Gold, 3.5)
			-- Point everyone's camera at the champion for a moment
			local model = data.WinnerModel
			local hum = model and model:FindFirstChildOfClass("Humanoid")
			if hum then
				camera.CameraSubject = hum
				task.delay(Config.EndScreenTime - 0.3, function()
					if humanoid then
						camera.CameraSubject = humanoid
					end
				end)
			end
		else
			UI.ShowCenter("DRAW!", C.Muted, 2.5)
		end
		if data.MVP then
			UI.ShowBanner("MVP: " .. data.MVP .. "  (" .. data.MVPKOs .. " KOs)", C.Pink, 4)
		end
	elseif kind == "Rewards" then
		UI.ShowResults(data)
	end
end)

Remotes.Effect.OnClientEvent:Connect(Effects.Handle)

Remotes.DataUpdate.OnClientEvent:Connect(function(data)
	UI.SetProfile(data)
end)
task.spawn(function()
	pcall(function()
		Remotes.Shop:InvokeServer("Get")
	end)
end)

-- Welcome
task.delay(2, function()
	UI.ShowBanner("Welcome to " .. Config.GameName .. "! Bonk everyone off the arena.", C.Cyan, 5)
end)
