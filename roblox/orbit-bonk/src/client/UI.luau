-- All of Orbit Bonk's screen UI, built in code.

local Players = game:GetService("Players")
local ReplicatedStorage = game:GetService("ReplicatedStorage")
local TweenService = game:GetService("TweenService")
local UserInputService = game:GetService("UserInputService")

local Config = require(ReplicatedStorage.Shared.Config)
local Remotes = ReplicatedStorage.Remotes

local player = Players.LocalPlayer
local C = Config.Colors

local UI = {} :: any

local function new(className: string, props: { [string]: any }?, children: { Instance }?): any
	local inst = Instance.new(className)
	if props then
		for k, v in props do
			if k ~= "Parent" then
				(inst :: any)[k] = v
			end
		end
	end
	if children then
		for _, child in children do
			child.Parent = inst
		end
	end
	if props and props.Parent then
		inst.Parent = props.Parent
	end
	return inst
end

local function corner(r: number?)
	return new("UICorner", { CornerRadius = UDim.new(0, r or 12) })
end

local function stroke(color: Color3?, thickness: number?, transparency: number?)
	return new("UIStroke", {
		Color = color or C.Cyan,
		Thickness = thickness or 2,
		Transparency = transparency or 0,
		ApplyStrokeMode = Enum.ApplyStrokeMode.Border,
	})
end

local function textStroke(thickness: number?)
	return new("UIStroke", { Thickness = thickness or 2, Color = Color3.fromRGB(10, 8, 30) })
end

local function panel(props: { [string]: any }): Frame
	local f = new("Frame", props)
	f.BackgroundColor3 = props.BackgroundColor3 or C.Panel
	f.BackgroundTransparency = props.BackgroundTransparency or 0.15
	corner(14).Parent = f
	stroke(C.Cyan, 1.5, 0.5).Parent = f
	return f
end

local function label(props: { [string]: any }): TextLabel
	local defaults = {
		BackgroundTransparency = 1,
		Font = Enum.Font.GothamBold,
		TextColor3 = C.Text,
		TextScaled = true,
	}
	for k, v in props do
		defaults[k] = v
	end
	return new("TextLabel", defaults)
end

local function pop(obj: GuiObject)
	local scale = obj:FindFirstChildOfClass("UIScale") or new("UIScale", { Parent = obj })
	scale.Scale = 0.6
	TweenService:Create(scale, TweenInfo.new(0.35, Enum.EasingStyle.Back, Enum.EasingDirection.Out), { Scale = 1 }):Play()
end

function UI.Build()
	local gui = new("ScreenGui", {
		Name = "OrbitBonkHUD",
		ResetOnSpawn = false,
		IgnoreGuiInset = false,
		ZIndexBehavior = Enum.ZIndexBehavior.Sibling,
		Parent = player:WaitForChild("PlayerGui"),
	})
	UI.Gui = gui

	-- Top status pill
	local top = panel({
		Name = "Status",
		AnchorPoint = Vector2.new(0.5, 0),
		Position = UDim2.new(0.5, 0, 0, 8),
		Size = UDim2.fromOffset(360, 64),
		Parent = gui,
	})
	new("UIGradient", {
		Color = ColorSequence.new(Color3.fromRGB(30, 20, 70), Color3.fromRGB(14, 16, 40)),
		Rotation = 90,
		Parent = top,
	})
	UI.StatusText = label({
		Text = "Waiting for players",
		Position = UDim2.new(0, 16, 0, 6),
		Size = UDim2.new(1, -120, 0, 22),
		TextXAlignment = Enum.TextXAlignment.Left,
		TextColor3 = C.Muted,
		Parent = top,
	})
	UI.TitleText = label({
		Text = Config.GameName,
		Font = Enum.Font.FredokaOne,
		Position = UDim2.new(0, 16, 0, 26),
		Size = UDim2.new(1, -120, 0, 32),
		TextXAlignment = Enum.TextXAlignment.Left,
		TextColor3 = C.Cyan,
		Parent = top,
	})
	UI.TimerText = label({
		Text = "",
		Font = Enum.Font.FredokaOne,
		AnchorPoint = Vector2.new(1, 0.5),
		Position = UDim2.new(1, -14, 0.5, 0),
		Size = UDim2.fromOffset(96, 44),
		TextXAlignment = Enum.TextXAlignment.Right,
		TextColor3 = C.Gold,
		Parent = top,
	})
	textStroke(2).Parent = UI.TimerText

	-- Left column: coins, shop, spectate
	local left = new("Frame", {
		Name = "Left",
		BackgroundTransparency = 1,
		AnchorPoint = Vector2.new(0, 0.5),
		Position = UDim2.new(0, 14, 0.45, 0),
		Size = UDim2.fromOffset(170, 200),
		Parent = gui,
	}, {
		new("UIListLayout", { Padding = UDim.new(0, 10), SortOrder = Enum.SortOrder.LayoutOrder }),
	})
	local coins = panel({ Name = "Coins", Size = UDim2.fromOffset(170, 50), LayoutOrder = 1, Parent = left })
	local coinIcon = new("Frame", {
		BackgroundColor3 = C.Gold,
		Position = UDim2.fromOffset(12, 11),
		Size = UDim2.fromOffset(28, 28),
		Parent = coins,
	}, { corner(14), stroke(Color3.fromRGB(255, 255, 200), 2) })
	label({ Text = "B", Font = Enum.Font.FredokaOne, Size = UDim2.fromScale(1, 1), TextColor3 = Color3.fromRGB(120, 70, 0), Parent = coinIcon })
	UI.CoinsText = label({
		Text = "0",
		Font = Enum.Font.FredokaOne,
		Position = UDim2.fromOffset(50, 8),
		Size = UDim2.new(1, -60, 0, 34),
		TextXAlignment = Enum.TextXAlignment.Left,
		TextColor3 = C.Gold,
		Parent = coins,
	})

	local function sideButton(text: string, color: Color3, order: number): TextButton
		local b = new("TextButton", {
			Text = text,
			Font = Enum.Font.FredokaOne,
			TextScaled = true,
			TextColor3 = C.Text,
			BackgroundColor3 = color,
			Size = UDim2.fromOffset(170, 46),
			LayoutOrder = order,
			AutoButtonColor = true,
			Parent = left,
		}, { corner(12), new("UIPadding", { PaddingTop = UDim.new(0, 8), PaddingBottom = UDim.new(0, 8) }) })
		textStroke(2).Parent = b
		new("UIGradient", {
			Color = ColorSequence.new(Color3.new(1, 1, 1), Color3.fromRGB(180, 180, 200)),
			Rotation = 90,
			Parent = b,
		})
		return b
	end
	UI.ShopButton = sideButton("HAMMER SHOP", C.Pink, 2)
	UI.SpectateButton = sideButton("SPECTATE", Color3.fromRGB(80, 90, 200), 3)
	UI.SpectateButton.Visible = false

	-- Spectate bar
	local spec = panel({
		Name = "SpectateBar",
		AnchorPoint = Vector2.new(0.5, 1),
		Position = UDim2.new(0.5, 0, 1, -24),
		Size = UDim2.fromOffset(380, 56),
		Visible = false,
		Parent = gui,
	})
	UI.SpectateBar = spec
	UI.SpectatePrev = new("TextButton", {
		Text = "<",
		Font = Enum.Font.FredokaOne,
		TextScaled = true,
		TextColor3 = C.Text,
		BackgroundColor3 = C.PanelLight,
		Position = UDim2.fromOffset(8, 8),
		Size = UDim2.fromOffset(40, 40),
		Parent = spec,
	}, { corner(10) })
	UI.SpectateNext = new("TextButton", {
		Text = ">",
		Font = Enum.Font.FredokaOne,
		TextScaled = true,
		TextColor3 = C.Text,
		BackgroundColor3 = C.PanelLight,
		AnchorPoint = Vector2.new(1, 0),
		Position = UDim2.new(1, -8, 0, 8),
		Size = UDim2.fromOffset(40, 40),
		Parent = spec,
	}, { corner(10) })
	UI.SpectateName = label({
		Text = "",
		Font = Enum.Font.FredokaOne,
		Position = UDim2.fromOffset(56, 10),
		Size = UDim2.new(1, -112, 0, 36),
		Parent = spec,
	})

	-- Bottom fight HUD: big % and ability slots
	local fight = new("Frame", {
		Name = "Fight",
		BackgroundTransparency = 1,
		AnchorPoint = Vector2.new(0.5, 1),
		Position = UDim2.new(0.5, 0, 1, -16),
		Size = UDim2.fromOffset(420, 150),
		Visible = false,
		Parent = gui,
	})
	UI.Fight = fight
	UI.PercentText = label({
		Text = "0%",
		Font = Enum.Font.FredokaOne,
		AnchorPoint = Vector2.new(0.5, 0),
		Position = UDim2.new(0.5, 0, 0, 0),
		Size = UDim2.fromOffset(220, 70),
		Parent = fight,
	})
	textStroke(4).Parent = UI.PercentText
	UI.PowerText = label({
		Text = "",
		Font = Enum.Font.FredokaOne,
		AnchorPoint = Vector2.new(0.5, 1),
		Position = UDim2.new(0.5, 0, 0, -2),
		Size = UDim2.fromOffset(300, 26),
		Parent = fight,
	})
	textStroke(2).Parent = UI.PowerText

	local slots = new("Frame", {
		BackgroundTransparency = 1,
		AnchorPoint = Vector2.new(0.5, 1),
		Position = UDim2.new(0.5, 0, 1, 0),
		Size = UDim2.fromOffset(300, 70),
		Parent = fight,
	}, {
		new("UIListLayout", {
			FillDirection = Enum.FillDirection.Horizontal,
			HorizontalAlignment = Enum.HorizontalAlignment.Center,
			Padding = UDim.new(0, 12),
		}),
	})
	UI.Slots = {}
	local touch = UserInputService.TouchEnabled and not UserInputService.KeyboardEnabled
	for i, info in {
		{ "Swing", touch and "TAP" or "CLICK", C.Cyan },
		{ "Dash", touch and "DASH" or "Q", C.Gold },
		{ "Slam", touch and "SLAM" or "E", C.Pink },
	} do
		local slot = panel({ Name = info[1], Size = UDim2.fromOffset(86, 66), LayoutOrder = i, Parent = slots })
		label({
			Text = string.upper(info[1]),
			Font = Enum.Font.FredokaOne,
			Position = UDim2.fromOffset(0, 8),
			Size = UDim2.new(1, 0, 0, 26),
			TextColor3 = info[3],
			Parent = slot,
		})
		label({
			Text = info[2],
			Position = UDim2.fromOffset(0, 38),
			Size = UDim2.new(1, 0, 0, 18),
			TextColor3 = C.Muted,
			Parent = slot,
		})
		local cover = new("Frame", {
			Name = "Cooldown",
			BackgroundColor3 = Color3.new(0, 0, 0),
			BackgroundTransparency = 0.35,
			AnchorPoint = Vector2.new(0, 1),
			Position = UDim2.fromScale(0, 1),
			Size = UDim2.fromScale(1, 0),
			ZIndex = 3,
			Parent = slot,
		}, { corner(14) })
		UI.Slots[info[1]] = cover
	end

	-- Kill feed
	UI.Feed = new("Frame", {
		Name = "Feed",
		BackgroundTransparency = 1,
		AnchorPoint = Vector2.new(1, 0),
		Position = UDim2.new(1, -14, 0.3, 0),
		Size = UDim2.fromOffset(300, 220),
		Parent = gui,
	}, {
		new("UIListLayout", {
			Padding = UDim.new(0, 6),
			HorizontalAlignment = Enum.HorizontalAlignment.Right,
			VerticalAlignment = Enum.VerticalAlignment.Top,
			SortOrder = Enum.SortOrder.LayoutOrder,
		}),
	})

	-- Big centre text (countdown / banners)
	UI.Center = label({
		Text = "",
		Font = Enum.Font.FredokaOne,
		AnchorPoint = Vector2.new(0.5, 0.5),
		Position = UDim2.fromScale(0.5, 0.4),
		Size = UDim2.fromOffset(600, 120),
		TextColor3 = C.Text,
		Visible = false,
		Parent = gui,
	})
	textStroke(5).Parent = UI.Center
	UI.Banner = label({
		Text = "",
		Font = Enum.Font.FredokaOne,
		AnchorPoint = Vector2.new(0.5, 0),
		Position = UDim2.new(0.5, 0, 0, 84),
		Size = UDim2.fromOffset(640, 44),
		Visible = false,
		Parent = gui,
	})
	textStroke(3).Parent = UI.Banner

	-- Results card
	local results = panel({
		Name = "Results",
		AnchorPoint = Vector2.new(0.5, 0.5),
		Position = UDim2.fromScale(0.5, 0.55),
		Size = UDim2.fromOffset(380, 210),
		Visible = false,
		Parent = gui,
	})
	UI.Results = results
	UI.ResultsTitle = label({
		Text = "",
		Font = Enum.Font.FredokaOne,
		Position = UDim2.fromOffset(16, 14),
		Size = UDim2.new(1, -32, 0, 46),
		TextColor3 = C.Gold,
		Parent = results,
	})
	textStroke(3).Parent = UI.ResultsTitle
	UI.ResultsBody = label({
		Text = "",
		RichText = true,
		Position = UDim2.fromOffset(16, 70),
		Size = UDim2.new(1, -32, 0, 120),
		TextYAlignment = Enum.TextYAlignment.Top,
		Parent = results,
	})

	UI.BuildShop(gui)
	return gui
end

-- Toast message (shop feedback etc.)
function UI.Toast(text: string, color: Color3?)
	local t = label({
		Text = text,
		Font = Enum.Font.FredokaOne,
		AnchorPoint = Vector2.new(0.5, 1),
		Position = UDim2.new(0.5, 0, 1, -180),
		Size = UDim2.fromOffset(520, 34),
		TextColor3 = color or C.Text,
		Parent = UI.Gui,
	})
	textStroke(2).Parent = t
	pop(t)
	task.delay(2.2, function()
		TweenService:Create(t, TweenInfo.new(0.4), { TextTransparency = 1 }):Play()
		task.wait(0.4)
		t:Destroy()
	end)
end

function UI.SetStatus(state: string, timeLeft: number, status: string)
	UI.StatusText.Text = status
	if state == "Intermission" or state == "Round" or state == "Ended" then
		local m = math.floor(timeLeft / 60)
		local s = timeLeft % 60
		UI.TimerText.Text = state == "Round" and string.format("%d:%02d", m, s) or tostring(timeLeft)
	else
		UI.TimerText.Text = ""
	end
	if state == "Round" then
		UI.TitleText.Text = "BRAWL!"
		UI.TitleText.TextColor3 = C.Pink
		UI.TimerText.TextColor3 = timeLeft <= 15 and C.Danger or C.Gold
	elseif state == "Intermission" then
		UI.TitleText.Text = "INTERMISSION"
		UI.TitleText.TextColor3 = C.Cyan
		UI.TimerText.TextColor3 = C.Gold
	elseif state == "Countdown" or state == "Starting" then
		UI.TitleText.Text = "GET READY"
		UI.TitleText.TextColor3 = C.Gold
	elseif state == "Ended" then
		UI.TitleText.Text = "ROUND OVER"
		UI.TitleText.TextColor3 = C.Gold
	else
		UI.TitleText.Text = Config.GameName
		UI.TitleText.TextColor3 = C.Cyan
	end
end

local centerToken = 0
function UI.ShowCenter(text: string, color: Color3?, duration: number?)
	centerToken += 1
	local token = centerToken
	UI.Center.Text = text
	UI.Center.TextColor3 = color or C.Text
	UI.Center.TextTransparency = 0
	UI.Center.Visible = true
	pop(UI.Center)
	task.delay(duration or 0.9, function()
		if token == centerToken then
			TweenService:Create(UI.Center, TweenInfo.new(0.25), { TextTransparency = 1 }):Play()
			task.wait(0.25)
			if token == centerToken then
				UI.Center.Visible = false
			end
		end
	end)
end

local bannerToken = 0
function UI.ShowBanner(text: string, color: Color3?, duration: number?)
	bannerToken += 1
	local token = bannerToken
	UI.Banner.Text = text
	UI.Banner.TextColor3 = color or C.Gold
	UI.Banner.Visible = true
	pop(UI.Banner)
	task.delay(duration or 3, function()
		if token == bannerToken then
			UI.Banner.Visible = false
		end
	end)
end

local feedOrder = 0
function UI.AddFeed(richText: string)
	feedOrder += 1
	local item = new("Frame", {
		BackgroundColor3 = C.Panel,
		BackgroundTransparency = 0.25,
		Size = UDim2.fromOffset(300, 30),
		LayoutOrder = -feedOrder,
		Parent = UI.Feed,
	}, { corner(8) })
	local l = label({
		Text = richText,
		RichText = true,
		Position = UDim2.fromOffset(10, 5),
		Size = UDim2.new(1, -20, 1, -10),
		TextXAlignment = Enum.TextXAlignment.Right,
		Parent = item,
	})
	local items = {}
	for _, child in UI.Feed:GetChildren() do
		if child:IsA("Frame") then
			table.insert(items, child)
		end
	end
	if #items > 6 then
		table.sort(items, function(a, b)
			return a.LayoutOrder > b.LayoutOrder
		end)
		items[#items]:Destroy()
	end
	task.delay(6, function()
		if item.Parent then
			TweenService:Create(item, TweenInfo.new(0.5), { BackgroundTransparency = 1 }):Play()
			TweenService:Create(l, TweenInfo.new(0.5), { TextTransparency = 1 }):Play()
			task.wait(0.5)
			item:Destroy()
		end
	end)
end

function UI.SetPercent(p: number)
	UI.PercentText.Text = p .. "%"
	UI.PercentText.TextColor3 = Config.PercentColor(p)
	pop(UI.PercentText)
end

function UI.SetCooldown(slot: string, fraction: number)
	local cover = UI.Slots[slot]
	if cover then
		cover.Size = UDim2.fromScale(1, math.clamp(fraction, 0, 1))
	end
end

local function ordinal(n: number): string
	local suffix = "th"
	if n % 100 < 11 or n % 100 > 13 then
		if n % 10 == 1 then
			suffix = "st"
		elseif n % 10 == 2 then
			suffix = "nd"
		elseif n % 10 == 3 then
			suffix = "rd"
		end
	end
	return n .. suffix
end
UI.Ordinal = ordinal

function UI.ShowResults(data: { [string]: any })
	UI.ResultsTitle.Text = data.Won and "YOU WIN!" or ("You placed " .. ordinal(data.Placement))
	UI.ResultsTitle.TextColor3 = data.Won and C.Gold or C.Cyan
	UI.ResultsBody.Text = string.format(
		'KOs: <font color="#FF46BE">%d</font>\nCoins earned: <font color="#FFCD3C">+%d</font>\n<font color="#969FC8">Spend coins on new hammers in the shop!</font>',
		data.KOs,
		data.Coins
	)
	UI.Results.Visible = true
	pop(UI.Results)
	task.delay(Config.EndScreenTime - 0.5, function()
		UI.Results.Visible = false
	end)
end

---------------------------------------------------------------------------
-- Shop
---------------------------------------------------------------------------
local profile = { Coins = 0, Owned = {}, Equipped = Config.Skins[1].Id }
local cards: { [string]: { Button: TextButton, Price: TextLabel } } = {}

local function refreshShop()
	for id, card in cards do
		local skin = Config.GetSkin(id)
		if profile.Equipped == id then
			card.Button.Text = "EQUIPPED"
			card.Button.BackgroundColor3 = Color3.fromRGB(60, 200, 120)
		elseif profile.Owned[id] then
			card.Button.Text = "EQUIP"
			card.Button.BackgroundColor3 = C.Cyan
		else
			card.Button.Text = "BUY"
			card.Button.BackgroundColor3 = profile.Coins >= skin.Price and C.Gold or Color3.fromRGB(90, 90, 110)
		end
		card.Price.Text = profile.Owned[id] and "OWNED" or (skin.Price .. " coins")
	end
end

function UI.BuildShop(gui: ScreenGui)
	local shop = panel({
		Name = "Shop",
		AnchorPoint = Vector2.new(0.5, 0.5),
		Position = UDim2.fromScale(0.5, 0.5),
		Size = UDim2.fromScale(0.92, 0.86),
		Visible = false,
		ZIndex = 10,
		Parent = gui,
	})
	new("UISizeConstraint", { MaxSize = Vector2.new(640, 430), Parent = shop })
	UI.Shop = shop
	label({
		Text = "HAMMER SHOP",
		Font = Enum.Font.FredokaOne,
		Position = UDim2.fromOffset(20, 14),
		Size = UDim2.new(1, -100, 0, 40),
		TextXAlignment = Enum.TextXAlignment.Left,
		TextColor3 = C.Pink,
		Parent = shop,
	})
	UI.ShopCoins = label({
		Text = "",
		Font = Enum.Font.FredokaOne,
		AnchorPoint = Vector2.new(1, 0),
		Position = UDim2.new(1, -70, 0, 20),
		Size = UDim2.fromOffset(200, 30),
		TextXAlignment = Enum.TextXAlignment.Right,
		TextColor3 = C.Gold,
		Parent = shop,
	})
	local close = new("TextButton", {
		Text = "X",
		Font = Enum.Font.FredokaOne,
		TextScaled = true,
		TextColor3 = C.Text,
		BackgroundColor3 = C.Danger,
		AnchorPoint = Vector2.new(1, 0),
		Position = UDim2.new(1, -14, 0, 14),
		Size = UDim2.fromOffset(42, 42),
		Parent = shop,
	}, { corner(10), new("UIPadding", { PaddingTop = UDim.new(0, 6), PaddingBottom = UDim.new(0, 6) }) })
	close.Activated:Connect(function()
		shop.Visible = false
	end)
	label({
		Text = "Hammers are cosmetic: every bonk is fair. Earn coins from KOs, top-3 finishes and wins.",
		Position = UDim2.fromOffset(20, 56),
		Size = UDim2.new(1, -40, 0, 18),
		TextXAlignment = Enum.TextXAlignment.Left,
		TextColor3 = C.Muted,
		Parent = shop,
	})

	local grid = new("ScrollingFrame", {
		BackgroundTransparency = 1,
		Position = UDim2.fromOffset(16, 84),
		Size = UDim2.new(1, -32, 1, -100),
		CanvasSize = UDim2.new(),
		AutomaticCanvasSize = Enum.AutomaticSize.Y,
		ScrollBarThickness = 6,
		ScrollBarImageColor3 = C.Cyan,
		BorderSizePixel = 0,
		Parent = shop,
	}, {
		new("UIGridLayout", {
			CellSize = UDim2.fromOffset(186, 150),
			CellPadding = UDim2.fromOffset(12, 12),
			SortOrder = Enum.SortOrder.LayoutOrder,
		}),
	})

	for i, skin in Config.Skins do
		local card = new("Frame", {
			BackgroundColor3 = C.PanelLight,
			LayoutOrder = i,
			Parent = grid,
		}, { corner(12), stroke(skin.Trail, 1.5, 0.3) })
		-- Mini hammer preview
		local preview = new("Frame", {
			BackgroundTransparency = 1,
			Position = UDim2.fromOffset(0, 8),
			Size = UDim2.new(1, 0, 0, 56),
			Parent = card,
		})
		new("Frame", {
			BackgroundColor3 = Color3.fromRGB(40, 40, 60),
			AnchorPoint = Vector2.new(0.5, 0),
			Position = UDim2.new(0.5, 0, 0, 22),
			Size = UDim2.fromOffset(10, 34),
			Parent = preview,
		}, { corner(4) })
		local head = new("Frame", {
			BackgroundColor3 = skin.Head,
			AnchorPoint = Vector2.new(0.5, 0),
			Position = UDim2.new(0.5, 0, 0, 0),
			Size = UDim2.fromOffset(84, 30),
			Parent = preview,
		}, { corner(10), stroke(skin.Accent, 3) })
		for _, x in { 0.22, 0.78 } do
			new("Frame", {
				BackgroundColor3 = skin.Accent,
				BorderSizePixel = 0,
				AnchorPoint = Vector2.new(0.5, 0),
				Position = UDim2.fromScale(x, 0),
				Size = UDim2.new(0, 6, 1, 0),
				Parent = head,
			})
		end
		label({
			Text = skin.Name,
			Font = Enum.Font.FredokaOne,
			Position = UDim2.fromOffset(8, 68),
			Size = UDim2.new(1, -16, 0, 24),
			TextColor3 = skin.Trail,
			Parent = card,
		})
		local price = label({
			Text = "",
			Position = UDim2.fromOffset(8, 92),
			Size = UDim2.new(1, -16, 0, 16),
			TextColor3 = C.Muted,
			Parent = card,
		})
		local button = new("TextButton", {
			Text = "BUY",
			Font = Enum.Font.FredokaOne,
			TextScaled = true,
			TextColor3 = Color3.fromRGB(15, 15, 30),
			BackgroundColor3 = C.Gold,
			AnchorPoint = Vector2.new(0.5, 1),
			Position = UDim2.new(0.5, 0, 1, -8),
			Size = UDim2.new(1, -16, 0, 30),
			Parent = card,
		}, { corner(8), new("UIPadding", { PaddingTop = UDim.new(0, 5), PaddingBottom = UDim.new(0, 5) }) })
		cards[skin.Id] = { Button = button, Price = price }

		local busy = false
		button.Activated:Connect(function()
			if busy or profile.Equipped == skin.Id then
				return
			end
			busy = true
			local action = profile.Owned[skin.Id] and "Equip" or "Buy"
			local ok, result = pcall(function()
				return Remotes.Shop:InvokeServer(action, skin.Id)
			end)
			busy = false
			if ok and type(result) == "table" and result.msg then
				UI.Toast(result.msg, result.ok and C.Cyan or C.Danger)
			end
		end)
	end

	UI.ShopButton.Activated:Connect(function()
		shop.Visible = not shop.Visible
		if shop.Visible then
			pop(shop)
		end
	end)
	refreshShop()
end

function UI.SetProfile(data: { [string]: any })
	local before = profile.Coins
	profile = data
	UI.CoinsText.Text = tostring(data.Coins)
	if UI.ShopCoins then
		UI.ShopCoins.Text = data.Coins .. " coins"
	end
	if data.Coins > before and before > 0 then
		pop(UI.CoinsText)
	end
	refreshShop()
end

return UI
