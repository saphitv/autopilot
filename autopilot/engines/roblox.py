"""Roblox: a place open in Roblox Studio, played through Studio's built-in MCP server by the Python plugin in
autopilot/roblox (it connects like the other engines' plugins, hello "engine": "roblox"). It answers the engine-neutral
ops (find, transforms, pose, movement) in the kit frame, and `rc` in Roblox's own terms: instance paths from `game`
joined with "/" (`Workspace/Arena/Platform`, `Players/Alice/PlayerGui/HUD`), properties by name (`Position`,
`@Attribute`, `Position:Y`, `Pivot`), studs on Roblox's axes.

Roblox's frame: X right, Y up, -Z forward (a CFrame's LookVector), studs, right-handed; Orientation in degrees, y
turning left. The kit's: X forward, Y right, Z up, centimetres, with 1 stud = 28 cm:
    kit (X, Y, Z) = 28 * (-z, x, y);  kit yaw = -Orientation.y;  kit pitch = Orientation.x

The codex: the place's scripts, read live from Studio (path from `game`, then .server.luau / .client.luau / .luau by
script class), and the plugin's summary of the place (_autopilot/summary.txt: its instance tree's top, remotes, tags,
proximity prompts). A project folder on this machine (Studio's Script Sync or Rojo: .luau files beside a
sourcemap.json or *.project.json) works too.
"""

import os

from ..kit.geom import Vec, wrap
from .base import CODEX_OWN, OpEngine, safe_rel

STUD = 28.0  # centimetres per stud
EXPORT_PREFIX = "_autopilot/"
CODE_EXT = (".luau", ".lua")
SKIP = (".git", "node_modules", "Packages", "ServerPackages", "DevPackages", "build", ".venv", "_Index")


class Roblox(OpEngine):
    name = "roblox"
    title = "Roblox"
    export_dir = EXPORT_PREFIX.rstrip("/")
    error_log = True  # the plugin sends the errors and warnings both the client and the server log
    plugin_files = ()

    # -- the project on disk -----------------------------------------------------------------------------

    def project_at(self, path):
        """A folder with a sourcemap.json or a *.project.json (Script Sync or Rojo), or that folder's file."""
        if not path:
            return None
        path = os.path.abspath(path)
        if os.path.isfile(path):
            name = os.path.basename(path)
            return os.path.dirname(path) if name == "sourcemap.json" or name.endswith(".project.json") else None
        if not os.path.isdir(path):
            return None
        try:
            names = os.listdir(path)
        except OSError:
            return None
        if "sourcemap.json" in names or any(n.endswith(".project.json") for n in names):
            return path
        return None

    # -- the game backend --------------------------------------------------------------------------------

    def name_of(self, path):
        """The last part of a path ("Workspace/Arena/Coin~2" -> "Coin~2": the view's `name`)."""
        return str(path).rstrip("/").rsplit("/", 1)[-1]

    def all_actors(self, game):
        """The level's things: the models a part belongs to (a Model with a Humanoid, or one no bigger than 60
        studs), and loose parts; not every part of a model, nor the big models an area is organised in."""
        return list((game.request("find", {"things": True}) or {}).get("objects") or [])

    # -- the codex ---------------------------------------------------------------------------------------

    def codex_path(self, rel):
        """Scripts (.luau, .lua) at their paths; the plugin's summary (_autopilot/) at the codex's top."""
        parts = safe_rel(rel)
        if parts is None:
            return None
        if rel.startswith(EXPORT_PREFIX) and len(parts) > 1:
            dest = rel[len(EXPORT_PREFIX):]
            return dest if dest not in CODEX_OWN else None
        if any(p in SKIP or p.startswith(".") for p in parts[:-1]):
            return None
        if rel.lower().endswith(CODE_EXT):
            return rel
        if len(parts) == 1 and (rel == "sourcemap.json" or rel.endswith(".project.json") or rel in (
                "wally.toml", "selene.toml", ".luaurc")):
            return rel
        return None

    def skip_dir(self, rel):
        name = rel.split("/")[-1]
        return name in SKIP or name.startswith(".")

    def has_export(self, codex_root):
        return os.path.exists(os.path.join(codex_root, "summary.txt"))

    no_export_note = "note: no summary of the place in the codex (the Roblox plugin didn't make one)"

    def index_notes(self, codex_root):
        return ["", "## Where to start", "",
                "- summary.txt: the place's instance tree (top levels), its remotes (client <-> server messages), "
                "CollectionService tags and proximity prompts.",
                "- Scripts are at their place in the game: ServerScriptService/... (server), "
                "StarterPlayer/StarterPlayerScripts/... (each player's client), ReplicatedStorage/... "
                "(shared modules); .server.luau is a Script, .client.luau a LocalScript, .luau a ModuleScript.",
                "- A script's `require(script.Parent.X)` is the file X.luau beside it; "
                "`game.ReplicatedStorage.Shared.X` is ReplicatedStorage/Shared/X.luau."]

    # -- the kit frame -----------------------------------------------------------------------------------

    def to_kit(self, v):
        n = Vec.of(v)
        return None if n is None else Vec(-STUD * n.z, STUD * n.x, STUD * n.y)

    def to_native(self, v):
        k = Vec.of(v)
        return {"x": k.y / STUD, "y": k.z / STUD, "z": -k.x / STUD}

    def rotation_to_kit(self, r):
        """Orientation {"x","y","z"} (degrees), or a CFrame as `game.get` reads it ({"rx","ry","rz"}), as (yaw,
        pitch)."""
        r = r or {}
        if "ry" in r:
            return wrap(-float(r.get("ry", 0.0))), wrap(float(r.get("rx", 0.0)))
        return wrap(-float(r.get("y", 0.0))), wrap(float(r.get("x", 0.0)))

    def rotation_to_native(self, yaw, pitch=0.0):
        return {"x": pitch, "y": -wrap(yaw), "z": 0.0}

