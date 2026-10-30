"""Acceptance test for T7 (session): a scripted game with a fake engine, a fake learner, fake topics and a fake
explainer drives the real GoGame, ConstrainedSelector and ThompsonCurriculum through sensei.session."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import yaml

from sensei.interfaces import Candidate, EngineAnalysis, Explanation, LearnerEstimate

session_mod = pytest.importorskip("sensei.session")
game_mod = pytest.importorskip("sensei.game_go")
selector_mod = pytest.importorskip("sensei.selector")
curriculum_mod = pytest.importorskip("sensei.curriculum")

ROOT = Path(__file__).resolve().parents[1]
TOPICS = ["corner", "edge", "center"]
CENTER = ["K10", "L11", "M12", "N13", "J9"]
EDGE = ["K1", "L1", "M1", "N1", "O1"]


class FakeEngine:
    """Candidates are the first three legal points in row-major order (top-left corner area) at 60/59/55%."""

    def __init__(self):
        self.game = game_mod.GoGame()
        self.queries = 0

    def analyze(self, pos, max_visits):
        self.queries += 1
        legal = [m for m in self.game.legal_moves(pos) if m != "pass"][:3]
        rows = list(zip(legal, (0.5, 0.3, 0.2), (0.60, 0.59, 0.55), (300, 100, 50), (2.0, 1.5, 0.5), strict=False))
        cands = tuple(Candidate(move=m, prior=p, value=v, visits=n, score_lead=s) for m, p, v, n, s in rows)
        total = sum(c.prior for c in cands)
        return EngineAnalysis(pos, cands, {c.move: c.prior / total for c in cands}, 0.60)

    def close(self):
        return None


class FakeLearner:
    def policy(self, pos, legal):
        return {m: 1.0 / len(legal) for m in legal}

    def estimate(self, history):
        return LearnerEstimate(rank=-9.0, rank_sd=1.0, n_moves=len(history))


class RegionTopics:
    """One topic per region token, so the test controls which topic a move lands in."""

    n_topics = 3

    def fit(self, bags):
        return None

    def assign(self, bag):
        for k, name in enumerate(TOPICS):
            if bag.get(f"region:{name}"):
                return [1.0 if i == k else 0.0 for i in range(3)]
        return [1 / 3] * 3

    def describe(self, k, top_n=8):
        return [(f"region:{TOPICS[k]}", 1.0)]


class FakeExplainer:
    def __init__(self):
        self.explained, self.mistakes, self.positions, self.profiles = [], [], [], []

    def _out(self, text):
        return Explanation(text=text, model="fake", prompt_tokens=10, completion_tokens=5, grounded_on={})

    def explain(self, analysis, choice, learner, lang="en"):
        self.explained.append(choice.move)
        return self._out(f"Sensei played {choice.move}.")

    def explain_mistake(self, analysis, played, best, loss, learner, topic=None, position_class=None, lang="en"):
        self.mistakes.append((played, best, topic))
        return self._out(f"{played} lost {loss:.2f}; {best} was better.")

    def set_position(self, pos, analysis=None):
        self.positions.append(pos)

    def set_learner_profile(self, profile):
        self.profiles.append(profile)


def make_session(tmp_path):
    cfg = yaml.safe_load((ROOT / "configs" / "default.yaml").read_text(encoding="utf-8"))
    profile = session_mod.LearnerProfile(learner_id="test-learner", declared_rank="10k", target_topic="center")
    curriculum = curriculum_mod.ThompsonCurriculum(arms=TOPICS, discount=0.9, prior=(1.0, 1.0))
    selector = selector_mod.ConstrainedSelector(
        delta=1e-6, min_visits_abs=10, min_visits_frac=0.05, entropy_low=0.5, entropy_high=2.0
    )
    explainer = FakeExplainer()
    sess = session_mod.Session(
        game=game_mod.GoGame(),
        engine=FakeEngine(),
        selector=selector,
        learner=FakeLearner(),
        topics=RegionTopics(),
        curriculum=curriculum,
        explainer=explainer,
        cfg=cfg,
        profile=profile,
        rng=np.random.default_rng(0),
        learner_color="B",
        turns_log=tmp_path / "turns.jsonl",
        hardware={"gpu": "fake"},
        topic_names=TOPICS,
    )
    return sess, explainer, curriculum


def play_script(sess):
    reports = []
    for i in range(5):
        best = sess.analyze(sess.pos).candidates[0].move
        for move in (best, CENTER[i], EDGE[i]):
            reports.append(sess.learner_move(move))
            sess.sensei_move()
    return reports


def test_scripted_game_updates_profile_curriculum_and_explains_only_target_mistakes(tmp_path):
    sess, explainer, curriculum = make_session(tmp_path)
    reports = play_script(sess)

    assert len(reports) == 15 and len(sess.pos.moves) == 30
    assert [r.is_mistake for r in reports] == [False, True, True] * 5
    assert {r.topic for r in reports if not r.is_mistake} <= {"corner", "edge"}  # the engine's top-row points
    assert [r.topic for r in reports if r.is_mistake] == ["center", "edge"] * 5
    explained = [r for r in reports if r.explanation is not None]
    assert [r.move for r in explained] == CENTER  # only mistakes in the target topic are explained
    assert [m[2] for m in explainer.mistakes] == ["center"] * 5
    assert len(explainer.explained) == 15  # every Sensei move is explained
    assert explainer.positions[-1] == sess.pos  # Ask Sensei follows the game
    assert all(r.loss == pytest.approx(0.2) for r in reports if r.is_mistake)

    summary = sess.end_game(profiles_dir=tmp_path / "profiles")

    saved = json.loads((tmp_path / "profiles" / "test-learner.json").read_text(encoding="utf-8"))
    assert saved["games_played"] == 1
    assert saved["topics"]["center"]["mistakes"] == 5 and saved["topics"]["center"]["moves"] == 5
    good_moves = {t: sum(1 for r in reports if r.topic == t and not r.is_mistake) for t in TOPICS}
    assert saved["topics"]["edge"]["mistakes"] == 5 and saved["topics"]["edge"]["moves"] == 5 + good_moves["edge"]
    assert saved["topics"]["corner"]["mistakes"] == 0 and saved["topics"]["corner"]["moves"] == good_moves["corner"]
    assert saved["topics"]["center"]["example"]["played"] == "K10"
    assert curriculum.posterior("center") == pytest.approx((6.0, 1.0))
    assert curriculum.posterior("edge") == pytest.approx((6.0, 1.0 + good_moves["edge"]))
    assert curriculum.posterior("corner") == pytest.approx((1.0, 1.0 + good_moves["corner"]))
    assert saved["target_topic"] in TOPICS and saved["rank_history"][0]["n_moves"] == 15
    assert {t["topic"] for t in summary["weakest_topics"]} == {"center", "edge"}
    assert summary["result"]["winner"] in ("B", "W") and summary["mistakes_this_game"] == 10
    assert explainer.profiles[-1]["games_played"] == 1

    lines = (tmp_path / "turns.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 15
    first = json.loads(lines[0])
    assert {"engine_ms", "human_policy_ms", "explanation_ms", "hardware"} <= set(first)


def test_profile_round_trip_and_turn_order(tmp_path):
    sess, _explainer, _curriculum = make_session(tmp_path)
    with pytest.raises(RuntimeError):
        sess.sensei_move()  # Black (the learner) moves first
    sess.learner_move(sess.analyze(sess.pos).candidates[0].move)
    with pytest.raises(RuntimeError):
        sess.learner_move("K10")
    path = sess.profile.save(tmp_path / "p")
    loaded = session_mod.LearnerProfile.load(tmp_path / "p", "test-learner", "10k")
    assert loaded.to_json()["topics"] == sess.profile.to_json()["topics"]
    fresh = session_mod.LearnerProfile.load(tmp_path / "p", "someone-else", "5k")
    assert fresh.declared_rank == "5k" and fresh.games_played == 0 and path.is_file()


def test_local_tags():
    game = game_mod.GoGame()
    pos = game.initial_position()
    for m in ["A2", "B2", "B1", "C1"]:  # White B2 then C1; Black A2, B1
        pos = game.play(pos, m)
    before = game.board(pos)
    after_pos = game.play(pos, "A1")  # Black A1 connects to B1 next to White C1 (contact), no capture
    tags = session_mod.local_tags(before, game.board(after_pos), "A1", "B", 19)
    assert "capture" not in tags
    pos2 = game.initial_position()
    for m in ["B1", "A1", "Q16"]:  # White A1 is in atari after Black B1; Black then captures with A2
        pos2 = game.play(pos2, m)
    pos2 = game.play(pos2, "D4")
    cap = session_mod.local_tags(game.board(pos2), game.board(game.play(pos2, "A2")), "A2", "B", 19)
    assert "capture" in cap and "contact" in cap
