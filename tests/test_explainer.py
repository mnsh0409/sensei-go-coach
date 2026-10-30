"""Offline acceptance tests for T5. Skip until sensei.explainer exists."""

from __future__ import annotations

import pytest

from sensei.interfaces import Choice, LearnerEstimate, PositionClass

explainer_mod = pytest.importorskip("sensei.explainer")

EXPECTED_TOOLS = {"analyze_position", "evaluate_move", "show_variation", "get_learner_profile"}


def sample(analysis_factory):
    a = analysis_factory([("Q16", 0.5, 0.60, 300), ("R14", 0.3, 0.59, 100), ("C3", 0.2, 0.55, 50)])
    choice = Choice(
        move="R14",
        instructiveness=1.02,
        value_loss=0.01,
        position_class=PositionClass.STRATEGIC,
        scores={"Q16": -0.29, "R14": 1.02},
        admissible=("Q16", "R14"),
    )
    learner = LearnerEstimate(rank=-9.0, rank_sd=1.0, n_moves=40)
    return a, choice, learner


def test_messages_are_grounded(analysis_factory):
    a, choice, learner = sample(analysis_factory)
    msgs = explainer_mod.build_explanation_messages(a, choice, learner, lang="en")
    assert msgs[0]["role"] == "system"
    system = msgs[0]["content"].lower()
    assert "never" in system and ("invent" in system or "make up" in system)
    user = " ".join(m["content"] for m in msgs[1:] if isinstance(m.get("content"), str))
    for needle in ("R14", "Q16", "strategic", "10k"):
        assert needle in user, needle
    assert "0.01" in user or "1.0%" in user or "1%" in user


def test_tool_schemas():
    names = {t["function"]["name"] for t in explainer_mod.TOOLS}
    assert names == EXPECTED_TOOLS
    for t in explainer_mod.TOOLS:
        assert t["type"] == "function"
        params = t["function"]["parameters"]
        assert params["type"] == "object"
        assert params.get("additionalProperties") is False


def test_ungrounded_coordinate_guard():
    f = explainer_mod.find_ungrounded_coords
    assert f("R14 builds a framework while Q16 is safe.", {"R14", "Q16"}, 19) == []
    assert f("R14 then K10 kills.", {"R14"}, 19) == ["K10"]
    assert f("I think so.", set(), 19) == []
    # Lowercase coordinates must not slip past the guard.
    assert f("r14 then k10 kills.", {"R14"}, 19) == ["K10"]
