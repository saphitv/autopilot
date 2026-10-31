"""Luna as the decision model (autopilot/luna.py): the questions as asked, the answers as Jev's (no network)."""

import json

import pytest

from autopilot import luna, runtime
from autopilot.kit import Choice, Noul, Score


def questions():
    return {"pick": Choice(instructions="Pick one.", criteria={"go": "walk to the coin", "flee": "run", "wait": None}),
            "danger": Noul(instructions="The player is in danger.", criteria={"true": "an enemy is near"}),
            "mood": Score(instructions="How tense is it?", criteria=["calm", "tense", "panic"])}


def test_ask_schema_bounds_each_answer():
    message, schema = luna.ask({"goal": "coins"}, questions())
    sent = json.loads(message)
    assert sent["state"] == {"goal": "coins"}
    assert sent["questions"]["pick"]["options"] == {"go": "walk to the coin", "flee": "run", "wait": None}
    assert sent["questions"]["mood"]["levels"] == {"0": "calm", "1": "tense", "2": "panic"}
    assert sent["questions"]["danger"]["yes_means"] == "an enemy is near"
    props = schema["properties"]
    assert schema["required"] == ["pick", "danger", "mood"]
    assert props["pick"]["properties"]["choice"]["enum"] == ["go", "flee", "wait"]
    assert props["mood"]["properties"]["level"]["enum"] == [0, 1, 2]


def test_answers_read_as_jevs():
    r = luna.response("gpt-6-luna", questions(),
                      {"pick": {"choice": "flee", "confidence": 0.8}, "danger": {"yes": 0.9}, "mood": {"level": 2}},
                      {"input": 120, "output": 30})
    pick = r.choices["pick"]
    assert pick.choice == "flee" and pick.probabilities["flee"] == pytest.approx(0.8)
    assert pick.probabilities["go"] == pytest.approx(0.1) and sum(pick.probabilities.values()) == pytest.approx(1)
    assert r.nouls["danger"].noul == pytest.approx(0.9)
    assert r.scores["mood"].score == 2.0 and r.scores["mood"].legend[2] == "panic"
    assert r.usage.input_tokens == 120 and r.model == "gpt-6-luna"


def test_bad_answers_fall_back_within_bounds():
    r = luna.response("m", questions(), {"pick": {"choice": "dance", "confidence": 7}, "danger": {"yes": -1},
                                         "mood": {"level": 9}})
    assert r.choices["pick"].choice == "go" and r.choices["pick"].confidence == 1.0
    assert r.nouls["danger"].noul == 0.0 and r.scores["mood"].score == 0.0


def test_decider_picks_luna(monkeypatch):
    monkeypatch.setenv("AUTOPILOT_DECIDER", "luna")
    assert isinstance(runtime.jev_client(warm=False), luna.LunaClient)
    monkeypatch.setenv("AUTOPILOT_DECIDER", "")
    assert not luna.enabled()
