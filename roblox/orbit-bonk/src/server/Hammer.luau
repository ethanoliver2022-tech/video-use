-- Builds the Bonk Hammer, either as a player Tool or welded into a bot's hand.

local ReplicatedStorage = game:GetService("ReplicatedStorage")

local Config = require(ReplicatedStorage.Shared.Config)

local Hammer = {}

local GRIP = CFrame.new(0, 0, -1.5)

local function build(skinId: string, parent: Instance): BasePart
	local skin = Config.GetSkin(skinId)

	local handle = Instance.new("Part")
	handle.Name = "Handle"
	handle.Size = Vector3.new(0.45, 0.45, 4.4)
	handle.Color = Color3.fromRGB(40, 40, 60)
	handle.Material = Enum.Material.Metal
	handle.CanCollide = false
	handle.Massless = true
	handle.CFrame = CFrame.new()
	handle.Parent = parent

	local function piece(name: string, size: Vector3, offset: CFrame, color: Color3, material: Enum.Material, shape: Enum.PartType?)
		local p = Instance.new("Part")
		p.Name = name
		if shape then
			p.Shape = shape
		end
		p.Size = size
		p.Color = color
		p.Material = material
		p.CanCollide = false
		p.CanQuery = false
		p.CanTouch = false
		p.Massless = true
		p.CFrame = handle.CFrame * offset
		local w = Instance.new("WeldConstraint")
		w.Part0 = handle
		w.Part1 = p
		w.Parent = p
		p.Parent = parent
		return p
	end

	local headOffset = CFrame.new(0, 0, 2.2)
	local head = piece("Head", Vector3.new(3.6, 2.1, 2.1), headOffset, skin.Head, skin.Material, Enum.PartType.Cylinder)
	for _, x in { -1.25, 1.25 } do
		piece("Band", Vector3.new(0.35, 2.25, 2.25), headOffset * CFrame.new(x, 0, 0), skin.Accent, Enum.Material.Neon, Enum.PartType.Cylinder)
	end
	for _, x in { -1.85, 1.85 } do
		piece("Cap", Vector3.new(0.2, 1.5, 1.5), headOffset * CFrame.new(x, 0, 0), skin.Accent, Enum.Material.Neon, Enum.PartType.Cylinder)
	end
	piece("Grip", Vector3.new(0.55, 0.55, 1.4), CFrame.new(0, 0, -1.3), skin.Accent, Enum.Material.Neon)

	-- Swing trail
	local a0 = Instance.new("Attachment")
	a0.Position = Vector3.new(-1.8, 0, 0)
	a0.Parent = head
	local a1 = Instance.new("Attachment")
	a1.Position = Vector3.new(1.8, 0, 0)
	a1.Parent = head
	local trail = Instance.new("Trail")
	trail.Attachment0 = a0
	trail.Attachment1 = a1
	trail.Color = ColorSequence.new(skin.Trail)
	trail.Transparency = NumberSequence.new(0.2, 1)
	trail.Lifetime = 0.18
	trail.LightEmission = 1
	trail.FaceCamera = true
	trail.Parent = head

	local light = Instance.new("PointLight")
	light.Color = skin.Trail
	light.Range = 8
	light.Brightness = 1.5
	light.Parent = head

	return handle
end

function Hammer.MakeTool(skinId: string): Tool
	local tool = Instance.new("Tool")
	tool.Name = "Bonk Hammer"
	tool.CanBeDropped = false
	tool.RequiresHandle = true
	tool.ToolTip = "Click to BONK"
	tool.Grip = GRIP
	tool:SetAttribute("Skin", skinId)
	build(skinId, tool)
	return tool
end

-- For bots: weld a hammer into the right hand. Returns the weld so it can be animated.
function Hammer.AttachToRig(character: Model, skinId: string): Weld?
	local hand = character:FindFirstChild("RightHand") :: BasePart?
	if not hand then
		return nil
	end
	local holder = Instance.new("Model")
	holder.Name = "BotHammer"
	local handle = build(skinId, holder)
	holder.Parent = character
	local gripAttachment = hand:FindFirstChild("RightGripAttachment") :: Attachment?
	local weld = Instance.new("Weld")
	weld.Name = "HammerGrip"
	weld.Part0 = hand
	weld.Part1 = handle
	weld.C0 = gripAttachment and gripAttachment.CFrame or CFrame.new(0, -0.5, 0)
	weld.C1 = GRIP
	weld.Parent = handle
	return weld
end

return Hammer
