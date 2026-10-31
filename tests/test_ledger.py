"""Autopilot's AI usage ledger (autopilot/ledger.py): one line a run, and its summary."""

import io
import json

from autopilot import ledger


def test_record_and_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger.codexmod, "MEMORY", str(tmp_path))
    usage = [{"kind": "decision", "model": "gpt-6-luna", "calls": 40, "input": 9000, "output": 900, "cache_read": 0,
              "cache_write": 0},
             {"kind": "llm", "model": "gpt-6.1-sol", "calls": 2, "input": 12000, "output": 300, "cache_read": 0,
              "cache_write": 0}]
    report = {"outcome": "CONFIRMED", "seconds": 300.0, "agent": "codex", "model": "gpt-6.1-sol",
              "tokens": {"input": 500000, "output": 4000, "cacheRead": 450000}, "turns": 12}
    plan = ({"plan": "prolite", "windows": [{"minutes": 10080, "used_percent": 15, "resets_at": 1}]},
            {"plan": "prolite", "windows": [{"minutes": 10080, "used_percent": 16, "resets_at": 1}]})
    line = ledger.record("r1", "Don't Tip!", "Verify the gift", "verify", report, usage, 1793450000.0, plan=plan)
    assert line["decision"][0]["calls"] == 40 and line["vision"][0]["model"] == "gpt-6.1-sol"
    assert line["plan"]["windows"][0] == {"minutes": 10080, "before": 15, "after": 16, "reset_between": False}
    ledger.record("r2", "Don't Tip!", "Explore", "explore", {"agent": "external", "seconds": 60}, [], 1793450100.0)
    entries = ledger.lines()
    assert [e["run"] for e in entries] == ["r1", "r2"]
    assert json.loads(open(ledger.path()).readline())["run"] == "r1"
    out = io.StringIO()
    ledger.summary(entries, out=out)
    text = out.getvalue()
    assert "15->16" in text and "own plan" in text and "2 runs" in text


def test_no_plan_without_chatgpt(monkeypatch):
    monkeypatch.delenv("AUTOPILOT_CODEX_AUTH", raising=False)
    monkeypatch.delenv("AUTOPILOT_DECIDER", raising=False)
    assert ledger.plan_limits() is None
