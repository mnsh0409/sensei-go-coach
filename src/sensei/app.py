"""Web app for Sensei: a FastAPI backend plus one static page (TASKS.md T9, SCOPE.md "The one user journey").

Modes
-----
live    KataGo from .env (`KataGoEngine.from_env`), the Meta Model API for explanations and Ask Sensei. Mistake topics
        come from `topics.model` and `topics.names` (T7); without them every move is tagged "untagged".
fake    `tests/fake_katago.py` as the engine and a `CannedExplainer` (no network), so the whole UI works offline.
        Demo-only deviations, all labelled by the "demo (fake engine)" badge: the mistake threshold is 0.01 (the fake
        engine's candidate values differ by at most 0.06, so 0.05 would almost never fire), the topics are three
        region stand-ins, and profiles go to `<profiles_dir>/fake/` so they never mix with real ones.
replay  no engine and no model: plays back recorded sessions (`sessions/*.json`, `results/*.json`, and the file given
        to `--replay FILE`, which becomes the default; `--replay` alone lists the directories only). This is the mode
        to host publicly: it spends no API credits and needs no GPU.

    python -m sensei.app [--fake | --replay [FILE]] [--host 127.0.0.1] [--port 8000] [--backend cuda] [--seed N]

Endpoints (JSON; the learner is always Black and moves first)
-------------------------------------------------------------
GET  /                      the page (src/sensei/static/index.html, no external dependencies)
GET  /api/info              mode, mode label, rank choices, board settings, default recording
POST /api/game              {learner_id, rank}: start a game (replaces the learner's active game), returns the state
POST /api/move              {learner_id, move}: play the learner's move (400 with the reason if illegal), then Sensei's
                            reply; returns {report, sensei, state}; `sensei` is null when the game is over
POST /api/pass              {learner_id}: the same as /api/move with "pass"
POST /api/ask               {learner_id, question}: returns {text, variations, tool_calls, ...}
POST /api/end               {learner_id}: Session.end_game summary (result, rank estimate, weakest topics, next target)
GET  /api/state             ?learner_id=...: board, last move, to move, move number, captures, profile, history
GET  /api/replay            recordings found in the sessions directory, in results/, and the --replay file
GET  /api/replay/{name}     one recording (format `sensei-session/1`)

One `Session` per learner id lives in memory with its own lock (engine and model calls block, so every handler runs its
work through `run_in_threadpool`). Each game also has its own explainer object, because `MetaExplainer` keeps the
current position and learner profile for Ask Sensei, and a shared one would mix up concurrent learners. The engine,
selector, game rules, topic model and API client are built once at startup and shared.

Every game is written to `sessions/<learner>_<timestamp>.json` after each event (move, ask, end), so an abandoned game
still replays. The file holds the moves (with captured stones), the move reports, Sensei's moves with their
explanations, the Ask Sensei exchanges with their variations, and the end-of-game summary.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import platform
import re
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import numpy as np
import yaml
from fastapi import FastAPI, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator

from sensei import __version__, metaapi
from sensei.coords import PASS, find_coords
from sensei.curriculum import ThompsonCurriculum
from sensei.engine_katago import KataGoEngine
from sensei.explainer import (
    MetaExplainer,
    allowed_coords_from_facts,
    coords_in,
    drop_ungrounded_sentences,
    explanation_facts,
    find_ungrounded_coords,
    mistake_facts,
    template_explanation,
    template_mistake,
)
from sensei.game_go import GoGame
from sensei.interfaces import EngineAnalysis, Explanation, MistakeTopics, Move, Position
from sensei.learner import HumanSLLearner
from sensei.selector import ConstrainedSelector
from sensei.session import UNTAGGED, LearnerProfile, MoveReport, SenseiMove, Session
from sensei.topics import LdaMistakeTopics

logger = logging.getLogger(__name__)

ROOT = metaapi.ROOT
STATIC_DIR = Path(__file__).resolve().parent / "static"
FAKE_ENGINE = ROOT / "tests" / "fake_katago.py"
FAKE_STDERR_LOG = ROOT / "logs" / "fake_katago_stderr.log"  # keeps the fake engine's noise out of the real KataGo log
DEFAULT_TOPICS_PATH = "results/topics_v1.json"

MODES = ("live", "fake", "replay")
MODE_LABELS = {"live": "live", "fake": "demo (fake engine)", "replay": "replay"}
RANKS: tuple[str, ...] = (*(f"{n}k" for n in range(20, 0, -1)), *(f"{n}d" for n in range(1, 6)))
DEFAULT_RANK = "10k"
LEARNER_ID_PATTERN = r"^[A-Za-z0-9_-]{1,40}$"
RECORDING_FORMAT = "sensei-session/1"
LEARNER_COLOR = "B"  # the learner is Black and moves first; Sensei is White

CANNED_MODEL = "canned-demo"
TEMPLATE_MODEL = "template (model unavailable)"
DEMO_MISTAKE_THRESHOLD = 0.01
DEMO_LOSS_BINS = [0.01, 0.02, 0.03]  # Session needs loss_bins[0] <= play.mistake_threshold
ASK_HISTORY_MESSAGES = 6
WARMUP_WORKERS = 8


# ---- small helpers -------------------------------------------------------------------------------------------------


def _deep_merge(base: Mapping[str, Any], over: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), Mapping):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _json_default(obj: Any) -> Any:
    if hasattr(obj, "item"):  # numpy scalars
        return obj.item()
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, set | frozenset):
        return sorted(obj)
    raise TypeError(f"{type(obj).__name__} is not JSON serializable")


def _jsonable(value: Any) -> Any:
    """A JSON round trip, so responses and recordings only hold plain lists, dicts, strings and numbers."""
    return json.loads(json.dumps(value, default=_json_default))


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _join_and(items: Sequence[str]) -> str:
    items = [str(i) for i in items]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _topic_text(topic: str) -> str:
    return str(topic).replace("_", " ")


def _board_size(pos: Position) -> int:
    return int(pos.meta.get("size", 19))


# ---- stand-ins for fake mode ---------------------------------------------------------------------------------------


class DemoTopics:
    """Mistake topics for fake mode: one topic per region token of the situation bag (corner, edge, center).

    It implements `MistakeTopics` only to exercise the curriculum and the summary offline; the real topics are the
    fitted `LdaMistakeTopics` (T7), loaded by `load_topics`.
    """

    names = ("corner_moves", "edge_moves", "center_moves")
    n_topics = 3
    _tokens = ("region:corner", "region:edge", "region:center")

    def fit(self, bags: Sequence[Mapping[str, int]]) -> None:
        return None

    def assign(self, bag: Mapping[str, int]) -> list[float]:
        for k, token in enumerate(self._tokens):
            if bag.get(token):
                return [1.0 if i == k else 0.0 for i in range(self.n_topics)]
        return [1.0 / self.n_topics] * self.n_topics

    def describe(self, k: int, top_n: int = 8) -> list[tuple[str, float]]:
        return [(self._tokens[k], 1.0)][:top_n]


def canned_move_text(facts: Mapping[str, Any]) -> str:
    """Three sentences about Sensei's move, built only from `explanation_facts` (no coordinate is invented)."""
    move = str(facts["move"])
    candidates = list(facts.get("candidates", ()))
    top = candidates[0] if candidates else None
    label = facts["learner"]["label"]
    position_class = str(facts["position_class"]).replace("_", "-")
    n = int(facts["n_admissible"])
    action = "Sensei passes" if move == PASS else f"Sensei plays {move}"
    if n <= 1:
        first = f"{action}, the only move that stays within the allowed winrate cost in this {position_class} position."
    else:
        first = f"{action}, one of {n} moves within the allowed winrate cost in this {position_class} position."
    if float(facts["instructiveness"]) > 0.0:
        second = (
            f"The engine likes it much more than a typical {label} player would, "
            "which makes it a good move to learn from."
        )
    else:
        second = f"A typical {label} player would consider it too, so it is a familiar move to compare against."
    loss = float(facts["value_loss_pct"])
    if top is None:
        third = f"It gives up {loss:.1f}% winrate against the engine's best move."
    elif top["move"] == move and loss <= 0.0:
        third = f"It is also the engine's most-searched candidate at {top['winrate_pct']:.1f}%, so it costs nothing."
    elif loss <= 0.0:
        third = (
            f"The engine's most-searched candidate is {top['move']} at {top['winrate_pct']:.1f}%, "
            "and this move gives up nothing against the engine's best."
        )
    else:
        third = (
            f"The engine's most-searched candidate is {top['move']} at {top['winrate_pct']:.1f}%, "
            f"and this move gives up {loss:.1f}% winrate against the engine's best."
        )
    return " ".join([first, second, third])


def canned_mistake_text(facts: Mapping[str, Any]) -> str:
    """Sentences about a learner's mistake, built only from `mistake_facts`.

    The page already states the cost and the engine's preferred move above this text, so it adds the engine's two
    winrates and the topic instead of repeating them.
    """
    played, best = str(facts["played"]), str(facts["best_move"])
    by_move = {c["move"]: c for c in facts.get("candidates", ())}
    if played in by_move and best in by_move:
        parts = [
            f"The engine rated {played} at {by_move[played]['winrate_pct']:.1f}% "
            f"and {best} at {by_move[best]['winrate_pct']:.1f}%."
        ]
    elif best in by_move:
        parts = [f"The engine rated {best} at {by_move[best]['winrate_pct']:.1f}%, ahead of {played}."]
    else:
        parts = [f"The engine preferred {best} to {played}, which cost {float(facts['loss_pct']):.1f}% winrate."]
    if facts.get("topic"):
        parts.append(
            f"This is a {_topic_text(facts['topic'])} situation, your current focus topic, "
            "so Sensei points it out right away."
        )
    return " ".join(parts)


_PROFILE_WORDS = ("rank", "profile", "weak", "topic", "how am i", "improve", "strength", "level", "progress")
CANNED_NOTE = "Demo answer from the engine's numbers, no language model."


class CannedExplainer(MetaExplainer):
    """Offline stand-in for `MetaExplainer` (fake mode): the same tools and the same `grounded_on` shapes, no client.

    Every sentence is assembled from the facts it is given (`explanation_facts`, `mistake_facts`) or from tool results,
    and the usual guard (`find_ungrounded_coords`) runs over the text as a safety net, so it cannot mention a
    coordinate that did not come from the engine. Answers are deterministic.
    """

    def __init__(self, engine: Any, game: Any, cfg: Mapping[str, Any]) -> None:
        super().__init__(None, engine, game, cfg, model=CANNED_MODEL)

    def _checked(self, text: str, allowed: set[str], size: int) -> tuple[str, dict[str, Any]]:
        guard: dict[str, Any] = {"regenerated": False, "dropped": [], "ungrounded": []}
        bad = find_ungrounded_coords(text, allowed, size)
        if bad:
            guard["ungrounded"] = list(bad)
            text, guard["dropped"] = drop_ungrounded_sentences(text, allowed, size)
        return text.strip(), guard

    def explain(self, analysis: EngineAnalysis, choice: Any, learner: Any, lang: str = "en") -> Explanation:
        facts = explanation_facts(analysis, choice, learner)
        self.set_position(analysis.position, analysis)
        self._last_learner = learner
        allowed = allowed_coords_from_facts(facts)
        text, guard = self._checked(canned_move_text(facts), allowed, _board_size(analysis.position))
        grounded = {**facts, "language": lang, "allowed_coords": sorted(allowed), "guard": guard}
        return Explanation(text=text, model=CANNED_MODEL, prompt_tokens=0, completion_tokens=0, grounded_on=grounded)

    def explain_mistake(
        self,
        analysis: EngineAnalysis,
        played: Move,
        best: Move,
        loss: float,
        learner: Any,
        topic: str | None = None,
        position_class: Any = None,
        lang: str = "en",
    ) -> Explanation:
        facts = mistake_facts(analysis, played, best, loss, learner, topic, position_class)
        allowed = {str(c["move"]).upper() for c in facts["candidates"]} | {played.upper(), best.upper()}
        allowed.discard(PASS.upper())
        text, guard = self._checked(canned_mistake_text(facts), allowed, _board_size(analysis.position))
        grounded = {**facts, "language": lang, "allowed_coords": sorted(allowed), "guard": guard}
        return Explanation(text=text, model=CANNED_MODEL, prompt_tokens=0, completion_tokens=0, grounded_on=grounded)

    def ask(
        self,
        question: str,
        history: Sequence[Mapping[str, Any]] = (),
        *,
        position: Position | None = None,
        lang: str = "en",
    ) -> Explanation:
        """Answer from tool results only: the named move's evaluation and the engine's reply line, the top candidates
        with their main line, or the learner profile, depending on what the question mentions."""
        pos = position if position is not None else self.position
        if pos is None:
            raise ValueError("no current position: call set_position() or explain() before ask()")
        size = _board_size(pos)
        text_q = str(question).strip()
        mentioned = find_coords(text_q.upper(), size)
        lowered = text_q.lower()
        tool_log: list[dict[str, Any]] = []
        variations: list[list[str]] = []

        def call(name: str, **arguments: Any) -> dict[str, Any]:
            raw = json.dumps(arguments)
            result = self._run_tool(pos, name, raw, variations)
            tool_log.append({"round": 1, "name": name, "arguments": raw, "result": result})
            return result

        top = call("analyze_position", top_k=3)
        candidates = list(top.get("candidates", ()))
        side = pos.to_move
        sentences: list[str] = []

        answered = False
        if mentioned:
            answered = self._answer_about_move(call, mentioned[0], side, sentences)
        elif any(word in lowered for word in _PROFILE_WORDS):
            self._answer_about_profile(call("get_learner_profile"), sentences)
            answered = True
        if not answered:
            if candidates:
                shown = [f"{c['move']} ({c['winrate_pct']:.1f}%)" for c in candidates]
                sentences.append(f"The engine's most-searched candidates for {side} are {_join_and(shown)}.")
                line = list(self.analysis_for(pos).candidates[0].pv[:4])
                if len(line) >= 2:
                    result = call("show_variation", moves=line)
                    if "error" not in result:
                        sentences.append(
                            f"Its main line runs {' '.join(result['moves'])} and ends at {result['winrate_pct']:.1f}% "
                            f"for {side}; it is drawn on the board."
                        )
            else:
                sentences.append("The engine returned no candidate moves for this position.")
        if len(variations) > 1:
            variations[:] = variations[-1:]  # the longest line only: the shorter ones are its prefixes
        sentences.append(CANNED_NOTE)

        allowed = set(find_coords(text_q.upper(), size)) | coords_in([entry["result"] for entry in tool_log], size)
        text, guard = self._checked(" ".join(sentences), allowed, size)
        grounded = {
            "question": question,
            "to_move": pos.to_move,
            "move_number": len(pos.moves) + 1,
            "tool_rounds": 1,
            "tool_calls": tool_log,
            "variations": variations,
            "allowed_coords": sorted(allowed),
            "language": lang,
            "guard": guard,
        }
        return Explanation(text=text, model=CANNED_MODEL, prompt_tokens=0, completion_tokens=0, grounded_on=grounded)

    @staticmethod
    def _answer_about_move(call: Callable[..., dict[str, Any]], move: str, side: str, out: list[str]) -> bool:
        """Evaluate the named move and draw the engine's reply line; False when the move is not playable here."""
        evaluation = call("evaluate_move", move=move)
        if "error" in evaluation:
            out.append(str(evaluation["error"]).rstrip(".") + ".")
            return False
        lead = "" if evaluation["score_lead"] is None else f" and a score lead of {evaluation['score_lead']:+.1f}"
        out.append(f"After {move} the engine gives {side} a {evaluation['winrate_pct']:.1f}% winrate{lead}.")
        if evaluation["move"] == evaluation["best_move"]:
            out.append("That is the engine's best move.")
        elif evaluation["loss_pct"] <= 0.0:
            out.append(
                f"That gives up nothing against the engine's best move, {evaluation['best_move']}, "
                f"which scores {evaluation['best_winrate_pct']:.1f}%."
            )
        else:
            out.append(
                f"That is {evaluation['loss_pct']:.1f}% below the engine's best move, {evaluation['best_move']}, "
                f"which scores {evaluation['best_winrate_pct']:.1f}%."
            )
        line = [evaluation["move"]]
        result = call("show_variation", moves=line)
        for _ in range(2):  # the engine's reply, then its reply to that: at most three moves on the board
            reply = None if "error" in result else result.get("engine_reply")
            if not reply or reply == PASS:
                break
            line = [*line, str(reply)]
            result = call("show_variation", moves=line)
        if "error" not in result and len(line) > 1:
            out.append(
                f"If the game continues {' '.join(line)}, the engine rates the position {result['winrate_pct']:.1f}% "
                f"for {side}; the line is drawn on the board."
            )
        return True

    @staticmethod
    def _answer_about_profile(profile: Mapping[str, Any], out: list[str]) -> None:
        label = profile.get("rank_label")
        if label:
            sd = profile.get("rank_sd")
            spread = f" (give or take {float(sd):.1f} ranks)" if sd else ""
            out.append(f"You are estimated at about {label}{spread}.")
        weak = [w for w in (profile.get("weak_topics") or ()) if int(w.get("mistakes", 0)) > 0]
        if weak:
            shown = [f"{_topic_text(w['topic'])} ({w['mistakes']} mistakes in {w['moves']} moves)" for w in weak]
            out.append(f"Your weakest topics so far are {_join_and(shown)}.")
        else:
            out.append("No weakness topic stands out yet.")
        target = profile.get("target_topic")
        if target and target != UNTAGGED:
            out.append(f"The current focus topic is {_topic_text(target)}.")


class ResilientExplainer:
    """Keeps a game going when the model fails: `explain` and `explain_mistake` fall back to the fact templates.

    `ask` has no sensible fallback text, so its errors propagate (the endpoint answers 502).
    """

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    @property
    def model(self) -> str:
        return str(getattr(self.inner, "model", "unknown"))

    def set_position(self, pos: Position, analysis: EngineAnalysis | None = None) -> None:
        self.inner.set_position(pos, analysis)

    def set_learner_profile(self, profile: Mapping[str, Any]) -> None:
        self.inner.set_learner_profile(profile)

    def _template(self, text: str, facts: Mapping[str, Any], allowed: set[str], lang: str, error: str) -> Explanation:
        guard = {"template": True, "error": error, "regenerated": False, "dropped": [], "ungrounded": []}
        grounded = {**facts, "language": lang, "allowed_coords": sorted(allowed), "guard": guard}
        return Explanation(text=text, model=TEMPLATE_MODEL, prompt_tokens=0, completion_tokens=0, grounded_on=grounded)

    def explain(self, analysis: EngineAnalysis, choice: Any, learner: Any, lang: str = "en") -> Explanation:
        try:
            return self.inner.explain(analysis, choice, learner, lang)
        except Exception as exc:  # noqa: BLE001 - any model or network failure must not stop the game
            logger.exception("explain failed; using the template explanation")
            facts = explanation_facts(analysis, choice, learner)
            allowed = allowed_coords_from_facts(facts)
            return self._template(template_explanation(facts), facts, allowed, lang, repr(exc))

    def explain_mistake(
        self,
        analysis: EngineAnalysis,
        played: Move,
        best: Move,
        loss: float,
        learner: Any,
        topic: str | None = None,
        position_class: Any = None,
        lang: str = "en",
    ) -> Explanation:
        try:
            return self.inner.explain_mistake(analysis, played, best, loss, learner, topic, position_class, lang)
        except Exception as exc:  # noqa: BLE001 - as above
            logger.exception("explain_mistake failed; using the template explanation")
            facts = mistake_facts(analysis, played, best, loss, learner, topic, position_class)
            allowed = {str(c["move"]).upper() for c in facts["candidates"]} | {played.upper(), best.upper()}
            allowed.discard(PASS.upper())
            return self._template(template_mistake(facts), facts, allowed, lang, repr(exc))

    def ask(
        self,
        question: str,
        history: Sequence[Mapping[str, Any]] = (),
        *,
        position: Position | None = None,
        lang: str = "en",
    ) -> Explanation:
        return self.inner.ask(question, history, position=position, lang=lang)


# ---- components built once at startup ------------------------------------------------------------------------------


def load_topics(cfg: Mapping[str, Any], root: Path = ROOT) -> tuple[MistakeTopics | None, list[str]]:
    """The fitted mistake topics and their names, or `(None, [])` when the app has to run untagged.

    The T7 contract (configs/default.yaml): `topics.model` is the JSON that scripts/fit_topics.py writes (default
    results/topics_v1.json, read with `LdaMistakeTopics.load`) and `topics.names` has one name per fitted topic, set
    by the human at the T7 gate (the readable topics' names, or the hand-coded `topics.fallback_categories`).

    Untagged means that every move is tagged "untagged", the curriculum has the single arm "untagged" and the summary
    lists no weakness topics. That is the normal state before T7. It is also the result, with a warning that says what
    to fix, when the model file cannot be read or when the names are missing or do not fit the model.
    """
    section = cfg.get("topics") or {}
    names = [str(n).strip() for n in section.get("names") or []]
    path = Path(str(section.get("model") or DEFAULT_TOPICS_PATH))
    path = path if path.is_absolute() else root / path
    if not path.is_file():
        if names:
            logger.warning("topics.names is set but there is no topic model at %s; moves will be 'untagged'", path)
        return None, []
    try:
        model = LdaMistakeTopics.load(path)
        n_topics = int(model.n_topics)
    except (OSError, AttributeError, KeyError, TypeError, ValueError):
        logger.warning("cannot load the mistake topics from %s; moves will be tagged 'untagged'", path, exc_info=True)
        return None, []
    if not names:
        problem = f"topics.names is empty (name the topics described in {path.with_suffix('.md').name} at the T7 gate)"
    elif len(names) != n_topics:
        problem = f"topics.names has {len(names)} entries but the model has {n_topics} topics"
    elif len(set(names)) != len(names) or "" in names or UNTAGGED in names:
        problem = f"topics.names must be unique, non-empty and not {UNTAGGED!r}"
    else:
        return model, names
    logger.warning("not using the topic model %s: %s; moves will be tagged 'untagged'", path, problem)
    return None, []


def _hardware_block(backend: str | None) -> dict[str, Any]:
    try:
        from eval.hardware import hardware_block  # the repository's eval package; absent in a bare install
    except ImportError:
        return {"host": platform.node(), "katago_backend": backend, "python": platform.python_version()}
    return hardware_block(backend)


@dataclass
class Components:
    """Everything built once at startup and shared by all games (plus per-game factories)."""

    mode: str
    cfg: dict[str, Any]
    prereg: dict[str, Any]
    game: GoGame = field(default_factory=GoGame)
    engine: Any = None
    selector: ConstrainedSelector | None = None
    topics: Any = None
    topic_names: list[str] = field(default_factory=list)
    client: Any = None
    model: str | None = None
    hardware: dict[str, Any] = field(default_factory=dict)
    profiles_dir: Path = field(default_factory=lambda: ROOT / "profiles")
    sessions_dir: Path = field(default_factory=lambda: ROOT / "sessions")
    turns_log: Path | None = None
    seed: int | None = None
    replay_file: Path | None = None

    @property
    def playable(self) -> bool:
        return self.mode != "replay"

    @property
    def arms(self) -> list[str]:
        """Curriculum arms: the topic names, or the single arm 'untagged' when no topics are fitted."""
        return list(self.topic_names) if self.topic_names else [UNTAGGED]

    def make_explainer(self) -> ResilientExplainer:
        """One explainer per game: it holds the current position and learner profile for Ask Sensei."""
        if self.mode == "fake":
            return ResilientExplainer(CannedExplainer(self.engine, self.game, self.cfg))
        return ResilientExplainer(MetaExplainer(self.client, self.engine, self.game, self.cfg, model=self.model))

    def close(self) -> None:
        if self.engine is not None:
            self.engine.close()


def build_components(
    mode: str,
    *,
    cfg_overrides: Mapping[str, Any] | None = None,
    profiles_dir: str | os.PathLike[str] | None = None,
    sessions_dir: str | os.PathLike[str] | None = None,
    turns_log: str | os.PathLike[str] | None = None,
    seed: int | None = None,
    replay_file: str | os.PathLike[str] | None = None,
    engine_stderr_log: str | os.PathLike[str] | None = None,
    backend: str | None = None,
) -> Components:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    cfg = metaapi.load_config()
    if mode == "fake":
        demo = {"play": {"mistake_threshold": DEMO_MISTAKE_THRESHOLD}, "topics": {"loss_bins": DEMO_LOSS_BINS}}
        cfg = _deep_merge(cfg, demo)
    if cfg_overrides:
        cfg = _deep_merge(cfg, cfg_overrides)
    prereg = yaml.safe_load((ROOT / "configs" / "prereg.yaml").read_text(encoding="utf-8"))

    base_profiles = ROOT / str(cfg.get("profiles_dir", "profiles"))
    if profiles_dir is not None:
        profiles = Path(profiles_dir)
    else:
        profiles = base_profiles / "fake" if mode == "fake" else base_profiles
    sessions = Path(sessions_dir) if sessions_dir is not None else ROOT / "sessions"
    pinned = None
    if replay_file is not None:
        pinned = Path(replay_file)
        if not pinned.is_file():
            raise FileNotFoundError(f"--replay file not found: {pinned}")
        if not _is_recording(_read_json(pinned)):
            raise ValueError(f"{pinned} is not a Sensei session recording (format {RECORDING_FORMAT})")

    comp = Components(
        mode=mode,
        cfg=cfg,
        prereg=prereg,
        profiles_dir=profiles,
        sessions_dir=sessions,
        seed=seed,
        replay_file=pinned,
    )
    if mode == "replay":
        return comp

    comp.selector = ConstrainedSelector(
        delta=float(prereg["selector"]["delta"]),
        min_visits_abs=int(prereg["engine"]["min_candidate_visits_abs"]),
        min_visits_frac=float(prereg["engine"]["min_candidate_visits_frac"]),
        entropy_low=float(cfg["selector"]["entropy_low"]),
        entropy_high=float(cfg["selector"]["entropy_high"]),
    )
    if mode == "fake":
        if not FAKE_ENGINE.is_file():
            raise FileNotFoundError(f"fake mode needs the repository's test engine at {FAKE_ENGINE}")
        comp.engine = KataGoEngine(
            exe=sys.executable,
            exe_args=[str(FAKE_ENGINE)],
            model="fake-main.bin.gz",
            human_model="fake-human.bin.gz",
            config="fake-analysis.cfg",
            stderr_log=FAKE_STDERR_LOG if engine_stderr_log is None else engine_stderr_log,
            query_timeout=120.0,
        )
        comp.topics, comp.topic_names = DemoTopics(), list(DemoTopics.names)
        comp.model = CANNED_MODEL
        # Synthetic timings stay out of logs/turns.jsonl, which feeds the poster: fake mode logs turns only on request.
        comp.turns_log = Path(turns_log) if turns_log is not None else None
        if comp.turns_log is not None:
            comp.hardware = _hardware_block("fake")
        return comp

    comp.engine = KataGoEngine.from_env(stderr_log=engine_stderr_log)
    try:
        comp.client, comp.model = metaapi.make_client()
    except Exception:
        comp.engine.close()
        raise
    comp.topics, comp.topic_names = load_topics(cfg)
    comp.hardware = _hardware_block(backend or os.environ.get("KATAGO_BACKEND") or None)
    comp.turns_log = Path(turns_log) if turns_log is not None else ROOT / "logs" / "turns.jsonl"
    return comp


# ---- recordings ----------------------------------------------------------------------------------------------------


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _is_recording(data: Any) -> bool:
    return isinstance(data, dict) and data.get("format") == RECORDING_FORMAT and isinstance(data.get("moves"), list)


class Recorder:
    """The JSON recording of one game, rewritten after every event so that an abandoned game still replays."""

    def __init__(self, path: Path, header: Mapping[str, Any]) -> None:
        self.path = path
        self._seq = 0
        self.data: dict[str, Any] = {
            "format": RECORDING_FORMAT,
            **header,
            "started": _now(),
            "updated": None,
            "finished": None,
            "status": "in_progress",
            "moves": [],
            "reports": [],
            "sensei_moves": [],
            "asks": [],
            "summary": None,
        }

    def _next(self) -> int:
        self._seq += 1
        return self._seq

    def add_move(self, ply: int, color: str, move: str, by: str, captured: Sequence[str]) -> None:
        self.data["moves"].append({"ply": ply, "color": color, "move": move, "by": by, "captured": list(captured)})
        self.save()

    def add_report(self, report: Mapping[str, Any]) -> None:
        self.data["reports"].append({"seq": self._next(), **report})
        self.save()

    def add_sensei(self, entry: Mapping[str, Any]) -> None:
        self.data["sensei_moves"].append({"seq": self._next(), **entry})
        self.save()

    def add_ask(self, entry: Mapping[str, Any]) -> None:
        self.data["asks"].append({"seq": self._next(), **entry})
        self.save()

    def finish(self, status: str, summary: Mapping[str, Any] | None = None) -> None:
        self.data["status"] = status
        self.data["finished"] = _now()
        if summary is not None:
            self.data["summary"] = summary
        self.save()

    def save(self) -> None:
        self.data["updated"] = _now()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            logger.warning("cannot write the session recording %s", self.path, exc_info=True)

    def history(self) -> list[dict[str, Any]]:
        """Reports, Sensei's moves and Ask Sensei exchanges in the order they happened (`kind` tells them apart)."""
        entries = [
            *({"kind": "learner", **r} for r in self.data["reports"]),
            *({"kind": "sensei", **s} for s in self.data["sensei_moves"]),
            *({"kind": "ask", **a} for a in self.data["asks"]),
        ]
        return sorted(entries, key=lambda e: e["seq"])


class RecordingIndex:
    """The recordings available for replay: every valid `*.json` in the given directories, plus one pinned file."""

    def __init__(self, directories: Sequence[Path], pinned: Path | None = None) -> None:
        self.directories = [Path(d) for d in directories]
        self.pinned = pinned
        self._cache: dict[Path, tuple[tuple[int, int], dict[str, Any] | None]] = {}
        self._lock = threading.Lock()

    def _header(self, path: Path) -> dict[str, Any] | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        stamp = (stat.st_mtime_ns, stat.st_size)
        with self._lock:
            cached = self._cache.get(path)
            if cached is not None and cached[0] == stamp:
                return cached[1]
        data = _read_json(path)
        header = None
        if _is_recording(data):
            header = {
                "name": path.stem,
                "learner_id": data.get("learner_id"),
                "mode": data.get("mode"),
                "declared_rank": data.get("declared_rank"),
                "started": data.get("started"),
                "status": data.get("status"),
                "n_moves": len(data["moves"]),
                "has_summary": data.get("summary") is not None,
            }
        with self._lock:
            self._cache[path] = (stamp, header)
        return header

    def _paths(self) -> list[Path]:
        paths: list[Path] = [self.pinned] if self.pinned is not None else []
        for directory in self.directories:
            if directory.is_dir():
                paths.extend(sorted(directory.glob("*.json")))
        return paths

    def entries(self) -> list[dict[str, Any]]:
        found: dict[str, tuple[Path, dict[str, Any]]] = {}
        for path in self._paths():
            header = self._header(path)
            if header is not None and header["name"] not in found:
                found[header["name"]] = (path, header)
        rows = [header for _, header in found.values()]
        return sorted(rows, key=lambda r: str(r.get("started") or ""), reverse=True)

    def load(self, name: str) -> dict[str, Any] | None:
        for path in self._paths():
            if path.stem == name and self._header(path) is not None:
                data = _read_json(path)
                return data if _is_recording(data) else None
        return None


# ---- the game service ----------------------------------------------------------------------------------------------


@dataclass
class ActiveGame:
    learner_id: str
    session: Session
    explainer: ResilientExplainer
    recorder: Recorder
    declared_rank: str
    lock: threading.Lock = field(default_factory=threading.Lock)
    ended: bool = False
    summary: dict[str, Any] | None = None
    ask_history: list[dict[str, Any]] = field(default_factory=list)


@contextmanager
def _engine_errors() -> Iterator[None]:
    """Engine failures (a dead KataGo process, a timeout) become 503 instead of an unexplained 500."""
    try:
        yield
    except (RuntimeError, TimeoutError, OSError) as exc:
        logger.exception("engine failure")
        raise HTTPException(status_code=503, detail=f"engine error: {exc}") from exc


def _normalize_move(move: str) -> str:
    text = str(move).strip()
    return PASS if text.lower() == PASS else text.upper()


class GameService:
    """The logic behind the endpoints: one `ActiveGame` per learner id, every call under that game's lock."""

    def __init__(self, components: Components) -> None:
        self.c = components
        self.games: dict[str, ActiveGame] = {}
        self._registry_lock = threading.Lock()
        self._game_counter = 0
        self.recordings = RecordingIndex(self._recording_dirs(), components.replay_file)

    def _recording_dirs(self) -> list[Path]:
        dirs = [self.c.sessions_dir]
        results = ROOT / "results"  # TASKS.md T9: a recorded demo session may be committed there
        if results.resolve() != self.c.sessions_dir.resolve():
            dirs.append(results)
        return dirs

    # -- lookups ------------------------------------------------------------------------------------------------

    def _require_playable(self) -> None:
        if not self.c.playable:
            raise HTTPException(status_code=409, detail="this server is in replay mode: it only plays back recordings")

    def _game(self, learner_id: str) -> ActiveGame:
        self._require_playable()
        with self._registry_lock:
            game = self.games.get(learner_id)
        if game is None:
            raise HTTPException(status_code=404, detail=f"no active game for learner {learner_id!r}; start one first")
        return game

    @staticmethod
    def _require_open(game: ActiveGame) -> None:
        if game.ended:
            raise HTTPException(status_code=409, detail="this game has ended; start a new game")

    # -- starting a game ----------------------------------------------------------------------------------------

    def start_game(self, learner_id: str, rank: str) -> dict[str, Any]:
        self._require_playable()
        c = self.c
        arms = c.arms
        profile = LearnerProfile.load(c.profiles_dir, learner_id, rank)
        profile.declared_rank = rank
        if set(profile.curriculum_state) - set(arms) or (profile.target_topic and profile.target_topic not in arms):
            logger.warning("profile %s was saved with other topics; its curriculum state is reset", learner_id)
            profile.curriculum_state, profile.target_topic = {}, None
        curriculum = ThompsonCurriculum(
            arms, float(c.cfg["curriculum"]["discount"]), tuple(float(x) for x in c.cfg["curriculum"]["prior"])
        )
        learner_cfg = c.prereg["learner"]
        learner = HumanSLLearner(
            c.engine,
            learner_cfg["profile_grid"],
            rank,
            int(learner_cfg["min_moves_for_estimate"]),
            float(learner_cfg["delta"]),
        )
        with self._registry_lock:
            self._game_counter += 1
            counter = self._game_counter
        rng = np.random.default_rng(None if c.seed is None else [c.seed, counter])
        explainer = c.make_explainer()
        session = Session(
            game=c.game,
            engine=c.engine,
            selector=c.selector,
            learner=learner,
            topics=c.topics,
            curriculum=curriculum,
            explainer=explainer,
            cfg=c.cfg,
            profile=profile,
            rng=rng,
            learner_color=LEARNER_COLOR,
            turns_log=c.turns_log,
            hardware=c.hardware,
            topic_names=c.topic_names or None,
        )
        recorder = Recorder(self._new_recording_path(learner_id), self._recording_header(learner_id, rank, session))
        game = ActiveGame(learner_id, session, explainer, recorder, rank)
        recorder.save()
        with self._registry_lock:
            previous = self.games.get(learner_id)
            self.games[learner_id] = game
        if previous is not None:
            with previous.lock:  # waits for a request still running on the replaced game
                if not previous.ended:
                    previous.recorder.finish("abandoned")
        with game.lock:
            return self._state(game, with_history=True)

    def _new_recording_path(self, learner_id: str) -> Path:
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        path = self.c.sessions_dir / f"{learner_id}_{stamp}.json"
        n = 1
        with self._registry_lock:  # the lock keeps two games of one second from choosing the same name
            while path.exists():
                n += 1
                path = self.c.sessions_dir / f"{learner_id}_{stamp}-{n}.json"
            self.c.sessions_dir.mkdir(parents=True, exist_ok=True)
            path.write_text("{}", encoding="utf-8")  # reserve the name; the recorder overwrites it
        return path

    def _recording_header(self, learner_id: str, rank: str, session: Session) -> dict[str, Any]:
        meta = session.pos.meta
        play = self.c.cfg["play"]
        return {
            "learner_id": learner_id,
            "mode": self.c.mode,
            "model": self.c.model,
            "declared_rank": rank,
            "learner_color": LEARNER_COLOR,
            "game": {
                "size": int(meta.get("size", 19)),
                "komi": float(meta.get("komi", 7.5)),
                "rules": meta.get("rules"),
            },
            "settings": {
                "epsilon": float(play["epsilon"]),
                "max_visits": int(play["max_visits"]),
                "mistake_threshold": float(play["mistake_threshold"]),
            },
        }

    # -- state --------------------------------------------------------------------------------------------------

    def _state(self, game: ActiveGame, *, with_history: bool = False) -> dict[str, Any]:
        s = game.session
        pos = s.pos
        board = self.c.game.board(pos)
        placed = Counter(color for color, move in pos.moves if move != PASS)
        on_board = Counter(board.values())
        last = None
        if pos.moves:
            color, move = pos.moves[-1]
            by = "learner" if color == s.learner_color else "sensei"
            last = {"color": color, "move": move, "ply": len(pos.moves), "by": by}
        over = self.c.game.is_terminal(pos)
        state: dict[str, Any] = {
            "mode": self.c.mode,
            "learner_id": game.learner_id,
            "declared_rank": game.declared_rank,
            "learner_color": s.learner_color,
            "size": int(pos.meta.get("size", 19)),
            "komi": float(pos.meta.get("komi", 7.5)),
            "rules": pos.meta.get("rules"),
            "board": board,
            "last_move": last,
            "to_move": pos.to_move,
            "move_number": len(pos.moves),
            "captured": {"B": placed["W"] - on_board["W"], "W": placed["B"] - on_board["B"]},
            "game_over": over,
            "ended": game.ended,
            "your_turn": pos.to_move == s.learner_color and not over and not game.ended,
            "awaiting_sensei": pos.to_move != s.learner_color and not over and not game.ended,
            "profile": s.profile_summary(),
            "recording": game.recorder.path.stem,
        }
        if with_history:
            state["history"] = game.recorder.history()
        return _jsonable(state)

    def get_state(self, learner_id: str) -> dict[str, Any]:
        game = self._game(learner_id)
        with game.lock:
            return self._state(game, with_history=True)

    # -- moves --------------------------------------------------------------------------------------------------

    def play(self, learner_id: str, move: str) -> dict[str, Any]:
        game = self._game(learner_id)
        with game.lock:
            self._require_open(game)
            s = game.session
            rules = self.c.game
            if rules.is_terminal(s.pos):
                raise HTTPException(
                    status_code=409, detail="both players passed: the game is over, end it to see the summary"
                )
            if s.pos.to_move != s.learner_color:
                raise HTTPException(
                    status_code=409, detail="Sensei still owes a move; start a new game if this persists"
                )
            move = _normalize_move(move)
            try:
                rules.play(s.pos, move)  # the server is the referee: an illegal move never reaches the engine
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

            with _engine_errors():
                before = s.pos
                report = s.learner_move(move)
                self._record_move(game, before, s.pos, "learner")
                report_json = self._report_json(report, len(s.pos.moves))
                game.recorder.add_report(report_json)

                sensei_json = None
                if not rules.is_terminal(s.pos):
                    before = s.pos
                    reply = s.sensei_move()
                    self._record_move(game, before, s.pos, "sensei")
                    sensei_json = self._sensei_json(reply, len(s.pos.moves))
                    game.recorder.add_sensei(sensei_json)
            return {"report": report_json, "sensei": sensei_json, "state": self._state(game)}

    def _record_move(self, game: ActiveGame, before: Position, after: Position, by: str) -> None:
        board_before, board_after = self.c.game.board(before), self.c.game.board(after)
        captured = sorted(point for point in board_before if point not in board_after)
        color, played = after.moves[-1]
        game.recorder.add_move(len(after.moves), color, played, by, captured)

    @staticmethod
    def _report_json(report: MoveReport, ply: int) -> dict[str, Any]:
        explanation = report.explanation
        return {
            "ply": ply,
            "move": report.move,
            "loss": float(report.loss),
            "loss_pct": round(100.0 * float(report.loss), 1),
            "is_mistake": bool(report.is_mistake),
            "severity": report.severity,
            "topic": report.topic,
            "best_move": report.best_move,
            "explanation": None if explanation is None else explanation.text,
            "explanation_model": None if explanation is None else explanation.model,
        }

    @staticmethod
    def _sensei_json(reply: SenseiMove, ply: int) -> dict[str, Any]:
        choice, explanation = reply.choice, reply.explanation
        return {
            "ply": ply,
            "move": reply.move,
            "explanation": explanation.text,
            "model": explanation.model,
            "value_loss": float(choice.value_loss),
            "value_loss_pct": round(100.0 * float(choice.value_loss), 1),
            "position_class": choice.position_class.value,
            "n_admissible": len(choice.admissible),
            "timing_ms": {k: round(float(v), 1) for k, v in reply.timing_ms.items()},
            "prompt_tokens": int(explanation.prompt_tokens),
            "completion_tokens": int(explanation.completion_tokens),
        }

    # -- Ask Sensei ---------------------------------------------------------------------------------------------

    def ask(self, learner_id: str, question: str) -> dict[str, Any]:
        game = self._game(learner_id)
        with game.lock:
            self._require_open(game)
            pos = game.session.pos
            ply = len(pos.moves)
            history = [  # follow-ups only make sense for the position they were asked about
                {"role": e["role"], "content": e["content"]} for e in game.ask_history if e["ply"] == ply
            ][-ASK_HISTORY_MESSAGES:]
            try:
                answer = game.explainer.ask(question, history, position=pos)
            except Exception as exc:  # the model client and the engine raise their own error types
                logger.exception("ask failed")
                raise HTTPException(status_code=502, detail=f"Sensei could not answer right now: {exc}") from exc
            grounded = answer.grounded_on if isinstance(answer.grounded_on, Mapping) else {}
            variations = [[str(m) for m in line] for line in grounded.get("variations", ())]
            tool_calls = self._tool_summary(grounded)
            game.ask_history.append({"ply": ply, "role": "user", "content": question})
            game.ask_history.append({"ply": ply, "role": "assistant", "content": answer.text})
            result = {
                "ply": ply,
                "question": question,
                "text": answer.text,
                "variations": variations,
                "to_move": pos.to_move,
                "tool_calls": tool_calls,
                "model": answer.model,
                "guard": grounded.get("guard"),
                "prompt_tokens": int(answer.prompt_tokens),
                "completion_tokens": int(answer.completion_tokens),
            }
            result = _jsonable(result)
            keep = ("ply", "question", "text", "variations", "to_move", "tool_calls", "model")
            game.recorder.add_ask({k: result[k] for k in keep})
            return result

    @staticmethod
    def _tool_summary(grounded: Mapping[str, Any]) -> list[dict[str, Any]]:
        out = []
        for call in grounded.get("tool_calls", ()):
            arguments = call.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    pass
            result = call.get("result")
            error = result.get("error") if isinstance(result, Mapping) else None
            out.append({"round": call.get("round"), "name": call.get("name"), "arguments": arguments, "error": error})
        return out

    # -- end of game --------------------------------------------------------------------------------------------

    def end_game(self, learner_id: str) -> dict[str, Any]:
        game = self._game(learner_id)
        with game.lock:
            if game.summary is not None:  # idempotent: ending twice must not update the curriculum twice
                return game.summary
            s = game.session
            with _engine_errors():
                self._warm_human_policies(s)
                summary = _jsonable(s.end_game(profiles_dir=self.c.profiles_dir))
            min_moves = int(getattr(s.learner, "min_moves", 0))
            summary["rank"]["min_moves"] = min_moves
            summary["rank"]["declared_only"] = summary["rank"]["n_moves"] < min_moves
            summary["games_played"] = s.profile.games_played
            summary["declared_rank"] = game.declared_rank
            for topic in summary.get("weakest_topics", ()):
                self._decorate_example(topic.get("example"), s)
            game.summary, game.ended = summary, True
            game.recorder.finish("ended", summary)
            return summary

    def _decorate_example(self, example: dict[str, Any] | None, session: Session) -> None:
        """Add the move number and the board before the mistake, so the page can draw the example."""
        if not example:
            return
        moves = tuple((str(color), str(move)) for color, move in example.get("moves", ()))
        example["move_number"] = len(moves) + 1
        try:
            pos = Position(game="go", moves=moves, to_move=session.learner_color, meta=session.pos.meta)
            example["board"] = self.c.game.board(pos)
        except ValueError:
            logger.warning("cannot rebuild the board of a stored example", exc_info=True)

    def _warm_human_policies(self, session: Session) -> None:
        """Fetch the human policies the rank estimate needs concurrently (11 profiles per move are slow serially)."""
        learner = session.learner
        if not hasattr(learner, "grid") or len(session.history) < int(getattr(learner, "min_moves", 0)):
            return
        jobs = [(pos, profile) for pos, _move in session.history for profile in learner.grid]
        with ThreadPoolExecutor(max_workers=WARMUP_WORKERS) as pool:
            list(pool.map(lambda job: learner.human_policy(*job), jobs))

    # -- replay -------------------------------------------------------------------------------------------------

    def list_recordings(self) -> dict[str, Any]:
        rows = self.recordings.entries()
        default = None
        if self.c.replay_file is not None:
            default = self.c.replay_file.stem
        elif rows:
            default = rows[0]["name"]
        return {"recordings": rows, "default": default}

    def get_recording(self, name: str) -> dict[str, Any]:
        data = self.recordings.load(name)
        if data is None:
            raise HTTPException(status_code=404, detail=f"no recording named {name!r}")
        return data


# ---- request models ------------------------------------------------------------------------------------------------

LearnerId = Annotated[str, Field(pattern=LEARNER_ID_PATTERN, description="letters, digits, '_' and '-', up to 40")]


class NewGameRequest(BaseModel):
    learner_id: LearnerId
    rank: str = DEFAULT_RANK

    @field_validator("rank")
    @classmethod
    def _rank_in_dropdown(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in RANKS:
            raise ValueError(f"rank must be one of {RANKS[0]} to {RANKS[-1]}")
        return value


class MoveRequest(BaseModel):
    learner_id: LearnerId
    move: str = Field(min_length=2, max_length=8)


class LearnerRequest(BaseModel):
    learner_id: LearnerId


class AskRequest(BaseModel):
    learner_id: LearnerId
    question: str = Field(min_length=1, max_length=600)

    @field_validator("question")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("the question is empty")
        return value


# ---- the app -------------------------------------------------------------------------------------------------------


def render_index(mode: str) -> str:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return html.replace("__MODE_LABEL__", MODE_LABELS[mode]).replace("__MODE__", mode)


_INLINE_SCRIPT = re.compile(r"<script>(.*?)</script>", re.DOTALL)


def page_headers(html: str) -> dict[str, str]:
    """Response headers for the page. The policy lets only the page's own inline script run (by hash), only same-origin
    requests go out and nothing can frame the page, which enforces "no external dependencies" in the browser too."""
    hashes = [
        "'sha256-" + base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode("ascii") + "'"
        for body in _INLINE_SCRIPT.findall(html)
    ]
    policy = [
        "default-src 'none'",
        "script-src " + (" ".join(hashes) if hashes else "'none'"),
        "style-src 'unsafe-inline'",
        "img-src data:",
        "connect-src 'self'",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'none'",
    ]
    return {
        "Cache-Control": "no-cache",
        "Content-Security-Policy": "; ".join(policy),
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    }


def create_app(
    mode: str = "live",
    *,
    replay_file: str | os.PathLike[str] | None = None,
    cfg: Mapping[str, Any] | None = None,
    profiles_dir: str | os.PathLike[str] | None = None,
    sessions_dir: str | os.PathLike[str] | None = None,
    turns_log: str | os.PathLike[str] | None = None,
    seed: int | None = None,
    engine_stderr_log: str | os.PathLike[str] | None = None,
    backend: str | None = None,
) -> FastAPI:
    """Build the app. The engine and the other shared components are created here, once, and closed at shutdown.

    mode: "live", "fake" or "replay" (see the module docstring). `cfg` is merged over configs/default.yaml.
    `backend` labels the KataGo build (cuda, trt, opencl, eigen) in the hardware block of logs/turns.jsonl.
    `profiles_dir`, `sessions_dir` and `turns_log` default to the repository's `profiles/` (`profiles/fake/` in fake
    mode), `sessions/` and `logs/turns.jsonl` (live mode only). `seed` makes the games' random choices reproducible.
    Use the app as a context manager with TestClient (`with TestClient(app) as client:`) so that shutdown runs.
    """
    components = build_components(
        mode,
        cfg_overrides=cfg,
        profiles_dir=profiles_dir,
        sessions_dir=sessions_dir,
        turns_log=turns_log,
        seed=seed,
        replay_file=replay_file,
        engine_stderr_log=engine_stderr_log,
        backend=backend,
    )
    service = GameService(components)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            await run_in_threadpool(components.close)

    app = FastAPI(title="Sensei", version=__version__, lifespan=lifespan)
    app.state.components = components
    app.state.service = service

    @app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)  # HEAD: uptime and health checks
    async def index() -> HTMLResponse:
        html = render_index(components.mode)
        return HTMLResponse(html, headers=page_headers(html))

    @app.get("/api/info")
    async def info() -> dict[str, Any]:
        return {
            "mode": components.mode,
            "mode_label": MODE_LABELS[components.mode],
            "ranks": list(RANKS),
            "default_rank": DEFAULT_RANK,
            "board_size": 19,
            "komi": float(components.prereg["game"]["komi"]),
            "rules": components.prereg["game"]["rules"],
            "model": components.model,
            "playable": components.playable,
            "topics_fitted": bool(components.topic_names),
            "version": __version__,
        }

    @app.post("/api/game")
    async def new_game(request: NewGameRequest) -> dict[str, Any]:
        return await run_in_threadpool(service.start_game, request.learner_id, request.rank)

    @app.post("/api/move")
    async def play_move(request: MoveRequest) -> dict[str, Any]:
        return await run_in_threadpool(service.play, request.learner_id, request.move)

    @app.post("/api/pass")
    async def pass_move(request: LearnerRequest) -> dict[str, Any]:
        return await run_in_threadpool(service.play, request.learner_id, PASS)

    @app.post("/api/ask")
    async def ask(request: AskRequest) -> dict[str, Any]:
        return await run_in_threadpool(service.ask, request.learner_id, request.question)

    @app.post("/api/end")
    async def end(request: LearnerRequest) -> dict[str, Any]:
        return await run_in_threadpool(service.end_game, request.learner_id)

    @app.get("/api/state")
    async def state(learner_id: Annotated[str, Query(pattern=LEARNER_ID_PATTERN)]) -> dict[str, Any]:
        return await run_in_threadpool(service.get_state, learner_id)

    @app.get("/api/replay")
    async def replay_list() -> dict[str, Any]:
        return await run_in_threadpool(service.list_recordings)

    @app.get("/api/replay/{name}")
    async def replay_get(name: str) -> JSONResponse:
        return JSONResponse(await run_in_threadpool(service.get_recording, name))

    return app


# ---- command line --------------------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--fake", action="store_true", help="demo mode: fake engine and canned explanations, no network")
    group.add_argument(
        "--replay",
        metavar="FILE",
        nargs="?",
        const="",
        help="replay mode: play back recorded sessions (no engine, no model); FILE becomes the default recording",
    )
    ap.add_argument("--host", default="127.0.0.1", help="interface to bind (default: this machine only)")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--backend", default=None, help="label for the KataGo build in logs/turns.jsonl (cuda, trt, ...)")
    ap.add_argument("--seed", type=int, default=None, help="reproducible random choices (for demos and screenshots)")
    return ap


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    mode = "replay" if args.replay is not None else "fake" if args.fake else "live"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        app = create_app(mode, replay_file=args.replay or None, seed=args.seed, backend=args.backend)
    except (OSError, RuntimeError, ValueError) as exc:
        hint = "; the offline demo is `python -m sensei.app --fake`" if mode == "live" else ""
        print(f"sensei.app: cannot start in {mode} mode: {exc}{hint}", file=sys.stderr)
        return 2
    import uvicorn

    print(f"Sensei ({MODE_LABELS[mode]}) on http://{args.host}:{args.port}/  (Ctrl-C to stop)", flush=True)
    t0 = time.time()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    logger.info("stopped after %.0f s", time.time() - t0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
