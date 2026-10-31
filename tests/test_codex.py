"""The Codex agent (autopilot/agents/codex.py): the file tools it gets instead of its own (files.py), the MCP server on
stdio that hands it the game's tools (codex_tools.py), its lockdown and declined requests; and, when Codex is installed
(the codex extra), a whole run against the fake game, with a stand-in for OpenAI's API scripting the model."""

import http.server
import importlib.util
import io
import json
import os
import threading
import time
import types

import pytest
from test_autopilot import FakeLink

from autopilot import agents, codex
from autopilot.agents import codex as codexagent
from autopilot.agents import codex_tools
from autopilot.agents import session as sessionmod
from autopilot.agents.files import FileTools
from autopilot.kit.game import Game


@pytest.fixture
def root(tmp_path):
    game = tmp_path / "memory" / "Farm"
    (game / "runs" / "r1").mkdir(parents=True)
    (game / "codex").mkdir()
    (game / "codex" / "README.md").write_text("Crate: open it with E\nGate: lever\n", encoding="utf-8")
    (game / "notes.md").write_text("old notes\n", encoding="utf-8")
    (game / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (tmp_path / "memory" / "Other").mkdir()
    (tmp_path / "memory" / "Other" / "notes.md").write_text("another game's\n", encoding="utf-8")
    return game


def test_the_file_tools_read_the_games_folder_and_the_kit_and_write_only_the_runs_files(root):
    files = FileTools(str(root), "runs/r1", sessionmod.PACKAGE)
    assert "Crate: open it with E" in files.call("read_file", {"path": "codex/README.md"})
    assert "     2\tGate: lever" in files.call("read_file", {"path": "codex/README.md", "offset": 2})
    assert "Refused" not in files.call("read_file", {"path": os.path.join(sessionmod.PACKAGE, "KIT.md")})
    for path in ("../Other/notes.md", ".env", os.path.join(sessionmod.PACKAGE, "agents", "session.py"),
                 "C:/Windows/win.ini" if os.name == "nt" else "/etc/passwd"):
        assert files.call("read_file", {"path": path}).startswith("Refused"), path
    assert files.call("list_files", {"pattern": "codex/**/*.md"}) == "codex/README.md"
    assert files.call("search", {"pattern": "lever"}) == "codex/README.md:2: Gate: lever"
    assert files.call("search", {"pattern": "SECRET="}).startswith("No match")  # (.env files are never searched)
    assert files.call("write_file", {"path": "runs/r1/harness.py", "content": "# h\n"}).startswith("Wrote")
    assert files.call("write_file", {"path": "notes.md", "content": "new\n"}).startswith("Wrote")
    assert files.call("edit_file", {"path": "notes.md", "old": "new", "new": "newer"}).startswith("Wrote")
    assert (root / "notes.md").read_text(encoding="utf-8") == "newer\n"
    for path in ("codex/README.md", "runs/r0/harness.py", "runs/r1", "../Other/notes.md", "lib.py/../codex/x"):
        assert files.call("write_file", {"path": path, "content": "x"}).startswith("Refused"), path
    assert (root / "codex" / "README.md").read_text(encoding="utf-8").startswith("Crate")
    files.call("write_file", {"path": "lib.py", "content": "a a"})
    assert "appears 2 times" in files.call("edit_file", {"path": "lib.py", "old": "a", "new": "b"})
    assert files.call("edit_file", {"path": "lib.py", "old": "a", "new": "b", "replace_all": True}).startswith("Wrote")


def test_the_mcp_server_lists_the_tools_and_passes_calls_on(root):
    calls = []

    class ToolServer(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            calls.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            body = b"the crate is open"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ToolServer)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        out = io.StringIO()
        server = codex_tools.Server({"url": f"http://127.0.0.1:{httpd.server_port}/secret",
                                     "tools": codexagent.mcp_tools()}, out=out)
        lines = [json.dumps(m) for m in (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "probe", "arguments": {"code": "1"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "nope"})]
        server.serve(lines)
        deadline = time.time() + 5
        while out.getvalue().count("\n") < 4 and time.time() < deadline:
            time.sleep(0.02)
        answers = {m["id"]: m for m in map(json.loads, out.getvalue().splitlines())}
        assert answers[1]["result"]["protocolVersion"] == "2025-06-18"
        names = [t["name"] for t in answers[2]["result"]["tools"]]
        assert names == ["probe", "run_harness", "snapshot", "describe_frames", "restart_level", "report", "read_file",
                         "list_files", "search", "write_file", "edit_file"]
        assert answers[3]["result"] == {"content": [{"type": "text", "text": "the crate is open"}], "isError": False}
        assert calls == [("/secret/probe", {"code": "1"})]
        assert answers[4]["error"]["code"] == -32601
    finally:
        httpd.shutdown()


def test_codex_is_locked_down_and_says_no_to_what_it_asks(monkeypatch, tmp_path):
    config = codexagent.thread_config("C:/run/home/autopilot-tools.json", 1800, "http://127.0.0.1:9/proxy/openai/")
    for key in ("features.shell_tool", "features.unified_exec", "features.view_image", "agents.enabled"):
        assert config[key] is False, key
    assert config["web_search"] == "disabled" and config["project_doc_max_bytes"] == 0
    assert config["openai_base_url"] == "http://127.0.0.1:9/proxy/openai"
    mcp = config["mcp_servers.autopilot"]
    assert mcp["args"][0] == "-I" and mcp["args"][1].endswith("codex_tools.py") and mcp["tool_timeout_sec"] >= 1800
    assert codexagent.decline("item/commandExecution/requestApproval", {}) == {"decision": "decline"}
    assert codexagent.decline("item/fileChange/requestApproval", {}) == {"decision": "decline"}
    assert codexagent.decline("execCommandApproval", {}) == {"decision": "denied"}
    assert codexagent.decline("item/tool/requestUserInput", {}) == {"answers": {}}
    assert codexagent.decline("mcpServer/elicitation/request", {}) == {"action": "decline"}
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "an-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://elsewhere.test")
    env = codexagent.app_env("C:/run/home", str(tmp_path))
    assert env["CODEX_HOME"] == "C:/run/home"
    assert not [k for k in env if k.endswith("_API_KEY") or k.startswith("OPENAI_")]


def test_codex_is_picked_by_name_and_runs_with_openai(monkeypatch):
    monkeypatch.setenv("AUTOPILOT_AGENT", "codex")
    assert agents.select() is codexagent and codexagent.current_provider() == "openai"
    assert agents.select("claude") is not codexagent  # (a run through Aura names its agent)
    from autopilot import account

    assert account.llm_provider({}) == "openai"


class FakeTurn:
    """A Codex turn: its events, or (block) none until it is interrupted."""

    def __init__(self, text, block=False):
        self.text, self.block = text, block
        self.interrupted = threading.Event()

    def interrupt(self):
        self.interrupted.set()

    def stream(self):
        if self.block:
            self.interrupted.wait(10)
            yield types_ns(method="turn/completed", payload={"turn": {"status": "interrupted"}})
            return
        yield types_ns(method="item/completed", payload={"item": {"type": "agentMessage", "text": self.text}})
        yield types_ns(method="thread/tokenUsage/updated", payload={"tokenUsage": {"total": {
            "inputTokens": 50, "outputTokens": 5, "cachedInputTokens": 10, "reasoningOutputTokens": 1}}})
        yield types_ns(method="turn/completed", payload={"turn": {"status": "completed"}})


def types_ns(**kw):
    return types.SimpleNamespace(**kw)


class FakeThread:
    def __init__(self, block_from=None):
        self.messages = []
        self.block_from = block_from

    def turn(self, message, effort=None):
        self.messages.append(message)
        return FakeTurn(f"answer {len(self.messages)}", block=len(self.messages) == self.block_from)


def test_the_users_late_words_start_another_turn_and_stop_interrupts_one(tmp_path, monkeypatch):
    monkeypatch.setattr(codex, "MEMORY", str(tmp_path / "memory"))
    session = sessionmod.Session(Game(FakeLink()), "fake", "Open the crate", "r1")
    session.stop = lambda: False
    session.steering.add("try the other crate")
    events = codexagent.Events(session)
    thread = FakeThread()
    assert codexagent.run_turns(thread, "the request", events, session, time.time() + 60, None) is None
    assert thread.messages[0] == "the request" and "try the other crate" in thread.messages[1]
    assert len(thread.messages) == 2 and events.last_text == "answer 2" and events.tokens["cacheRead"] == 10

    stopping = threading.Event()
    session.stop = stopping.is_set
    thread = FakeThread(block_from=1)
    threading.Timer(0.3, stopping.set).start()
    assert codexagent.run_turns(thread, "the request", events, session, time.time() + 60, None) == "stopped"
    thread = FakeThread(block_from=1)
    session.stop = lambda: False
    assert codexagent.run_turns(thread, "the request", events, session, time.time() + 0.5, None) == "timed out"


# -- a whole run, when Codex is installed ------------------------------------------------------------------------

needs_codex = pytest.mark.skipif(importlib.util.find_spec("openai_codex") is None,
                                 reason="Codex isn't installed (the codex extra)")


class StandIn:
    """OpenAI's Responses API on 127.0.0.1, playing the model: each request is answered with the next scripted
    code-mode exec call, then a last message. What Codex sent is kept (requests)."""

    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.requests = []
        stand_in = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(404)
                self.end_headers()

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                stand_in.requests.append(body)
                n = len(stand_in.requests)
                if stand_in.scripts:
                    item = {"type": "custom_tool_call", "id": f"ctc_{n}", "call_id": f"call_{n}", "name": "exec",
                            "namespace": "functions", "input": stand_in.scripts.pop(0), "status": "completed"}
                else:
                    item = {"id": f"msg_{n}", "type": "message", "role": "assistant", "status": "completed",
                            "content": [{"type": "output_text", "text": "Reported.", "annotations": []}]}
                rid, model = f"resp_{n}", body.get("model")
                events = [{"type": "response.created", "response": {"id": rid, "object": "response", "created_at": 0,
                                                                    "status": "in_progress", "model": model,
                                                                    "output": []}},
                          {"type": "response.output_item.added", "output_index": 0,
                           "item": {**item, "status": "in_progress"}},
                          {"type": "response.output_item.done", "output_index": 0, "item": item},
                          {"type": "response.completed", "response": {
                              "id": rid, "object": "response", "created_at": 0, "status": "completed", "model": model,
                              "output": [item], "usage": {"input_tokens": 100, "input_tokens_details": {
                                  "cached_tokens": 40}, "output_tokens": 20, "output_tokens_details": {
                                  "reasoning_tokens": 5}, "total_tokens": 120}}}]
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for e in events:
                    self.wfile.write(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode())
                    self.wfile.flush()

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/v1"

    def outputs(self):
        """What each exec call gave back, in order (the scripts' text())."""
        found = {}  # call id -> output (each request carries the earlier ones again)
        for body in self.requests:
            for item in body.get("input", []):
                if item.get("type") == "custom_tool_call_output" and item.get("call_id") not in found:
                    output = item.get("output")
                    found[item.get("call_id")] = ("".join(c.get("text", "") for c in output if isinstance(c, dict))
                                                  if isinstance(output, list) else str(output))
        return list(found.values())


@needs_codex
def test_a_codex_run_uses_only_autopilots_tools_held_to_the_games_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(codex, "MEMORY", str(tmp_path / "memory"))
    first = """
const names = ALL_TOOLS.map(t => t.name);
const w = async (path, content) => (await tools.mcp__autopilot__write_file({path, content})).content[0].text;
const r = async (path) => (await tools.mcp__autopilot__read_file({path})).content[0].text;
text(JSON.stringify({names, notes: await w("notes.md", "the crate opens with E"),
  harness: await w("runs/r1/harness.py", "# the harness"), codex: await w("codex/README.md", "changed"),
  outside: await r("../../escape.txt"),
  snapshot: (await tools.mcp__autopilot__snapshot({})).content[0].text.slice(0, 200)}));
"""
    second = """
text((await tools.mcp__autopilot__report({outcome: "CONFIRMED", summary: "It opens.", evidence:
["e"]})).content[0].text);
"""
    stand_in = StandIn([first, second])
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("OPENAI_BASE_URL", stand_in.url)
    (tmp_path / "escape.txt").write_text("outside the memory", encoding="utf-8")
    game = Game(FakeLink())
    game.look = {"x": 0.1, "y": 0.1}
    try:
        report = codexagent.implement(game, "fake", "Verify that the crate opens", run_id="r1", timeout=240)
    finally:
        stand_in.httpd.shutdown()
    root = tmp_path / "memory" / "fake"
    out = json.loads(stand_in.outputs()[0].split("Output:\n", 1)[-1])
    # Codex's own reach is off: no shell, web, sub-agents or images; its patch tool stays refused (read-only)
    assert not {"exec_command", "write_stdin", "web__run", "view_image", "create_goal"} & set(out["names"])
    assert {"mcp__autopilot__probe", "mcp__autopilot__write_file", "mcp__autopilot__report"} <= set(out["names"])
    assert out["notes"].startswith("Wrote") and out["harness"].startswith("Wrote")
    assert out["codex"].startswith("Refused") and out["outside"].startswith("Refused")
    assert (root / "notes.md").read_text(encoding="utf-8") == "the crate opens with E"
    assert (root / "runs" / "r1" / "harness.py").exists() and not (root / "codex" / "README.md").exists()
    assert "no run_harness episode" in stand_in.outputs()[1]  # (the report's own rule, as for every agent)
    first_request = stand_in.requests[0]
    developer = " ".join(c.get("text", "") for item in first_request["input"] if item.get("role") == "developer"
                         for c in item.get("content", []) if isinstance(c, dict))
    assert "Your tools here" in developer and "runs/r1/harness.py" in developer
    assert report["agent"] == "codex" and report["model"] == sessionmod.MODELS["openai"]
    assert report["tokens"]["input"] >= 100 and report["tokens"]["cacheRead"] >= 40
    assert report["stopped"] is None


@needs_codex
def test_a_codex_runs_model_calls_are_counted_and_kept_like_any_agents(tmp_path, monkeypatch):
    """Codex's calls go through the run's telemetry proxy (OpenAI's Responses API): the Settings page's tokens and the
    run's llm.jsonl, as for OpenCode and Claude Code."""
    from test_telemetry import SendLink

    from autopilot import telemetry

    monkeypatch.setattr(codex, "MEMORY", str(tmp_path / "memory"))
    stand_in = StandIn(['text("hello")'])
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("OPENAI_BASE_URL", stand_in.url)
    link = SendLink()
    game = Game(FakeLink())
    game.look = {"x": 0.1, "y": 0.1}
    run_dir = tmp_path / "memory" / "fake" / "runs" / "r1"
    try:
        with telemetry.RunTelemetry(link, str(run_dir), "r1", "fake", "Verify that x", "verify", True,
                                    providers=(codexagent.current_provider(),)) as tel:
            assert os.environ["OPENAI_BASE_URL"] != stand_in.url  # (the proxy, in front of the stand-in)
            tel.report = codexagent.implement(game, "fake", "Verify that x", run_id="r1", timeout=120)
    finally:
        stand_in.httpd.shutdown()
    assert os.environ["OPENAI_BASE_URL"] == stand_in.url and len(stand_in.requests) == 2
    final = [f for f in link.sent if f["type"] == "usage"][-1]
    llm = [m for m in final["models"] if m["kind"] == "llm"]
    assert llm and llm[0]["model"] == sessionmod.MODELS["openai"] and llm[0]["calls"] == 2
    assert llm[0]["input"] + llm[0]["cache_read"] == 200 and llm[0]["output"] == 40
    assert (run_dir / "telemetry" / "llm.jsonl.gz").exists()


# -- the user's Codex sign-in with ChatGPT (AUTOPILOT_CODEX_AUTH=chatgpt) ---------------------------------------

def test_chatgpt_sign_in_needs_asking(tmp_path, monkeypatch):
    (tmp_path / "auth.json").write_text('{"auth_mode": "chatgpt", "tokens": {"refresh_token": "r1"}}')
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.delenv("AUTOPILOT_CODEX_AUTH", raising=False)
    assert codexagent.chatgpt_sign_in() is None
    monkeypatch.setenv("AUTOPILOT_CODEX_AUTH", "chatgpt")
    assert codexagent.chatgpt_sign_in() == str(tmp_path / "auth.json")
    (tmp_path / "auth.json").write_text('{"OPENAI_API_KEY": null}')
    with pytest.raises(RuntimeError, match="codex login"):
        codexagent.chatgpt_sign_in()


def test_refreshed_sign_in_goes_back(tmp_path):
    src, home = tmp_path / "auth.json", tmp_path / "home"
    home.mkdir()
    src.write_text("r1")
    copy, original = codexagent.lend_sign_in(str(src), str(home))
    assert open(copy).read() == "r1" and (os.stat(copy).st_mode & 0o777) == 0o600
    assert not codexagent.return_sign_in(str(src), copy, original)  # (not refreshed: nothing to do)
    open(copy, "w").write("r2")  # the run's Codex refreshed it
    assert codexagent.return_sign_in(str(src), copy, original)
    assert src.read_text() == "r2"


def test_users_own_refresh_wins(tmp_path):
    src, home = tmp_path / "auth.json", tmp_path / "home"
    home.mkdir()
    src.write_text("r1")
    copy, original = codexagent.lend_sign_in(str(src), str(home))
    open(copy, "w").write("r2")
    src.write_text("r3")  # the user's own Codex refreshed meanwhile
    assert not codexagent.return_sign_in(str(src), copy, original)
    assert src.read_text() == "r3"
