"""Interface contracts for Sensei.

This module is the source of truth for types. INTERFACES.md explains each contract and fixes the math.
Do not change anything here without the human's approval (see CLAUDE.md).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

Move = str
"""GTP coordinate for Go ("Q16", "pass"); UCI for chess ("e2e4")."""

Color = str
""""B" or "W" for Go; "w" or "b" for chess."""


@dataclass(frozen=True)
class Position:
    """Immutable game state. Not hashable (meta is a dict): cache on (moves, to_move, size, komi, rules)."""

    game: str
    moves: tuple[tuple[Color, Move], ...]
    to_move: Color
    meta: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Candidate:
    """One engine root candidate. value and score_lead are for the side to move at the root."""

    move: Move
    prior: float
    value: float
    visits: int
    score_lead: float | None = None
    pv: tuple[Move, ...] = ()


@dataclass(frozen=True)
class EngineAnalysis:
    """Engine output for one position, all values for the side to move at the root.

    candidates are sorted by visits, descending; policy is pi_E over legal moves; ownership (Go) is row-major
    from the top left, positive = owned by the side to move.
    """

    position: Position
    candidates: tuple[Candidate, ...]
    policy: Mapping[Move, float]
    root_value: float
    ownership: tuple[float, ...] | None = None


class PositionClass(StrEnum):
    ONLY_MOVE = "only_move"
    TACTICAL = "tactical"
    STRATEGIC = "strategic"


@dataclass(frozen=True)
class Choice:
    move: Move
    instructiveness: float
    value_loss: float
    position_class: PositionClass
    scores: Mapping[Move, float]
    admissible: tuple[Move, ...]


@dataclass(frozen=True)
class LearnerEstimate:
    rank: float
    rank_sd: float
    n_moves: int


@dataclass(frozen=True)
class Explanation:
    text: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    grounded_on: Mapping[str, Any]


@runtime_checkable
class Game(Protocol):
    name: str

    def initial_position(self, **kwargs: Any) -> Position: ...

    def legal_moves(self, pos: Position) -> Sequence[Move]: ...

    def play(self, pos: Position, move: Move) -> Position:
        """Return the new position. Raise ValueError for an illegal move."""
        ...

    def is_terminal(self, pos: Position) -> bool: ...

    def result(self, pos: Position) -> float | None:
        """Score for the first player (Black in Go, White in chess): 1, 0.5 or 0.

        None if the game is not over or cannot score itself; GoGame always returns None because Go scoring is
        delegated to the sign of the engine's score lead (SCOPE.md, adjudication).
        """
        ...

    def render(self, pos: Position) -> str: ...


@runtime_checkable
class Engine(Protocol):
    def analyze(self, pos: Position, max_visits: int) -> EngineAnalysis: ...

    def close(self) -> None: ...


@runtime_checkable
class HumanPolicySource(Protocol):
    def human_policy(self, pos: Position, profile: str) -> Mapping[Move, float]:
        """Rank-conditioned human policy over legal moves, summing to 1. Profile e.g. "rank_10k"."""
        ...


@runtime_checkable
class LearnerModel(Protocol):
    def policy(self, pos: Position, legal: Sequence[Move]) -> Mapping[Move, float]: ...

    def estimate(self, history: Sequence[tuple[Position, Move]]) -> LearnerEstimate: ...


@runtime_checkable
class Selector(Protocol):
    def choose(
        self,
        analysis: EngineAnalysis,
        learner_policy: Mapping[Move, float],
        epsilon: float,
        temperature: float,
        rng: Any,
    ) -> Choice: ...


@runtime_checkable
class Explainer(Protocol):
    def explain(
        self,
        analysis: EngineAnalysis,
        choice: Choice,
        learner: LearnerEstimate,
        lang: str = "en",
    ) -> Explanation: ...


@runtime_checkable
class MistakeTopics(Protocol):
    n_topics: int

    def fit(self, bags: Sequence[Mapping[str, int]]) -> None: ...

    def assign(self, bag: Mapping[str, int]) -> Sequence[float]: ...

    def describe(self, k: int, top_n: int = 8) -> Sequence[tuple[str, float]]: ...


@runtime_checkable
class Curriculum(Protocol):
    arms: Sequence[str]

    def select(self, rng: Any) -> str: ...

    def update(self, observations: Mapping[str, tuple[float, float]]) -> None:
        """One call per round. Maps arm -> (successes, failures); a success is an observed mistake."""
        ...
