"""The Roblox Studio plugin of Autopilot, in Python: it connects to the brain like the Godot and Unity plugins do (a
WebSocket client, protocol 1, engine "roblox") and answers the brain's requests through Roblox Studio's built-in MCP
server (autopilot/roblox/studio_mcp.py), with an agent it installs in the playtest (agent.luau) for the reads.

    python -m autopilot.roblox --studio-mcp "ssh mini /Applications/RobloxStudio.app/Contents/MacOS/StudioMCP"

Order: inputs (key, ui_act, session, ...) are applied one after another in the order they came; a read waits for
every input that came before it, then runs (several reads at once). Key holds are leased here: the brain renews a held
key every tick, and only a change (down, up) costs a call to Studio.
"""

import base64
import concurrent.futures
import hashlib
import itertools
import json
import os
import queue
import re
import sys
import threading
import time
import traceback
from fractions import Fraction

from websockets.sync.client import connect

from .holds import Holds, HoldsError
from .studio_mcp import StudioError, StudioMCP, default_command

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_VERSION = "0.1.0"
DEG_PER_COUNT = 0.25  # degrees the view turns per mouse count (MouseX / MouseY values)
MIN_LEASE = 0.6  # seconds a held key stays down without a renewal
PRESS_MS = 60

# Unreal FKey names (the kit's) -> Roblox KeyCode names
KEYS = {**{c: c for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"},
        **{d: d for d in ("Zero", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine")},
        **{f"F{i}": f"F{i}" for i in range(1, 13)},
        "SpaceBar": "Space", "Space": "Space", "Enter": "Return", "Return": "Return", "Escape": "Escape", "Tab": "Tab",
        "BackSpace": "Backspace", "Backspace": "Backspace", "Delete": "Delete", "Insert": "Insert", "Home": "Home",
        "End": "End", "PageUp": "PageUp", "PageDown": "PageDown", "CapsLock": "CapsLock",
        "LeftShift": "LeftShift", "RightShift": "RightShift", "LeftControl": "LeftControl",
        "RightControl": "RightControl", "LeftAlt": "LeftAlt", "RightAlt": "RightAlt",
        "Up": "Up", "Down": "Down", "Left": "Left", "Right": "Right",
        "Comma": "Comma", "Period": "Period", "Slash": "Slash", "Semicolon": "Semicolon", "Minus": "Minus",
        "Equals": "Equals", "Tilde": "Tilde", "Backquote": "Backquote", "Quote": "Quote",
        "LeftBracket": "LeftBracket", "RightBracket": "RightBracket", "Backslash": "BackSlash",
        "NumPadZero": "KeypadZero", "NumPadOne": "KeypadOne", "NumPadTwo": "KeypadTwo", "NumPadThree": "KeypadThree",
        "NumPadFour": "KeypadFour", "NumPadFive": "KeypadFive", "NumPadSix": "KeypadSix",
        "NumPadSeven": "KeypadSeven", "NumPadEight": "KeypadEight", "NumPadNine": "KeypadNine"}
ROBLOX_TO_FKEY = {"Space": "SpaceBar", "Return": "Enter", "Backspace": "BackSpace", "MouseButton1": "LeftMouseButton",
                  "MouseButton2": "RightMouseButton", "MouseButton3": "MiddleMouseButton"}
MOUSE_BUTTONS = {"LeftMouseButton": "left", "RightMouseButton": "right"}
MOUSE_AXES = ("MouseX", "MouseY")
DEFAULT_CONTROLS = [
    {"name": "Move forward", "action": "forward", "keys": ["W", "Up"], "digital": True},
    {"name": "Move back", "action": "back", "keys": ["S", "Down"], "digital": True},
    {"name": "Move left", "action": "left", "keys": ["A"], "digital": True},
    {"name": "Move right", "action": "right", "keys": ["D"], "digital": True},
    {"name": "Jump", "action": "jump", "keys": ["SpaceBar"], "digital": True},
    {"name": "Turn the camera", "action": "look", "keys": ["MouseX", "MouseY", "Left", "Right"], "digital": False},
]
INPUT_OPS = {"key", "input", "ui_act", "pause", "session", "move"}
# The ops answered without a playtest of this plugin's own (every other one reads or plays the game)
NO_PLAY_OPS = {"ping", "pause", "session", "context", "record", "input", "codex_manifest", "codex_read"}
SLOW_OPS = {"capture", "record", "codex_manifest", "codex_read", "levels"}
UNKNOWN = {"levels", "move", "detectors", "timestep"}


class OpError(Exception):
    def __init__(self, status, error):
        super().__init__(error)
        self.status, self.error = status, error


def log(*a):
    print("[roblox]", *a, file=sys.stderr, flush=True)


# -- the agent in the playtest ---------------------------------------------------------------------------------

class Agent:
    """agent.luau in one datamodel of the playtest ("Client" or "Server"), installed on first use."""

    def __init__(self, mcp, datamodel, on_time=None):
        self.mcp, self.datamodel = mcp, datamodel
        self.role = datamodel.lower()
        with open(os.path.join(HERE, "agent.luau"), encoding="utf-8") as f:
            src = f.read()
        self.version = hashlib.sha1(src.encode()).hexdigest()[:12]
        self.source = src.replace("__AP_VERSION__", self.version).replace("__AP_ROLE__", self.role)
        self.on_time = on_time
        self._install_lock = threading.Lock()

    def install(self):
        with self._install_lock:
            out = self.mcp.luau(self.source, self.datamodel, timeout=30).strip()
            if out not in ("installed", "ready"):
                raise StudioError(f"agent install in {self.datamodel}: {out[:300]}")

    def call(self, op, args=None, timeout=None):
        payload = json.dumps(args or {}, separators=(",", ":"))
        level = "=="
        while f"]{level}]" in payload:
            level += "="
        code = (f'local AP=_G.__AP if not AP or AP.version~="{self.version}" then return "__AP_MISSING__" end '
                f'return AP.call("{op}", [{level}[{payload}]{level}])')
        out = self.mcp.luau(code, self.datamodel, timeout=timeout).strip()
        if out == "__AP_MISSING__":
            self.install()
            out = self.mcp.luau(code, self.datamodel, timeout=timeout).strip()
        env = json.loads(out)
        if "paged" in env:
            parts = []
            for i in range(1, int(env["count"]) + 1):
                parts.append(self.mcp.luau(f'return _G.__AP.page("{env["paged"]}", {i})', self.datamodel,
                                           timeout=timeout))
            env = json.loads("".join(parts))
        if env.get("t") is not None and self.on_time:
            self.on_time(float(env["t"]))
        if not env.get("ok"):
            raise OpError(env.get("status") or 400, env.get("error") or "error")
        return env.get("result")


# -- the plugin -------------------------------------------------------------------------------------------------

class RobloxPlugin:
    def __init__(self, url, studio_command=None, place_id=None, project=None, holds=None, media=True,
                 holder="omy-autopilot"):
        self.url = url
        self.studio_command = studio_command or default_command()
        self.place_id = place_id
        self.project = project
        self.media_enabled = media
        self.holds_spec = holds
        self.holder = holder
        self.mcp = None  # control lane
        self.media_mcp = None  # captures and the recording (slow: kept off the control lane)
        self.client = self.server = None
        self.ws = None
        self._send_lock = threading.Lock()
        self.closed = threading.Event()
        # order: inputs one by one in arrival order; reads after the inputs that came before them
        self._inputs = queue.Queue()
        self._seq_lock = threading.Lock()
        self._submitted = 0
        self._done = 0
        self._done_cv = threading.Condition()
        self._reads = concurrent.futures.ThreadPoolExecutor(8, thread_name_prefix="read")
        self._slow = concurrent.futures.ThreadPoolExecutor(4, thread_name_prefix="slow")
        # the session
        self.running = False
        self.started_play = False
        self._t_game = None
        self._t_wall = None
        self.held = {}  # roblox key or mouse axis -> {"until": monotonic, "value": v, "rate": bool, "seconds": s}
        self._held_lock = threading.Lock()
        self.controls = []
        self.inset = 58
        self.viewport = (1805, 1031)
        self.recording = None
        self.record_settings = None  # (fps, max_dim, quality) the brain asked to record at, until it asks to stop
        self._frame_n = itertools.count(1)
        self.codex = {}
        self.findings = {}
        self.holds = None

    # -- game time ---------------------------------------------------------------------------------------

    def _on_time(self, t):
        self._t_game, self._t_wall = t, time.monotonic()

    def t(self):
        if not self.running or self._t_game is None:
            return None
        return round(self._t_game + (time.monotonic() - self._t_wall), 2)

    # -- the connection ----------------------------------------------------------------------------------

    def start_studio(self):
        self.mcp = StudioMCP(self.studio_command, log=None).start()
        sid = self.mcp.studio(self.place_id)
        name = next((s["name"] for s in self.mcp.studios() if s["id"] == sid), "")
        m = re.match(r"^(.*) \(placeId: (\d+)\)$", name or "")
        if m:
            self.project = self.project or m.group(1)
            self.place_id = self.place_id or m.group(2)
        self.project = self.project or "Roblox place"
        self.client = Agent(self.mcp, "Client", self._on_time)
        self.server = Agent(self.mcp, "Server")
        st = self.mcp.state()
        if self.holds_spec:
            self.holds = Holds(self.holds_spec, self.holder, self.place_id, sid)
        # (a playtest going on is this plugin's only when its own hold says it started it: one a person or another
        # task started is never played, nor stopped, from here)
        self.running = self.started_play = st.get("mode") == "Play" and self._owns_play()
        log(f"Studio {sid}: {name} ({st.get('mode')})")

    def hello(self):
        return {"type": "hello", "protocol": 1, "engine": "roblox", "project": self.project,
                "project_id": f"roblox-place-{self.place_id}" if self.place_id else None, "host": "editor",
                "plugin_version": PLUGIN_VERSION, "engine_version": "Roblox Studio (Studio MCP)",
                "features": ["media"] if self.media_enabled else [], "share": False}

    def send(self, frame):
        data = json.dumps(frame, separators=(",", ":"))
        with self._send_lock:
            if self.ws is None:
                return
            try:
                self.ws.send(data)
            except Exception as e:
                log(f"send failed: {e}")

    def run(self, once=False):
        if self.mcp is None:
            self.start_studio()
        threading.Thread(target=self._input_worker, daemon=True, name="inputs").start()
        threading.Thread(target=self._lease_worker, daemon=True, name="leases").start()
        threading.Thread(target=self._heartbeat_worker, daemon=True, name="heartbeat").start()
        backoff = 1.0
        while not self.closed.is_set():
            try:
                with connect(self.url, max_size=None, open_timeout=10) as ws:
                    self.ws = ws
                    ws.send(json.dumps(self.hello()))
                    welcome = json.loads(ws.recv(timeout=15))
                    if welcome.get("type") != "welcome":
                        raise ConnectionError(f"brain: {welcome}")
                    log(f"connected to the brain ({welcome.get('role')}, session {welcome.get('session')})")
                    backoff = 1.0
                    for raw in ws:
                        self._on_frame(json.loads(raw))
            except (OSError, ConnectionError, TimeoutError) as e:
                log(f"brain connection: {e}")
            except Exception as e:  # (websockets' ConnectionClosed and the like)
                log(f"brain connection ended: {type(e).__name__}: {e}")
            finally:
                self.ws = None
            if once:
                break
            time.sleep(backoff)
            backoff = min(backoff * 2, 15.0)
        self.shutdown()

    def shutdown(self):
        self.closed.set()
        self._release_all_now()
        self._stop_recording()
        for m in (self.mcp, self.media_mcp):
            if m:
                m.close()

    def _on_frame(self, frame):
        kind = frame.get("type")
        if kind == "request":
            self._dispatch(frame)
        elif kind == "status":
            log("status:", frame.get("text"))
        elif kind == "result":
            r = frame.get("result") or {}
            log("result:", json.dumps({k: r.get(k) for k in ("verdict", "reached", "reason", "notes", "goal",
                                                                "claim") if k in r})[:2000])
        elif kind == "activity":
            text = frame.get("text") or ""
            if text:
                log("activity:", text[:300])
        elif kind == "error":
            log("brain error:", frame.get("message"))

    # -- dispatch ----------------------------------------------------------------------------------------

    def _dispatch(self, frame):
        op = frame.get("op")
        if op in INPUT_OPS:
            with self._seq_lock:
                self._submitted += 1
                self._inputs.put((self._submitted, frame))
            return
        with self._seq_lock:
            need = self._submitted
        pool = self._slow if op in SLOW_OPS else self._reads
        pool.submit(self._run_read, frame, need)

    def _input_worker(self):
        while True:
            n, frame = self._inputs.get()
            try:
                self._answer(frame)
            finally:
                with self._done_cv:
                    self._done = n
                    self._done_cv.notify_all()

    def _run_read(self, frame, need):
        with self._done_cv:
            self._done_cv.wait_for(lambda: self._done >= need, timeout=30)
        self._answer(frame)

    def _answer(self, frame):
        if "_expire" in frame:  # (the lease worker's: no answer)
            self._expire(frame["_expire"])
            return
        rid, op, args = frame.get("id"), frame.get("op"), frame.get("args") or {}
        try:
            fn = getattr(self, "op_" + str(op), None)
            if fn is None or op in UNKNOWN:
                raise OpError(400, f"unknown op {op}")
            if op not in NO_PLAY_OPS:
                self._need_play()  # (reads and inputs only in a playtest this plugin started: never someone else's)
            result = fn(args)
            out = {"type": "response", "id": rid, "ok": True, "result": result if result is not None else {}}
        except OpError as e:
            out = {"type": "response", "id": rid, "ok": False, "status": e.status, "error": e.error}
        except StudioError as e:
            out = {"type": "response", "id": rid, "ok": False, "status": 409 if "not available" in str(e) else 500,
                   "error": str(e)}
        except Exception as e:
            log(f"{op} failed:\n{traceback.format_exc()}")
            out = {"type": "response", "id": rid, "ok": False, "status": 500, "error": f"{type(e).__name__}: {e}"}
        out["t"] = self.t()
        self.send(out)

    def _need_play(self):
        if not self.running:
            raise OpError(409, "No game is running (start one with session start)")

    # -- the session -------------------------------------------------------------------------------------

    def op_ping(self, a):
        return {"game_time": self.t(), "wall_time": time.time()}

    def op_pause(self, a):
        # Roblox Studio can't pause a playtest from outside: the world keeps going (roblox.md says so)
        return {"paused": bool(a.get("paused")), "note": "Roblox playtests can't pause; the game keeps running"}

    def op_session(self, a):
        action = a.get("action") or "status"
        if action == "status":
            st = self.mcp.state()
            self.running = st.get("mode") == "Play" and self.started_play
            return {"running": self.running, "scene": self.project}
        if a.get("map"):
            raise OpError(400, f"No scene {a['map']} (a Roblox place has one world: play it without a map)")
        if action == "start":
            return self._start_play()
        if action == "stop":
            return self._stop_play()
        if action == "restart":
            self._stop_play()
            return self._start_play()
        raise OpError(400, f"unknown session action {action}")

    def _start_play(self):
        st = self.mcp.state()
        if st.get("mode") == "Play":
            if not (self.started_play or self._owns_play()):
                raise OpError(409, "Studio is already in a playtest this plugin didn't start (a person's or another "
                                   "task's): it is left alone. Run again once it has stopped and been released.")
            self.running = self.started_play = True
            self._wait_character(30)
            return {"running": True, "scene": self.project}
        if st.get("mode") != "Edit":
            raise OpError(409, f"Studio is in {st.get('mode')} mode, not Edit")
        if self.holds:
            try:
                self.holds.claim(f"Autopilot playtest ({self.holder}): Play only, no edits, no save.")
            except HoldsError as e:
                raise OpError(409, str(e))
        self.mcp.call("start_stop_play", {"is_start": True}, timeout=60)
        self.started_play = True
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if "Client" in (self.mcp.state().get("datamodels") or []):
                break
            time.sleep(0.3)
        self.running = True
        self._t_game, self._t_wall = 0.0, time.monotonic()
        self._wait_character(40)
        try:
            self.server.install()
        except StudioError as e:
            log(f"server log tap: {e}")
        self._read_screen_facts()
        if self.record_settings and self.media_enabled:
            try:
                self._begin_recording(*self.record_settings)
            except Exception as e:
                log(f"record after the restart: {e}")
        self.send({"type": "events", "events": [{"kind": "session", "t": self.t(), "running": True}], "t": self.t()})
        return {"running": True, "scene": self.project}

    def _wait_character(self, seconds):
        deadline = time.monotonic() + seconds
        last = None
        while time.monotonic() < deadline:
            try:
                ctx = self.client.call("context")
                if ctx.get("character"):
                    return ctx
            except (StudioError, OpError, ValueError) as e:
                last = e
            time.sleep(0.4)
        log(f"no character after {seconds}s ({last})")
        return None

    def _read_screen_facts(self):
        try:
            facts = self.client.call("rc", {"verb": "property", "body": {"objectPath": "Workspace/Camera",
                                                                       "propertyName": "ViewportSize"}})
            vs = facts.get("ViewportSize") or {}
            self.viewport = (int(vs.get("x", 1805)), int(vs.get("y", 1031)))
        except Exception:
            pass
        try:
            self.inset = int(float(self.mcp.luau('return tostring(game:GetService("GuiService"):GetGuiInset().Y)',
                                                 "Client")))
        except Exception:
            pass

    def _owns_play(self):
        if not self.holds:
            return False  # (no holds to tell whose it is)
        try:
            return self.holds.mine()
        except Exception as e:
            log(f"holds: {e}")
            return False

    def _stop_play(self):
        self._release_all_now()
        self._stop_recording()
        st = self.mcp.state()
        if st.get("mode") != "Edit" and not self.started_play:
            self.running = False  # (not this plugin's playtest: never stopped from here)
            return {"running": False, "scene": self.project}
        if st.get("mode") != "Edit":
            self.mcp.call("start_stop_play", {"is_start": False}, timeout=30)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and self.mcp.state().get("mode") != "Edit":
                time.sleep(0.3)
        was = self.running
        self.running = False
        self.started_play = False
        if self.holds:
            try:
                self.holds.release()
            except HoldsError as e:
                log(str(e))
        if was:
            self.send({"type": "events", "events": [{"kind": "session", "t": None, "running": False}], "t": None})
        return {"running": False}

    # -- reads -------------------------------------------------------------------------------------------

    def op_context(self, a):
        base = {"world": "", "scene": self.project, "character": "", "controller": "", "input_subsystem": "",
                "camera_manager": "", "camera": "", "dimension": "3d", "game_name": self.project, "host": "editor",
                "engine": "roblox", "session_running": False, "paused": False, "game_time": None, "project_dir": "",
                "controls": DEFAULT_CONTROLS}
        if not self.running:
            return base  # (no playtest of this plugin's own: one a person or another task started isn't read)
        ctx = self.client.call("context")
        game_controls = []
        for c in ctx.pop("controls", None) or []:
            keys = [ROBLOX_TO_FKEY.get(k, k) for k in c.get("keys") or []]
            game_controls.append({"name": c["name"], "action": c["action"], "keys": keys, "digital": True})
        self.controls = game_controls
        base.update(ctx)
        base.update({"scene": self.project, "game_name": self.project, "session_running": True, "engine": "roblox",
                     "host": "editor", "project_dir": "",
                     "controls": DEFAULT_CONTROLS + game_controls,
                     "net": {"mode": "client", "note": "the agent plays a client; the server is the same Studio"},
                     "input": {"notes": [
                         "Roblox: the playtest can't be paused; the world keeps running while you think.",
                         "Mouse look turns the camera around the character (MouseX/MouseY); "
                         "WASD move relative to the camera.",
                     ]}})
        return base

    def op_pose(self, a):
        self._need_play()
        return self.client.call("pose")

    def op_movement(self, a):
        self._need_play()
        return self.client.call("movement")

    def op_find(self, a):
        self._need_play()
        return self.client.call("find", a, timeout=60)

    def op_transforms(self, a):
        self._need_play()
        if not a.get("objects"):
            return {"objects": {}}
        return self.client.call("transforms", a)

    def op_view(self, a):
        if not self.running:
            raise OpError(400, "No camera")
        return self.client.call("view", a)

    def op_screen(self, a):
        if not self.running:
            return {"texts": {}}
        return self.client.call("screen", a)

    def op_ui(self, a):
        if not self.running:
            return {"elements": []}
        return self.client.call("ui", a)

    def op_rc(self, a):
        if a.get("verb") == "probe":
            return {"ok": True}
        self._need_play()
        body = a.get("body")
        if a.get("verb") == "call" and isinstance(body, dict) and isinstance(body.get("parameters"), dict):
            # (Luau tables lose a JSON object's order: the arguments go in the order the kit gave them)
            a = {**a, "body": {**body, "parameters": list(body["parameters"].values())}}
        return self.client.call("rc", a)

    def op_watch(self, a):
        """What changed on these objects over `seconds`: moved or turned, from two snapshots (not every frame)."""
        objects = a.get("objects") or []
        seconds = float(a.get("seconds") or 0.6)
        if not objects or not self.running:
            time.sleep(min(seconds, 5))
            return {"changes": []}
        before = self.client.call("transforms", {"objects": objects})["objects"]
        time.sleep(seconds)
        after = self.client.call("transforms", {"objects": objects})["objects"]
        changes = []
        moved_min, turned_min = float(a.get("moved") or 2), float(a.get("turned") or 1)
        for o in objects:
            b, c = before.get(o), after.get(o)
            if not b or not c:
                continue
            d = sum((b["location"][k] - c["location"][k]) ** 2 for k in "XYZ") ** 0.5
            if d >= moved_min:
                changes.append({"t": self.t(), "object": o, "what": "moved"})
            if abs(((b["rotation"]["Yaw"] - c["rotation"]["Yaw"]) + 180) % 360 - 180) >= turned_min:
                changes.append({"t": self.t(), "object": o, "what": "turned"})
        return {"changes": changes}

    # -- inputs ------------------------------------------------------------------------------------------

    def _roblox_key(self, key):
        if key in MOUSE_AXES or key in MOUSE_BUTTONS or key in ("MouseScrollUp", "MouseScrollDown"):
            return key
        if key in KEYS:
            return KEYS[key]
        for c in self.controls:  # an action the game binds (ContextActionService), by its name
            if c["action"] == key or c["name"] == key:
                for k in c["keys"]:
                    if k in KEYS or k in MOUSE_BUTTONS:
                        return KEYS.get(k, k)
        raise OpError(400, f"Unknown key {key}")

    def _keyboard(self, actions):
        self.mcp.call("user_keyboard_input", {"datamodel_type": "Client", "actions": actions})

    def _mouse(self, actions):
        self.mcp.call("user_mouse_input", {"datamodel_type": "Client", "actions": actions})

    @staticmethod
    def _click(button, at):
        """A click as a hand makes it: on the spot, a moment of hover, then down and up PRESS_MS apart (games that
        tell a click from a drag see both, in different frames)."""
        return [{"action": "moveTo", **at}, {"action": "wait", "wait_time_ms": 50},
                {"action": "mouseButtonDown", "mouse_button": button}, {"action": "wait", "wait_time_ms": PRESS_MS},
                {"action": "mouseButtonUp", "mouse_button": button}]

    def _center(self):
        return {"x": self.viewport[0] // 2, "y": max(self.viewport[1] // 2 - self.inset, 0)}

    def _down(self, rk, value=None):
        if rk in MOUSE_BUTTONS:
            self._mouse([{"action": "moveTo", **self._center()},
                         {"action": "mouseButtonDown", "mouse_button": MOUSE_BUTTONS[rk]}])
        elif rk not in MOUSE_AXES:
            self._keyboard([{"action": "keyDown", "key_code": rk}])

    def _up(self, rk):
        if rk in MOUSE_BUTTONS:
            self._mouse([{"action": "mouseButtonUp", "mouse_button": MOUSE_BUTTONS[rk]}])
        elif rk not in MOUSE_AXES:
            self._keyboard([{"action": "keyUp", "key_code": rk}])

    def _turn(self, axis, counts):
        deg = float(counts) * DEG_PER_COUNT
        self.client.call("turn", {"yaw": deg} if axis == "MouseX" else {"pitch": deg})

    def op_key(self, a):
        mode = a.get("mode") or "press"
        if mode == "release_all":
            self._release_all_now()
            return {}
        key = a.get("key") or ""
        rk = self._roblox_key(key)
        self._need_play()
        value = a.get("value")
        if mode == "press":
            if rk in MOUSE_AXES:
                self._turn(rk, value if value is not None else 1.0)
            elif rk in ("MouseScrollUp", "MouseScrollDown"):
                self._mouse([{"action": "moveTo", **self._center()},
                             {"action": "scrollUp" if rk == "MouseScrollUp" else "scrollDown"}])
            elif rk in MOUSE_BUTTONS:
                self._mouse(self._click(MOUSE_BUTTONS[rk], self._center()))
            else:
                with self._held_lock:
                    was_held = self.held.pop(rk, None)
                if was_held:
                    self._keyboard([{"action": "keyUp", "key_code": rk}])
                self._keyboard([{"action": "keyDown", "key_code": rk},
                                {"action": "wait", "wait_time_ms": PRESS_MS},
                                {"action": "keyUp", "key_code": rk}])
            return {}
        if mode == "hold":
            lease = max(float(a.get("lease") or MIN_LEASE), MIN_LEASE)
            seconds = a.get("seconds")
            until = time.monotonic() + (float(seconds) if seconds is not None else lease)
            with self._held_lock:
                cur = self.held.get(rk)
                self.held[rk] = {"until": until, "value": value, "rate": bool(a.get("rate")),
                                 "timed": seconds is not None, "last": time.monotonic()}
            if cur is None:
                self._down(rk, value)
            return {}
        if mode == "release":
            with self._held_lock:
                cur = self.held.pop(rk, None)
            if cur is not None:
                self._up(rk)
            return {}
        raise OpError(400, f"unknown key mode {mode}")

    def _release_all_now(self):
        with self._held_lock:
            keys = list(self.held)
            self.held.clear()
        if not keys or self.mcp is None or not self.running:
            return
        kb = [{"action": "keyUp", "key_code": k} for k in keys if k not in MOUSE_AXES and k not in MOUSE_BUTTONS]
        try:
            if kb:
                self._keyboard(kb)
            for k in keys:
                if k in MOUSE_BUTTONS:
                    self._up(k)
        except StudioError as e:
            log(f"release_all: {e}")

    def _lease_worker(self):
        """Let go of keys whose lease ran out (through the input lane, in order), and turn the view for held mouse
        axes."""
        while not self.closed.is_set():
            time.sleep(0.05)
            now = time.monotonic()
            expired, turning = [], []
            with self._held_lock:
                for k, h in list(self.held.items()):
                    if now > h["until"]:
                        expired.append(k)
                    elif k in MOUSE_AXES and h.get("value"):
                        dt = now - h["last"]
                        h["last"] = now
                        counts = float(h["value"]) * (dt if h["rate"] else dt * 60.0)
                        turning.append((k, counts))
            for k in expired:
                self._dispatch({"op": "key", "id": None, "args": {"key": "__expire__", "mode": "release"},
                                "_expire": k})
            for k, counts in turning:
                if self.running:
                    try:
                        self._turn(k, counts)
                    except Exception:
                        pass

    def op_input(self, a):
        # Roblox has no input actions apart from keys: the kit sends `input` only for Unreal's /Game/... paths
        raise OpError(400, f"No input action named {a.get('action') or ''}")

    def _expire(self, k):
        with self._held_lock:
            h = self.held.get(k)
            if h is None or time.monotonic() <= h["until"]:
                return  # renewed (or let go) meanwhile
            self.held.pop(k, None)
        try:
            self._up(k)
        except Exception as e:
            log(f"lease release {k}: {e}")

    def op_ui_act(self, a):
        self._need_play()
        action = a.get("action") or "click"
        target = self.client.call("ui_target", {"id": a.get("id"), "to": a.get("to")})
        x, y = target["x"], target["y"]
        # Studio finds the element itself by its instance path when the path can be said that way (GUIs move: a
        # position read a moment before can be stale); else the position just read
        at = {"instance_path": target["mcp_path"]} if target.get("mcp_path") else {"x": x, "y": y}
        if action in ("click", "double_click", "right_click"):
            button = "right" if action == "right_click" else "left"
            steps = self._click(button, at)
            if action == "double_click":
                steps += [{"action": "mouseButtonDown", "mouse_button": button},
                          {"action": "wait", "wait_time_ms": PRESS_MS},
                          {"action": "mouseButtonUp", "mouse_button": button}]
            self._mouse(steps)
            return {"via": "mouse"}
        if action == "set":
            if target.get("kind") != "text_input":
                raise OpError(400, "set needs a text input")
            self._mouse(self._click("left", at))
            text = str(a.get("text") if a.get("text") is not None else a.get("value") or "")
            self._keyboard([{"action": "textInput", "text_inputs": text}])
            return {"via": "mouse"}
        if action == "drag":
            to = target.get("to")
            if not to:
                raise OpError(400, f"no element {a.get('to')} on screen")
            self._mouse([{"action": "moveTo", **at}, {"action": "mouseButtonDown", "mouse_button": "left"},
                         {"action": "moveTo", "x": to["x"], "y": to["y"]},
                         {"action": "mouseButtonUp", "mouse_button": "left"}])
            return {"via": "mouse"}
        raise OpError(400, f"unknown action {action}")

    # -- pictures ----------------------------------------------------------------------------------------

    def _media(self):
        if self.media_mcp is None:
            self.media_mcp = StudioMCP(self.studio_command).start()
            self.media_mcp.studio_id = self.mcp.studio()
        return self.media_mcp

    def _picture(self, max_dim, quality):
        mime, data = self._media().screenshot()
        raw = base64.b64decode(data)
        return resize_jpeg(raw, max_dim, quality)

    def op_capture(self, a):
        self._need_play()
        try:
            jpeg, w, h = self._picture(int(a.get("max_dim") or 1920), int(a.get("quality") or 90))
        except StudioError as e:
            raise OpError(409, f"no picture: {e}")
        return {"width": w, "height": h, "t": self.t(), "jpeg": base64.b64encode(jpeg).decode()}

    def op_record(self, a):
        if (a.get("action") or "start") == "stop":
            self.record_settings = None
            n = self._stop_recording()
            return {"recording": False, "frames": n}
        self._need_play()
        if not self.media_enabled:
            raise OpError(409, "no picture: recording is off (--no-media)")
        fps = min(max(float(a.get("fps") or 5), 0.1), 60)
        max_dim, quality = int(a.get("max_dim") or 960), int(a.get("quality") or 75)
        # (kept: Stop ends the recording with the playtest, and a restart's new playtest records on with these)
        self.record_settings = (fps, max_dim, quality)
        return self._begin_recording(fps, max_dim, quality)

    def _begin_recording(self, fps, max_dim, quality):
        self._stop_recording()
        jpeg, w, h = self._picture(max_dim, quality)  # (fails here, not in the loop, when there's no picture)
        rec = {"stop": threading.Event(), "frames": 0}
        self.recording = rec
        threading.Thread(target=self._record_loop, args=(rec, fps, max_dim, quality), daemon=True,
                         name="record").start()
        return {"recording": True, "width": w, "height": h}

    def _record_loop(self, rec, fps, max_dim, quality):
        period = 1.0 / fps
        while not rec["stop"].is_set() and self.running and not self.closed.is_set():
            t0 = time.monotonic()
            try:
                jpeg, w, h = self._picture(max_dim, quality)
                self.send({"type": "frame", "n": next(self._frame_n), "t": self.t(), "wall": time.time(),
                           "width": w, "height": h, "jpeg": base64.b64encode(jpeg).decode()})
                rec["frames"] += 1
            except Exception as e:
                log(f"record: {e}")
                time.sleep(1.0)
            rec["stop"].wait(max(0.0, period - (time.monotonic() - t0)))

    def _stop_recording(self):
        rec, self.recording = self.recording, None
        if rec:
            rec["stop"].set()
            return rec["frames"]
        return 0

    # -- the codex (the game's scripts, live from Studio) -------------------------------------------------

    def op_codex_manifest(self, a):
        t0 = time.monotonic()
        files = {}
        agent = self.server if self.running else Agent(self.mcp, "Edit")
        scripts = agent.call("scripts", {}, timeout=120)["scripts"]
        for s in scripts:
            ext = {"Script": ".server.luau", "LocalScript": ".client.luau"}.get(s["class"], ".luau")
            files[s["path"] + ext] = s["source"]
        try:
            summary = (self.client if self.running else agent).call("summary", {}, timeout=60)["text"]
            files["_autopilot/summary.txt"] = summary
        except Exception as e:
            log(f"summary: {e}")
        self.codex = files
        out = [{"path": p, "size": len(t.encode()), "hash": hashlib.md5(t.encode()).hexdigest()}
               for p, t in sorted(files.items())]
        return {"project": self.project, "files": out, "truncated": False,
                "export": {"ran": True, "seconds": round(time.monotonic() - t0, 2), "exit_code": 0}}

    def op_codex_read(self, a):
        budget = min(int(a.get("max_bytes") or 1 << 20), 4 << 20)
        files, missing, too_big = {}, [], []
        used = 0
        for p in a.get("paths") or []:
            if p not in self.codex:
                missing.append(p)
                continue
            text = self.codex[p]
            n = len(text.encode())
            if n > budget:
                too_big.append(p)
                continue
            if used + n > budget:
                break
            files[p] = text
            used += n
        return {"files": files, "missing": missing, "too_big": too_big}

    # -- heartbeats, logs and findings --------------------------------------------------------------------

    def _heartbeat_worker(self):
        while not self.closed.is_set():
            time.sleep(1.0)
            if self.ws is None:
                continue
            events = [{"kind": "heartbeat", "t": self.t(), "fps": None, "paused": False}]
            if self.running:
                for agent in (self.client, self.server):
                    try:
                        got = agent.call("logs", timeout=10) or {}
                    except Exception:
                        continue
                    for entry in got.get("logs") or []:
                        events.extend(self._log_events(entry))
            self.send({"type": "events", "events": events, "t": self.t()})

    def _log_events(self, entry):
        t = self.t()
        msg = entry.get("message") or ""
        out = [{"kind": "log", "t": t, "verbosity": entry.get("verbosity"), "category": f"roblox.{entry.get('side')}",
                "message": msg[:2000]}]
        if entry.get("verbosity") == "Error":
            sig = "log_error:" + re.sub(r"\d+", "#", msg.splitlines()[0] if msg else "")[:200]
            n = self.findings[sig] = self.findings.get(sig, 0) + 1
            if n in (1, 10, 100, 1000):
                out.append({"kind": "finding", "t": t, "finding": {
                    "detector": "log_error", "severity": "error", "summary": msg.splitlines()[0][:300] if msg else "",
                    "signature": sig, "details": msg[:1500], "t": t, "count": n, "source": "engine"}})
        return out


def resize_jpeg(data, max_dim, quality):
    """(jpeg, width, height): the picture with its longer side at most max_dim (PyAV: decode, scale, encode)."""
    import av

    dec = av.CodecContext.create("mjpeg", "r")
    frames = dec.decode(av.Packet(data))
    if not frames:
        raise StudioError("screen_capture: not a JPEG")
    fr = frames[0]
    w, h = fr.width, fr.height
    scale = min(1.0, max_dim / max(w, h))
    if scale >= 1.0 and quality >= 85:
        return data, w, h
    nw, nh = max(2, int(w * scale) // 2 * 2), max(2, int(h * scale) // 2 * 2)
    small = fr.reformat(width=nw, height=nh, format="yuvj420p")
    enc = av.CodecContext.create("mjpeg", "w")
    enc.width, enc.height, enc.pix_fmt, enc.time_base = nw, nh, "yuvj420p", Fraction(1, 25)
    q = max(2, min(31, round(2 + (100 - quality) * 0.3)))
    enc.options = {"qmin": str(q), "qmax": str(q)}
    small.pts = 0
    out = b"".join(bytes(p) for p in enc.encode(small) + enc.encode(None))
    return out, nw, nh

