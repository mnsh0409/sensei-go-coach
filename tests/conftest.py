from __future__ import annotations

import os
from pathlib import Path

import pytest

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except ImportError:
    pass

from sensei.interfaces import Candidate, EngineAnalysis, Position


def make_analysis(cands, policy=None, to_move="B"):
    """cands: list of (move, prior, value, visits). Policy defaults to the priors, renormalized."""
    pos = Position(game="go", moves=(), to_move=to_move, meta={"size": 19, "komi": 7.5})
    candidates = tuple(Candidate(move=m, prior=p, value=v, visits=n) for m, p, v, n in cands)
    if policy is None:
        total = sum(p for _, p, _, _ in cands)
        policy = {m: p / total for m, p, _, _ in cands}
    return EngineAnalysis(
        position=pos,
        candidates=candidates,
        policy=policy,
        root_value=max(v for _, _, v, _ in cands),
    )


@pytest.fixture
def analysis_factory():
    return make_analysis


def require_env(*names: str) -> None:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        pytest.skip(f"prerequisite missing: {', '.join(missing)} not set in .env")
