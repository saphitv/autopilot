"""Autopilot's AI usage, one line a run, in the memory folder's usage.jsonl (`python -m autopilot.ledger` sums it up).

Each line: when, the game, the request and its kind, the outcome and how long it took; the agent (who wrote the
harness: "codex" through Autopilot, "external" for the user's own Claude Code or Codex through `autopilot mcp`, whose
tokens are on that agent's own plan and not seen here) and its tokens; the decision model's calls and tokens (Jev, or
Luna through the ChatGPT sign-in); the vision model's; and, signed in with ChatGPT, the plan's Codex limits as the run
started and ended (OpenAI's own % of each window: whole numbers, shared with anything else on the plan meanwhile).
"""

import datetime
import json
import os
import sys
import threading

from . import codex as codexmod

LEDGER = "usage.jsonl"


def path():
    return os.path.join(codexmod.MEMORY, LEDGER)


def chatgpt_plan():
    """Whether the run's models go through the user's ChatGPT sign-in (the Codex agent, Luna or vision on it)."""
    return ((os.environ.get("AUTOPILOT_CODEX_AUTH") or "").strip().lower() == "chatgpt"
            or (os.environ.get("AUTOPILOT_DECIDER") or "").strip().lower() == "luna")


def plan_limits():
    """The ChatGPT plan's Codex limits now: {"plan", "windows": [{"minutes", "used_percent", "resets_at"}]}, or None
    (not signed in with ChatGPT, or Codex can't say)."""
    if not chatgpt_plan():
        return None
    from .luna import LunaClient

    client = LunaClient()
    try:
        raw = client._start()._client._request_raw("account/rateLimits/read", {})
    except Exception:  # noqa: BLE001 - usage is told as known; a run never fails for it
        return None
    finally:
        client.close()
    limits = (raw or {}).get("rateLimits") or {}
    windows = [{"minutes": w.get("windowDurationMins"), "used_percent": w.get("usedPercent"),
                "resets_at": w.get("resetsAt")}
               for w in (limits.get("primary"), limits.get("secondary")) if isinstance(w, dict)]
    return {"plan": limits.get("planType"), "windows": windows}


class PlanWatch:
    """The plan's limits read as a run starts (in the background: the run doesn't wait), then again at its end."""

    def __init__(self):
        self.before = None
        self.thread = threading.Thread(target=self._read, daemon=True, name="plan-limits")
        self.thread.start()

    def _read(self):
        self.before = plan_limits()

    def end(self):
        self.thread.join(timeout=60)
        return self.before, plan_limits()


def _plan_change(before, after):
    if not before or not after:
        return None
    out = []
    for b, a in zip(before.get("windows") or [], after.get("windows") or []):
        if b.get("minutes") == a.get("minutes") and a.get("used_percent") is not None:
            out.append({"minutes": a["minutes"], "before": b.get("used_percent"), "after": a["used_percent"],
                        "reset_between": b.get("resets_at") != a.get("resets_at")})
    return {"plan": after.get("plan"), "windows": out}


def record(run_id, project, prompt, kind, report, usage, started, plan=None):
    """Adds the run's line. usage: telemetry.Usage.snapshot() (the decision and vision models' calls); plan: (before,
    after) of plan_limits()."""
    report = report or {}
    models = {"decision": [], "llm": []}
    for m in usage or []:
        models.setdefault(m.get("kind") or "other", []).append(
            {k: m.get(k) for k in ("model", "calls", "input", "output", "cache_read", "cache_write")})
    line = {
        "run": run_id, "project": project, "started": datetime.datetime.fromtimestamp(started).isoformat(
            timespec="seconds"), "seconds": round(report.get("seconds") or 0, 1), "kind": kind,
        "request": (prompt or "")[:300], "outcome": report.get("outcome"),
        "agent": {"who": report.get("agent"), "model": report.get("model"), "tokens": report.get("tokens") or None,
                  "turns": report.get("turns")},
        "decision": models.pop("decision"), "vision": models.pop("llm"),
        "plan": _plan_change(*(plan or (None, None))),
    }
    if models:
        line["other"] = models
    os.makedirs(os.path.dirname(path()), exist_ok=True)
    with open(path(), "a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")
    return line


# -- the summary -------------------------------------------------------------------------------------------------

def _k(n):
    n = int(n or 0)
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}k" if n >= 1e3 else str(n)


def lines(since=None):
    try:
        with open(path(), encoding="utf-8") as f:
            out = [json.loads(x) for x in f if x.strip()]
    except FileNotFoundError:
        return []
    return [x for x in out if not since or x.get("started", "") >= since]


def summary(entries, out=sys.stdout):
    if not entries:
        print(f"No runs recorded yet ({path()}).", file=out)
        return
    print(f"{'started':16} {'min':>5} {'outcome':14} {'agent':22} {'agent tok':>10} {'decisions':>9} "
          f"{'dec tok':>8} {'vision':>6} {'plan %':>8}  request", file=out)
    days = {}
    for e in entries:
        a = e.get("agent") or {}
        tok = a.get("tokens") or {}
        dec = e.get("decision") or []
        vis = e.get("vision") or []
        dec_calls = sum(d.get("calls") or 0 for d in dec)
        dec_tok = sum((d.get("input") or 0) + (d.get("cache_read") or 0) + (d.get("output") or 0) for d in dec)
        vis_calls = sum(v.get("calls") or 0 for v in vis)
        plan = ""
        for w in ((e.get("plan") or {}).get("windows") or [])[:1]:
            plan = f"{w.get('before')}->{w.get('after')}" + ("*" if w.get("reset_between") else "")
        who = f"{a.get('who') or '?'} {a.get('model') or ''}".strip()
        agent_tok = _k(tok.get("input", 0) + tok.get("output", 0)) if tok else "own plan" if a.get(
            "who") == "external" else "-"
        print(f"{e.get('started', '')[:16]:16} {e.get('seconds', 0) / 60:5.1f} {str(e.get('outcome'))[:14]:14} "
              f"{who[:22]:22} {agent_tok:>10} {dec_calls:9d} {_k(dec_tok):>8} {vis_calls:6d} {plan:>8}  "
              f"{(e.get('request') or '')[:60]}", file=out)
        d = days.setdefault(e.get("started", "")[:10], {"runs": 0, "minutes": 0.0, "agent": 0, "decisions": 0,
                                                         "dec_tok": 0, "vision": 0})
        d["runs"] += 1
        d["minutes"] += (e.get("seconds") or 0) / 60
        d["agent"] += tok.get("input", 0) + tok.get("output", 0) if tok else 0
        d["decisions"] += dec_calls
        d["dec_tok"] += dec_tok
        d["vision"] += vis_calls
    print("\nPer day:", file=out)
    for day, d in sorted(days.items()):
        print(f"  {day}: {d['runs']} runs, {d['minutes']:.0f} min, agent {_k(d['agent'])} tokens, "
              f"{d['decisions']} decisions ({_k(d['dec_tok'])} tokens), {d['vision']} vision calls", file=out)
    print("\n(agent tokens include cached input; 'own plan': the calling agent's tokens are on its own plan; plan %: "
          "the ChatGPT plan's Codex window as the run started -> ended, * a reset between)", file=out)


def main(argv=None):
    import argparse

    ap = argparse.ArgumentParser(prog="python -m autopilot.ledger", description="Autopilot's AI usage per run")
    ap.add_argument("--since", help="only runs started on or after this date (YYYY-MM-DD)")
    ap.add_argument("--plan", action="store_true", help="also read the ChatGPT plan's Codex limits now")
    ap.add_argument("--json", action="store_true", help="the lines as they are")
    args = ap.parse_args(argv)
    entries = lines(args.since)
    if args.json:
        for e in entries:
            print(json.dumps(e))
    else:
        summary(entries)
    if args.plan:
        os.environ.setdefault("AUTOPILOT_CODEX_AUTH", "chatgpt")
        now = plan_limits()
        if now:
            for w in now["windows"]:
                resets = datetime.datetime.fromtimestamp(w["resets_at"]).strftime("%a %d %b %H:%M") if w.get(
                    "resets_at") else "?"
                mins = w.get("minutes") or 0
                span = f"{mins / 1440:.0f}-day" if mins >= 1440 else f"{mins / 60:.0f}-hour"
                print(f"\nChatGPT plan ({now['plan']}): {w['used_percent']}% of the {span} Codex window used, "
                      f"resets {resets}")
        else:
            print("\nThe plan's limits couldn't be read (signed in with ChatGPT in Codex?)")


if __name__ == "__main__":
    main()
