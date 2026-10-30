"""T5: scripts/explainer_set.py builds the set from the splits and runs it (fake KataGo, scripted model)."""

from __future__ import annotations

import importlib.util
import json
import random
import sys
from pathlib import Path

import pytest
import yaml

from sensei.explainer import MetaExplainer
from sensei.game_go import GoGame
from tests.test_c2 import game_sgf
from tests.test_explainer_live_fake import FakeClient, text_response, tool_response

ROOT = Path(__file__).resolve().parents[1]
PRE = yaml.safe_load((ROOT / "configs" / "prereg.yaml").read_text(encoding="utf-8"))
RUNTIME = yaml.safe_load((ROOT / "configs" / "default.yaml").read_text(encoding="utf-8"))


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def scripted(kwargs):
    """One analyze_position tool call per question, then a plain answer; explanations are plain text."""
    messages = kwargs["messages"]
    if kwargs.get("tool_choice") == "auto" and not any(m.get("role") == "tool" for m in messages):
        return tool_response(("analyze_position", {"top_k": 3}))
    return text_response("Keep your stones connected and look at the biggest area.")


def test_build_and_run_the_explainer_set(tmp_path):
    from sensei.engine_katago import KataGoEngine

    raw = tmp_path / "raw"
    raw.mkdir()
    rng = random.Random(11)
    for game_id in ("x-1", "x-2"):
        (raw / f"{game_id}.sgf").write_text(game_sgf(rng, "8k", n=30), encoding="utf-8")
    splits = {"version": 1, "seed": 1, "source": "test", "raw_dir": str(raw), "pilot_openings": [],
              "confirmatory_openings": [], "c2_positions": {}, "topic_games": [],
              "explainer_positions": [{"game": "x-1", "ply": 10, "phase": "opening", "declared_rank": "12k"},
                                      {"game": "x-2", "ply": 25, "phase": "middle"}]}  # fmt: skip
    splits_path = tmp_path / "splits.json"
    splits_path.write_text(json.dumps(splits), encoding="utf-8")
    es = load_script("explainer_set")

    data = es.build_set(splits_path, ROOT)
    items = data["positions"]
    assert [i["id"] for i in items] == ["e1", "e2"] and items[0]["declared_rank"] == "12k"
    assert items[1]["declared_rank"] == "8k"  # falls back to the mover's rank in the SGF
    assert len(items[1]["moves"]) == 25 and items[1]["to_move"] == "W" and items[0]["meta"]["rules"] == "japanese"

    engine = KataGoEngine(
        exe=sys.executable, exe_args=[str(ROOT / "tests" / "fake_katago.py")], model="fake-main.bin.gz",
        human_model="fake-human.bin.gz", config="fake-analysis.cfg", stderr_log=tmp_path / "err.log",
        query_timeout=60.0,
    )  # fmt: skip
    try:
        client = FakeClient(default=scripted)
        explainer = MetaExplainer(client, engine, GoGame(), RUNTIME, model="fake", log_path=tmp_path / "api.jsonl")
        records = es.run_set(items, engine, explainer, PRE, RUNTIME)
    finally:
        engine.close()
    assert [r["id"] for r in records] == ["e1", "e2"]
    first = records[0]
    assert first["choice"]["move"] in first["choice"]["admissible"]
    assert first["explanation"]["text"].startswith("Keep your stones") and first["explanation"]["cost_usd"] > 0
    assert first["ask"]["tool_calls"][0]["name"] == "analyze_position"
    assert "?" in first["ask"]["question"]
    header = {"version": "v1", "created": "now", "git_commit": "no-git", "git_dirty": True, "model": "fake",
              "epsilon": 0.02}  # fmt: skip
    md = es.to_markdown(records, header)
    assert "## e1: opening, 12k learner" in md and "| e2 | | | |" in md and "analyze_position" in md


def test_question_wording():
    es = load_script("explainer_set")
    assert "instead of Q16" in es.question_for("R14", "Q16")
    assert es.question_for("Q16", "Q16").startswith("What is the idea behind Q16")
    with pytest.raises(SystemExit):
        es.main([])
