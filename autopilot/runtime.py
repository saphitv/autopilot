"""Runs a harness on the live game: the tick loop, Jev's decisions, the executors, and the trace (see kit/harness.py
for the contract). One runtime plays every game; what differs per game is the harness.
"""

import concurrent.futures as futures
import importlib.util
import inspect
import json
import os
import sys
import threading
import time
import traceback
import weakref
from collections import Counter, defaultdict, deque

from typesafe_sdk import Choice, RetryPolicy

from .kit.geom import Vec, wrap
from .kit.harness import Harness, Option, Status, Step

DEFAULT_INSTRUCTIONS = (
    "You are the player of a video game. Choose the option to do now that best serves `goal`. `situation` holds facts "
    "about the game now. `options.<name>` holds facts about each option. `now_doing` is the option being carried out "
    "now: let it finish unless another option is clearly better now. `recent` lists the last options taken and "
    "whether they worked: don't keep repeating one that keeps failing in the same situation. `plan`, when present, "
    "is the goal's steps in order: do what `plan.now` needs; the steps in `plan.done` are done, don't redo them."
)
MAX_IN_FLIGHT = 2
MIN_ASK_GAP = 0.08  # seconds between two questions
# Jev's version, by its exact id rather than an alias (jev-latest moves when TypeSafe ships one): play only changes
# when this does, and Aura prices each id (an id it has no price for has its runs' bills refused). AUTOPILOT_JEV_MODEL
# overrides it, e.g. to evaluate a newer version
JEV_MODEL = "jev-1.13.0"
JEV_TIMEOUT = 1.5
KEEPALIVE = 120.0  # seconds an idle connection to Jev is kept
WARM_EVERY = 15.0  # seconds Jev may be idle before a keep-warm call
WARM_POLL = 1.0
WARM_TIMEOUT = 5.0  # (a server waking takes over a second: longer than a decision may wait)
MAX_CONSECUTIVE_ERRORS = 25
OBS_TRACE_EVERY = 1.0  # seconds of game time between observations written to the trace
STEP_SAMPLES = 4000  # executor step times kept per option (for the summary's median and p90)
SLOW_WAITS = 3.0  # an option whose steps wait on the game this many times on average is flagged in the summary
# seconds after the first observation by which a game held paused runs again, an option started or not
RESUME_AFTER = 1.5
# The player stops answering its inputs (dead, a menu or cutscene holding the controls) when the view doesn't turn
# though the mouse was moved to turn it by RESPONSE_MIN_TURN degrees or more, for UNRESPONSIVE_AFTER seconds
RESPONSE_EVERY = 1.0  # seconds of game time between checks
RESPONSE_MIN_TURN = 10.0
UNRESPONSIVE_AFTER = 2.0


def load_harness(path):
    """The Harness subclass defined last in the file at path, instantiated."""
    name = f"harness_{abs(hash((os.path.abspath(path), os.path.getmtime(path))))}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    classes = [c for c in vars(module).values()
               if inspect.isclass(c) and issubclass(c, Harness) and c is not Harness and c.__module__ == name]
    if not classes:
        raise ValueError(f"{path} defines no Harness subclass")
    return classes[-1]()


def jev_client(warm=True):
    """Jev's client. Its connection is kept open across idle stretches (the agent thinking between episodes, a long
    option), and while warm, a light call (the model list) is made at once and whenever Jev has been idle for
    WARM_EVERY: a decision made after an idle stretch, a dodge, never waits on a new connection or a server waking.
    Bodies go uncompressed: the SDK zstd-compresses them wherever zstd is importable (Python 3.14+), and Aura's relay
    doesn't pass Content-Encoding on to TypeSafe.

    With AUTOPILOT_DECIDER=luna, Luna answers instead, through Codex on the user's ChatGPT sign-in (autopilot/luna.py).
    """
    from . import luna

    if luna.enabled():
        return luna.LunaClient()
    import httpx2
    from dotenv import load_dotenv
    from typesafe_sdk import TypeSafeClient

    load_dotenv()
    used = [time.monotonic()]
    http = httpx2.Client(timeout=1.0, limits=httpx2.Limits(keepalive_expiry=KEEPALIVE),
                         event_hooks={"request": [lambda request: used.__setitem__(0, time.monotonic())]})
    client = TypeSafeClient(timeout=1.0, retry=RetryPolicy(max_retries=1, backoff_initial=0.05, timeout=JEV_TIMEOUT),
                            http_client=http, compression=False, model=jev_model())
    if warm:
        threading.Thread(target=_keep_warm, args=(weakref.ref(client), http, used), name="jev-warm",
                         daemon=True).start()
    return client


def jev_model():
    return (os.environ.get("AUTOPILOT_JEV_MODEL") or "").strip() or JEV_MODEL


def _keep_warm(ref, http, used):
    """Keeps Jev's connection open and its server awake until the client is closed or dropped."""
    first = True
    while not http.is_closed:
        if first or time.monotonic() - used[0] >= WARM_EVERY:
            client = ref()
            if client is None:
                return
            try:
                client.models.list(timeout=WARM_TIMEOUT, retry=RetryPolicy(max_retries=0, timeout=WARM_TIMEOUT))
            except Exception:
                pass  # (best effort: a decision opens its own connection)
            del client
            first = False
        time.sleep(WARM_POLL)
        if ref() is None:
            return


def jsonable(x, depth=0):
    """x made JSON-safe (Vecs as lists, objects as strings)."""
    if depth > 8:
        return str(x)
    if isinstance(x, dict):
        return {str(k): jsonable(v, depth + 1) for k, v in x.items()}
    if isinstance(x, (list, tuple, set)):
        return [jsonable(v, depth + 1) for v in x]
    if isinstance(x, float):
        return round(x, 3)
    if isinstance(x, (int, str, bool)) or x is None:
        return x
    if hasattr(x, "rounded"):
        return x.rounded()
    if hasattr(x, "__dataclass_fields__"):
        return jsonable(vars(x), depth + 1)
    return str(x)


class Trace:
    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.f = open(path, "w", encoding="utf-8")
        self.events = []
        self.lock = threading.Lock()  # (observe runs in its own thread, beside the executor)

    def __call__(self, kind, **fields):
        e = {"kind": kind, "wall": round(time.time(), 3), **jsonable(fields)}
        with self.lock:
            self.events.append(e)
            if not self.f.closed:
                self.f.write(json.dumps(e, ensure_ascii=False) + "\n")
                self.f.flush()

    def close(self):
        with self.lock:
            self.f.close()


class Running:
    def __init__(self, option, gen, obs, t, p):
        self.option, self.gen, self.before, self.t0, self.p = option, gen, obs, t, p
        self.wall0 = time.monotonic()


STILL_GAP = 2.0  # seconds between two stills of findings, at least


class Runtime:
    def __init__(self, game, harness, trace_path, client=None, max_seconds=120.0, say=None, stop=None, resume=None,
                 telemetry=None, progress=None):
        self.game, self.h = game, harness
        self.trace = Trace(trace_path)
        # The run's telemetry folder when its data is shared (autopilot/telemetry.py): each decision is kept whole
        # there, decisions<n>.jsonl beside trace<n>.jsonl, and stills of the game are taken (autopilot/media.py)
        self.telemetry = telemetry
        self.episode = os.path.splitext(os.path.basename(trace_path))[0]
        self.calls = None
        if telemetry:
            os.makedirs(telemetry, exist_ok=True)
            self.calls = open(os.path.join(telemetry, f"{self.episode.replace('trace', 'decisions')}.jsonl"), "a",
                              encoding="utf-8")
        self.call_ids = 0
        self.stills_at = {}  # finding signature -> wall time of its still
        self.last_still = 0.0
        self.log_before = game.log  # given back when the episode ends (probes after it log there)
        self.game.log = self.trace
        self.client = client
        self.max_seconds = max_seconds
        self.say = say or (lambda text: None)  # progress in words (the Autopilot tab's status line)
        self.stop = stop or (lambda: False)  # an outside request to stop
        # Set when the game is held paused through setup() and the first observation (play continued from where it
        # was: a player nobody drives yet would stand there, hit): called once, just before the player is first driven
        self.resume = resume
        # The previous episode's progress when this one continues it (run_harness restart=False, see
        # Harness.resume): the harness is told, and the steps done then start done
        self.progress = progress
        # The plan (kit.harness.Step list, read after setup): the steps done (name -> game time; None: before this
        # episode), the current step's index (None: every step done) and the game time it began
        self.plan, self.plan_done, self.plan_step, self.step_since = [], {}, None, None
        self.plan_seen = False  # (the first observation has placed the plan)
        self.done_failed, self.mismatched = set(), set()  # steps whose done() raised / whose options weren't legal
        # The stall watch: game time of the last progress (a step achieved, an option that worked; the first
        # observation to begin with), where the player stood then, what each option did since, and the stall the
        # episode ended on
        self.progress_t, self.stall_at, self.stall_tried, self.stalled = None, None, defaultdict(Counter), None
        self.t_start = None
        self.pool = futures.ThreadPoolExecutor(max_workers=MAX_IN_FLIGHT + 1, thread_name_prefix="jev")
        self.pending = []  # (future, asked_at, option names)
        self.running = None
        self.recent = deque(maxlen=max(1, getattr(harness, "recent_kept", 6)))
        self.stats = defaultdict(Counter)
        self.decisions = 0
        self.last_names = None
        self.last_ask = -1e9
        self.errors = Counter()
        self.idle_gen = None
        self.cooldown = {}  # option name -> game time until which it isn't offered (it kept failing at once)
        self.quick_fails = Counter()
        self.ending = []  # options ended, waiting for an observation begun after they did (settle_endings)
        self.response = {"t": -1e9, "yaw": None, "asked": 0.0, "frozen_since": None, "flagged": False}
        # Executor steps per option: how long each took and how many times it waited on the game (see Game.waits)
        self.steps = defaultdict(lambda: {"n": 0, "ms": [], "waits": 0, "max_waits": 0})
        self.ticks, self.wall_start = 0, None

    # -- Jev ---------------------------------------------------------------------------------------------

    def ask(self, obs, opts):
        h = self.h
        now_doing = None
        if self.running:
            now_doing = {"option": self.running.option.name,
                         "for_seconds": round(self.game.t - self.running.t0, 1)}
        state = {
            "goal": h.goal,
            "situation": jsonable(h.facts(obs)),
            "options": {o.name: jsonable(o.facts) for o in opts},
            "now_doing": now_doing,
            "recent": list(self.recent),
        }
        if self.plan:
            state["plan"] = self.plan_facts()
        questions = {"pick": Choice(instructions=h.instructions or DEFAULT_INSTRUCTIONS,
                                    criteria={o.name: o.description for o in opts})}
        try:
            questions.update(h.questions(obs) or {})
        except Exception as e:
            self.error("questions", e)
        names = [o.name for o in opts]
        t = self.game.t
        asked = time.monotonic()
        self.call_ids += 1
        meta = {"call": self.call_ids, "wall": round(time.time(), 3), "options": names}

        def call():
            r = self.client.system_one(state=state, questions=questions)
            return r, time.monotonic() - asked

        fut = self.pool.submit(call)
        sent = {"state": state,
                "questions": {k: q.model_dump(mode="json", exclude_none=True) for k, q in questions.items()}}
        self.pending.append((fut, asked, names, t, sent, meta))
        self.last_ask = asked

    def answers(self):
        """Answers that came back, oldest question first: [(probs, extra answers, latency, t, what was sent, meta)];
        meta: the call's id, when it was asked (wall clock), the options offered, and the answer as it came."""
        done = []
        keep = []
        for item in self.pending:
            fut = item[0]
            if not fut.done():
                if time.monotonic() - item[1] > JEV_TIMEOUT * 2.5:
                    self.trace("jev_error", error="no answer in time", t=self.game.t, call=item[5]["call"])
                    self.keep_call(item, error="no answer in time")
                    continue  # forget it
                keep.append(item)
                continue
            try:
                r, latency = fut.result()
            except Exception as e:
                self.errors[f"jev: {type(e).__name__}"] += 1
                self.trace("jev_error", error=f"{type(e).__name__}: {e}"[:300], t=self.game.t, call=item[5]["call"])
                self.keep_call(item, error=f"{type(e).__name__}: {e}"[:1000])
                continue
            pick = r.choices.get("pick")
            probs = dict(getattr(pick, "probabilities", None) or {}) if pick else {}
            if not probs and pick is not None:
                probs = {pick.choice: 1.0}
            extra = {k: a.noul for k, a in r.nouls.items()}
            for k, a in r.choices.items():
                if k != "pick":
                    extra[k] = dict(getattr(a, "probabilities", None) or {a.choice: 1.0})
            for k, a in r.scores.items():
                extra[k] = a.score
            meta = dict(item[5], latency=latency)
            if self.calls is not None:
                meta["response"] = answer_as_sent(r)
            done.append((probs, extra, latency, item[3], item[4], meta))
        self.pending = keep
        return done

    # -- options -----------------------------------------------------------------------------------------

    def start(self, option, obs, p=None, why="picked"):
        try:
            gen = option.run(self.game, obs)
        except Exception as e:
            self.error(f"option {option.name} start", e)
            self.finish_record(option, obs, obs, self.game.t, ok=False, why=f"error: {type(e).__name__}: {e}")
            return
        if not inspect.isgenerator(gen):
            # done at once
            self.finish_record(option, obs, obs, self.game.t, ok=True if gen is None else bool(gen),
                               why=str(getattr(gen, "why", "")) if gen is not None else "done")
            return
        self.running = Running(option, gen, obs, self.game.t, p)
        self.stats[option.name]["started"] += 1
        self.stall_tried[option.name]["started"] += 1
        self.trace("option_start", t=self.game.t, option=option.name, p=p, why=why, facts=option.facts)
        self.say(f"Now: {option.description[:80]}")

    def watch_response(self):
        """Whether the player still answers its inputs: the view turns when the mouse turns it. When it stops (a dead
        player, a menu or cutscene holding the controls, input blocked) for UNRESPONSIVE_AFTER s, an "unresponsive"
        event goes to the trace, and "responsive" when it turns again; the summary leads with these spans."""
        g, r = self.game, self.response
        try:
            yaw = g.view_rotation()[0]
        except Exception:
            return
        asked, now = g.turn_asked - r["asked"], g.t
        if r["yaw"] is not None and asked >= RESPONSE_MIN_TURN:
            turned = abs(wrap(yaw - r["yaw"]))
            if turned < min(1.0, 0.1 * asked):
                r["frozen_since"] = r["frozen_since"] if r["frozen_since"] is not None else r["t"]
                if not r["flagged"] and now - r["frozen_since"] >= UNRESPONSIVE_AFTER:
                    r["flagged"] = True
                    self.trace("unresponsive", t=r["frozen_since"], asked_degrees=round(asked),
                               turned_degrees=round(turned, 1))
            else:
                if r["flagged"]:
                    self.trace("responsive", t=now)
                r["frozen_since"], r["flagged"] = None, False
        r.update(t=now, yaw=yaw, asked=g.turn_asked)

    def timed_step(self, name, step):
        """step(), its time and the reads it waited on counted under name."""
        waits = getattr(self.game, "waits", None)
        w0, t0 = waits() if waits else 0, time.monotonic()
        try:
            step()
        finally:
            s = self.steps[name]
            n = (waits() - w0) if waits else 0
            s["n"] += 1
            s["waits"] += n
            s["max_waits"] = max(s["max_waits"], n)
            if len(s["ms"]) < STEP_SAMPLES:
                s["ms"].append((time.monotonic() - t0) * 1000.0)

    def step_report(self):
        """The steps in brief: per option, ms a step (median, p90, max) and waits on the game a step (mean, max)."""
        out = {}
        for name, s in self.steps.items():
            if not s["n"]:
                continue
            ms = sorted(s["ms"])
            out[name] = {"steps": s["n"], "median_ms": round(ms[len(ms) // 2], 1),
                         "p90_ms": round(ms[int(len(ms) * 0.9)], 1), "max_ms": round(ms[-1], 1),
                         "waits_mean": round(s["waits"] / s["n"], 2), "waits_max": s["max_waits"]}
        return out

    def stop_running(self, why):
        r = self.running
        if not r:
            return
        try:
            r.gen.close()
        except Exception as e:
            self.error(f"option {r.option.name} close", e)
        self.game.release_all()
        self.running = None
        self.stats[r.option.name]["interrupted"] += 1
        self.stall_tried[r.option.name]["interrupted"] += 1
        self.trace("option_end", t=self.game.t, option=r.option.name, seconds=round(self.game.t - r.t0, 2),
                   ok=None, why=why, interrupted=True)
        self.recent.append({"option": r.option.name, "seconds": round(self.game.t - r.t0, 1), "worked": None,
                            "how": f"stopped: {why}"})

    def finish_record(self, option, before, after, t0, ok, why, data=None, t_end=None):
        t_end = self.game.t if t_end is None else t_end  # (the game time it ended at)
        effect = None
        if option.effect is not None:
            try:
                effect = option.effect(before, after)
            except Exception as e:
                self.error(f"option {option.name} effect", e)
        worked = effect if effect is not None else ok
        # An option failing at once (a harness bug, or not possible now) is held back for a while, longer each time,
        # so play goes on with the others instead of retrying it every tick
        if not worked and t_end - t0 < 0.5:
            self.quick_fails[option.name] += 1
            hold = min(8.0, 0.5 * 2 ** (self.quick_fails[option.name] - 1))
            self.cooldown[option.name] = self.game.t + hold
            self.stats[option.name]["held_back"] += 1
        elif worked:
            self.quick_fails[option.name] = 0
        self.stats[option.name]["finished"] += 1
        how = "worked" if worked else ("failed" if worked is False else "unknown")
        self.stats[option.name][how] += 1
        self.stall_tried[option.name][how] += 1
        if worked:
            self.progressed(after)
        self.trace("option_end", t=t_end, option=option.name, seconds=round(t_end - t0, 2), ok=ok,
                   why=why, effect=effect, data=data)
        self.recent.append({"option": option.name, "seconds": round(t_end - t0, 1), "worked": worked,
                            "how": str(why)[:80]})

    def step_running(self, obs):
        r = self.running
        if not r:
            return
        if self.game.t - r.t0 > r.option.max_seconds:
            self.stop_running(f"took longer than {r.option.max_seconds:.0f}s")
            self.stats[r.option.name]["timed_out"] += 1
            return
        try:
            next(r.gen)
            return
        except StopIteration as stop:
            res = stop.value
            ok = bool(res) if res is not None else True
            why = getattr(res, "why", None) or ("done" if res is None else str(res)[:120])
            data = getattr(res, "data", None)
        except Exception as e:
            self.error(f"option {r.option.name}", e)
            ok, why, data = False, f"error: {type(e).__name__}: {e}"[:200], None
        self.running = None
        self.game.release_all()
        # Whether it worked is judged on the first observation begun after it ended (observe runs on its own: the
        # latest one may be older than what the option just did)
        self.ending.append((r.option, r.before, r.t0, ok, why, data, time.monotonic(), self.game.t))

    def settle_endings(self, obs, begun=None):
        """Record the options that ended before this observation began (all of them when begun is None)."""
        for e in list(self.ending):
            if begun is None or begun > e[6]:
                self.ending.remove(e)
                option, before, t0, ok, why, data, _, t_end = e
                self.finish_record(option, before, obs, t0, ok, why, data, t_end=t_end)

    def keep_call(self, item, **fields):
        """One decision model call, whole, in decisions<n>.jsonl (when the run's data is shared)."""
        if self.calls is None:
            return
        _, _, _, t, sent, meta = item[:6]
        line = {"call": meta["call"], "episode": self.episode, "t": t, "wall": meta["wall"],
                "options": meta.get("options"), "request": sent, **fields}
        try:
            self.calls.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
            self.calls.flush()
        except (OSError, ValueError):
            pass

    def apply(self, probs, extra, latency, opts_now, obs, asked_t, state, meta=None):
        self.decisions += 1
        by_name = {o.name: o for o in opts_now}
        live = {k: v for k, v in probs.items() if k in by_name}
        action = "dropped"
        chosen = max(live, key=live.get) if live else None
        try:
            self.h.on_answers(extra, obs)
        except Exception as e:
            self.error("on_answers", e)
        if chosen is None:
            pass
        elif self.running is None:
            self.start(by_name[chosen], obs, p=round(live[chosen], 3))
            action = "started"
        elif chosen == self.running.option.name:
            action = "kept"
            self.running.p = live[chosen]
        else:
            current = self.running.option.name
            margin = live[chosen] - live.get(current, 0.0)
            if self.running.option.interruptible and (by_name[chosen].urgent or margin >= self.h.switch_margin):
                self.stop_running(f"switched to {chosen}")
                self.start(by_name[chosen], obs, p=round(live[chosen], 3), why=f"switched from {current}")
                action = "switched"
            else:
                action = "kept_running"
        meta = meta or {}
        self.trace("decision", t=asked_t, applied_t=self.game.t, latency=round(latency, 3), probs=probs,
                   extra=extra, chosen=chosen, action=action, state=state.get("state"), call=meta.get("call"))
        if meta:
            self.keep_call((None, None, None, asked_t, state, meta), latency_s=round(latency, 4),
                           response=meta.get("response"), applied_t=self.game.t,
                           applied_wall=round(time.time(), 3), chosen=chosen, action=action,
                           running=self.running.option.name if self.running else None)
        self.inspect(state, probs, extra, latency, asked_t, chosen, action)

    def inspect(self, sent, probs, extra, latency, asked_t, chosen, action):
        """The decision to the editor's policy log (the Autopilot tab), when it asked for Jev's calls and is on this
        machine (docs/protocol.md, Inspection)."""
        link = self.game.link
        hello = getattr(link, "hello", None) or {}
        allowed = getattr(link, "loopback", False) or os.environ.get("AUTOPILOT_INSPECT") == "1"
        if not hello.get("inspect") or not allowed:
            return
        frame = {"type": "jev", "call": self.decisions, "kind": "play", "step": self.decisions, "t": asked_t,
                 "wall": time.time(), "goal": self.h.goal,
                 "plan_step": self.running.option.description[:120] if self.running else None,
                 "seconds": {"jev": round(latency, 3), "decide": round(latency, 3)},
                 "input": jsonable(sent), "output": {"answers": jsonable({"pick": probs, **extra})},
                 "decided": {"summary": f"{action}: {chosen}", "chosen": [chosen] if chosen else []},
                 "truncated": False, "dropped": 0}
        try:
            self.pool.submit(link.send, frame)
        except Exception:
            pass

    # -- the plan and the stall watch --------------------------------------------------------------------

    def current_step(self):
        """The plan's current step (kit.harness.Step): the first not yet done; None without a plan or when all are."""
        return self.plan[self.plan_step] if self.plan and self.plan_step is not None else None

    def plan_facts(self):
        """Where the plan stands, for Jev: the steps done, the current one (and how long it has been), the rest."""
        step = self.current_step()
        now = None
        if step is not None:
            since = self.step_since if self.step_since is not None else self.game.t
            now = {"step": step.name, "what": step.description, "for_seconds": round(self.game.t - since, 1)}
        i = self.plan_step if self.plan_step is not None else len(self.plan)
        return {"done": list(self.plan_done), "now": now, "next": [s.description for s in self.plan[i + 1:]]}

    def plan_update(self, obs):
        """Where the plan stands on this observation: the steps achieved so far and the current step (the first not
        yet achieved). A step achieved stays achieved for the episode, and is progress for the stall watch."""
        if not self.plan:
            return
        n = len(self.plan_done)
        step = plan_advance(self.plan, obs, self.plan_done, self.game.t, on_error=self.done_error)
        index = self.plan.index(step) if step is not None else None
        if not self.plan_seen or index != self.plan_step:
            was = self.current_step()
            self.trace("step", t=self.game.t, **{"from": was.name if was else None, "to": step.name if step else None},
                       done=list(self.plan_done))
            self.plan_seen, self.plan_step, self.step_since = True, index, self.game.t
            self.stats_at_step = {k: Counter(v) for k, v in self.stats.items()}  # (what each option did since)
            self.say(f"Plan: {step.description[:70]}" if step else "Plan: every step is done")
        if len(self.plan_done) > n:
            self.progressed(obs)

    def done_error(self, step, e):
        """A step's done() raised (once per step in the trace): it counts as not done."""
        if step.name not in self.done_failed:
            self.done_failed.add(step.name)
            self.error(f"plan step {step.name} done()", e)

    def plan_offer(self, opts):
        """The options Jev is offered in the current step: those the step names plus urgent ones (step_options); all
        when none of the named ones is legal now, which goes to the trace once per step (a plan_mismatch event)."""
        step = self.current_step()
        offered, mismatch = step_options(step, opts)
        if mismatch and step.name not in self.mismatched:
            self.mismatched.add(step.name)
            self.trace("plan_mismatch", t=self.game.t, step=step.name, listed=mismatch, legal=[o.name for o in opts])
        return offered

    def progressed(self, obs):
        """Progress (a step achieved, an option that worked): the stall watch starts over from now and from where
        the player stands."""
        self.progress_t = self.game.t
        self.stall_at = pose_at(obs)
        self.stall_tried = defaultdict(Counter)

    def stall_info(self, obs):
        """The stall the episode ends on: the step it stood at, since when, what each option did meanwhile, and where
        the player stood when it began and now (when the observation has a "_pose" with "at")."""
        step = self.current_step()
        now = pose_at(obs)
        player = None
        if self.stall_at is not None and now is not None:
            player = {"at": [int(v) for v in self.stall_at.rounded()], "now": [int(v) for v in now.rounded()],
                      "moved": round(float(now.dist(self.stall_at)))}
        return {"kind": "no progress", "step": step.name if step else None, "since_t": round(self.progress_t, 2),
                "seconds": round(self.game.t - self.progress_t, 1),
                "tried": {k: dict(v) for k, v in self.stall_tried.items()}, "player": player}

    # -- the loop ----------------------------------------------------------------------------------------

    def error(self, where, e):
        key = f"{where}: {type(e).__name__}: {str(e)[:120]}"
        self.errors[key] += 1
        if self.errors[key] <= 3:
            self.trace("error", t=self.game.t, where=where, error=f"{type(e).__name__}: {e}"[:500],
                       traceback=traceback.format_exc(limit=6)[-1500:])

    def on_event(self, event):
        """The plugin's events during the episode: its detectors' findings and the engine's error log lines go to the
        trace (what looked wrong in the game, for verification and exploration)."""
        kind = event.get("kind")
        if kind == "finding":
            f = event.get("finding") or {}
            self.trace("finding", t=event.get("t"), detector=f.get("detector"), severity=f.get("severity"),
                       summary=f.get("summary"), actor=f.get("actor"), signature=f.get("signature"))
            key, now = f.get("signature") or f.get("summary"), time.time()
            if key not in self.stills_at and now - self.last_still >= STILL_GAP:
                self.stills_at[key], self.last_still = now, now
                self.still("finding", signature=key, detector=f.get("detector"), summary=f.get("summary"))
        elif kind == "log":
            self.trace("game_log", t=event.get("t"), verbosity=event.get("verbosity"), category=event.get("category"),
                       message=str(event.get("message"))[:300])

    def run(self):
        g = self.game
        listeners = getattr(g.link, "listeners", None)
        if isinstance(listeners, list):
            listeners.append(self.on_event)
        try:
            return self._run()
        finally:
            if isinstance(listeners, list) and self.on_event in listeners:
                listeners.remove(self.on_event)

    def still(self, reason, wait=False, **extra):
        """A still of the game now, kept with the run's data (only when it is shared)."""
        if not self.telemetry:
            return
        from . import media

        args = (self.game.link, self.telemetry, reason, {"episode": self.episode, **extra}, self.game.t)
        (media.still if wait else media.still_later)(*args)

    def _run(self):
        g, h = self.game, self.h
        self.trace("start", harness=type(h).__name__, goal=h.goal, tempo=h.tempo, tick_hz=h.tick_hz,
                   max_seconds=self.max_seconds)
        self.still("episode_start")
        end = {"reason": "", "status": None}
        try:
            h.setup(g)
        except Exception as e:
            self.error("setup", e)
            end["reason"] = f"setup failed: {type(e).__name__}: {e}"
            return self.finish(end, None)
        if self.progress is not None:  # (this episode continues the one before: play went on from where it was)
            try:
                h.resume(g, self.progress)
            except Exception as e:
                self.error("harness resume", e)
            self.trace("continued", t=g.t, progress=self.progress)
        self.plan = [s for s in (getattr(h, "plan", None) or []) if isinstance(s, Step)]
        if self.plan:
            names = {s.name for s in self.plan}
            for name in (self.progress or {}).get("done") or []:
                if name in names:
                    self.plan_done[name] = None  # (done before this episode)
            self.trace("plan", t=g.t, done=list(self.plan_done), stall_seconds=h.stall_seconds,
                       steps=[{"name": s.name, "what": s.description, "options": s.options} for s in self.plan])
        t_start, wall_start = g.t, time.monotonic()
        self.t_start, self.wall_start = t_start, wall_start
        first_obs = None  # (when the first observation came in: wall clock)
        tick = 1.0 / max(1.0, h.tick_hz)
        consecutive = 0
        last_obs_trace = -1e9
        obs, opts, names = None, [], []
        status = Status()
        # The harness's observe() runs in a thread of its own while the running executor steps every tick with the
        # latest observation: an observe that takes long (on a machine that runs the game slowly every read waits for
        # a frame) doesn't slow the aiming and walking down with it
        observer = futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="observe")
        seen = None

        def look():
            begun = time.monotonic()
            o = h.observe(g)
            st = h.status(o) or Status()
            op = [x for x in (h.options(o) or []) if isinstance(x, Option) and self.cooldown.get(x.name, -1e9) <= g.t]
            return o, st, op, begun

        try:
            while True:
                t0 = time.monotonic()
                if self.stop():
                    end["reason"] = "stopped"
                    break
                if g.link.closed:
                    end["reason"] = f"the game disconnected: {g.link.closed}"
                    break
                g.renew()
                if seen is None:
                    seen = observer.submit(look)
                if seen.done():
                    try:
                        obs, status, opts, begun = seen.result()
                        consecutive = 0
                        self.settle_endings(obs, begun)
                    except Exception as e:
                        consecutive += 1
                        self.error("observe/status/options", e)
                        seen = None
                        try:
                            if not g.context(fresh=True).get("character"):
                                status = Status("failed", "the player is gone (died or despawned)")
                                end["reason"] = "the player is gone"
                                break
                        except Exception:
                            pass
                        if consecutive >= MAX_CONSECUTIVE_ERRORS:
                            end["reason"] = f"observe kept failing: {type(e).__name__}: {e}"
                            break
                        time.sleep(tick)
                        continue
                    seen = None
                    if self.progress_t is None:  # (the first observation: the stall watch starts here)
                        self.progress_t, self.stall_at = g.t, pose_at(obs)
                    self.plan_update(obs)
                    opts = self.plan_offer(opts)
                    names = [o.name for o in opts]
                    if g.t - self.response["t"] >= RESPONSE_EVERY:
                        self.watch_response()
                    if g.t - last_obs_trace >= OBS_TRACE_EVERY:
                        last_obs_trace = g.t
                        step = self.current_step()
                        self.trace("obs", t=g.t, facts=h.facts(obs), status=status.state, note=status.note,
                                   options=names, running=self.running.option.name if self.running else None,
                                   step=step.name if step else None)
                    if status.over:
                        end["reason"] = f"status {status.state}"
                        break
                if obs is None:  # (the first observation isn't in yet)
                    time.sleep(min(tick, 0.02))
                    continue
                first_obs = first_obs or time.monotonic()
                if g.t - t_start > self.max_seconds or time.monotonic() - wall_start > self.max_seconds * 3 + 30:
                    end["reason"] = "time limit"
                    break
                step_long = (self.current_step() is not None and getattr(h, "step_seconds", 0) > 0
                             and self.step_since is not None and g.t - self.step_since >= h.step_seconds)
                if (h.stall_seconds > 0 and g.t - self.progress_t >= h.stall_seconds) or step_long:
                    # Nothing advanced the plan and no option worked for stall_seconds of play, or one step has
                    # taken step_seconds though options kept working (a loop that gathers what is lost again, counts
                    # the wrong thing): the episode ends here, with what was tried, so the agent revises the plan's
                    # continuation (status stays as the harness says: the goal isn't failed, it isn't reached either)
                    self.stalled = self.stall_info(obs)
                    if step_long:  # (what each option did since the step began, not since the last progress)
                        before = getattr(self, "stats_at_step", {})
                        tried = {name: {how: n - before.get(name, Counter()).get(how, 0) for how, n in c.items()
                                        if n - before.get(name, Counter()).get(how, 0) > 0}
                                 for name, c in self.stats.items()}
                        self.stalled.update(kind="step", seconds=round(g.t - self.step_since, 1),
                                            since_t=round(self.step_since, 2),
                                            tried={k: v for k, v in tried.items() if v})
                    self.trace("stalled", t=g.t,
                               **{("cause" if k == "kind" else k): v for k, v in self.stalled.items()})
                    end["reason"] = "stalled"
                    break
                if self.ending and not self.running:
                    # An option just ended: the next decision waits for an observation begun after it, so it isn't
                    # made on a world from before what the option did (an item it took still lying there)
                    spent = time.monotonic() - t0
                    if spent < tick:
                        time.sleep(tick - spent)
                    continue
                # A running option the harness no longer lists keeps going: its executor ends it (or max_seconds, or a
                # switch Jev makes). Until then Jev is offered it as it started, so its pick and the margin work as
                # usual
                offered = lambda: (opts + [self.running.option]
                                   if self.running and self.running.option.name not in names else opts)
                # Answers in
                for probs, extra, latency, asked_t, state, meta in self.answers():
                    self.apply(probs, extra, latency, offered(), obs, asked_t, state, meta)
                # Ask?
                now = time.monotonic()
                changed = names != self.last_names
                self.last_names = names
                due = (self.running is None or changed
                       or (h.tempo > 0 and now - self.last_ask >= 1.0 / h.tempo))
                if due and len(self.pending) < MAX_IN_FLIGHT and now - self.last_ask >= MIN_ASK_GAP:
                    if len(opts) == 1 and self.running is None:
                        self.start(opts[0], obs, p=1.0, why="the only option")
                        self.last_ask = now
                    elif len(offered()) >= 2 and self.client is not None and (self.running is None or not self.pending):
                        self.ask(obs, offered())
                # A game held paused runs again once there is something to drive the player with (or soon anyway)
                if self.resume is not None:
                    if not self.running and time.monotonic() - first_obs < RESUME_AFTER:
                        spent = time.monotonic() - t0
                        if spent < tick:
                            time.sleep(tick - spent)
                        continue
                    resume, self.resume = self.resume, None
                    try:
                        resume()
                    except Exception as e:
                        self.error("resume", e)
                    self.trace("resumed", t=g.t, held_seconds=round(time.monotonic() - wall_start, 2),
                               option=self.running.option.name if self.running else None)
                # Act: every tick, with the latest observation
                self.ticks += 1
                if self.running:
                    self.idle_gen = None
                    self.timed_step(self.running.option.name, lambda: self.step_running(obs))
                else:
                    self.timed_step("(idle)", lambda: self.idle_step(obs))
                # Keep the tick rate
                spent = time.monotonic() - t0
                if spent < tick:
                    time.sleep(tick - spent)
        finally:
            observer.shutdown(wait=True)  # (an observe in flight ends before teardown)
        return self.finish(end, status, obs)

    def idle_step(self, obs):
        try:
            if self.idle_gen is None:
                self.idle_gen = self.h.idle(self.game, obs)
            if inspect.isgenerator(self.idle_gen):
                next(self.idle_gen)
        except StopIteration:
            self.idle_gen = None
        except Exception as e:
            self.idle_gen = None
            self.error("idle", e)

    def finish(self, end, status, obs=None):
        if self.running:
            self.stop_running(f"episode ended ({end['reason']})")
        self.settle_endings(obs)
        try:
            self.game.release_all()
        except Exception:
            pass
        try:
            self.h.teardown(self.game)
        except Exception as e:
            self.error("teardown", e)
        result = {
            "reason": end["reason"],
            "status": jsonable(status) if status else None,
            "seconds": None,
            "decisions": self.decisions,
            "options": {k: dict(v) for k, v in self.stats.items()},
            "errors": dict(self.errors),
            "steps": self.step_report(),
            "ticks": {"asked_hz": self.h.tick_hz,
                      "ran_hz": round(self.ticks / max(1e-3, time.monotonic() - self.wall_start), 1)
                      if self.wall_start else None},
            "writes": self.game.writes,
            "last_facts": jsonable(self.h.facts(obs)) if obs is not None else None,
        }
        # Where the plan stands, for the next episode to go on from (Harness.resume; the session fills "episode")
        step = self.current_step()
        result["progress"] = {"episode": None, "done": list(self.plan_done), "step": step.name if step else None,
                              "t": round(self.game.t - self.t_start, 1) if self.t_start is not None else None,
                              "stalled": self.stalled, "last_facts": result["last_facts"]}
        self.trace("end", t=self.game.t, **result)
        self.still("episode_end", wait=True, ended=end["reason"])
        self.trace.close()
        if self.calls is not None:
            self.calls.close()
        self.game.log = self.log_before
        self.pool.shutdown(wait=False, cancel_futures=True)
        return result


def answer_as_sent(r):
    """A decision model's answer as its API gave it (model, usage, answers), JSON-safe."""
    raw = getattr(r, "raw", None)
    if raw is not None:
        return raw
    try:
        return r.model_dump(mode="json")
    except Exception:
        return None


def pose_at(obs):
    """Where the player stands by an observation's "_pose" (a kit pose: its "at", a Vec or {"X","Y","Z"}), as a Vec;
    None when the observation doesn't say."""
    pose = obs.get("_pose") if isinstance(obs, dict) else None
    try:
        return Vec.of(pose["at"]) if isinstance(pose, dict) and pose.get("at") is not None else None
    except (TypeError, ValueError, KeyError):
        return None


def plan_advance(plan, obs, done, t, on_error=None):
    """Moves the plan (kit.harness.Step list) on by an observation: from the first step not in `done`, every step
    whose done(obs) holds is added to done (name -> t) until one doesn't. Returns the current step (the first not
    done), or None when every step is. A done() that raises counts as not done (on_error(step, error))."""
    for step in plan:
        if step.name in done:
            continue
        try:
            achieved = bool(step.done(obs))
        except Exception as e:
            achieved = False
            if on_error is not None:
                on_error(step, e)
        if not achieved:
            return step
        done[step.name] = t
    return None


def step_options(step, opts):
    """(the options legal in step, the mismatch): those the step names plus urgent ones; all of them when the step is
    None or names none. When none of the named ones is legal now the player isn't stranded: all are offered, and the
    names the step listed come back as the mismatch (else None)."""
    if step is None or step.options is None:
        return opts, None
    if not opts or any(o.name in step.options for o in opts):
        return [o for o in opts if o.name in step.options or o.urgent], None
    return opts, list(step.options)


CHECK_SECONDS = 8.0  # how long check() lets each option's executor run, at most
CHECK_MAX_OPTIONS = 12  # the most options one check() tries


def check(game, harness, seconds=CHECK_SECONDS, restart=True, only=None):
    """Try a harness out without Jev, as a probe can: start the level over (restart), run setup(), read the first
    observation (facts, status, the options legal now, where the plan stands), then run each option's executor alone,
    one after another, for up to `seconds` each, and say how each ended: its Result, its effect, what changed in the
    facts, the errors, a plan step it achieved. With a plan the options tried are the current step's (plus urgent
    ones), as Jev would be offered them; only: the option names to try instead. It isn't an episode: nothing Jev
    decides, nothing that settles the request. Returns the report as text."""
    from .kit.control import Result as _Result

    out = []
    done = {}  # plan steps achieved: name -> game time
    noted = set()  # steps whose listed options weren't legal (said once)

    def facts_of(obs):
        try:
            return jsonable(harness.facts(obs))
        except Exception as e:
            return {"(facts failed)": f"{type(e).__name__}: {e}"}

    def look():
        obs = harness.observe(game)
        opts = [o for o in (harness.options(obs) or []) if isinstance(o, Option)]
        step = plan_advance(plan, obs, done, game.t, on_error=lambda s, e: out.append(
            f"  plan step {s.name}: done() raised {type(e).__name__}: {e} (it counts as not done)"))
        return obs, harness.status(obs) or Status(), opts, step

    def standing(step):
        if step is None:
            return f"every step is done: {list(done)}"
        return f"at step {plan.index(step) + 1} of {len(plan)}, {step.name} ({step.description}); done: {list(done)}"

    def to_try(step, opts):
        """The names tried from these options: `only`, else what Jev would be offered in the current step."""
        if only is not None:
            return [o.name for o in opts if o.name in only]
        offered, mismatch = step_options(step, opts)
        if mismatch and step.name not in noted:
            noted.add(step.name)
            out.append(f"  PLAN MISMATCH: step {step.name} lists options {mismatch} but none of them is legal now: "
                       "every legal option is offered")
        return [o.name for o in offered]

    if restart:
        if not game.restart(map=getattr(harness, "start_map", None), wait_for_pawn=10.0):
            out.append("After the restart there was no player pawn within 10 s (a menu? a loading screen?).")
    game.pause(False)
    try:
        harness.setup(game)
    except Exception:
        return "\n".join(out + ["setup() failed:\n" + traceback.format_exc(limit=6)])
    out.append("setup() ran.")
    plan = [s for s in (getattr(harness, "plan", None) or []) if isinstance(s, Step)]
    try:
        obs, status, opts, step = look()
    except Exception:
        return "\n".join(out + ["observe/status/options failed on the first observation:\n"
                                + traceback.format_exc(limit=6)])
    out.append(f"First observation: status={status.state}{f' ({status.note})' if status.note else ''}")
    if status.over:
        out.append("  THE EPISODE WOULD END AT ONCE: status is not running at the start.")
    out.append("  facts: " + json.dumps(facts_of(obs), default=str)[:1500])
    out.append(f"  options legal now: {[o.name for o in opts]}"
               + ("" if len(opts) >= 2 else "  (fewer than 2: Jev has nothing to choose between here)"))
    if plan:
        out.append(f"  plan: {standing(step)}")
    queue = to_try(step, opts)
    if plan and queue != [o.name for o in opts]:
        out.append(f"  tried (the current step's options): {queue}")
    tried = []
    while queue and len(tried) < CHECK_MAX_OPTIONS:
        name = queue.pop(0)
        tried.append(name)
        try:
            obs, status, opts, step = look()
        except Exception:
            out.append(f"\n{name}: observe failed before it:\n" + traceback.format_exc(limit=4))
            continue
        option = next((o for o in opts if o.name == name), None)
        if option is None:
            out.append(f"\n{name}: not legal any more (after the options before it)")
            continue
        f0, t0, w0 = facts_of(obs), game.t, time.monotonic()
        result, error = None, None
        try:
            gen = option.run(game, obs)
            if inspect.isgenerator(gen):
                limit = min(float(seconds), option.max_seconds)
                while True:
                    if time.monotonic() - w0 > limit * 3 + 5 or game.t - t0 > limit:
                        gen.close()
                        result = _Result(False, f"still running after {limit:.0f} s (check's limit)")
                        break
                    game.renew()
                    try:
                        next(gen)
                    except StopIteration as s:
                        result = s.value
                        break
                    time.sleep(1.0 / max(1.0, harness.tick_hz))
            else:
                result = gen
        except Exception:
            error = traceback.format_exc(limit=5)
        finally:
            try:
                game.release_all()
            except Exception:
                pass
        later, then = [], None
        try:
            after, now, later, then = look()
            f1 = facts_of(after)
            effect = option.effect(obs, after) if option.effect is not None else None
        except Exception as e:
            after, f1, effect = None, {}, f"(effect failed: {type(e).__name__}: {e})"
        changed = {k: [f0.get(k), f1.get(k)] for k in set(f0) | set(f1)
                   if isinstance(f0, dict) and isinstance(f1, dict) and f0.get(k) != f1.get(k)}
        how = ("error" if error else "ok" if (result is None or bool(result)) else "failed")
        why = getattr(result, "why", None) or ("" if result is None else str(result)[:160])
        out.append(f"\n{name}: {how} in {game.t - t0:.1f} s game time{f': {why}' if why else ''}; "
                   f"effect={effect}")
        if error:
            out.append("  " + error.strip().replace("\n", "\n  ")[-1200:])
        out.append("  facts changed: " + (json.dumps(changed, default=str)[:900] if changed else "(none)"))
        if after is None:
            continue
        if plan and then is not step:
            out.append(f"  plan moved on: {standing(then)}")
        new = [n for n in to_try(then, later) if n not in tried and n not in queue]
        if new:  # (options that only became legal now: a later stage of play, the next step)
            out.append(f"  legal now as well: {new} (tried next)")
            queue += new
        if now.over:
            out.append(f"  status is {now.state} now{f' ({now.note})' if now.note else ''}: the check stops here.")
            break
    try:
        harness.teardown(game)
    except Exception as e:
        out.append(f"teardown() failed: {type(e).__name__}: {e}")
    out.append("\n(Options were run one after another, each from where the one before left the game. The level is "
               "restarted by the next run_harness.)")
    return "\n".join(out)


def summarize(trace_path, max_timeline=50, error_log=False):
    """The trace in brief, for the implementer: how each option fared, the timeline, decisions, errors, findings.
    error_log: the game's errors were captured in play (Engine.error_log), so an episode without any says so."""
    events = []
    with open(trace_path, encoding="utf-8") as f:
        for line in f:
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
    if not events:
        return "(empty trace)"
    out = []
    start = next((e for e in events if e["kind"] == "start"), {})
    end = next((e for e in reversed(events) if e["kind"] == "end"), None)
    t_first = next((e.get("t") for e in events if e.get("t") is not None), 0) or 0
    out.append(f"Harness {start.get('harness')}  goal: {start.get('goal')!r}")
    if end:
        st = end.get("status") or {}
        out.append(f"Ended: {end.get('reason')} at t={end.get('t', 0) - t_first:.1f}s  status={st.get('state')} "
                   f"note={st.get('note')!r} evidence={json.dumps(st.get('evidence'))[:400]}")
        out.append(f"Decisions by Jev: {end.get('decisions')}")
    else:
        out.append("Ended: (no end event: the run crashed or is still going)")
    spans, since = [], None
    for e in events:
        if e["kind"] == "unresponsive" and since is None:
            since = e.get("t") or 0
        elif e["kind"] == "responsive" and since is not None:
            spans.append(f"t={since - t_first:.1f}-{(e.get('t') or 0) - t_first:.1f}s")
            since = None
    if since is not None:
        spans.append(f"t={since - t_first:.1f}s to the end")
    if spans:
        out.append(f"THE PLAYER DIDN'T ANSWER ITS INPUTS: {', '.join(spans)}: the view didn't turn though the mouse "
                   "turned it. Dead? A menu, cutscene or other screen holding the controls? Find out (the player's "
                   "state, the screen) before reading what options did then: their failures there say nothing of them.")
    # the plan: each step done (when) or not reached, the current one at the end, and the stall it ended on
    plan = next((e for e in events if e["kind"] == "plan"), None)
    progress = (end or {}).get("progress") or {}
    if plan and plan.get("steps"):
        before = set(plan.get("done") or [])  # (done before this episode: it continues the one before)
        done_at = {}
        for e in events:
            if e["kind"] == "step":
                for name in e.get("done") or []:
                    done_at.setdefault(name, e.get("t") or 0)
        out.append("\nPlan (the steps in order):")
        for i, s in enumerate(plan["steps"], 1):
            name = s["name"]
            how = ("done before this episode" if name in before
                   else f"done at t={done_at[name] - t_first:.1f}s" if name in done_at else "not reached")
            if name == progress.get("step"):
                how += "  <- the current step at the end"
            out.append(f"  {i}. {name}: {how}  ({s.get('what')})")
        for e in events:
            if e["kind"] == "plan_mismatch":
                out.append(f"  PLAN MISMATCH at step {e.get('step')}: it lists options {e.get('listed')} but the legal "
                           f"options were {e.get('legal')}, so all of them were offered. Name the options the step "
                           "really has, or make them legal in it.")
    stalled = progress.get("stalled")
    if stalled:
        tried = "; ".join(f"{name} " + ", ".join(f"{how} {n}" for how, n in c.items())
                          for name, c in (stalled.get("tried") or {}).items()) or "nothing"
        player = stalled.get("player")
        moved = (f"the player moved {player.get('moved')} units (from {player.get('at')} to {player.get('now')})"
                 if player else "where the player stood isn't known (no _pose in the observation)")
        at = f" at step {stalled['step']}" if stalled.get("step") else ""
        since = (stalled.get("since_t") or 0) - t_first
        why = (f"the step has taken that long though options worked (since the last one that worked: {tried}): what "
               "they achieve doesn't get the step done (what they gather is lost again, the wrong thing is counted, "
               "or the step's done() reads the wrong state)"
               if stalled.get("kind") == "step" else
               f"nothing advanced the plan and no option worked (tried: {tried})")
        out.append(f"STALLED{at} for {stalled.get('seconds')} s (from t={since:.1f}s): {why}; {moved}. "
                   + ("THE PLAYER DIDN'T ANSWER ITS INPUTS MEANWHILE: first find out whether it is dead (its health, "
                      "the screen's texts) or a menu holds the controls; a dead player can't be continued (make "
                      "status() say failed when it dies, and run_harness with a restart), a menu is closed in a probe. "
                      "Otherwise look" if spans else "Look")
                   + " at where the player is and what blocked it (describe_frames if it can, a probe), then revise "
                   "the plan's continuation (add or change the steps from here on, or an option's approach) and go "
                   "on from this state with run_harness(restart=False): the level is not restarted and the steps "
                   "already done stay done.")
    # options
    out.append("\nOptions (started / worked / failed / unknown / interrupted / timed out):")
    stats = (end or {}).get("options") or {}
    for name, s in sorted(stats.items(), key=lambda kv: -kv[1].get("started", 0)):
        out.append(f"  {name}: {s.get('started', 0)} / {s.get('worked', 0)} / {s.get('failed', 0)} / "
                   f"{s.get('unknown', 0)} / {s.get('interrupted', 0)} / {s.get('timed_out', 0)} / "
                   f"{s.get('held_back', 0)}")
    whys = defaultdict(Counter)
    for e in events:
        if e["kind"] == "option_end":
            whys[e["option"]][str(e.get("why"))[:80]] += 1
    for name, c in whys.items():
        out.append(f"  {name} ended because: " + "; ".join(f"{w} x{n}" for w, n in c.most_common(4)))
    # executor steps: how long they take and how many reads they wait on one after another
    steps = (end or {}).get("steps") or {}
    if steps:
        ticks = (end or {}).get("ticks") or {}
        asked = ticks.get("asked_hz") or start.get("tick_hz") or 15.0
        out.append(f"\nExecutor steps (steps, ms a step median/p90/max, waits on the game a step mean/max; a tick is "
                   f"{1000 / max(1.0, asked):.0f} ms at tick_hz {asked:g}, "
                   f"the loop ran {ticks.get('ran_hz')} ticks/s):")
        slow = []
        for name, s in sorted(steps.items(), key=lambda kv: -kv[1]["steps"]):
            out.append(f"  {name}: {s['steps']} steps, {s['median_ms']:.0f}/{s['p90_ms']:.0f}/{s['max_ms']:.0f} ms, "
                       f"{s['waits_mean']:.1f}/{s['waits_max']} waits")
            if s["steps"] >= 10 and s["waits_mean"] >= SLOW_WAITS:
                per = s["median_ms"] / max(1.0, s["waits_mean"])
                slow.append(f"{name} (~{s['median_ms']:.0f} ms a step, {s['waits_mean']:.1f} waits of ~{per:.0f} ms)")
        if slow:
            out.append("  SLOW STEPS: " + "; ".join(slow) + ". Every read waits for the game's answer (about a frame) "
                       "and reads one after another add up: a step reacts to what it read that long ago, and the next "
                       "step comes that much later. Gather a step's reads into one game.batch([...]) (one wait) and "
                       "read only what the step needs. A reaction timed to the game (dodging before a hit lands, "
                       "jumping at an edge) must allow for the step time, which changes with the frame rate.")
    # timeline
    out.append("\nTimeline (t: option, p, how it ended, seconds):")
    lines = []
    starts = {}
    for e in events:
        if e["kind"] == "option_start":
            starts[e["option"]] = e
        elif e["kind"] == "option_end":
            s = starts.pop(e["option"], {})
            p = s.get("p")
            ended = ("worked" if e.get("effect") or (e.get("effect") is None and e.get("ok"))
                     else "FAILED" if e.get("ok") is False or e.get("effect") is False else "stopped")
            lines.append(f"  t={(s.get('t') or e.get('t') or 0) - t_first:6.1f} {e['option']}"
                         f"{f' p={p:.2f}' if isinstance(p, (int, float)) else ''} -> "
                         f"{ended}"
                         f" ({str(e.get('why'))[:70]}) {e.get('seconds')}s")
    if len(lines) > max_timeline:
        lines = (lines[: max_timeline // 2] + [f"  ... {len(lines) - max_timeline} more ..."]
                 + lines[-max_timeline // 2:])
    out += lines
    # decisions
    dec = [e for e in events if e["kind"] == "decision"]
    if dec:
        lat = sorted(d["latency"] for d in dec)
        acts = Counter(d["action"] for d in dec)
        out.append(f"\nJev: {len(dec)} answers, latency median {lat[len(lat) // 2]:.2f}s; actions {dict(acts)}")
        first = dec[0]
        out.append("First decision Jev saw (state): " + json.dumps(first.get("state"))[:1500])
        out.append("Its answer: " + json.dumps(first.get("probs")))
    jerr = [e for e in events if e["kind"] == "jev_error"]
    if jerr:
        out.append(f"Jev errors: {len(jerr)} e.g. {jerr[0].get('error')}")
    # status changes
    obs = [e for e in events if e["kind"] == "obs"]
    if obs:
        out.append("\nSituation over time (every ~5 s):")
        last_t = -1e9
        for o in obs:
            if o["t"] - last_t >= 5 or o is obs[-1]:
                last_t = o["t"]
                out.append(f"  t={o['t'] - t_first:6.1f} {json.dumps(o.get('facts'))[:300]}  [{o.get('status')}] "
                           f"options={o.get('options')}" + (f" step={o['step']}" if o.get("step") else ""))
    errs = [e for e in events if e["kind"] == "error"]
    if errs:
        out.append("\nErrors in harness code:")
        seen = set()
        for e in errs:
            k = (e.get("where"), e.get("error"))
            if k in seen:
                continue
            seen.add(k)
            out.append(f"  [{e.get('where')}] {e.get('error')}\n    " + (e.get("traceback") or "").strip().replace(
                "\n", "\n    ")[-700:])
            if len(seen) >= 6:
                break
    finds = [e for e in events if e["kind"] in ("finding", "game_log")]
    if finds:
        out.append("\nWhat the game's own detectors and error log reported (count, first time):")
        groups = {}
        for e in finds:
            key = e.get("signature") or e.get("summary") or e.get("message")
            group = groups.setdefault(key, {"n": 0, "first": e})
            group["n"] += 1
        for key, group in sorted(groups.items(), key=lambda kv: -kv[1]["n"])[:15]:
            e = group["first"]
            what = e.get("summary") or f"[{e.get('verbosity')}] {e.get('category')}: {e.get('message')}"
            out.append(f"  x{group['n']} t={(e.get('t') or 0) - t_first:.1f} {e['kind']}: {str(what)[:220]}")
    if error_log and not any(e["kind"] == "game_log" for e in events):
        out.append("\nThe game's error log: no errors logged during this episode.")
    writes = [e for e in events if e["kind"] == "write"]
    if writes:
        out.append(f"\nState written directly (not player input): {len(writes)}")
    return "\n".join(out)
