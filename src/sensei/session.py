"""The app-side loop for one game (INTERFACES.md, "Session").

After each learner move: mistake check, situation tagging, profile update, and an immediate explanation when the
mistake falls in the current target topic. On Sensei's turn: analysis, learner policy, constrained selection,
explanation, and one line in logs/turns.jsonl with the timing of each component. At the end of the game: result
from the engine's score lead, curriculum update, rank estimate, next target topic, profile saved as JSON.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sensei.coords import gtp_to_rc, neighbors
from sensei.interfaces import (
    Choice,
    Curriculum,
    Engine,
    EngineAnalysis,
    Explainer,
    Explanation,
    Game,
    LearnerEstimate,
    LearnerModel,
    MistakeTopics,
    Move,
    Position,
    PositionClass,
    Selector,
)
from sensei.ranks import label_to_numeric, numeric_to_label
from sensei.topics import loss_bin, situation_bag

UNTAGGED = "untagged"


# Profile ---------------------------------------------------------------------------------------------------


@dataclass
class TopicRecord:
    moves: int = 0
    mistakes: int = 0
    example: dict[str, Any] | None = None

    @property
    def rate(self) -> float:
        return self.mistakes / self.moves if self.moves else 0.0


@dataclass
class LearnerProfile:
    learner_id: str
    declared_rank: str
    games_played: int = 0
    rank_history: list[dict[str, Any]] = field(default_factory=list)
    topics: dict[str, TopicRecord] = field(default_factory=dict)
    curriculum_state: dict[str, list[float]] = field(default_factory=dict)
    target_topic: str | None = None
    updated: str | None = None

    def topic(self, name: str) -> TopicRecord:
        return self.topics.setdefault(name, TopicRecord())

    def current_rank(self) -> LearnerEstimate:
        if self.rank_history:
            last = self.rank_history[-1]
            return LearnerEstimate(
                rank=float(last["rank"]), rank_sd=float(last["rank_sd"]), n_moves=int(last["n_moves"])
            )
        return LearnerEstimate(rank=label_to_numeric(self.declared_rank), rank_sd=3.0, n_moves=0)

    def weakest_topics(self, n: int = 2, min_moves: int = 3) -> list[tuple[str, TopicRecord]]:
        """Topics by mistake rate, descending; topics with fewer than min_moves moves are ignored."""
        items = [(k, v) for k, v in self.topics.items() if v.moves >= min_moves and k != UNTAGGED]
        items.sort(key=lambda kv: (-kv[1].rate, -kv[1].mistakes, kv[0]))
        return items[:n]

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["topics"] = {k: asdict(v) for k, v in self.topics.items()}
        return data

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> LearnerProfile:
        topics = {k: TopicRecord(**v) for k, v in dict(data.get("topics", {})).items()}
        return cls(
            learner_id=str(data["learner_id"]),
            declared_rank=str(data["declared_rank"]),
            games_played=int(data.get("games_played", 0)),
            rank_history=list(data.get("rank_history", [])),
            topics=topics,
            curriculum_state={k: list(v) for k, v in dict(data.get("curriculum_state", {})).items()},
            target_topic=data.get("target_topic"),
            updated=data.get("updated"),
        )

    def save(self, profiles_dir: Path) -> Path:
        profiles_dir = Path(profiles_dir)
        profiles_dir.mkdir(parents=True, exist_ok=True)
        self.updated = datetime.now(UTC).isoformat(timespec="seconds")
        path = profiles_dir / f"{self.learner_id}.json"
        path.write_text(json.dumps(self.to_json(), indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, profiles_dir: Path, learner_id: str, declared_rank: str) -> LearnerProfile:
        path = Path(profiles_dir) / f"{learner_id}.json"
        if path.is_file():
            return cls.from_json(json.loads(path.read_text(encoding="utf-8")))
        return cls(learner_id=learner_id, declared_rank=declared_rank)


# Board helpers for the local shape tags --------------------------------------------------------------------


def _group_and_liberties(board: Mapping[tuple[int, int], str], rc: tuple[int, int], size: int) -> tuple[set, set]:
    color = board[rc]
    stack, seen, libs = [rc], {rc}, set()
    while stack:
        p = stack.pop()
        for q in neighbors(p, size):
            c = board.get(q)
            if c is None:
                libs.add(q)
            elif c == color and q not in seen:
                seen.add(q)
                stack.append(q)
    return seen, libs


def local_tags(
    board_before: Mapping[Move, str], board_after: Mapping[Move, str], move: Move, color: str, size: int
) -> list[str]:
    """capture, atari and contact for one move, from the boards before and after it (GTP keyed)."""
    rc = gtp_to_rc(move, size)
    if rc is None:
        return []
    before = {gtp_to_rc(k, size): v for k, v in board_before.items()}
    after = {gtp_to_rc(k, size): v for k, v in board_after.items()}
    opponent = "W" if color == "B" else "B"
    tags: list[str] = []
    if len(after) < len(before) + 1:
        tags.append("capture")
    if any(before.get(q) == opponent for q in neighbors(rc, size)):
        tags.append("contact")
    for q in neighbors(rc, size):
        if after.get(q) == opponent:
            _, libs = _group_and_liberties(after, q, size)
            if len(libs) == 1:
                tags.append("atari")
                break
    return tags


# Session ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class MoveCheck:
    """The mistake check and situation bag of one move (INTERFACES.md, "Mistakes and mistake topics")."""

    loss: float
    is_mistake: bool
    severity: str | None
    best: Move
    bag: dict[str, int]
    position_class: PositionClass


def check_move(
    game: Game,
    analyze: Callable[[Position], EngineAnalysis],
    selector: Any,
    before_pos: Position,
    before: EngineAnalysis,
    move: Move,
    after_pos: Position,
    mistake_threshold: float,
    loss_bins: Sequence[float],
) -> MoveCheck:
    """Loss V* - V(played), mistake flag, severity, the engine's best reliable move and the situation bag.

    `before` is the analysis of `before_pos`; `analyze` is called once more, on `after_pos`, only when the played
    move is not a reliable candidate. The app (Session.learner_move) and the topic fitting (scripts/fit_topics.py)
    both use this function, so the fitted topics see exactly the bags the app produces.
    """
    v_star = selector.best_value(before)
    reliable = {c.move: c for c in selector.reliable(before)}
    if move in reliable:
        v_played = float(reliable[move].value)
    elif game.is_terminal(after_pos):
        v_played = v_star
    else:
        v_played = 1.0 - float(analyze(after_pos).root_value)
    loss = max(0.0, v_star - v_played)
    is_mistake = loss > mistake_threshold
    severity = loss_bin(loss, loss_bins) if is_mistake else None

    best = max(reliable.values(), key=lambda c: (float(c.value), int(c.visits))).move
    size = int(before_pos.meta.get("size", 19))
    previous = before_pos.moves[-1][1] if before_pos.moves else None
    tags = local_tags(game.board(before_pos), game.board(after_pos), move, before_pos.to_move, size)
    position_class = selector.position_class(before)
    bag = situation_bag(
        ply=len(before_pos.moves),
        played_rc=gtp_to_rc(move, size),
        best_rc=gtp_to_rc(best, size),
        previous_rc=gtp_to_rc(previous, size) if previous else None,
        position_class=position_class,
        local_tags=tags,
        size=size,
    )
    return MoveCheck(loss, is_mistake, severity, best, dict(bag), position_class)


@dataclass
class MoveReport:
    """What the UI shows after a learner move."""

    move: Move
    loss: float
    is_mistake: bool
    severity: str | None
    topic: str
    best_move: Move
    explanation: Explanation | None


@dataclass
class SenseiMove:
    """What the UI shows after Sensei's move."""

    move: Move
    choice: Choice
    explanation: Explanation
    timing_ms: dict[str, float]


class Session:
    """One game between a learner and Sensei. All engine, model and profile side effects happen here."""

    def __init__(
        self,
        *,
        game: Game,
        engine: Engine,
        selector: Selector,
        learner: LearnerModel,
        topics: MistakeTopics | None,
        curriculum: Curriculum,
        explainer: Explainer,
        cfg: Mapping[str, Any],
        profile: LearnerProfile,
        rng: Any,
        learner_color: str = "B",
        turns_log: Path | None = None,
        hardware: Mapping[str, Any] | None = None,
        topic_names: Sequence[str] | None = None,
    ) -> None:
        self.game, self.engine, self.selector, self.learner = game, engine, selector, learner
        self.topics, self.curriculum, self.explainer = topics, curriculum, explainer
        self.cfg, self.profile, self.rng = cfg, profile, rng
        self.learner_color = learner_color
        self.turns_log = Path(turns_log) if turns_log else None
        self.hardware = dict(hardware or {})
        self.topic_names = list(topic_names) if topic_names else list(curriculum.arms)
        play = cfg["play"]
        self.max_visits = int(play["max_visits"])
        self.epsilon = float(play["epsilon"])
        self.mistake_threshold = float(play["mistake_threshold"])
        self.loss_bins = list(cfg.get("topics", {}).get("loss_bins", [0.05, 0.10, 0.20]))
        self.pos: Position = game.initial_position()
        self.history: list[tuple[Position, Move]] = []  # the learner's moves, for rank estimation
        self.game_topic_counts: dict[str, list[int]] = {}  # topic -> [mistakes, non-mistakes]
        self.reports: list[MoveReport] = []
        self._analysis_cache: dict[tuple, EngineAnalysis] = {}
        if profile.curriculum_state and hasattr(curriculum, "load_state"):
            curriculum.load_state({k: tuple(v) for k, v in profile.curriculum_state.items()})
        if profile.target_topic is None and self.topic_names:
            profile.target_topic = curriculum.select(rng)
        self._sync_explainer()

    def profile_summary(self) -> dict[str, Any]:
        """What Ask Sensei's get_learner_profile tool returns."""
        est = self.profile.current_rank()
        return {
            "rank_label": numeric_to_label(est.rank),
            "rank": round(est.rank, 2),
            "rank_sd": round(est.rank_sd, 2),
            "games_played": self.profile.games_played,
            "weak_topics": [
                {"topic": k, "mistakes": v.mistakes, "moves": v.moves, "rate": round(v.rate, 3)}
                for k, v in self.profile.weakest_topics(3)
            ],
            "target_topic": self.profile.target_topic,
        }

    def _sync_explainer(self) -> None:
        """Point Ask Sensei at the current position and the current profile (when the explainer supports it)."""
        if hasattr(self.explainer, "set_position"):
            self.explainer.set_position(self.pos)
        if hasattr(self.explainer, "set_learner_color"):
            self.explainer.set_learner_color(self.learner_color)
        if hasattr(self.explainer, "set_learner_profile"):
            self.explainer.set_learner_profile(self.profile_summary())

    # Engine access with a per-position cache ---------------------------------------------------------------

    def _key(self, pos: Position) -> tuple:
        return (
            pos.moves,
            pos.to_move,
            pos.meta.get("size", 19),
            pos.meta.get("komi", 7.5),
            pos.meta.get("rules", "chinese"),
        )

    def analyze(self, pos: Position) -> EngineAnalysis:
        key = self._key(pos)
        if key not in self._analysis_cache:
            self._analysis_cache[key] = self.engine.analyze(pos, self.max_visits)
        return self._analysis_cache[key]

    def temperature(self) -> float:
        rank = self.profile.current_rank().rank
        for band in self.cfg["play"].get("temperature_by_rank", []):
            if rank <= float(band["max_rank"]):
                return float(band["temperature"])
        return 0.0

    # Learner move ------------------------------------------------------------------------------------------

    def learner_move(self, move: Move) -> MoveReport:
        if self.pos.to_move != self.learner_color:
            raise RuntimeError("it is not the learner's turn")
        before_pos = self.pos
        before = self.analyze(before_pos)
        after_pos = self.game.play(before_pos, move)  # raises ValueError on an illegal move
        check = check_move(
            self.game, self.analyze, self.selector, before_pos, before, move, after_pos,
            self.mistake_threshold, self.loss_bins,
        )  # fmt: skip
        loss, is_mistake, severity, best = check.loss, check.is_mistake, check.severity, check.best
        topic = self._topic_of(check.bag)

        record = self.profile.topic(topic)
        record.moves += 1
        counts = self.game_topic_counts.setdefault(topic, [0, 0])
        if is_mistake:
            record.mistakes += 1
            counts[0] += 1
            if record.example is None:
                record.example = {
                    "moves": [list(m) for m in before_pos.moves],
                    "played": move,
                    "best": best,
                    "loss": round(loss, 4),
                    "severity": severity,
                }
        else:
            counts[1] += 1

        explanation = None
        if is_mistake and topic == self.profile.target_topic and hasattr(self.explainer, "explain_mistake"):
            explanation = self.explainer.explain_mistake(
                before, move, best, loss, self.profile.current_rank(), topic, check.position_class
            )

        self.history.append((before_pos, move))
        self.pos = after_pos
        self._sync_explainer()
        report = MoveReport(move, loss, is_mistake, severity, topic, best, explanation)
        self.reports.append(report)
        return report

    def _topic_of(self, bag: Mapping[str, int]) -> str:
        if self.topics is None or not self.topic_names:
            return UNTAGGED
        dist = list(self.topics.assign(bag))
        k = max(range(len(dist)), key=lambda i: dist[i])
        return self.topic_names[k] if k < len(self.topic_names) else UNTAGGED

    # Sensei move -------------------------------------------------------------------------------------------

    def sensei_move(self) -> SenseiMove:
        if self.pos.to_move == self.learner_color:
            raise RuntimeError("it is the learner's turn")
        t0 = time.perf_counter()
        analysis = self.analyze(self.pos)
        t1 = time.perf_counter()
        learner_policy = self.learner.policy(self.pos, self.game.legal_moves(self.pos))
        t2 = time.perf_counter()
        choice = self.selector.choose(analysis, learner_policy, self.epsilon, self.temperature(), self.rng)
        explanation = self.explainer.explain(analysis, choice, self.profile.current_rank())
        t3 = time.perf_counter()
        self.pos = self.game.play(self.pos, choice.move)
        self._sync_explainer()
        timing = {"engine_ms": (t1 - t0) * 1e3, "human_policy_ms": (t2 - t1) * 1e3, "explanation_ms": (t3 - t2) * 1e3}
        self._log_turn(choice, explanation, timing)
        return SenseiMove(choice.move, choice, explanation, timing)

    def _log_turn(self, choice: Choice, explanation: Explanation, timing: Mapping[str, float]) -> None:
        if self.turns_log is None:
            return
        self.turns_log.parent.mkdir(parents=True, exist_ok=True)
        line = {
            "ts": round(time.time(), 3),
            "learner": self.profile.learner_id,
            "ply": len(self.pos.moves),
            "move": choice.move,
            "value_loss": round(choice.value_loss, 5),
            "position_class": choice.position_class.value,
            "prompt_tokens": explanation.prompt_tokens,
            "completion_tokens": explanation.completion_tokens,
            "cost_usd": explanation.grounded_on.get("cost_usd")
            if isinstance(explanation.grounded_on, Mapping)
            else None,
            **{k: round(v, 2) for k, v in timing.items()},
            "hardware": self.hardware,
        }
        with self.turns_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")

    # End of game -------------------------------------------------------------------------------------------

    def result(self) -> dict[str, Any]:
        """Winner from the sign of the main net's score lead at the final position (area scoring)."""
        if self.game.is_terminal(self.pos):
            final = self.pos
            # KataGo analyzes the side to move even after two passes; use the last non-terminal position's analysis
            # perspective: score lead for the side to move at the final position.
            analysis = self.engine.analyze(
                Position(final.game, final.moves[:-2], final.moves[-2][0], final.meta), self.max_visits
            )
            to_move = final.moves[-2][0]
        else:
            analysis = self.analyze(self.pos)
            to_move = self.pos.to_move
        lead = None
        if hasattr(self.engine, "analyze_raw"):  # rootInfo.scoreLead is the root's own estimate
            _, raw = self.engine.analyze_raw(analysis.position, self.max_visits)
            lead = raw.get("rootInfo", {}).get("scoreLead")
        if lead is None and analysis.candidates and analysis.candidates[0].score_lead is not None:
            lead = analysis.candidates[0].score_lead
        if lead is None:
            lead = (float(analysis.root_value) - 0.5) * 2.0  # fallback sign when no score lead is reported
        black_lead = lead if to_move == "B" else -lead
        winner = "B" if black_lead > 0 else "W"
        return {"winner": winner, "black_lead": round(black_lead, 1), "learner_won": winner == self.learner_color}

    def end_game(self, profiles_dir: Path | None = None) -> dict[str, Any]:
        outcome = self.result()
        observations = {
            t: (float(c[0]), float(c[1])) for t, c in self.game_topic_counts.items() if t in self.curriculum.arms
        }
        self.curriculum.update(observations)
        estimate = self.learner.estimate(self.history)
        self.profile.rank_history.append(
            {
                "game": self.profile.games_played + 1,
                "rank": estimate.rank,
                "rank_sd": estimate.rank_sd,
                "n_moves": estimate.n_moves,
            }
        )
        self.profile.games_played += 1
        if hasattr(self.curriculum, "state"):
            self.profile.curriculum_state = {k: list(v) for k, v in self.curriculum.state().items()}
        next_target = self.curriculum.select(self.rng) if self.curriculum.arms else None
        self.profile.target_topic = next_target
        if profiles_dir is not None:
            self.profile.save(profiles_dir)
        self._sync_explainer()
        weak = self.profile.weakest_topics(2)
        return {
            "result": outcome,
            "rank": {
                "estimate": estimate.rank,
                "label": numeric_to_label(estimate.rank),
                "sd": estimate.rank_sd,
                "n_moves": estimate.n_moves,
            },
            "weakest_topics": [
                {"topic": k, "mistakes": v.mistakes, "moves": v.moves, "rate": round(v.rate, 3), "example": v.example}
                for k, v in weak
            ],
            "next_target": next_target,
            "mistakes_this_game": sum(c[0] for c in self.game_topic_counts.values()),
            "moves_this_game": len(self.history),
        }


def topics_fallback(names: Iterable[str]) -> list[str]:
    """Hand-coded category names used when fitted topics are unreadable (configs/default.yaml)."""
    return list(names)
