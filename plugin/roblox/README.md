# Autopilot for Roblox Studio

Roblox Studio plugins can't press keys or see the screen. Roblox Studio's built-in MCP server (`StudioMCP`) can. So
the Roblox "plugin" is a Python process, [`autopilot/roblox`](../../autopilot/roblox). It connects to the Autopilot
brain the way the Godot and Unity plugins do (hello `"engine": "roblox"`) and answers the brain's requests through
Studio MCP:

| The brain asks | The plugin does |
|---|---|
| `session start` / `stop` / `restart` | `start_stop_play`, then waits for the character |
| `key` (W, SpaceBar, E, LeftShift, ...) | `user_keyboard_input` keyDown / keyUp (real input: the game's UserInputService and ContextActionService see it). A held key's lease is kept by the plugin, so only changes cost a call |
| `key` MouseX / MouseY | turns the camera around its focus, 0.25° per count |
| `key` LeftMouseButton, `ui_act` | `user_mouse_input`: a hover, then down and up |
| `pose`, `view` (+ sonar), `find`, `transforms`, `movement`, `screen`, `ui`, `rc` | one `execute_luau` call each, to an agent (`agent.luau`) that the plugin installs in the playtest's Client datamodel |
| `capture`, `record` | `screen_capture` on a second MCP connection, scaled with PyAV |
| `codex_manifest` / `codex_read` | the place's scripts, read live in the Server datamodel (without Wally packages) |
| logs and findings | LogService taps in the Client and Server datamodels |

Measured against a real place, with the brain on Linux and Studio on a Mac over ssh: about 35 ms for each read or
input, about 0.6 s for a still. Starting a playtest takes about 10 s.

## Run it

Studio has the place open in Edit mode.

```sh
# 1. a brain (this repo's venv): `autopilot serve` for the MCP tools, or one run:
autopilot play "Pick today's daily gift card, then stand on the arena platform"

# 2. the plugin, beside it (same machine as the brain: it binds 127.0.0.1)
python -m autopilot.roblox \
  --studio-mcp "ssh mini /Applications/RobloxStudio.app/Contents/MacOS/StudioMCP" \
  --holds mini:~/dev/roblox/dont-tip --once
```

- `--studio-mcp`: the StudioMCP command. Leave it out when Studio runs on the same machine (on macOS the default is
  `/Applications/RobloxStudio.app/Contents/MacOS/StudioMCP`; on Windows the newest `%LOCALAPPDATA%\Roblox\Versions\*\StudioMCP.exe`).
- `--place-id`: which Studio to use when several are open.
- `--holds host:repo`: Studio is shared with other agents. Before Play the plugin checks the repo's `holds.json` records
  (`scripts/studio-holds.py`), refuses while someone else holds `play-stop` or `scene`, and claims `play-stop` for itself.
  It releases the claim after Stop.
- `--no-media`: no recording or stills.

### On a ChatGPT plan, with no API keys

```sh
export AUTOPILOT_AGENT=codex AUTOPILOT_CODEX_AUTH=chatgpt AUTOPILOT_DECIDER=luna
autopilot play "..."
```

Codex writes and revises the test, and Luna (`gpt-6-luna`, no reasoning, normal speed: `autopilot/luna.py`) makes the
player's decisions in place of Jev. Both use the ChatGPT sign-in of your own Codex (`codex login`), and count against the
plan's Codex usage. A Luna decision takes about 0.7-1.6 s (Jev: 0.2-0.3 s), up to 3 s on a fresh thread. With no
Anthropic or OpenAI key there is no picture viewer: a check of how something *looks* comes back inconclusive.

## Limits

- **A playtest can't pause**: the world keeps running while the agent thinks (`autopilot/engines/roblox.md` tells the agent).
- The agent plays the **client**. Server-only state is read from what replicates, and from the codex.
- `view` doesn't report `held`, `openings`, `ways_up` or `touching`.
- Clicks on fast-moving GUIs can land before the game accepts them. The agent retries, as a player would.
