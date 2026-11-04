"""Explainer prompt v2 (2026-11-04, after the T5 grading of v1): colors and voice, no engine internals, locations in
plain words, what a move does to the stones next to it, and tool results without prior or visits."""

from __future__ import annotations

import pytest

ex = pytest.importorskip("sensei.explainer")
from sensei.game_go import GoGame  # noqa: E402
from sensei.interfaces import Candidate, Choice, EngineAnalysis, LearnerEstimate, PositionClass  # noqa: E402

JARGON = ("prior", "visits", "instructiveness", "admissible", "value bound", "policy")


def test_describe_point_in_plain_go_words():
    assert ex.describe_point("S7") == "right side, 2nd line, lower half"
    assert ex.describe_point("D17") == "upper-left corner (3-4 point)"
    assert ex.describe_point("Q16") == "upper-right corner (4-4 point)"
    assert ex.describe_point("C9") == "left side, 3rd line, lower half"
    assert ex.describe_point("K2") == "bottom side, 2nd line"
    assert ex.describe_point("K10") == "center"
    assert ex.describe_point("pass") is None and ex.describe_point("Z99") is None and ex.describe_point(None) is None


def play(moves):
    game = GoGame()
    pos = game.initial_position()
    for m in moves:
        pos = game.play(pos, m)
    return game, pos


def test_local_facts_atari_capture_and_liberties():
    # White D4 is surrounded by Black on three sides; Black to play D3 captures it.
    game, pos = play(["C4", "D4", "E4", "Q16", "D5", "Q4"])
    capture = ex.local_facts(game, pos, "D3")
    assert capture["captured"] == 1 and capture["touches_opponent"] and capture["atari"] == []
    assert "captures 1 of your stone." in ex.local_text(capture, "my", "your")
    # Two Black stones next to White D4: the third one (D5) puts it in atari.
    game, pos = play(["C4", "D4", "E4", "Q16"])
    atari = ex.local_facts(game, pos, "D5")
    assert atari["atari"] == ["D4"] and atari["captured"] == 0
    assert "puts your stones at D4 in atari" in ex.local_text(atari, "my", "your")
    quiet = ex.local_facts(game, pos, "K10")
    assert not quiet["touches_opponent"] and quiet["own_group"] == {"stones": 1, "liberties": 4}
    assert ex.local_facts(game, pos, "pass") is None


def sample_facts():
    game, pos = play(["D4", "Q16", "C16", "Q3", "R4"])  # White to move: Sensei plays White, the learner Black
    cands = (
        Candidate("R14", 0.30, 0.62, 120, 4.5, ("R14", "Q5", "R6")),
        Candidate("Q5", 0.40, 0.61, 90, 2.0, ("Q5", "R14")),
        Candidate("D17", 0.10, 0.55, 30, 0.5, ("D17",)),
    )
    analysis = EngineAnalysis(pos, cands, {c.move: c.prior for c in cands}, 0.60)
    choice = Choice("Q5", 0.8, 0.01, PositionClass.STRATEGIC, {"Q5": 0.8, "R14": -0.1}, ("R14", "Q5"))
    learner = LearnerEstimate(rank=-9.0, rank_sd=1.0, n_moves=40)
    return game, analysis, choice, learner


def test_explanation_prompt_v2_voice_locations_and_no_jargon():
    game, analysis, choice, learner = sample_facts()
    facts = ex.explanation_facts(analysis, choice, learner, game=game)
    assert (
        facts["sensei_color"] == "W"
        and facts["learner_color"] == "B"
        and facts["prompt_version"] == ex.PROMPT_VERSION == "v4.1"
    )
    assert facts["best_move"] == "R14" and facts["points_vs_best"] == 2.5 and facts["continuation"] == ["R14"]
    assert facts["where"]["Q5"] == "lower-right corner (4-5 point)"
    msgs = ex.build_explanation_messages(analysis, choice, learner, max_sentences=4, facts=facts)
    system, user = msgs[0]["content"], msgs[1]["content"]
    assert "You play White; the learner plays Black" in system and "never call White's stones" in system
    assert "I (White) now play Q5 (lower-right corner (4-5 point))" in user
    assert "Last move: Black (you) played R4" in user
    assert "R14 (right side, 3rd line, upper half): about 2.5 points better for me" in user
    assert "but R14 gains about 2.5 points more, so for points R14 is the better move" in user
    assert "do not call my move the best one" in user
    assert "Winning chances before my move: White (me) about 60%" in user  # a close game keeps the percentages
    assert "A likely answer for you is R14" in user and "players around 10k rarely find" in user
    assert not any(word in user.lower() for word in JARGON), user
    assert ex.allowed_coords_from_facts(facts) >= {"Q5", "R14", "R4", "D17"}


def test_template_explanation_has_no_jargon():
    game, analysis, choice, learner = sample_facts()
    facts = ex.explanation_facts(analysis, choice, learner, game=game)
    text = ex.template_explanation(facts)
    assert text.startswith("I played Q5") and "R14" in text and "prior" not in text


class Client:
    """Answers once with plain text; records the messages."""

    def __init__(self):
        self.calls = []
        outer = self

        class Completions:
            def create(self, **kwargs):
                outer.calls.append(kwargs)
                from types import SimpleNamespace as NS

                message = NS(content="Fine.", tool_calls=None)
                return NS(choices=[NS(message=message)], usage=NS(prompt_tokens=1, completion_tokens=1))

        self.chat = type("Chat", (), {"completions": Completions()})()


class Engine:
    def __init__(self, analysis):
        self.analysis = analysis

    def analyze(self, pos, max_visits):
        return self.analysis


def test_ask_prompt_and_tools_name_the_sides(tmp_path):
    game, analysis, choice, learner = sample_facts()
    client = Client()
    explainer = ex.MetaExplainer(client, Engine(analysis), game, {"explainer": {}}, model="m", log_path=tmp_path / "a")
    explainer.explain(analysis, choice, learner)
    assert explainer.learner_color == "B"
    pos = analysis.position
    explainer.ask("Why Q5?", position=pos)
    system = client.calls[-1]["messages"][0]["content"]
    assert "playing White against the learner" in system and "who plays Black" in system and "my turn" in system
    result = explainer.tool_analyze_position(pos, 2)
    assert result["for"] == "White (me)" and result["candidates"][0]["where"] == "right side, 3rd line, upper half"
    assert all("prior_pct" not in c and "visits" not in c for c in result["candidates"])
    move = explainer.tool_evaluate_move(pos, "R5")
    assert move["for"] == "White (me)" and "visits" not in move and move["where"] == "lower-right corner (3-5 point)"
    assert "touches Black (you)'s stones" in move["on_the_board"]
    line = explainer.tool_show_variation(pos, ["Q5", "R14"])
    assert [s["by"] for s in line["steps"]] == ["White (me)", "Black (you)"]
    explainer.set_learner_color(None)
    explainer.ask("And now?", position=pos)
    assert "Refer to the players as Black and White" in client.calls[-1]["messages"][0]["content"]


def test_v3_decided_games_drop_percentages_and_even_scores_say_even():
    game, analysis, choice, learner = sample_facts()
    won = EngineAnalysis(analysis.position, analysis.candidates, analysis.policy, 0.99)
    facts = ex.explanation_facts(won, choice, learner, game=game)
    user = ex.build_explanation_messages(won, choice, learner, max_sentences=4, facts=facts)[1]["content"]
    assert "Winning chances" not in user and "%" not in user and "talk about points" not in user
    assert "White (me) ahead by about 2.0 points" in user
    even = [Candidate(c.move, c.prior, c.value, c.visits, 0.2 if c.move == "Q5" else c.score_lead, c.pv)
            for c in analysis.candidates]  # fmt: skip
    close = EngineAnalysis(analysis.position, tuple(even), analysis.policy, 0.60)
    facts = ex.explanation_facts(close, choice, learner, game=game)
    user = ex.build_explanation_messages(close, choice, learner, max_sentences=4, facts=facts)[1]["content"]
    assert "Score after my move, by the engine's count: about even." in user


def test_v3_quiet_moves_name_the_nearest_stones_and_my_new_stone():
    game, pos = play(["D4", "Q16"])
    local = ex.local_facts(game, pos, "K10")
    assert local["nearest"] == {"own": None, "opponent": None}
    assert "No stone is within 4 points of it: it is in an open area." in ex.local_text(local, "my", "your")
    local = ex.local_facts(game, pos, "E8")  # Black's own D4 is 4 points away, in no named shape
    text = ex.local_text(local, "my", "your")
    assert local["nearest"]["own"] == {"stone": "D4", "distance": 4} and "my stone at D4, 4 points away" in text
    assert "After it, my new stone has 4 liberties." in text
    jump = ex.local_text(ex.local_facts(game, pos, "D7"), "my", "your")
    assert "a two-space jump from my stone at D4" in jump and "points away" not in jump  # named once, not twice
    assert (
        ex.describe_point("K6") == "center, toward the bottom"
        and ex.describe_point("F14") == "center, toward the top-left"
    )


def test_v3_1_engine_top_is_the_most_searched_move_not_the_highest_winrate():
    # D17 is searched most but R14 has the higher winrate: the engine's top move is D17 (what KataGo shows first).
    game, analysis, choice, learner = sample_facts()
    cands = (
        Candidate("D17", 0.30, 0.60, 150, 3.0, ("D17",)),
        Candidate("R14", 0.30, 0.62, 120, 4.5, ("R14", "Q5", "R6")),
        Candidate("Q5", 0.40, 0.61, 90, 4.0, ("Q5", "R14")),
    )
    top = EngineAnalysis(analysis.position, cands, {c.move: c.prior for c in cands}, 0.60)
    facts = ex.explanation_facts(top, choice, learner, game=game)
    assert facts["best_move"] == "D17" and facts["points_vs_best"] == -1.0
    user = ex.build_explanation_messages(top, choice, learner, max_sentences=4, facts=facts)[1]["content"]
    assert "The engine's top move was D17" in user and "my move scores about 1.0 points more" in user
    assert "My move is also the engine's top move" not in user and "a loss of" not in user  # my move is stronger
    assert "mine scores about 1.0 points more" in ex.template_explanation(facts)
    # The choice itself being searched most is the only case that says "also the engine's top move".
    first = EngineAnalysis(analysis.position, (cands[2], cands[0], cands[1]), top.policy, 0.60)
    facts = ex.explanation_facts(first, choice, learner, game=game)
    user = ex.build_explanation_messages(first, choice, learner, max_sentences=4, facts=facts)[1]["content"]
    assert facts["best_move"] == "Q5" and "My move is also the engine's top move" in user


def test_v3_1_evaluate_move_measures_against_the_most_searched_move(tmp_path):
    game, analysis, choice, learner = sample_facts()
    cands = (
        Candidate("D17", 0.30, 0.60, 150, 3.0, ("D17",)),
        Candidate("R14", 0.30, 0.62, 120, 4.5, ("R14",)),
    )
    top = EngineAnalysis(analysis.position, cands, {c.move: c.prior for c in cands}, 0.60)
    explainer = ex.MetaExplainer(Client(), Engine(top), game, {"explainer": {}}, model="m", log_path=tmp_path / "a")
    result = explainer.tool_evaluate_move(top.position, "R14")
    assert result["best_move"] == "D17" and result["loss_pct"] == 0.0
    assert explainer.tool_evaluate_move(top.position, "D17")["loss_pct"] == 0.0


# ---- v4: the reason from the engine (what the other side would play if Sensei passed) ----------------------------


def test_v4_local_facts_report_a_move_that_connects_two_groups():
    game, pos = play(["C3", "Q16", "C5", "Q4"])  # Black C3 and C5 are separate; Black C4 joins them
    local = ex.local_facts(game, pos, "C4")
    assert local["joins"] == ["C3", "C5"] or local["joins"] == ["C5", "C3"]
    text = ex.local_text(local, "my", "your")
    assert "It connects my stones at" in text and "into one group" in text and "next to my own" not in text
    assert ex.local_facts(game, pos, "D4")["joins"] == []


class PassEngine:
    """Root analysis for the position itself; after a pass, the other side's top move is `threat`."""

    def __init__(self, analysis, threat, score=-1.0):
        self.analysis, self.threat, self.score, self.queries = analysis, threat, score, []

    def analyze(self, pos, max_visits):
        self.queries.append(pos)
        if pos.moves and pos.moves[-1][1] == "pass":
            c = Candidate(self.threat, 0.5, 0.6, 100, self.score, (self.threat,))
            return EngineAnalysis(pos, (c,), {c.move: 0.5}, 0.6)
        return self.analysis


def test_v4_threat_facts_and_prompt(tmp_path):
    # Black C4 and E4 flank White D4; Black (Sensei) plays D5. If Black passed, White would play D5 itself.
    game, pos = play(["C4", "D4", "E4", "Q16"])
    cands = (Candidate("D5", 0.6, 0.62, 150, 4.0, ("D5", "D3")), Candidate("Q4", 0.4, 0.55, 50, 1.0, ("Q4",)))
    analysis = EngineAnalysis(pos, cands, {c.move: c.prior for c in cands}, 0.60)
    choice = Choice("D5", 0.8, 0.0, PositionClass.TACTICAL, {"D5": 0.8}, ("D5",))
    learner = LearnerEstimate(rank=-9.0, rank_sd=1.0, n_moves=40)
    client = Client()
    engine = PassEngine(analysis, "D5", score=-1.0)
    explainer = ex.MetaExplainer(client, engine, game, {"explainer": {}}, model="m", log_path=tmp_path / "a")
    out = explainer.explain(analysis, choice, learner)
    threat = out.grounded_on["threat"]
    assert threat["move"] == "D5" and threat["same_point"] and threat["local"] and threat["worth_points"] == 3.0
    user = client.calls[0]["messages"][1]["content"]
    assert (
        "If I had passed instead, your best move would be this same point, D5: it would give your stones at D4 4 "
        "liberties instead of 2." in user
    )
    assert "the main reason to play here is what you would do here if I passed" in user
    assert "Compared with passing, my move is worth about 3.0 points" in user
    assert "Position type: a must-play (only move) situation" in user  # one admissible move: no "quiet" wording
    # The template fallback carries the threat too.
    assert "If I had passed, your best move would be D5" in ex.template_explanation(out.grounded_on)


def test_v4_threat_text_names_captures_and_sharpness():
    # Black C4, E4 and D3 surround White D4. If White passed, Black's D5 would capture it.
    game, pos = play(["C4", "D4", "E4", "Q16", "D3", "Q4", "K10"])  # White to move
    capture = ex.local_facts(game, game.play(pos, "pass"), "D5")
    assert ex.threat_text(capture, "my", "your") == "it would capture 1 of my stone"
    assert ex.threat_sharp({"local": True, "facts": capture})
    assert not ex.threat_sharp({"local": False, "facts": capture})
    quiet = ex.local_facts(game, game.play(pos, "pass"), "K16")
    assert ex.threat_text(quiet, "my", "your") == "" and not ex.threat_sharp({"local": True, "facts": quiet})


def test_v4_far_threat_is_not_a_reason_and_passes_skip_the_query(tmp_path):
    game, analysis, choice, learner = sample_facts()  # Sensei (White) plays Q5
    engine = PassEngine(analysis, "D17", score=-1.0)  # far from Q5
    client = Client()
    explainer = ex.MetaExplainer(client, engine, game, {"explainer": {}}, model="m", log_path=tmp_path / "a")
    out = explainer.explain(analysis, choice, learner)
    assert out.grounded_on["threat"]["local"] is False
    user = client.calls[0]["messages"][1]["content"]
    assert "If I had passed" not in user and "the main reason to play here" not in user
    passed = game.play(analysis.position, "pass")
    assert explainer.threat_after_pass(passed, "Q5", 1.0) is None  # passing again would end the game


def test_v4_evaluate_move_reports_what_the_other_side_would_play(tmp_path):
    game, pos = play(["C4", "D4", "E4", "Q16"])
    cands = (Candidate("D5", 0.6, 0.62, 150, 4.0, ("D5",)), Candidate("Q4", 0.4, 0.55, 50, 1.0, ("Q4",)))
    analysis = EngineAnalysis(pos, cands, {c.move: c.prior for c in cands}, 0.60)
    explainer = ex.MetaExplainer(
        Client(), PassEngine(analysis, "D5", score=-1.0), game, {"explainer": {}}, model="m", log_path=tmp_path / "a"
    )
    explainer.set_learner_color("W")
    result = explainer.tool_evaluate_move(pos, "D5")
    assert result["if_not_played"]["move"] == "D5" and result["if_not_played"]["same_point"]
    assert result["if_not_played"]["by"] == "White (you)" and result["worth_vs_pass_points"] == 3.0
    far = explainer.tool_evaluate_move(pos, "Q4")
    assert far["if_not_played"] is None


def test_v4_a_move_that_rescues_a_short_group_says_so():
    # White D4 between Black C4 and E4 has 2 liberties; White D5 gives the pair 4.
    game, pos = play(["C4", "D4", "E4", "Q16", "Q4"])  # White to move
    local = ex.local_facts(game, pos, "D5")
    assert ex.rescued(local) == [{"stone": "D4", "stones": 1, "liberties": 2}]
    assert "It gives my stones at D4 4 liberties instead of 2." in ex.local_text(local, "my", "your")
    assert ex.rescued(ex.local_facts(game, pos, "K10")) == []


def test_v4_1_a_threat_that_does_nothing_to_the_stones_is_not_a_reason(tmp_path):
    game, pos = play(["C4", "D4", "E4", "Q16"])  # Black (Sensei) plays K10; after a pass White's best is K13
    cands = (Candidate("K10", 0.6, 0.62, 150, 4.0, ("K10",)), Candidate("Q4", 0.4, 0.61, 50, 3.5, ("Q4",)))
    analysis = EngineAnalysis(pos, cands, {c.move: c.prior for c in cands}, 0.60)
    choice = Choice("K10", 0.8, 0.0, PositionClass.STRATEGIC, {"K10": 0.8}, ("K10", "Q4"))
    client = Client()
    explainer = ex.MetaExplainer(
        client, PassEngine(analysis, "K13", score=-1.0), game, {"explainer": {}}, model="m", log_path=tmp_path / "a"
    )
    out = explainer.explain(analysis, choice, LearnerEstimate(rank=-9.0, rank_sd=1.0, n_moves=40))
    assert out.grounded_on["threat"]["local"] and not ex.threat_sharp(out.grounded_on["threat"])
    user = client.calls[0]["messages"][1]["content"]
    assert "If I had passed" not in user and "Compared with passing, my move is worth about 3.0 points" in user
    assert explainer.tool_evaluate_move(pos, "K10")["if_not_played"] is None


def test_v4_1_forcing_exchange_from_the_engine_line(tmp_path):
    assert ex.forcing_exchange("D7", ("D7", "C6", "F4"), "F4", 19) == {"reply": "C6", "then": "F4"}
    assert ex.forcing_exchange("D7", ("D7", "Q16", "F4"), "F4", 19) is None  # the answer is not next to it
    assert ex.forcing_exchange("D7", ("D7", "C6", "K10"), "F4", 19) is None  # then not the top move
    assert ex.forcing_exchange("F4", ("F4", "C6", "F4"), "F4", 19) is None  # my move is the top move
    game, pos = play(["D4", "Q16", "C16", "Q3", "R4"])  # White (Sensei) to move
    cands = (
        Candidate("R14", 0.30, 0.62, 120, 4.5, ("R14",)),
        Candidate("Q5", 0.40, 0.61, 90, 4.2, ("Q5", "R5", "R14")),
    )
    analysis = EngineAnalysis(pos, cands, {c.move: c.prior for c in cands}, 0.60)
    choice = Choice("Q5", 0.8, 0.01, PositionClass.TACTICAL, {"Q5": 0.8}, ("R14", "Q5"))
    facts = ex.explanation_facts(analysis, choice, LearnerEstimate(rank=-9.0, rank_sd=1.0, n_moves=40), game=game)
    assert facts["forcing"] == {"reply": "R5", "then": "R14"}
    user = ex.build_explanation_messages(analysis, choice, None, max_sentences=4, facts=facts)[1]["content"]
    assert "it is a forcing move (sente): the engine expects your answer at R5 next to it" in user
    explainer = ex.MetaExplainer(
        Client(), Engine(analysis), game, {"explainer": {}}, model="m", log_path=tmp_path / "a"
    )
    assert explainer.tool_evaluate_move(pos, "Q5")["forcing_exchange"] == {"reply": "R5", "then": "R14"}
