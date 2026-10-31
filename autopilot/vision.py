"""Looking at the game's picture, for a request about how something looks (an animation, an effect, a material or
light, the UI): the frames the run's recording got lately, described in words by a vision model that is never told
the request (docs/architecture.md, "Looking at the picture").

    Frames      the recording's latest frames (JPEG, with the game and wall time each shows), in memory: the last
                KEPT_BYTES of them, so the last few minutes of play.
    Describer   a run's describe_frames: picks up to MAX_FRAMES of an episode's frames, sends them with the agent's
                questions (and SWEEP) to the vision model and returns its description. The model sees the frames and
                the questions only: what the agent hopes to see can't colour what it is told is there.
    route()     which API and model describe frames for a run: the agent's own provider's (Aura's relay in a run
                through Aura), a model that takes pictures; None, with why, when the provider has none.

The description is all the agent gets: no agent takes pictures in its tool results (Codex's image viewing is off,
OpenCode's and the game tools' results are text), and an agent reading frames itself would read them knowing what it
hopes to see. The one exception is the user's own Claude Code or Codex (autopilot/agents/external.py): it is handed
the frames (Describer.frames_of) for a subagent of its own to describe, told only the questions, so the description
comes from the user's plan too.
"""

import base64
import collections
import contextlib
import json
import os
import threading
import time

import requests

# The vision model, on Anthropic (direct, through Aura, through OpenCode Zen). Of the models Aura's relay has a price
# for (it refuses others), Opus 5.5 is the one that described game frames without misreading them (2026-10-19: Sonnet 5
# made up an animation and guessed a covered digit)
MODEL = "claude-opus-5-5"
OPENROUTER_MODEL = "anthropic/claude-opus-5.5"
FALLBACK = "claude-opus-5-5"  # when another model is refused (AUTOPILOT_VISION_MODEL one Aura's relay has no price for)
EFFORT = "low"  # its thinking: describing is perceiving, not working something out
MAX_TOKENS = 4000
MAX_FRAMES = 12  # frames one description looks at, at most
DEFAULT_FRAMES = 8
MAX_QUESTIONS = 4
MAX_REQUEST_BYTES = 3_000_000  # the frames' base64 in one request, at most (Aura's relay takes 4.5 MB in all)
KEPT_BYTES = 160 * 1024 * 1024  # frames kept in memory, at most (the oldest go first)
FILM_FPS = (1, 20)  # the frame rates run_harness may film an episode at (frames_fps)
TIMEOUT = 150  # seconds one description may take
LOG = "describe.jsonl"  # in a shared run's telemetry/: each description, its frames by time (the pictures are in the
                        # recording)

# A provider's API for describing frames: (wire, API root, key variables, model, path for the "openai" wire). The
# root is <PROVIDER>_BASE_URL when the environment sets one (the run's proxy, Aura's relay, a gateway); model None is
# the agent's own (session.MODELS: those take pictures)
ROUTES = {
    "anthropic": ("anthropic", "https://api.anthropic.com", ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"), MODEL, None),
    "opencode": ("anthropic", "https://opencode.ai/zen", ("OPENCODE_API_KEY",), MODEL, None),
    "openrouter": ("openai", "https://openrouter.ai/api/v1", ("OPENROUTER_API_KEY",), OPENROUTER_MODEL,
                   "/chat/completions"),
    "openai": ("openai", "https://api.openai.com/v1", ("OPENAI_API_KEY", "CODEX_API_KEY"), None, "/chat/completions"),
    "google": ("openai", "https://generativelanguage.googleapis.com/v1beta",
               ("GEMINI_API_KEY", "GOOGLE_GENERATIVE_AI_API_KEY", "GOOGLE_API_KEY"), None, "/openai/chat/completions"),
}
NO_VISION = {"deepseek": "DeepSeek's models can't look at pictures"}

SYSTEM = """You describe frames of a video game for someone who can't see them. You are not told what they are
checking, and you don't guess it: you say what is visible, as exactly as you can.

- Answer each question by its number, then the last one.
- Motion: name what moves from frame to frame (which parts of a body or thing, which way, through what arc, from
  which frame to which), what everyday action it looks most like, and what stays still.
- Effects, lights and materials: their colour, shape, size and place on screen, and the frames they show in.
- Text and icons: quote text exactly where it is readable; where it isn't, say "unreadable", never a guess.
- Say what you can't make out (too small, too dark, blurred, covered, off the edge). Say so when a frame is nearly
  black or blank.
- Never judge: don't say whether anything is correct, working, broken, intended or a bug. Describe it.
- At most 400 words."""

SWEEP = ("Last: anything in these frames that looks unusual for a game's picture (a thing passing through another, "
         "a surface missing, stretched or flickering, a body in a pose a body can't take, something frozen while the "
         "rest moves, text cut off or overlapping): say what and in which frames; or say nothing like that shows.")


CHATGPT_MODEL = "gpt-6.1-sol"  # through the user's ChatGPT sign-in, when the agent's model isn't known
_chatgpt = None  # the Codex that describes through the ChatGPT sign-in (autopilot/luna.py), started on first use
_chatgpt_lock = threading.Lock()


def _chatgpt_sign_in():
    """The user's Codex sign-in with ChatGPT, when Autopilot is asked to use it (AUTOPILOT_CODEX_AUTH=chatgpt)."""
    from .agents import codex

    try:
        return codex.chatgpt_sign_in()
    except Exception:  # noqa: BLE001 - asked for, but not signed in: no route
        return None


def _chatgpt_client():
    global _chatgpt
    with _chatgpt_lock:
        if _chatgpt is None:
            import atexit

            from .luna import LunaClient

            _chatgpt = LunaClient()
            atexit.register(_chatgpt.close)
        return _chatgpt


def route(provider, agent_model=None):
    """(wire, API root, key, model, path) that describe frames for an agent on provider, else (None, why)."""
    if provider in NO_VISION:
        return None, NO_VISION[provider]
    if provider not in ROUTES:
        return None, f"no model of {provider} is known to look at pictures"
    wire, root, keys, model, path = ROUTES[provider]
    root = (os.environ.get(provider.upper().replace("-", "_") + "_BASE_URL") or "").strip().rstrip("/") or root
    key = next((os.environ[k] for k in keys if (os.environ.get(k) or "").strip()), None)
    if not key and provider == "openai" and _chatgpt_sign_in():
        # (no key, but the user's own Codex signed in with ChatGPT: its models look at pictures too)
        return ("codex", None, None, (os.environ.get("AUTOPILOT_VISION_MODEL") or "").strip() or agent_model
                or CHATGPT_MODEL, None), None
    if not key:
        return None, f"there is no {provider} key"
    model = (os.environ.get("AUTOPILOT_VISION_MODEL") or "").strip() or model or agent_model
    return (wire, root, key, model, path), None


class Frames:
    """The recording's latest frames, the oldest dropped past KEPT_BYTES. Thread-safe: frames come on the recorder's
    thread."""

    def __init__(self, limit=KEPT_BYTES):
        self.lock = threading.Lock()
        self.items = collections.deque()  # {"jpeg", "t", "wall", "width", "height"}, oldest first
        self.bytes = 0
        self.limit = limit
        self.dropped_before = None  # the wall time of the newest frame dropped (none older are kept)

    def add(self, jpeg, t, wall, width=None, height=None):
        with self.lock:
            self.items.append({"jpeg": jpeg, "t": t, "wall": wall, "width": width, "height": height})
            self.bytes += len(jpeg)
            while self.bytes > self.limit and len(self.items) > 1:
                old = self.items.popleft()
                self.bytes -= len(old["jpeg"])
                self.dropped_before = old["wall"]

    def between(self, wall0, wall1):
        """The frames captured from wall0 to wall1 (Unix seconds), oldest first."""
        with self.lock:
            return [f for f in self.items if wall0 <= f["wall"] <= wall1]


def pick(frames, count):
    """count of frames, spread evenly from the first to the last."""
    if len(frames) <= count:
        return list(frames)
    if count == 1:
        return [frames[len(frames) // 2]]
    return [frames[round(i * (len(frames) - 1) / (count - 1))] for i in range(count)]


class Describer:
    """A run's describe_frames. recorder: the run's media.Recorder (its frames, its fps); provider, agent_model: the
    agent's; usage: the run's telemetry.Usage (a call not through the run's proxy is counted here); proxied: the
    proxy's URL prefix (calls through it are counted there); log: a shared run's telemetry folder, or None."""

    def __init__(self, recorder, provider, agent_model=None, usage=None, proxied=None, log=None):
        self.recorder = recorder
        self.frames = getattr(recorder, "frames", None)
        self.provider = provider
        self.route, self.no_model = route(provider, agent_model)
        self.usage = usage
        self.proxied = proxied
        self.log = log
        self.calls = 0
        self.no_picture = False  # the last describe() found no picture at all of the time asked for
        self.lock = threading.Lock()

    def cannot(self):
        """Why frames can't be described in this run at all (no model that looks, a game without rendering), or
        None: known as the run starts."""
        if self.route is None:
            return f"the agent's LLM provider has no model that looks at pictures ({self.no_model})"
        return self.cannot_film()

    def cannot_film(self):
        """Why the game can't be filmed in this run at all (it runs without a picture), or None: known as the run
        starts. (The user's own agent looks at frames itself, autopilot/agents/external.py: no model of ours needed.)"""
        from . import media

        if self.recorder is None or not media.can_capture(self.recorder.link):
            return "the game runs without a picture (headless: nothing is rendered)"
        return None

    def unavailable(self):
        """Why frames can't be described now (cannot(), or no picture has come from the game: its window minimized or
        hidden), or None."""
        return self._received(self.cannot())

    def unfilmed(self):
        """Why the game's frames can't be had now (cannot_film(), or no picture has come from the game), or None."""
        return self._received(self.cannot_film())

    def _received(self, why):
        if why is None and not self.recorder.received:
            why = self.recorder.problem or "no picture has come from the game"
        return why

    def alert(self, why, filming=False):
        """What the user is told when a run needed to look and couldn't (the result's `alerts`). filming: why is the
        game's picture's (no model of ours was needed: the user's own agent looks itself)."""
        fix = ("choose an LLM provider whose models look at pictures (Anthropic, OpenAI, Google or OpenRouter)"
               if self.route is None and not filming else "run the game with its window shown (not headless, not "
               "minimized)")
        return f"The recording could not be analyzed: {why}. To fix it, {fix}."

    @contextlib.contextmanager
    def filming(self, fps):
        """The recording at fps frames a second inside the block (an episode filmed closely: a fast animation), then
        back as it was. A plugin restarts its recording with new settings (docs/protocol.md, `record`)."""
        recorder = self.recorder
        before = getattr(recorder, "fps", None)
        fps = max(FILM_FPS[0], min(FILM_FPS[1], float(fps or 0))) if fps else None
        changed = bool(fps and before and fps != before)
        if changed:
            recorder.set_fps(fps)  # (not recording yet, the game not running: its start asks for it)
        try:
            yield changed
        finally:
            if changed:
                recorder.set_fps(before)

    def describe(self, wall0, wall1, questions, count=DEFAULT_FRAMES, t0=None, t1=None, label=""):
        """The vision model's description of up to count frames captured from wall0 to wall1 (and, given, showing game
        times t0 to t1), with questions. Returns (text, ok): ok False when nothing was described (text says why)."""
        why = self.unavailable()
        if why:
            return f"Can't look at the game's picture in this run: {why}.", False
        chosen, frames, why = self.frames_of(wall0, wall1, count, t0, t1, label)
        if why:
            return why, False
        questions = [str(q).strip() for q in (questions or []) if str(q).strip()][:MAX_QUESTIONS]
        started = time.time()
        try:
            text, model, usage = self._ask(chosen, questions)
        except Exception as e:  # noqa: BLE001 - the agent is told, and the run goes on
            return f"The frames couldn't be described: {type(e).__name__}: {str(e)[:300]}", False
        with self.lock:
            self.calls += 1
        self._log(chosen, questions, text, model, usage, time.time() - started, label)
        head = (f"{len(chosen)} frames of {len(frames)} ({label or 'the stretch asked for'}), at game times "
                + ", ".join(f"{f['t']:.2f}" if f["t"] is not None else "?" for f in chosen) + " s:")
        return f"{head}\n{text}", True

    def frames_of(self, wall0, wall1, count=DEFAULT_FRAMES, t0=None, t1=None, label=""):
        """(up to count frames captured from wall0 to wall1, spread over them, and given game times t0 to t1 shown;
        the frames there were to pick from; None) for describe() or the user's own agent to look at, or (None, None,
        why there are none)."""
        frames = self.frames.between(wall0, wall1) if self.frames is not None else []
        if t0 is not None or t1 is not None:
            lo = -1e18 if t0 is None else float(t0)
            hi = 1e18 if t1 is None else float(t1)
            shown = [f for f in frames if f["t"] is not None and lo <= float(f["t"]) <= hi]
            if not shown:
                times = [f["t"] for f in frames if f["t"] is not None]
                span = f" (its frames show game times {min(times):.2f} to {max(times):.2f} s)" if times else ""
                return None, None, f"No frames of {label or 'that stretch'} show game times {t0} to {t1}{span}."
            frames = shown
        self.no_picture = False
        if not frames:
            dropped = self.frames.dropped_before if self.frames is not None else None
            if dropped is not None and wall1 <= dropped:
                return None, None, (f"The frames of {label or 'that stretch'} aren't kept any more (only the last "
                                    "minutes of play are): play it again, then look at it.")
            self.no_picture = True  # (the game drew nothing then: its window minimized or hidden)
            return None, None, (f"No frames of {label or 'that stretch'}: the game wasn't filmed while it played "
                                f"({self.recorder.missing() or 'no picture came'}).")
        count = max(1, min(MAX_FRAMES, int(count or DEFAULT_FRAMES)))
        chosen = pick(frames, count)
        while len(chosen) > 1 and sum(len(f["jpeg"]) for f in chosen) * 4 / 3 > MAX_REQUEST_BYTES:
            chosen = pick(frames, len(chosen) - 1)
        return chosen, frames, None

    # -- the call ------------------------------------------------------------------------------------------

    def _content(self, frames, questions):
        """The request's parts in order: per frame its label and its picture, then the questions. (kind, value)."""
        parts = [("text", "Frames of a video game, in order, with the game time each shows:")]
        prev = None
        for i, f in enumerate(frames, 1):
            gap = f" ({f['t'] - prev:.2f} s after the one before)" if prev is not None and f["t"] is not None else ""
            t = f"{f['t']:.2f} s" if f["t"] is not None else "unknown"
            parts.append(("text", f"Frame {i}: game time {t}{gap}"))
            parts.append(("image", base64.standard_b64encode(f["jpeg"]).decode("ascii")))
            prev = f["t"] if f["t"] is not None else prev
        asked = "\n".join(f"{i}. {q}" for i, q in enumerate(questions, 1)) or "1. Describe what happens."
        parts.append(("text", f"Questions:\n{asked}\n{SWEEP}"))
        return parts

    def _ask(self, frames, questions):
        """(the description, the model that gave it, its usage)."""
        wire, root, key, model, path = self.route
        parts = self._content(frames, questions)
        if wire == "codex":
            text, usage = _chatgpt_client().look(SYSTEM, parts, model, EFFORT)
            if self.usage is not None and usage:
                self.usage.add("llm", model, **usage)
            return text or "(no description)", model, usage
        if wire == "anthropic":
            try:
                return self._anthropic(root, key, model, parts)
            except Exception as e:
                if not _refused_model(e) or model == FALLBACK:
                    raise
                self.route = (wire, root, key, FALLBACK, path)  # (for the rest of the run)
                return self._anthropic(root, key, FALLBACK, parts)
        return self._openai(root + path, key, model, parts)

    def _anthropic(self, root, key, model, parts):
        import anthropic

        # (a key in ANTHROPIC_AUTH_TOKEN alone is a bearer token, as Anthropic's SDKs read it)
        bearer = not (os.environ.get("ANTHROPIC_API_KEY") or "").strip() and key == os.environ.get(
            "ANTHROPIC_AUTH_TOKEN")
        client = anthropic.Anthropic(api_key=None if bearer else key, auth_token=key if bearer else None,
                                     base_url=root, timeout=TIMEOUT, max_retries=2)
        content = [{"type": "text", "text": v} if kind == "text" else
                   {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": v}}
                   for kind, v in parts]
        message = client.messages.create(model=model, max_tokens=MAX_TOKENS, system=SYSTEM,
                                         messages=[{"role": "user", "content": content}],
                                         output_config={"effort": EFFORT})
        if message.stop_reason == "refusal":
            raise RuntimeError("the vision model declined to describe these frames")
        text = "\n".join(b.text for b in message.content if b.type == "text").strip()
        body = message.to_dict()
        self._count(root, body)
        return text or "(no description)", message.model, body.get("usage")

    def _openai(self, url, key, model, parts):
        content = [{"type": "text", "text": v} if kind == "text" else
                   {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{v}"}}
                   for kind, v in parts]
        # (OpenAI's newer models take max_completion_tokens only; other APIs' compatible endpoints take max_tokens)
        limit = "max_completion_tokens" if "api.openai.com" in url or self.provider == "openai" else "max_tokens"
        r = requests.post(url, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                          json={"model": model, limit: MAX_TOKENS,
                                "messages": [{"role": "system", "content": SYSTEM},
                                             {"role": "user", "content": content}]},
                          timeout=TIMEOUT)
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
        body = r.json()
        self._count(url, body)
        choice = (body.get("choices") or [{}])[0]
        text = ((choice.get("message") or {}).get("content") or "").strip()
        return text or "(no description)", body.get("model") or model, body.get("usage")

    def _count(self, url, body):
        """The call's tokens into the run's usage, unless the run's proxy counted them."""
        if self.usage is None or (self.proxied and url.startswith(self.proxied)):
            return
        from .telemetry import llm_usage

        model, usage = llm_usage(body)
        if usage:
            self.usage.add("llm", model, **usage)

    def _log(self, frames, questions, text, model, usage, seconds, label):
        """One line of telemetry/describe.jsonl in a shared run: what was asked about which frames (by time: the
        pictures are in the recording), and what came back."""
        if not self.log:
            return
        line = {"wall": round(time.time(), 3), "label": label, "model": model, "questions": questions,
                "frames": [{"t": f["t"], "wall": round(f["wall"], 3), "width": f["width"], "height": f["height"]}
                           for f in frames],
                "description": text, "usage": usage, "seconds": round(seconds, 2)}
        try:
            with self.lock, open(os.path.join(self.log, LOG), "a", encoding="utf-8") as f:
                f.write(json.dumps(line, default=str) + "\n")
        except OSError as e:
            print(f"note: a description wasn't logged: {e}")


def _refused_model(e):
    """Whether an API error refused the model itself (unknown, or not available through Aura's relay)."""
    status = getattr(e, "status_code", None)
    text = str(e).lower()
    return status in (400, 404) and "model" in text
