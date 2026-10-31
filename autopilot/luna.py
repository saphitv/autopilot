"""Luna as the decision model: Jev's interface (client.system_one(state, questions)) answered by OpenAI's gpt-6-luna
through Codex, signed in with the user's own ChatGPT plan (AUTOPILOT_DECIDER=luna). No API key, no TypeSafe account:
the decisions count against the plan's Codex usage.

    LunaClient.system_one(state, questions) -> typesafe_sdk SystemOneResponse
        Choice  -> {"choice": <option>, "confidence": 0..1}   probabilities: the choice gets the confidence, the rest
                                                              share what is left (the runtime's switch margin reads it)
        Score   -> {"level": <0..n-1>}                        score = the level
        Noul    -> {"yes": 0..1}                              noul = the probability of yes

Decisions go to a few ephemeral Codex threads, each answering one decision at a time, with a short system prompt in
place of Codex's own, Codex's tools and context off, a JSON schema for the answer, reasoning "none" and the normal speed
tier (AUTOPILOT_LUNA_EFFORT, AUTOPILOT_LUNA_TIER, AUTOPILOT_LUNA_MODEL change them). A thread is kept for the next
decisions: its earlier messages are then in OpenAI's prompt cache, which halves the time (Codex's own ~4k tokens of
tool definitions come before the first state). Past ROTATE_TOKENS of input a thread is dropped for a new one.

Measured from Linux with a ChatGPT plan (2026-10-31): a new thread's first decision 1.6-2.2 s, the next ones 0.8-1.3 s
(Jev: 0.2-0.3 s). The Fast tier ("priority") made no difference: the time is mostly Codex's and the network's.
"""

import json
import os
import shutil
import tempfile
import threading
import time

MODEL = "gpt-6-luna"
EFFORT = "none"
ROTATE_TOKENS = 60_000  # a thread's input past which the next decision gets a new thread
# Codex's context and tools a decision has no use for (on top of the Codex agent's LOCKDOWN)
QUIET = {"include_apps_instructions": False, "include_environment_context": False,
         "include_permissions_instructions": False, "include_collaboration_mode_instructions": False,
         "include_skills_usage_instructions": False, "include_plugin_usage_instructions": False,
         "include_apps_usage_instructions": False, "features.plugins": False, "features.apps": False,
         "features.code_mode": False}
SYSTEM = """You are the decision model of an AI player of a video game. Each message is the game's state now (JSON:
the goal, the situation, the options with their facts, what is being done now and how recent options turned out) and
questions about it. Each message is a new moment of the same game: earlier messages are moments past, their states
are out of date. Answer every question at once, from the state only, in the JSON shape asked:
- a choice: pick the one option that best follows its instructions now; confidence is how sure you are (0 to 1).
- a level: the level (its number) whose description fits the state best.
- yes: the probability (0 to 1) that the statement is true.
Decide fast and plainly; never explain."""


def enabled():
    return (os.environ.get("AUTOPILOT_DECIDER") or "").strip().lower() == "luna"


def _text(content):
    """A question's instructions or criterion as text."""
    if content is None:
        return None
    return content if isinstance(content, str) else json.dumps(content, default=str)


def _kind(q):
    return getattr(q, "type", None) or (q.get("type") if isinstance(q, dict) else None)


def _field(q, name):
    return getattr(q, name, None) if not isinstance(q, dict) else q.get(name)


def ask(state, questions):
    """The message and the answer's JSON schema for state and questions ({name: Choice | Score | Noul})."""
    asked, props = {}, {}
    for name, q in questions.items():
        kind, instructions, criteria = _kind(q), _text(_field(q, "instructions")), _field(q, "criteria")
        if kind == "choice":
            options = list(criteria or {})
            asked[name] = {"type": "choice", "instructions": instructions,
                           "options": {k: _text(v) for k, v in (criteria or {}).items()}}
            props[name] = {"type": "object", "additionalProperties": False, "required": ["choice", "confidence"],
                           "properties": {"choice": {"type": "string", "enum": options},
                                          "confidence": {"type": "number"}}}
        elif kind == "score":
            levels = [_text(c) for c in criteria or []]
            asked[name] = {"type": "level", "instructions": instructions, "levels": dict(enumerate(levels))}
            props[name] = {"type": "object", "additionalProperties": False, "required": ["level"],
                           "properties": {"level": {"type": "integer", "enum": list(range(len(levels)))}}}
        elif kind == "noul":
            crit = criteria or {}
            asked[name] = {"type": "yes", "statement": instructions,
                           "yes_means": _text(crit.get("true")), "no_means": _text(crit.get("false"))}
            props[name] = {"type": "object", "additionalProperties": False, "required": ["yes"],
                           "properties": {"yes": {"type": "number"}}}
        else:
            raise ValueError(f"Luna can't answer a {kind!r} question ({name})")
    schema = {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}
    message = json.dumps({"state": state, "questions": asked}, default=str, separators=(",", ":"))
    return message, schema


def answers(questions, data):
    """The typesafe answers ({name: answer dict}) that data (Luna's JSON) gives to questions."""
    out = {}
    for name, q in questions.items():
        a = data.get(name) or {}
        kind, criteria = _kind(q), _field(q, "criteria")
        if kind == "choice":
            options = list(criteria or {})
            pick = a.get("choice") if a.get("choice") in options else options[0]
            c = min(1.0, max(0.0, float(a.get("confidence") if a.get("confidence") is not None else 1.0)))
            if len(options) > 1:
                c = max(c, 1.0 / len(options) + 0.01)  # (the runtime plays the likeliest: the pick stays on top)
            rest = (1.0 - c) / (len(options) - 1) if len(options) > 1 else 0.0
            probs = {o: (c if o == pick else rest) for o in options}
            if len(options) == 1:
                probs[pick] = 1.0
            out[name] = {"type": "choice", "choice": pick, "confidence": c, "probabilities": probs}
        elif kind == "score":
            levels = [_text(x) for x in criteria or []]
            level = a.get("level")
            level = level if isinstance(level, int) and 0 <= level < len(levels) else 0
            out[name] = {"type": "score", "score": float(level), "confidence": 1.0,
                         "legend": dict(enumerate(levels)), "probabilities": {level: 1.0}}
        elif kind == "noul":
            out[name] = {"type": "noul", "noul": min(1.0, max(0.0, float(a.get("yes") or 0.0)))}
    return out


def response(model, questions, data, usage=None):
    from typesafe_sdk._core.response_types import SystemOneResponse

    usage = usage or {}
    return SystemOneResponse.model_validate({"model": model, "answers": answers(questions, data),
                                             "usage": {"input_tokens": usage.get("input"),
                                                       "output_tokens": usage.get("output")}})


class LunaClient:
    """Jev's client, answered by Luna through Codex on the user's ChatGPT sign-in. Started on first use; close() (or
    dropping it) stops Codex and hands a refreshed sign-in back."""

    def __init__(self, model=None, effort=None, tier=None):
        self.model = model or (os.environ.get("AUTOPILOT_LUNA_MODEL") or "").strip() or MODEL
        self.effort = effort or (os.environ.get("AUTOPILOT_LUNA_EFFORT") or "").strip() or EFFORT
        self.tier = tier or (os.environ.get("AUTOPILOT_LUNA_TIER") or "").strip() or None
        self.lock = threading.Lock()
        self.codex = None
        self.work = None
        self.lent = None
        self.sign_in = None
        self.idle = []  # [thread, input tokens so far]: threads not answering now, the most recent last

    def _start(self):
        with self.lock:
            if self.codex is not None:
                return self.codex
            from openai_codex import Codex, CodexConfig

            from .agents import codex as cx

            self.sign_in = cx.user_auth_file()
            if not os.path.isfile(self.sign_in):
                raise RuntimeError("Luna decides through Codex signed in with ChatGPT: sign in with `codex login` "
                                   "first (" + self.sign_in + " is missing).")
            self.work = tempfile.mkdtemp(prefix="autopilot-luna-")
            home = os.path.join(self.work, "home")
            os.makedirs(home)
            self.lent = cx.lend_sign_in(self.sign_in, home)
            codex = Codex(CodexConfig(env=cx.app_env(home, self.work), cwd=self.work))
            codex.__enter__()
            codex._client._approval_handler = cx.decline
            self.codex = codex
            return codex

    def system_one(self, state, questions, timeout=None, retry=None, **_):
        from openai_codex import ApprovalMode, Sandbox
        from openai_codex.types import ReasoningEffort

        from .agents import codex as cx

        codex = self._start()
        message, schema = ask(state, questions)
        effort = ReasoningEffort(self.effort) if self.effort in {e.value for e in ReasoningEffort} else None
        with self.lock:
            held = self.idle.pop() if self.idle else None
        if held is None:
            thread = codex.thread_start(approval_mode=ApprovalMode.deny_all, sandbox=Sandbox.read_only,
                                        cwd=self.work, model=self.model, base_instructions=SYSTEM, ephemeral=True,
                                        service_tier=self.tier, config=dict(cx.LOCKDOWN, **QUIET))
            held = [thread, 0]
        result = held[0].run(message, effort=effort, output_schema=schema)
        text = getattr(result, "final_response", None) or ""
        tokens = None
        usage = getattr(result, "usage", None)
        last = getattr(usage, "last", None) if usage is not None else None
        if last is not None:
            tokens = {"input": last.input_tokens or 0, "output": last.output_tokens}  # (cached input included)
            held[1] = last.input_tokens or 0
        if held[1] < ROTATE_TOKENS:
            with self.lock:
                if self.codex is codex:
                    self.idle.append(held)
        try:
            data = json.loads(text)
        except ValueError:
            raise RuntimeError(f"Luna's answer isn't JSON: {text[:300]!r}") from None
        return response(self.model, questions, data, tokens)

    def look(self, system, parts, model, effort="low"):
        """A model's answer to parts ([(kind, value)]: "text" and base64 JPEG "image", as autopilot/vision.py makes
        them), on a new thread with system as its instructions: (text, usage {"input", "output", "cache_read"})."""
        from openai_codex import ApprovalMode, ImageInput, Sandbox, TextInput
        from openai_codex.types import ReasoningEffort

        from .agents import codex as cx

        codex = self._start()
        items = [TextInput(v) if kind == "text" else ImageInput(f"data:image/jpeg;base64,{v}") for kind, v in parts]
        thread = codex.thread_start(approval_mode=ApprovalMode.deny_all, sandbox=Sandbox.read_only, cwd=self.work,
                                    model=model, base_instructions=system, ephemeral=True,
                                    config=dict(cx.LOCKDOWN, **QUIET))
        eff = ReasoningEffort(effort) if effort in {e.value for e in ReasoningEffort} else None
        result = thread.run(items, effort=eff)
        last = getattr(getattr(result, "usage", None), "last", None)
        usage = None
        if last is not None:
            cached = last.cached_input_tokens or 0
            usage = {"input": max(0, (last.input_tokens or 0) - cached), "output": last.output_tokens or 0,
                     "cache_read": cached, "cache_write": 0}
        return (getattr(result, "final_response", None) or "").strip(), usage

    def close(self):
        with self.lock:
            codex, self.codex = self.codex, None
            self.idle = []
            if codex is not None:
                try:
                    codex.__exit__(None, None, None)
                except Exception:
                    pass
            if self.lent:
                from .agents import codex as cx

                try:
                    cx.return_sign_in(self.sign_in, *self.lent)
                except OSError:
                    pass
                self.lent = None
            if self.work:
                shutil.rmtree(self.work, ignore_errors=True)
                self.work = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def timed(n=8):
    """n decisions on a made-up state, timed (python -m autopilot.luna)."""
    from typesafe_sdk import Choice, Noul

    client = LunaClient()
    state = {"goal": "collect coins without dying",
             "situation": {"door": "closed, 4 m ahead", "coin": "2 m left", "enemy": "12 m behind, coming"},
             "options": {"go_coin": {"distance_m": 2}, "open_door": {"distance_m": 4}, "flee": {}, "wait": {}},
             "now_doing": None, "recent": []}
    questions = {"pick": Choice(instructions="Pick the option to do now.",
                                criteria={"go_coin": "walk to the coin", "open_door": "open the door",
                                          "flee": "run from the enemy", "wait": "do nothing"}),
                 "danger": Noul(instructions="The player is in danger.")}
    try:
        for i in range(n):  # (the first on a new thread)
            t0 = time.monotonic()
            r = client.system_one(state=dict(state, tick=i), questions=questions)
            print(f"{time.monotonic() - t0:.2f} s  pick={r.choices['pick'].choice} "
                  f"p={r.choices['pick'].confidence:.2f} danger={r.nouls['danger'].noul:.2f}")
    finally:
        client.close()


if __name__ == "__main__":
    timed()
