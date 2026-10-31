"""The Roblox engine adapter and the Roblox plugin's own logic (no Studio: a fake StudioMCP)."""

import threading
import time

import pytest

from autopilot import engines
from autopilot.engines.roblox import Roblox
from autopilot.kit.geom import Vec
from autopilot.roblox import plugin as rp


def test_registered():
    assert isinstance(engines.get("roblox"), Roblox)
    assert isinstance(engines.for_hello({"engine": "roblox"}), Roblox)


def test_frame():
    eng = Roblox()
    # Roblox: X right, Y up, -Z forward, studs; kit: X forward, Y right, Z up, cm (1 stud = 28 cm)
    assert eng.to_kit({"x": 1, "y": 2, "z": -3}) == Vec(84, 28, 56)
    back = eng.to_native(Vec(84, 28, 56))
    assert back == pytest.approx({"x": 1, "y": 2, "z": -3})
    # Orientation.y turns left: a kit yaw of 90 (right) is y = -90
    assert eng.rotation_to_kit({"x": 10, "y": -90, "z": 0}) == (90, 10)
    assert eng.rotation_to_native(90, 10) == {"x": 10, "y": -90, "z": 0.0}
    assert eng.rotation_to_kit({"rx": 0, "ry": 45, "rz": 0})[0] == -45


def test_names_and_codex():
    eng = Roblox()
    assert eng.name_of("Workspace/Arena/Coins/Coin~3") == "Coin~3"
    script = "ServerScriptService/Server/Main.server.luau"
    assert eng.codex_path(script) == script
    assert eng.codex_path("_autopilot/summary.txt") == "summary.txt"
    assert eng.codex_path("ReplicatedStorage/Packages/React.luau") is None
    assert eng.codex_path("../etc/passwd.luau") is None
    assert eng.codex_path("src/Client/Main.client.luau") == "src/Client/Main.client.luau"
    assert eng.codex_path("assets/thing.png") is None


def test_kit_doc_has_roblox_section():
    doc = Roblox().kit_doc("Units: centimetres (Unreal units), degrees, seconds of game time.\n")
    assert "## The engine: Roblox" in doc and "28 cm" in doc


class FakeMCP:
    """Records the calls a plugin makes to Studio."""

    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()

    def call(self, tool, args=None, timeout=None, studio=True):
        with self.lock:
            self.calls.append((tool, args))
        return ""

    def keys(self):
        out = []
        for tool, args in self.calls:
            if tool == "user_keyboard_input":
                out += [(a["action"], a.get("key_code")) for a in args["actions"] if a["action"] != "wait"]
        return out


@pytest.fixture
def plugin():
    p = rp.RobloxPlugin("ws://unused", ["true"])
    p.mcp = FakeMCP()
    p.running = True
    p.send = lambda frame: None
    threading.Thread(target=p._input_worker, daemon=True).start()
    threading.Thread(target=p._lease_worker, daemon=True).start()
    yield p
    p.closed.set()


def test_key_names(plugin):
    assert plugin._roblox_key("SpaceBar") == "Space"
    assert plugin._roblox_key("Enter") == "Return"
    assert plugin._roblox_key("W") == "W"
    assert plugin._roblox_key("MouseX") == "MouseX"
    plugin.controls = [{"name": "Sprint", "action": "Sprint", "keys": ["LeftShift"], "digital": True}]
    assert plugin._roblox_key("Sprint") == "LeftShift"
    with pytest.raises(rp.OpError, match="Unknown key"):
        plugin._roblox_key("Gamepad_FaceButton_Bottom")


def test_hold_is_leased_here(plugin):
    """Renewals cost no call to Studio; the key goes up when they stop."""
    for _ in range(8):
        plugin.op_key({"key": "W", "mode": "hold", "lease": 0.6})
        time.sleep(0.05)
    assert plugin.mcp.keys() == [("keyDown", "W")]
    time.sleep(0.9)
    assert plugin.mcp.keys() == [("keyDown", "W"), ("keyUp", "W")]
    assert plugin.held == {}


def test_press_and_release_all(plugin):
    plugin.op_key({"key": "E", "mode": "press"})
    plugin.op_key({"key": "W", "mode": "hold"})
    plugin.op_key({"key": "LeftShift", "mode": "hold"})
    plugin.op_key({"mode": "release_all"})
    keys = plugin.mcp.keys()
    assert keys[:2] == [("keyDown", "E"), ("keyUp", "E")]
    assert set(keys[-2:]) == {("keyUp", "W"), ("keyUp", "LeftShift")}
    assert plugin.held == {}


def test_reads_wait_for_inputs_before_them(plugin):
    """A read that comes after an input is answered after that input was applied."""
    order = []

    def slow_key(a):
        time.sleep(0.2)
        order.append("key")
        return {}

    plugin.op_key = slow_key
    plugin.op_pose = lambda a: order.append("pose") or {}
    plugin._dispatch({"type": "request", "id": 1, "op": "key", "args": {}})
    plugin._dispatch({"type": "request", "id": 2, "op": "pose", "args": {}})
    time.sleep(0.5)
    assert order == ["key", "pose"]


def test_unknown_and_input_ops(plugin):
    sent = []
    plugin.send = sent.append
    plugin._answer({"type": "request", "id": 7, "op": "levels", "args": {}})
    plugin._answer({"type": "request", "id": 8, "op": "input", "args": {"action": ""}})
    assert sent[0]["error"].startswith("unknown op")
    assert not sent[1]["ok"] and not sent[1]["error"].startswith("unknown op")


def test_resize_jpeg():
    av = pytest.importorskip("av")
    from fractions import Fraction

    frame = av.VideoFrame(640, 360, "yuvj420p")
    enc = av.CodecContext.create("mjpeg", "w")
    enc.width, enc.height, enc.pix_fmt, enc.time_base = 640, 360, "yuvj420p", Fraction(1, 25)
    frame.pts = 0
    jpeg = b"".join(bytes(p) for p in enc.encode(frame) + enc.encode(None))
    small, w, h = rp.resize_jpeg(jpeg, 320, 75)
    assert (w, h) == (320, 180) and small[:2] == b"\xff\xd8"
    same, w, h = rp.resize_jpeg(jpeg, 1920, 90)
    assert same is jpeg and (w, h) == (640, 360)


def test_recording_goes_on_after_a_restart(plugin, monkeypatch):
    """A restart's Stop ends the recording with the playtest; the new playtest records on as asked."""
    started = []
    monkeypatch.setattr(plugin, "_need_play", lambda: None)
    monkeypatch.setattr(plugin, "_begin_recording", lambda *s: started.append(s) or {"recording": True})
    plugin.op_record({"action": "start", "fps": 4, "max_dim": 640, "quality": 70})
    assert plugin.record_settings == (4.0, 640, 70)
    plugin.op_record({"action": "stop"})
    assert plugin.record_settings is None and started == [(4.0, 640, 70)]


class PlayStudio(FakeMCP):
    """A Studio already in a playtest."""

    def __init__(self, mode="Play"):
        super().__init__()
        self.mode = mode

    def state(self):
        return {"mode": self.mode, "datamodels": ["Edit", "Server", "Client"]}

    def call(self, tool, args=None, timeout=None, studio=True):
        if tool == "start_stop_play":
            self.mode = "Play" if args.get("is_start") else "Edit"
        return super().call(tool, args, timeout, studio)


class FakeHolds:
    def __init__(self, mine, others=()):
        self._mine = mine
        self.others = list(others)
        self.released = 0

    def mine(self):
        return self._mine

    def blocking(self):
        return self.others

    def release(self):
        self.released += 1


def test_someone_elses_playtest_is_left_alone(plugin):
    """A playtest this plugin didn't start (its own hold doesn't say so) is neither played nor stopped."""
    plugin.mcp = PlayStudio()
    plugin.running = False
    plugin.holds = FakeHolds(mine=False)
    with pytest.raises(rp.OpError, match="no task holds"):
        plugin.op_session({"action": "start"})
    plugin.holds = FakeHolds(mine=False, others=[{"holderThreadId": "boss-task", "phase": "testing"}])
    with pytest.raises(rp.OpError, match="held by another task .boss-task"):
        plugin.op_session({"action": "start"})
    assert plugin.op_session({"action": "status"})["running"] is False
    plugin.op_session({"action": "stop"})
    assert not any(tool == "start_stop_play" for tool, _ in plugin.mcp.calls)


def test_own_playtest_is_taken_back_and_stopped(plugin, monkeypatch):
    """A plugin restarted mid-run takes back the playtest its own hold says it started, and stops it after."""
    plugin.mcp = PlayStudio()
    plugin.running = plugin.started_play = False
    plugin.holds = FakeHolds(mine=True)
    monkeypatch.setattr(plugin, "_wait_character", lambda s: None)
    assert plugin.op_session({"action": "start"})["running"] is True
    plugin.op_session({"action": "stop"})
    assert ("start_stop_play", {"is_start": False}) in plugin.mcp.calls and plugin.holds.released == 1


def test_no_reads_or_inputs_without_own_playtest(plugin):
    sent = []
    plugin.send = sent.append
    plugin.running = False
    for i, op in enumerate(["screen", "key", "ui_act", "rc", "pose"]):
        plugin._answer({"type": "request", "id": i, "op": op, "args": {"key": "W", "mode": "press"}})
    assert all(not r["ok"] and r["status"] == 409 for r in sent)
    assert plugin.mcp.calls == []


def test_a_roblox_run_that_cannot_start_fails(monkeypatch):
    """prepare() raises for Roblox when the playtest can't start (Studio held): no run whose every probe fails."""
    from autopilot import cli

    class Link:
        hello = {"engine": "roblox", "project": "Place"}

        def status(self, text):
            pass

    def refuse(*a, **k):
        raise RuntimeError("409: Studio is in a playtest held by another task")

    monkeypatch.setattr(cli, "remember_project", lambda *a, **k: None)
    monkeypatch.setattr(cli, "find_project", lambda *a, **k: None)
    monkeypatch.setattr(cli, "choose_level", lambda *a, **k: None)
    monkeypatch.setattr(cli, "start_level", refuse)
    with pytest.raises(RuntimeError, match="couldn't start: 409: Studio is in a playtest held"):
        cli.prepare(Link())


class FailingStart(PlayStudio):
    """A Studio whose Play request fails (Play started anyway, or not)."""

    def __init__(self, starts):
        super().__init__(mode="Edit")
        self.starts = starts

    def call(self, tool, args=None, timeout=None, studio=True):
        if tool == "start_stop_play" and args.get("is_start"):
            self.mode = "Play" if self.starts else "Edit"
            raise rp.StudioError("start_stop_play timed out")
        return super().call(tool, args, timeout, studio)


@pytest.mark.parametrize("starts", [False, True])
def test_a_failed_start_leaves_no_claim_or_playtest(plugin, starts):
    """Play requested under the plugin's claim, and the request fails: the claim is released, Play stopped if on."""
    plugin.mcp = FailingStart(starts)
    plugin.running = plugin.started_play = False
    plugin.holds = FakeHolds(mine=False)
    plugin.holds.claim = lambda reason: None
    with pytest.raises(rp.StudioError):
        plugin._start_play()
    assert plugin.holds.released == 1 and plugin.mcp.mode == "Edit" and not plugin.started_play
