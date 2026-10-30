"""Terminal play for checking the engine stack (TASKS.md T3).

    python -m sensei.cli --self-check                       # 20 random legal moves with GoGame, no engine
    python -m sensei.cli --auto --engine raw --moves 20     # KataGo plays both sides (most-visited move)
    python -m sensei.cli --auto --engine sensei --moves 20  # Sensei's selector plays White against the raw engine
    python -m sensei.cli --engine sensei --rank 10k         # you play Black against Sensei; type GTP moves
    add --fake to any of these to use tests/fake_katago.py instead of KataGo, --explain to add Meta API explanations

The final line reports the score lead from the engine (Black's perspective), which decides the game (SCOPE.md).
"""

from __future__ import annotations

import argparse
import random
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from sensei.coords import PASS
from sensei.game_go import GoGame
from sensei.interfaces import Position

ROOT = Path(__file__).resolve().parents[2]
FAKE_ENGINE = ROOT / "tests" / "fake_katago.py"


def load_cfg() -> dict[str, Any]:
    return yaml.safe_load((ROOT / "configs" / "default.yaml").read_text(encoding="utf-8"))


def self_check(moves: int, seed: int) -> int:
    game, rng = GoGame(), random.Random(seed)
    pos = game.initial_position()
    for _ in range(moves):
        legal = [m for m in game.legal_moves(pos) if m != PASS]
        pos = game.play(pos, rng.choice(legal) if legal else PASS)
    print(game.render(pos))
    print(f"self-check ok: {len(pos.moves)} legal moves played")
    return 0


def make_engine(fake: bool):
    from sensei.engine_katago import KataGoEngine  # imported lazily: the self-check needs no engine

    if fake:
        return KataGoEngine(
            exe=sys.executable,
            exe_args=[str(FAKE_ENGINE)],
            model="fake-main.bin.gz",
            human_model="fake-human.bin.gz",
            config="fake-analysis.cfg",
            query_timeout=120.0,
        )
    return KataGoEngine.from_env()


def black_score_lead(engine: Any, pos: Position, visits: int) -> float | None:
    analysis, raw = engine.analyze_raw(pos, visits)
    lead = raw.get("rootInfo", {}).get("scoreLead")
    if lead is None:
        return None
    return float(lead) if pos.to_move == "B" else -float(lead)


class Player:
    """Chooses moves for one side: the raw engine (most visits) or Sensei (constrained selector)."""

    def __init__(self, kind: str, engine: Any, cfg: dict[str, Any], rank: str, explainer: Any = None) -> None:
        self.kind, self.engine, self.cfg, self.explainer = kind, engine, cfg, explainer
        self.visits = int(cfg["play"]["max_visits"])
        self.rng = np.random.default_rng(0)
        if kind == "sensei":
            from sensei.learner import HumanSLLearner
            from sensei.selector import ConstrainedSelector

            pre = yaml.safe_load((ROOT / "configs" / "prereg.yaml").read_text(encoding="utf-8"))
            self.selector = ConstrainedSelector(
                delta=float(pre["selector"]["delta"]),
                min_visits_abs=int(pre["engine"]["min_candidate_visits_abs"]),
                min_visits_frac=float(pre["engine"]["min_candidate_visits_frac"]),
                entropy_low=float(cfg["selector"]["entropy_low"]),
                entropy_high=float(cfg["selector"]["entropy_high"]),
            )
            self.learner = HumanSLLearner(
                engine, pre["learner"]["profile_grid"], rank, int(pre["learner"]["min_moves_for_estimate"])
            )

    def move(self, game: GoGame, pos: Position) -> tuple[str, str]:
        analysis = self.engine.analyze(pos, self.visits)
        if self.kind == "raw" or not analysis.candidates:
            best = analysis.candidates[0].move if analysis.candidates else PASS
            return best, f"raw engine: {best} ({100 * analysis.root_value:.1f}% for {pos.to_move})"
        policy = self.learner.policy(pos, game.legal_moves(pos))
        choice = self.selector.choose(analysis, policy, float(self.cfg["play"]["epsilon"]), 0.0, self.rng)
        note = (
            f"sensei: {choice.move} (cost {100 * choice.value_loss:.1f}% winrate, "
            f"{choice.position_class.value}, {len(choice.admissible)} admissible)"
        )
        if self.explainer is not None:
            note += "\n  " + self.explainer.explain(analysis, choice, self.learner.estimate([])).text
        return choice.move, note


def run(args: argparse.Namespace) -> int:
    if args.self_check:
        return self_check(args.moves, args.seed)
    cfg = load_cfg()
    game = GoGame()
    engine = make_engine(args.fake)
    try:
        explainer = None
        if args.explain:
            from sensei.explainer import MetaExplainer
            from sensei.metaapi import make_client

            client, model = make_client()
            explainer = MetaExplainer(client, engine, game, cfg, model=model)
        white = Player(args.engine, engine, cfg, args.rank, explainer)
        black = Player("raw", engine, cfg, args.rank) if args.auto else None
        pos = game.initial_position(size=19, komi=7.5, rules="chinese")
        while len(pos.moves) < args.moves and not game.is_terminal(pos):
            if pos.to_move == "W":
                move, note = white.move(game, pos)
                print(note)
            elif black is not None:
                move, note = black.move(game, pos)
                print(note)
            else:
                print(game.render(pos))
                move = input("your move (GTP, e.g. Q16, or pass; q to quit): ").strip()
                if move.lower() in ("q", "quit", "exit"):
                    break
            try:
                pos = game.play(pos, move)
            except ValueError as err:
                print(f"  {err}")
        print(game.render(pos))
        lead = black_score_lead(engine, pos, int(cfg["play"]["max_visits"]))
        if lead is not None:
            print(f"score lead for Black: {lead:+.1f} -> {'Black' if lead > 0 else 'White'} ahead")
        print(f"game ok: {len(pos.moves)} moves")
    finally:
        engine.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--engine", choices=["raw", "sensei"], default="sensei", help="who plays White")
    ap.add_argument("--auto", action="store_true", help="the raw engine plays Black instead of you")
    ap.add_argument("--moves", type=int, default=20, help="stop after this many plies")
    ap.add_argument("--rank", default="10k", help="declared learner rank for Sensei's learner model")
    ap.add_argument("--fake", action="store_true", help="use tests/fake_katago.py instead of KataGo")
    ap.add_argument("--explain", action="store_true", help="explain Sensei's moves with the Meta Model API")
    ap.add_argument("--self-check", action="store_true", help="random legal moves only, no engine")
    ap.add_argument("--seed", type=int, default=0)
    return ap


def main(argv: Sequence[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
