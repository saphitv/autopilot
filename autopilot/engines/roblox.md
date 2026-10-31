
## The engine: Roblox

- **The game runs as a Roblox Studio playtest** (Play solo): one client (the player you control) and its server, in the
  same Studio. Everything the kit reads is the **client's** view of the game (what replicates to it, plus the client's
  own state: PlayerGui, the camera, LocalScripts' instances). The server's own state (DataStores, server-only values in
  ServerStorage / ServerScriptService) isn't readable: infer it from what replicates (attributes, values in
  ReplicatedStorage, the leaderstats folder under the player, GUI texts) and from the codex.
- **Things are instances**, named by paths from `game`, parts joined with `/`: `Workspace/Arena/Platform`,
  `Players/<Name>/PlayerGui/HUD/Score`, `ReplicatedStorage/Shared/Config`. Siblings with the same name are told apart
  by `~2`, `~3`... after the first (`Workspace/Arena/Coins/Coin~3`); `/`, `~`, `%` in a name are written `%2F`,
  `%7E`, `%25`. `game.name_of(path)` is the last part (`Coin~3`). The player's character is `Workspace/<PlayerName>`
  (`game.pawn`); `game.controller` is "".
- **What a "thing" is**: `game.all_actors()` / `game.view` group parts into the Model they belong to when that Model
  is one thing (it has a Humanoid, or it is at most 60 studs across); parts of big models (a whole area, the arena) and
  loose parts are their own things. Folders only organise. `game.named("coin")` searches those things.
- **Classes**: `game.actors("Model")`, `game.actors("BasePart")`, `game.actors("ProximityPrompt")` (a Roblox class:
  subclasses too, searched in Workspace, Players, ReplicatedStorage, Lighting, SoundService, Teams);
  `game.actors("group:Coin")` (or `"tag:Coin"`): the instances with a CollectionService tag. A name that is no class is
  matched as an exact instance name or a tag.
- **Reads and writes** (`game.get`, `game.props`, `game.set`, `game.call`, `game.describe`): a property by name
  (`game.get(path, "Position")`, `"Transparency"`, `"Text"`, `"Health"`); an attribute with `@`
  (`game.get(path, "@Coins")`); one part of a value with `:` (`game.get(path, "Position:Y")`); a ValueBase child by its
  name (`game.get("Players/<Name>/leaderstats", "Coins")` reads leaderstats.Coins.Value); `"Pivot"` is the instance's
  pivot (GetPivot / PivotTo: `game.set(model, "Pivot", game.to_native(point))` moves a model or the character).
  `game.call(path, "GetAttribute", name="Coins")` calls an instance's method (the arguments' names don't matter,
  their order does). `game.describe(path)`
  lists the usual properties of its class, its attributes (`@Name`), its children and tags (Roblox has no reflection:
  a property it doesn't list may still be readable by name). Writes and calls run on the client: a write to a
  server-owned instance changes only the client's copy (the server won't see it), except the character, which the
  client owns.
- **Values from `game.get` are Roblox's own**: studs, Roblox's axes (X right, Y up, -Z forward), vectors as
  `{"x","y","z"}`, a CFrame as `{"x","y","z","rx","ry","rz"}` (degrees, YXZ), a Color3 as `{"r","g","b"}`, an enum as
  its name, an instance as its path. `game.to_kit(value)` turns a position or velocity into the kit's frame
  (`kit = 28 * (-z, x, y)`: 1 stud = 28 cm); `game.to_native(vec)` turns a kit point back. Everything else the kit
  gives (`game.loc`, `game.locs`, `game.bounds`, `game.pose`, `game.view`, `game.sees`, `movement`) is in the kit's
  frame already.
- **The player**: a Humanoid character. `game.pose()["at"]` is its HumanoidRootPart (the body's middle), the feet
  `half_height` below; `eye`, `yaw` and `pitch` are the **camera's** (Roblox's default camera orbits behind the
  character: the view turns with the mouse, the character faces the way it walks). `movement(game)`: Humanoid.WalkSpeed,
  the jump's launch speed (JumpPower, or from JumpHeight and Workspace.Gravity), MaxSlopeAngle; a Humanoid steps up
  low ledges by itself but how high isn't known (`step` is None): jump onto anything about knee height or more.
- **Input**: keys are Unreal names as everywhere (W, A, S, D, SpaceBar, E, LeftShift, One..Nine, Enter, Escape,
  LeftMouseButton...); the plugin presses them as real keyboard and mouse input in the playtest (UserInputService,
  ContextActionService and the default controls all see them). The game's own ContextActionService actions are in
  controls.txt and can be pressed by name. WASD moves **relative to the camera**: turn the view first (`game.turn`,
  `look_at`, `walk_to` do). Mouse look (`game.mouse` / `game.turn`, MouseX / MouseY) turns the camera around the
  character, about 0.25 degree per count; the Left / Right arrow keys turn it too. LeftMouseButton clicks at the
  middle of the screen (most Roblox tools fire there). **ProximityPrompts** ("[E] Open") trigger when you hold their
  key near them: walk within their MaxActivationDistance (usually 10 studs = 280 cm), face them, then
  `game.press("E")` (or `game.hold_timed("E", HoldDuration + 0.2)` when the prompt has a HoldDuration);
  summary.txt in the codex lists the place's prompts and their keys.
- **The playtest can't pause**: `game.pause` does nothing, so the world (round timers, enemies, other things moving)
  keeps going while you think between probes. Keep probes short, read what changed rather than assume nothing did, and
  expect rounds to have moved on.
- **Scenes**: a Roblox place is one world; there are no maps to open. `game.restart()` stops the playtest and starts
  it again (about 10-15 seconds): the server and client start fresh (scripts rerun, the character respawns), but data
  the game saves through DataStores in Studio may persist between playtests (many games use mock data in Studio: read
  the codex). Game time (`game.t`) is Workspace.DistributedGameTime and starts at 0 in each playtest.
- **Physics**: the character is a Humanoid; it walks up slopes up to MaxSlopeAngle and climbs TrussParts. Falling into
  the void (below Workspace.FallenPartsDestroyHeight) kills it and it respawns after a few seconds (a new character:
  `game.pawn` stays `Workspace/<PlayerName>`). A dead character has no pose until it respawns ("No player").
- **`game.view`** here tells: visible things (name, path, class, center, size, distance, screen position,
  crosshair_on, top/bottom against the feet, speed, look: lights, BillboardGui/SurfaceGui texts, the animation playing),
  the camera (fov horizontal and vertical), the body (on_ground, falling, standing_on, floor slope) and, with
  `sonar=True`, the sonar ways (box sweeps from the knees up, rays down for drops). Parts fully transparent are left
  out; invisible collision boxes still block the line of sight and the sonar. It never tells `held`, `openings`,
  `ways_up`, slope ends or lanes, `under`/`overhead`, highlights, effects or `touching` (the answer's `omitted` lists
  them): use the codex and `game.locs` / `game.bounds` for those. Without `ways_up`, `walk_to` finds no way up to a
  raised goal by itself: walk it to the ramp's or stairs' foot and top first.
- **`game.screen()`** keys are `<ScreenGui>.<Name>` (`HUD.Score`, `Shop.Title`), with the path inside the ScreenGui
  when two share a name (`GameFeedback.HUD.Round.Alive`): every visible, non-empty text of the PlayerGui.
  `game.ui()` lists the visible GuiButtons (TextButton, ImageButton: kind "button") and TextBoxes ("text_input");
  `ui_act` clicks with the mouse at the element's middle (a full-screen backdrop button may sit under a menu: prefer
  the element whose label says what you want). ImageButtons often have no text: their `label` is a text inside them
  or their name.
- **The codex** has the place's scripts as Studio has them now: ServerScriptService/... (server Scripts:
  `.server.luau`), StarterPlayer/StarterPlayerScripts/... (the client: LocalScripts `.client.luau`, modules `.luau`),
  ReplicatedStorage/... (shared modules); summary.txt: the instance tree's top levels, the remotes, tags and proximity
  prompts. Server code tells you what the server will do with what you trigger; you see its effects only as they
  replicate.
- **In Studio**, the brain starts and stops the playtest itself; Studio may be shared with other people (Team Create)
  and other agents, so the playtest only starts when nobody else holds Studio. With no playtest running, only
  `game.context()` answers.
