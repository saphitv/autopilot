"""The agent on Codex (OpenAI's coding agent, through its Python SDK, openai-codex). AUTOPILOT_AGENT=codex picks it.

It gets the same request, instructions, briefing and game tools as Claude Code and OpenCode; only the agent differs.
The SDK runs Codex as `codex app-server`; the game's tools reach it as an MCP server on stdio (codex_tools.py) that
passes each call on to this process (toolserver.py), as OpenCode's do.

Codex's own tools are cut to what can't reach outside the game's folder: its shell, web search, sub-agents, goals, image
viewing and sleep are off; its sandbox is read-only (its patch tool is refused); it is never asked to approve anything,
and a request it makes anyway is declined (decline()). It reads and writes files through Autopilot's file tools
(files.py), held to the game's folder as OpenCode's are: read the folder and the kit; write this run's folder, notes.md
and lib.py. Every run gets a temporary Codex home, so the user's own Codex setup (config, AGENTS.md, skills, sign-in) is
never loaded and a run doesn't depend on what earlier runs left.

OpenAI only: the key is OPENAI_API_KEY (or CODEX_API_KEY); without one, AUTOPILOT_CODEX_AUTH=chatgpt lends the run the
user's own Codex sign-in with ChatGPT (their auth.json, copied into the run's Codex home and handed back refreshed:
see lend_sign_in). The model is AUTOPILOT_MODEL, else OpenAI's default
(session.MODELS); it must be one Codex knows (it offers no tools to others). Installed with the codex extra
(`autopilot[codex]`: the SDK and Codex itself, about 150 MB).
"""

import json
import os
import sys
import tempfile
import threading
import time
import traceback

from .. import engines
from . import session as sessionmod
from .files import FILE_TOOL_SPECS
from .opencode import remove_tree
from .toolserver import ToolServer

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS_SCRIPT = os.path.join(HERE, "codex_tools.py")
PROVIDER = "openai"

# Codex's settings for every run (config overrides, dotted keys): its own tools that reach outside the game's folder,
# or that nobody can answer, are off; nothing of the user's or the game's own Codex setup is read
LOCKDOWN = {
    "features.shell_tool": False,
    "features.unified_exec": False,
    "web_search": "disabled",
    "features.goals": False,
    "features.view_image": False,
    "features.sleep_tool": False,
    "features.multi_agent_v2": False,
    "agents.enabled": False,
    "features.collaboration_modes": False,
    "tools.experimental_request_user_input.enabled": False,
    "features.memories": False,
    "features.skip_host_skill_discovery": True,
    "features.responses_websockets": False,
    "features.responses_websockets_v2": False,
    "project_doc_max_bytes": 0,
    "check_for_update_on_startup": False,
    "analytics.enabled": False,
    "feedback.enabled": False,
    "history.persistence": "none",
}

CODEX_TOOLS = """## Your tools here

Codex's own shell, file and web tools are off here. Your tools are the game's (probe, run_harness, snapshot,
describe_frames, restart_level, report) and these file tools, all from the `autopilot` MCP server: read_file,
list_files and search (this game's folder and the kit: autopilot/KIT.md, autopilot/kit/); write_file and edit_file
({run}/harness.py and other files in {run}/, notes.md, lib.py: nothing else may be changed). Paths are relative to
this game's folder. There is no shell and no web: everything you need is in this folder and the live game. The game's
tools each return their result, then the time used of your budget. Call them one at a time: the game runs only inside
a call. Once report says "Reported", stop.
"""


def current_provider():
    """Codex's provider: always OpenAI."""
    return PROVIDER


def api_key():
    """The OpenAI key for Codex: OPENAI_API_KEY, else CODEX_API_KEY; None when neither is set."""
    return (os.environ.get("OPENAI_API_KEY") or os.environ.get("CODEX_API_KEY") or "").strip() or None


CHATGPT = "chatgpt"


def user_auth_file():
    """The user's own Codex sign-in: auth.json in their Codex home ($CODEX_HOME, else ~/.codex)."""
    home = os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
    return os.path.join(home, "auth.json")


def chatgpt_sign_in():
    """The user's Codex sign-in file when AUTOPILOT_CODEX_AUTH=chatgpt asks for it (their ChatGPT plan instead of an
    API key), else None. Raises when it was asked for but there is no ChatGPT sign-in to lend."""
    if (os.environ.get("AUTOPILOT_CODEX_AUTH") or "").strip().lower() != CHATGPT:
        return None
    path = user_auth_file()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict) or not data.get("tokens"):
        raise RuntimeError(f"AUTOPILOT_CODEX_AUTH=chatgpt, but {path} holds no ChatGPT sign-in: sign in with "
                           "`codex login` first.")
    return path


def lend_sign_in(src, home):
    """Copy the user's sign-in into the run's Codex home; returns (the copy, the original's bytes) for
    return_sign_in."""
    with open(src, "rb") as f:
        original = f.read()
    copy = os.path.join(home, "auth.json")
    fd = os.open(copy, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(original)
    return copy, original


def return_sign_in(src, copy, original):
    """Codex refreshes its tokens in the home it runs in, and a refresh can retire the refresh token the user's own
    file still holds: a sign-in the run refreshed goes back to the user's file, unless that file changed meanwhile
    (their own Codex refreshed it: theirs wins). Returns whether it was written back."""
    try:
        with open(copy, "rb") as f:
            new = f.read()
        with open(src, "rb") as f:
            current = f.read()
    except OSError:
        return False
    if new == original or current != original or not new.strip():
        return False
    tmp = src + ".autopilot-tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(new)
    os.replace(tmp, src)
    return True


def system_text(engine, run_rel):
    return sessionmod.system_prompt(engine, run_rel) + "\n" + CODEX_TOOLS.replace("{run}", run_rel)


def mcp_tools():
    """The tools the autopilot MCP server offers: the game's, then the file tools."""
    return [{"name": n, "description": d, "inputSchema": s} for n, d, s in sessionmod.TOOL_SPECS + FILE_TOOL_SPECS]


def thread_config(tools_spec, budget, base_url=None):
    """Codex's config overrides for a run: LOCKDOWN, the autopilot MCP server (this Python, isolated, running
    codex_tools.py on tools_spec, the spec file), and the model API's root when the environment moves it (the run's
    telemetry proxy, or a gateway)."""
    config = dict(LOCKDOWN)
    config["mcp_servers.autopilot"] = {
        "command": sys.executable,
        "args": ["-I", TOOLS_SCRIPT, tools_spec],
        "required": True,
        "startup_timeout_sec": 60,
        "tool_timeout_sec": int(budget) + 120,  # (an episode may take minutes: the run's budget bounds it)
        "default_tools_approval_mode": "approve",
    }
    if base_url:
        config["openai_base_url"] = base_url.rstrip("/")
    return config


def app_env(home, work):
    """The environment Codex runs in: no secrets (Codex signs in from its own home), no model API settings of the
    user's (the run passes its own), its home and temporary files in this run's folder `work`."""
    env = {k: v for k, v in os.environ.items()
           if not sessionmod.SECRET.search(k) and not k.upper().startswith(("OPENAI_", "CODEX_"))}
    tmp = os.path.join(work, "tmp")
    os.makedirs(tmp, exist_ok=True)
    env.update(CODEX_HOME=home, TEMP=tmp, TMP=tmp, TMPDIR=tmp)
    return env


def decline(method, params):
    """Codex's requests to the user, answered no: approvals declined, questions and forms left unanswered (approvals
    are never asked for here; this is in case one is)."""
    if method in ("applyPatchApproval", "execCommandApproval"):
        return {"decision": "denied"}
    if "requestApproval" in method or "Approval" in method:
        return {"decision": "decline"}
    if "elicitation" in method.lower():
        return {"action": "decline"}
    if "requestUserInput" in method:
        return {"answers": {}}
    return {}


def plain(model):
    """A generated model (or RootModel) as plain JSON data."""
    if model is None:
        return None
    if hasattr(model, "model_dump"):
        return model.model_dump(by_alias=True, mode="json")
    return model


class Events:
    """What Codex's notifications say: the implementer log, the status line, the tokens and turns, the model's last
    words, and how its turn ended."""

    def __init__(self, session, say=None):
        self.session = session
        self.say = say or (lambda text: None)
        self.tokens = {}
        self.turns = 0
        self.last_text = ""
        self.ended = None  # the last turn's status ("completed", "interrupted", "failed")
        self.error = None

    def event(self, method, payload):
        note = self.session.note
        data = plain(payload) or {}
        if method == "item/completed":
            item = data.get("item") or {}
            kind = item.get("type")
            if kind == "agentMessage" and (item.get("text") or "").strip():
                self.last_text = item["text"]
                self.turns += 1
                self.session.said(self.last_text)
            elif kind == "mcpToolCall":
                self.turns += 1
                if item.get("error"):
                    note(f"{item.get('tool')} failed: {json.dumps(item['error'])[:1000]}")
            elif kind and kind not in ("userMessage", "reasoning"):
                note(f"{kind}: {json.dumps(item)[:600]}")
        elif method == "thread/tokenUsage/updated":
            total = (data.get("tokenUsage") or {}).get("total") or {}
            self.tokens = {"input": int(total.get("inputTokens") or 0), "output": int(total.get("outputTokens") or 0),
                           "reasoning": int(total.get("reasoningOutputTokens") or 0),
                           "cacheRead": int(total.get("cachedInputTokens") or 0),
                           "cacheWrite": int(total.get("cacheWriteInputTokens") or 0)}
        elif method == "turn/completed":
            turn = data.get("turn") or {}
            self.ended = turn.get("status")
            err = turn.get("error") or {}
            self.error = err.get("message") if isinstance(err, dict) else None
            note(f"turn ended: {self.ended}" + (f" ({self.error})" if self.error else ""))
        elif method == "error":
            err = data.get("error") or {}
            note(f"Codex error: {str(err.get('message') if isinstance(err, dict) else err)[:2000]}"
                 + (" (retrying)" if data.get("willRetry") else ""))


def known_models(codex):
    """The models Codex knows (its catalog), or None when it can't say."""
    try:
        return {m.id for m in codex.models(include_hidden=True).data}
    except Exception:
        return None


def run_turns(thread, message, events, session, deadline, effort):
    """Run turns on the thread until the model's turn ends with nothing more to say to it, stop(), the deadline or a
    failure. The user's words that came after the last tool result go to it as a new turn. Returns None when it ended
    by itself, else why it was cut short."""
    while message:
        turn = thread.turn(message, effort=effort)
        why = {}
        finished = threading.Event()

        def watch(turn=turn, why=why, finished=finished):
            while not finished.wait(0.5):
                reason = "stopped" if session.stop() else "timed out" if time.time() > deadline else None
                if reason:
                    why["reason"] = reason
                    try:
                        turn.interrupt()
                    except Exception:
                        pass
                    return

        threading.Thread(target=watch, daemon=True).start()
        try:
            for event in turn.stream():
                events.event(event.method, event.payload)
        finally:
            finished.set()
        if why.get("reason"):
            return why["reason"]
        if events.ended not in (None, "completed"):
            return f"the model's turn {events.ended}: {events.error or 'no reason given'}"
        # (the user steered after the last tool result, with no tool call left to carry it: on as asked)
        message = session.steer_note().strip()
    return None


def implement(game, project, prompt, run_id=None, say=None, client=None, model=None, timeout=None, stop=None,
              steering=None, play_seconds=None, describer=None, activity=None):
    """Research, write, run and revise a harness for prompt on the live game; returns the report. The same arguments
    and report as autopilot.agents.claude_code.implement (the report says "agent": "codex")."""
    try:
        from openai_codex import ApprovalMode, Codex, CodexConfig, Sandbox
        from openai_codex.types import ReasoningEffort
    except ImportError:
        raise RuntimeError("The Codex agent needs Codex: install Autopilot with the codex extra "
                           "(`autopilot[codex]`), or pick another agent with AUTOPILOT_AGENT.") from None
    key = api_key()
    sign_in = None if key else chatgpt_sign_in()
    if not key and not sign_in:
        raise RuntimeError("The Codex agent needs an OpenAI key: set OPENAI_API_KEY (the first-run setup saves it "
                           "when OpenAI is picked), or AUTOPILOT_CODEX_AUTH=chatgpt to use your own Codex sign-in "
                           "with ChatGPT.")
    model = model or sessionmod.model(PROVIDER)
    session = sessionmod.open_session(game, project, prompt, run_id, say=say, client=client, timeout=timeout,
                                      stop=stop, steering=steering, play_seconds=play_seconds,
                                      describer=describer, activity=activity)
    session.note(f"agent: Codex (openai-codex), {PROVIDER} {model}")
    work = tempfile.mkdtemp(prefix="autopilot-codex-")  # (Codex's own home for this run: not in the game's memory)
    home = os.path.join(work, "home")
    os.makedirs(home)
    lent = lend_sign_in(sign_in, home) if sign_in else None
    server = ToolServer(session, say=session.say, files=True)
    spec = os.path.join(home, "autopilot-tools.json")
    with open(spec, "w", encoding="utf-8") as f:
        json.dump({"url": server.url, "tools": mcp_tools()}, f)
    events = Events(session, say=session.say)
    effort = ReasoningEffort(sessionmod.EFFORT) if sessionmod.EFFORT in {e.value for e in ReasoningEffort} else None
    message = sessionmod.first_message(session)

    done = sessionmod.watch_steering(session)
    t0 = time.time()
    stopped = None
    try:
        game.pause(True)  # (in the try: a game that doesn't answer still gets cleaned up and reported)
        with Codex(CodexConfig(env=app_env(home, work), cwd=session.root)) as codex:
            codex._client._approval_handler = decline  # (the SDK's own accepts commands and file changes)
            if key:
                codex.login_api_key(key)
            known = known_models(codex)
            if known is not None and model not in known:
                raise RuntimeError(f"Codex doesn't know the model {model!r} (it offers no tools to models it doesn't "
                                   f"know): set AUTOPILOT_MODEL to one of {', '.join(sorted(known))}.")
            thread = codex.thread_start(
                approval_mode=ApprovalMode.deny_all, sandbox=Sandbox.read_only, cwd=session.root, model=model,
                developer_instructions=system_text(engines.of(game), session.run_rel),
                # (signed in with ChatGPT, Codex talks to ChatGPT's own endpoint: the run's proxy and other API roots
                # are for API keys, and refuse the sign-in's token)
                config=thread_config(spec, session.budget, os.environ.get("OPENAI_BASE_URL") if key else None),
                ephemeral=True)
            stopped = run_turns(thread, message, events, session, t0 + session.budget, effort)
    except Exception as e:
        stopped = f"{type(e).__name__}: {e}"
        session.note(traceback.format_exc())
    finally:
        done.set()
        server.close()
        if session.sandbox:
            if stopped == "stopped":
                session.sandbox.stop()
            session.sandbox.close()
        if lent:
            try:
                if return_sign_in(sign_in, *lent):
                    session.note("codex: the run refreshed the ChatGPT sign-in; handed back to " + sign_in)
            except OSError as e:
                session.note(f"codex: couldn't hand the refreshed sign-in back: {e}")
        remove_tree(work)
        try:
            game.release_all()
            game.pause(False)
        except Exception:
            pass
    return sessionmod.finish(session, t0, stopped, events.last_text, agent="codex", provider=PROVIDER, model=model,
                             fast=False, tokens=events.tokens, turns=events.turns)

