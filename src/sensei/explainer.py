"""Explanations and "Ask Sensei" through Meta Model API (INTERFACES.md, "Explainer").

The model never chooses moves and never states a variation it did not get from a tool. `explain` sends the engine
facts about the chosen move in the prompt and asks for a short explanation; `ask` runs a tool loop where the model
queries the engine through `TOOLS`. A guard rejects any coordinate that did not come from those facts or tool
results: regenerate once, then drop the offending sentences. Every API call is logged through `sensei.metaapi`.

The client is injected, so tests drive the loop with a fake client, a fake engine and a fake game.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections import OrderedDict
from collections.abc import Hashable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from sensei import metaapi
from sensei.coords import PASS, find_coords
from sensei.interfaces import (
    Candidate,
    Choice,
    Engine,
    EngineAnalysis,
    Explanation,
    Game,
    LearnerEstimate,
    Move,
    Position,
)
from sensei.ranks import numeric_to_label

logger = logging.getLogger(__name__)

DEFAULT_TOP_CANDIDATES = 5
MAX_TOOL_CANDIDATES = 10
MAX_VARIATION_LENGTH = 30
ANALYSIS_CACHE_SIZE = 64
LANGUAGE_NAMES = {
    "en": "English",
    "zh": "Traditional Chinese",
    "zh-tw": "Traditional Chinese",
    "zh-hant": "Traditional Chinese",
    "ja": "Japanese",
}
FALLBACK_ASK_TEXT = "Sensei could not finish answering within its tool budget; please ask a narrower question."

# ---- tool schemas (OpenAI function-calling format) -------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "analyze_position",
            "description": (
                "Engine analysis of the current position: the top candidate moves with their policy prior, "
                "winrate and score lead for the side to move, and their visit counts."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "top_k": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_TOOL_CANDIDATES,
                        "description": "How many candidate moves to return, best first.",
                    }
                },
                "required": ["top_k"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "evaluate_move",
            "description": (
                "The engine's evaluation of one legal move in the current position: the winrate and score lead "
                "for the side to move after it, and its loss against the engine's best move."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "move": {
                        "type": "string",
                        "description": "A GTP coordinate such as Q16 (columns A to T, no I; rows 1 to 19) or pass.",
                    }
                },
                "required": ["move"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "show_variation",
            "description": (
                "Replay a sequence of moves from the current position, alternating colors starting with the side "
                "to move, and return the engine's evaluation of the end position. Illegal moves are rejected. "
                "The sequence is drawn on the learner's board."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "moves": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": MAX_VARIATION_LENGTH,
                        "description": "GTP coordinates in playing order, for example [\"D4\", \"C3\", \"C4\"].",
                    }
                },
                "required": ["moves"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_learner_profile",
            "description": (
                "The learner's profile: rank estimate with its uncertainty, top weakness topics and the topic the "
                "current session targets."
            ),
            "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        },
    },
]

TOOL_NAMES: frozenset[str] = frozenset(t["function"]["name"] for t in TOOLS)


# ---- small helpers -------------------------------------------------------------------------------------------------


def language_name(lang: str) -> str:
    return LANGUAGE_NAMES.get(str(lang).strip().lower(), str(lang))


def normalize_move(move: Any) -> Move:
    """"q16" -> "Q16", "PASS" -> "pass". Raises ValueError for a non-string."""
    if not isinstance(move, str):
        raise ValueError(f"move must be a string, got {type(move).__name__}")
    m = move.strip()
    return PASS if m.lower() == PASS else m.upper()


def _pct(x: float) -> float:
    return round(100.0 * float(x), 1)


def _position_key(pos: Position) -> tuple[Hashable, ...]:
    meta = pos.meta
    return (pos.moves, pos.to_move, meta.get("size", 19), meta.get("komi", 7.5), meta.get("rules", "chinese"))


def _board_size(pos: Position) -> int:
    return int(pos.meta.get("size", 19))


def _class_text(position_class: Any) -> str:
    value = getattr(position_class, "value", position_class)
    return str(value)


def coords_in(value: Any, size: int = 19) -> set[str]:
    """Every on-board coordinate inside the string values of a (nested) tool result."""
    found: set[str] = set()
    if isinstance(value, str):
        found.update(find_coords(value.upper(), size))
    elif isinstance(value, Mapping):
        for item in value.values():
            found |= coords_in(item, size)
    elif isinstance(value, list | tuple | set | frozenset):
        for item in value:
            found |= coords_in(item, size)
    return found


# ---- the guard -------------------------------------------------------------------------------------------------------


def find_ungrounded_coords(text: str, allowed: Iterable[str], size: int = 19) -> list[str]:
    """On-board coordinates mentioned in text (after uppercasing) that are not in allowed; uppercase, in order."""
    allowed_upper = {str(a).strip().upper() for a in allowed}
    out: list[str] = []
    for coord in find_coords(str(text).upper(), size):
        if coord not in allowed_upper and coord not in out:
            out.append(coord)
    return out


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|(?<=[。！？])")


def split_sentences(text: str) -> list[str]:
    """Sentences of text; splits after ., ! or ? followed by whitespace, and after CJK full stops."""
    return [s.strip() for s in _SENTENCE_SPLIT.split(str(text).strip()) if s and s.strip()]


def drop_ungrounded_sentences(text: str, allowed: Iterable[str], size: int = 19) -> tuple[str, list[str]]:
    """Remove every sentence that mentions an ungrounded coordinate. Returns (kept text, dropped sentences)."""
    allowed_set = set(allowed)
    kept: list[str] = []
    dropped: list[str] = []
    for sentence in split_sentences(text):
        if find_ungrounded_coords(sentence, allowed_set, size):
            dropped.append(sentence)
        else:
            kept.append(sentence)
    return " ".join(kept).strip(), dropped


def _correction_message(bad: Sequence[str], allowed: Iterable[str], max_sentences: int) -> str:
    allowed_list = ", ".join(sorted(set(allowed))) or "none"
    return (
        f"Your answer mentioned {', '.join(bad)}, which did not come from the facts or tool results you were given. "
        f"Rewrite it in at most {max_sentences} sentences mentioning only these coordinates: {allowed_list}. "
        "Do not mention any other coordinate and do not add new numbers."
    )


# ---- explanation prompt ----------------------------------------------------------------------------------------------


def _candidate_facts(c: Candidate) -> dict[str, Any]:
    return {
        "move": c.move,
        "prior_pct": _pct(c.prior),
        "winrate_pct": _pct(c.value),
        "visits": int(c.visits),
        "score_lead": None if c.score_lead is None else round(float(c.score_lead), 1),
    }


def explanation_facts(
    analysis: EngineAnalysis,
    choice: Choice,
    learner: LearnerEstimate,
    top_n: int = DEFAULT_TOP_CANDIDATES,
) -> dict[str, Any]:
    """The exact numbers given to the model for one explanation (also stored as Explanation.grounded_on)."""
    pos = analysis.position
    shown: list[Candidate] = list(analysis.candidates[:top_n])
    if choice.move not in {c.move for c in shown}:
        chosen = next((c for c in analysis.candidates if c.move == choice.move), None)
        if chosen is not None:
            shown.append(chosen)
    last = pos.moves[-1] if pos.moves else None
    return {
        "move": choice.move,
        "to_move": pos.to_move,
        "move_number": len(pos.moves) + 1,
        "last_move": None if last is None else {"color": last[0], "move": last[1]},
        "position_class": _class_text(choice.position_class),
        "instructiveness": round(float(choice.instructiveness), 2),
        "value_loss": round(float(choice.value_loss), 3),
        "value_loss_pct": _pct(choice.value_loss),
        "root_winrate_pct": _pct(analysis.root_value),
        "n_admissible": len(choice.admissible),
        "candidates": [_candidate_facts(c) for c in shown],
        "learner": {
            "rank": round(float(learner.rank), 2),
            "rank_sd": round(float(learner.rank_sd), 2),
            "n_moves": int(learner.n_moves),
            "label": numeric_to_label(learner.rank),
        },
    }


def allowed_coords_from_facts(facts: Mapping[str, Any]) -> set[str]:
    allowed = {str(c["move"]).upper() for c in facts.get("candidates", ())}
    allowed.add(str(facts["move"]).upper())
    last = facts.get("last_move")
    if last:
        allowed.add(str(last["move"]).upper())
    allowed.discard(PASS.upper())
    return allowed


def _system_prompt(language: str, max_sentences: int, rank_label: str) -> str:
    return (
        "You are Sensei, a Go coach who teaches by playing. You explain the move Sensei just played to a learner "
        f"of about {rank_label} strength. Ground every statement on the engine facts in the user message. "
        "Never invent moves, coordinates, variations or numbers: mention only coordinates and numbers that appear "
        "in those facts, and do not add reading of your own. Winrates are for the side to move. "
        f"Write in {language}, in plain prose without markdown, in at most {max_sentences} sentences."
    )


def _format_candidates(cands: Sequence[Mapping[str, Any]]) -> str:
    lines = []
    for c in cands:
        extra = "" if c.get("score_lead") is None else f", score lead {c['score_lead']:+.1f}"
        lines.append(
            f"- {c['move']}: prior {c['prior_pct']:.1f}%, winrate {c['winrate_pct']:.1f}%{extra}, visits {c['visits']}"
        )
    return "\n".join(lines)


def _user_prompt(facts: Mapping[str, Any], max_sentences: int) -> str:
    learner = facts["learner"]
    last = facts.get("last_move")
    last_text = "" if not last else f" after {last['color']} played {last['move']}"
    class_text = str(facts["position_class"]).replace("_", "-")
    return (
        f"Move {facts['move_number']}, {facts['to_move']} to play{last_text}. "
        f"Sensei played {facts['move']}.\n"
        f"Position type: {class_text}.\n"
        f"Value loss against the engine's best move: {facts['value_loss_pct']:.1f}% winrate "
        f"({facts['value_loss']:.3f}).\n"
        f"Instructiveness: {facts['instructiveness']:.2f} (positive when the engine likes the move much more than a "
        f"{learner['label']} player would play it).\n"
        f"Engine winrate for {facts['to_move']} before the move: {facts['root_winrate_pct']:.1f}%. "
        f"Admissible moves within the value bound: {facts['n_admissible']}.\n"
        "Engine candidates (prior = policy preference; winrate = for the side to move after the move):\n"
        f"{_format_candidates(facts['candidates'])}\n"
        f"Learner: about {learner['label']} (numeric rank {learner['rank']:+.1f}, sd {learner['rank_sd']:.1f}, "
        f"from {learner['n_moves']} moves).\n"
        f"In at most {max_sentences} sentences, explain why {facts['move']} is a good move to learn from here and how "
        "it compares with the engine's best candidate. Use only the moves and numbers above."
    )


def build_explanation_messages(
    analysis: EngineAnalysis,
    choice: Choice,
    learner: LearnerEstimate,
    lang: str = "en",
    *,
    max_sentences: int | None = None,
    facts: Mapping[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Chat messages for one explanation. max_sentences defaults to explainer.max_sentences in configs/default.yaml."""
    if facts is None:
        facts = explanation_facts(analysis, choice, learner)
    if max_sentences is None:
        max_sentences = int(metaapi.load_config().get("explainer", {}).get("max_sentences", 4))
    language = language_name(lang)
    return [
        {"role": "system", "content": _system_prompt(language, max_sentences, facts["learner"]["label"])},
        {"role": "user", "content": _user_prompt(facts, max_sentences)},
    ]


def mistake_facts(
    analysis: EngineAnalysis,
    played: Move,
    best: Move,
    loss: float,
    learner: LearnerEstimate,
    topic: str | None = None,
    position_class: Any = None,
    top_n: int = DEFAULT_TOP_CANDIDATES,
) -> dict[str, Any]:
    """The exact numbers given to the model when explaining a learner's mistake (Explanation.grounded_on)."""
    pos = analysis.position
    shown: list[Candidate] = list(analysis.candidates[:top_n])
    for move in (best, played):
        if move not in {c.move for c in shown}:
            extra = next((c for c in analysis.candidates if c.move == move), None)
            if extra is not None:
                shown.append(extra)
    last = pos.moves[-1] if pos.moves else None
    return {
        "kind": "mistake",
        "played": played,
        "best_move": best,
        "to_move": pos.to_move,
        "move_number": len(pos.moves) + 1,
        "last_move": None if last is None else {"color": last[0], "move": last[1]},
        "position_class": None if position_class is None else _class_text(position_class),
        "loss": round(float(loss), 3),
        "loss_pct": _pct(loss),
        "played_is_candidate": any(c.move == played for c in analysis.candidates),
        "root_winrate_pct": _pct(analysis.root_value),
        "candidates": [_candidate_facts(c) for c in shown],
        "topic": topic,
        "learner": {
            "rank": round(float(learner.rank), 2),
            "rank_sd": round(float(learner.rank_sd), 2),
            "n_moves": int(learner.n_moves),
            "label": numeric_to_label(learner.rank),
        },
    }


def build_mistake_messages(facts: Mapping[str, Any], lang: str, max_sentences: int) -> list[dict[str, str]]:
    learner = facts["learner"]
    last = facts.get("last_move")
    last_text = "" if not last else f" after {last['color']} played {last['move']}"
    topic_text = "" if not facts.get("topic") else " This kind of situation is the learner's current focus topic."
    system = (
        "You are Sensei, a Go coach. The learner just made a mistake and you explain it to a player of about "
        f"{learner['label']} strength. Ground every statement on the engine facts in the user message. Never invent "
        "moves, coordinates, variations or numbers: mention only coordinates and numbers that appear in those facts. "
        "Winrates are for the learner, the side that was to move. Be encouraging and concrete. "
        f"Write in {language_name(lang)}, in plain prose without markdown, in at most {max_sentences} sentences."
    )
    user = (
        f"Move {facts['move_number']}, {facts['to_move']} (the learner) to play{last_text}. "
        f"The learner played {facts['played']}; the engine's best move was {facts['best_move']}.\n"
        f"The learner's move cost {facts['loss_pct']:.1f}% winrate ({facts['loss']:.3f}).\n"
        f"Engine winrate for {facts['to_move']} before the move: {facts['root_winrate_pct']:.1f}%.\n"
        "Engine candidates (prior = policy preference; winrate = for the learner after the move):\n"
        f"{_format_candidates(facts['candidates'])}\n"
        f"In at most {max_sentences} sentences, say what {facts['best_move']} achieves that {facts['played']} does "
        f"not, using only the moves and numbers above.{topic_text}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def template_mistake(facts: Mapping[str, Any]) -> str:
    return (
        f"{facts['played']} cost {facts['loss_pct']:.1f}% winrate. The engine preferred {facts['best_move']} here."
    )


def template_explanation(facts: Mapping[str, Any]) -> str:
    """A deterministic explanation from the facts alone; used when the guard drops every sentence."""
    cands = list(facts.get("candidates", ()))
    chosen = next((c for c in cands if c["move"] == facts["move"]), None)
    best = cands[0] if cands else None
    class_text = str(facts["position_class"]).replace("_", "-")
    parts = [f"Sensei played {facts['move']} in a {class_text} position."]
    if chosen is not None:
        parts.append(
            f"The engine gives it a {chosen['winrate_pct']:.1f}% winrate with a prior of {chosen['prior_pct']:.1f}%."
        )
    if best is not None and best["move"] != facts["move"]:
        parts.append(
            f"The engine's top candidate is {best['move']} at {best['winrate_pct']:.1f}%, so the choice costs "
            f"{facts['value_loss_pct']:.1f}% winrate."
        )
    else:
        parts.append("It is also the engine's top candidate, so it costs nothing.")
    return " ".join(parts)


# ---- Ask Sensei prompt ---------------------------------------------------------------------------------------------


def _ask_system_prompt(pos: Position, language: str, max_sentences: int, rank_label: str | None) -> str:
    who = f" of about {rank_label} strength" if rank_label else ""
    return (
        f"You are Sensei, a Go coach answering a learner{who}. The current position is move "
        f"{len(pos.moves) + 1} with {pos.to_move} to play on a {_board_size(pos)}x{_board_size(pos)} board. "
        "You have tools that query the engine: analyze_position (top candidates), evaluate_move (one move's winrate, "
        "score lead and loss against the best move), show_variation (replays a sequence, rejects illegal moves and "
        "evaluates the end position; the learner sees it on the board) and get_learner_profile (rank estimate and "
        "weakness topics). Call the tools you need before answering. Never invent moves, coordinates, variations or "
        "numbers: mention only coordinates and numbers that a tool returned or that the learner's question named. "
        "Winrates are for the side to move unless a result says otherwise. "
        f"Answer in {language}, in plain prose without markdown, in at most {max_sentences} sentences."
    )


def _assistant_tool_message(message: Any, calls: Sequence[Any]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": getattr(message, "content", None),
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.function.name, "arguments": call.function.arguments},
            }
            for call in calls
        ],
    }


# ---- the explainer -------------------------------------------------------------------------------------------------


class MetaExplainer:
    """Explainer over an injected OpenAI-compatible client, an Engine and a Game (INTERFACES.md).

    cfg is the runtime config (configs/default.yaml): explainer.max_tool_rounds, explainer.max_sentences,
    explainer.reasoning_effort, play.max_visits and the api prices for the call log. The model name comes from
    `model` or META_MODEL. `position` (set by `explain`, `set_position` or the `position` argument of `ask`) is
    the position Ask Sensei tools work on; `learner_profile` is what get_learner_profile returns once the session
    sets it.
    """

    def __init__(
        self,
        client: Any,
        engine: Engine,
        game: Game,
        cfg: Mapping[str, Any],
        *,
        model: str | None = None,
        log_path: str | Path | None = None,
    ) -> None:
        self.client = client
        self.engine = engine
        self.game = game
        self.cfg = cfg
        self.model = model or os.environ.get("META_MODEL", "").strip() or metaapi.DEFAULT_MODEL
        self.log_path = log_path
        explainer_cfg = cfg.get("explainer", {}) if isinstance(cfg, Mapping) else {}
        self.max_tool_rounds = max(int(explainer_cfg.get("max_tool_rounds", 4)), 0)
        self.max_sentences = max(int(explainer_cfg.get("max_sentences", 4)), 1)
        self.reasoning_effort = explainer_cfg.get("reasoning_effort")
        play_cfg = cfg.get("play", {}) if isinstance(cfg, Mapping) else {}
        self.max_visits = int(play_cfg.get("max_visits", 200))
        engine_cfg = cfg.get("engine", {}) if isinstance(cfg, Mapping) else {}
        self.min_visits_abs = int(engine_cfg.get("min_candidate_visits_abs", 10))
        self.min_visits_frac = float(engine_cfg.get("min_candidate_visits_frac", 0.05))
        self.position: Position | None = None
        self.learner_profile: dict[str, Any] = {}
        self._last_learner: LearnerEstimate | None = None
        self._analyses: OrderedDict[tuple[Hashable, ...], EngineAnalysis] = OrderedDict()

    # ---- state used by Ask Sensei ----------------------------------------------------------------------------------

    def set_position(self, pos: Position, analysis: EngineAnalysis | None = None) -> None:
        """The position Ask Sensei tools refer to, with its analysis if already known."""
        self.position = pos
        if analysis is not None:
            self._remember(analysis)

    def set_learner_profile(self, profile: Mapping[str, Any]) -> None:
        """What get_learner_profile returns: rank estimate, weak topics, target topic (set by the session)."""
        self.learner_profile = dict(profile)

    def _remember(self, analysis: EngineAnalysis) -> None:
        key = _position_key(analysis.position)
        self._analyses[key] = analysis
        self._analyses.move_to_end(key)
        while len(self._analyses) > ANALYSIS_CACHE_SIZE:
            self._analyses.popitem(last=False)

    def analysis_for(self, pos: Position) -> EngineAnalysis:
        """Engine analysis of pos at play.max_visits, cached by position key."""
        key = _position_key(pos)
        cached = self._analyses.get(key)
        if cached is not None:
            self._analyses.move_to_end(key)
            return cached
        analysis = self.engine.analyze(pos, self.max_visits)
        self._remember(analysis)
        return analysis

    def _reliable(self, analysis: EngineAnalysis) -> list[Candidate]:
        """The selector's reliability rule (INTERFACES.md step 1), so losses are measured against a stable best."""
        total = sum(c.visits for c in analysis.candidates)
        threshold = max(self.min_visits_abs, self.min_visits_frac * total)
        reliable = [c for c in analysis.candidates if c.visits >= threshold]
        return reliable or list(analysis.candidates[:1])

    # ---- API calls -------------------------------------------------------------------------------------------------

    def _complete(self, messages: Sequence[Mapping[str, Any]], step: str, totals: list[int], **kwargs: Any) -> Any:
        """One chat completion, logged through metaapi.log_call; returns the first choice's message."""
        params: dict[str, Any] = {"model": self.model, "messages": list(messages), **kwargs}
        if self.reasoning_effort:
            params["reasoning_effort"] = self.reasoning_effort
        t0 = time.time()
        response = self.client.chat.completions.create(**params)
        record = metaapi.log_call(step, response, t0, self.model, self.log_path, self.cfg)
        totals[0] += record["prompt_tokens"] or 0
        totals[1] += record["completion_tokens"] or 0
        return response.choices[0].message

    # ---- Explainer protocol ------------------------------------------------------------------------------------

    def explain(
        self,
        analysis: EngineAnalysis,
        choice: Choice,
        learner: LearnerEstimate,
        lang: str = "en",
    ) -> Explanation:
        """Explain the chosen move from the engine facts; grounded_on holds the exact numbers sent."""
        facts = explanation_facts(analysis, choice, learner)
        messages = build_explanation_messages(
            analysis, choice, learner, lang, max_sentences=self.max_sentences, facts=facts
        )
        self.set_position(analysis.position, analysis)
        self._last_learner = learner
        size = _board_size(analysis.position)
        allowed = allowed_coords_from_facts(facts)
        totals = [0, 0]
        guard: dict[str, Any] = {"regenerated": False, "dropped": [], "ungrounded": []}

        text = self._complete(messages, "explain", totals).content or ""
        bad = find_ungrounded_coords(text, allowed, size)
        if bad:
            guard["regenerated"] = True
            guard["ungrounded"] = list(bad)
            retry = [
                *messages,
                {"role": "assistant", "content": text},
                {"role": "user", "content": _correction_message(bad, allowed, self.max_sentences)},
            ]
            text = self._complete(retry, "explain_regen", totals).content or ""
            bad = find_ungrounded_coords(text, allowed, size)
            if bad:
                guard["ungrounded"] = sorted(set(guard["ungrounded"]) | set(bad))
                text, dropped = drop_ungrounded_sentences(text, allowed, size)
                guard["dropped"] = dropped
        if not text.strip():
            guard["template"] = True
            text = template_explanation(facts)
        grounded_on: dict[str, Any] = {**facts, "language": lang, "allowed_coords": sorted(allowed), "guard": guard}
        return Explanation(
            text=text.strip(),
            model=self.model,
            prompt_tokens=totals[0],
            completion_tokens=totals[1],
            grounded_on=grounded_on,
        )

    def explain_mistake(
        self,
        analysis: EngineAnalysis,
        played: Move,
        best: Move,
        loss: float,
        learner: LearnerEstimate,
        topic: str | None = None,
        position_class: Any = None,
        lang: str = "en",
    ) -> Explanation:
        """Explain a learner's mistake from the analysis of the position before it; same guard as `explain`."""
        facts = mistake_facts(analysis, played, best, loss, learner, topic, position_class)
        messages = build_mistake_messages(facts, lang, self.max_sentences)
        size = _board_size(analysis.position)
        allowed = {str(c["move"]).upper() for c in facts["candidates"]} | {played.upper(), best.upper()}
        if facts.get("last_move"):
            allowed.add(str(facts["last_move"]["move"]).upper())
        allowed.discard(PASS.upper())
        totals = [0, 0]
        guard: dict[str, Any] = {"regenerated": False, "dropped": [], "ungrounded": []}
        text = self._complete(messages, "explain_mistake", totals).content or ""
        bad = find_ungrounded_coords(text, allowed, size)
        if bad:
            guard["regenerated"] = True
            guard["ungrounded"] = list(bad)
            retry = [
                *messages,
                {"role": "assistant", "content": text},
                {"role": "user", "content": _correction_message(bad, allowed, self.max_sentences)},
            ]
            text = self._complete(retry, "explain_mistake_regen", totals).content or ""
            bad = find_ungrounded_coords(text, allowed, size)
            if bad:
                guard["ungrounded"] = sorted(set(guard["ungrounded"]) | set(bad))
                text, guard["dropped"] = drop_ungrounded_sentences(text, allowed, size)
        if not text.strip():
            guard["template"] = True
            text = template_mistake(facts)
        return Explanation(
            text=text.strip(),
            model=self.model,
            prompt_tokens=totals[0],
            completion_tokens=totals[1],
            grounded_on={**facts, "language": lang, "allowed_coords": sorted(allowed), "guard": guard},
        )

    # ---- Ask Sensei -------------------------------------------------------------------------------------------------

    def ask(
        self,
        question: str,
        history: Sequence[Mapping[str, Any]] = (),
        *,
        position: Position | None = None,
        lang: str = "en",
    ) -> Explanation:
        """Answer a learner question with the tool loop (at most explainer.max_tool_rounds tool rounds).

        history holds earlier user/assistant messages of the Ask Sensei conversation. grounded_on carries the tool
        calls with their results, the variations the model showed (for the UI to draw), the allowed coordinates and
        what the guard did.
        """
        pos = position if position is not None else self.position
        if pos is None:
            raise ValueError("no current position: call set_position() or explain() before ask()")
        size = _board_size(pos)
        rank_label = self._rank_label()
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _ask_system_prompt(pos, language_name(lang), self.max_sentences, rank_label)},
            *[dict(m) for m in history],
            {"role": "user", "content": question},
        ]
        allowed: set[str] = set(find_coords(str(question).upper(), size))
        tool_log: list[dict[str, Any]] = []
        variations: list[list[str]] = []
        totals = [0, 0]
        text: str | None = None
        rounds = 0

        for round_index in range(1, self.max_tool_rounds + 1):
            message = self._complete(messages, f"ask_round_{round_index}", totals, tools=TOOLS, tool_choice="auto")
            calls = list(getattr(message, "tool_calls", None) or [])
            if not calls:
                text = message.content or ""
                break
            rounds += 1
            messages.append(_assistant_tool_message(message, calls))
            for call in calls:
                name, arguments = call.function.name, call.function.arguments
                result = self._run_tool(pos, name, arguments, variations)
                allowed |= coords_in(result, size)
                tool_log.append({"round": round_index, "name": name, "arguments": arguments, "result": result})
                messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(result)})
        if text is None:  # the tool budget is spent: one final answer without tools
            message = self._complete(messages, "ask_final", totals, tools=TOOLS, tool_choice="none")
            text = message.content or ""

        guard: dict[str, Any] = {"regenerated": False, "dropped": [], "ungrounded": []}
        bad = find_ungrounded_coords(text, allowed, size)
        if bad:
            guard["regenerated"] = True
            guard["ungrounded"] = list(bad)
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user", "content": _correction_message(bad, allowed, self.max_sentences)})
            text = self._complete(messages, "ask_regen", totals, tools=TOOLS, tool_choice="none").content or ""
            bad = find_ungrounded_coords(text, allowed, size)
            if bad:
                guard["ungrounded"] = sorted(set(guard["ungrounded"]) | set(bad))
                text, dropped = drop_ungrounded_sentences(text, allowed, size)
                guard["dropped"] = dropped
        if not text.strip():
            guard["fallback"] = True
            text = FALLBACK_ASK_TEXT
        return Explanation(
            text=text.strip(),
            model=self.model,
            prompt_tokens=totals[0],
            completion_tokens=totals[1],
            grounded_on={
                "question": question,
                "to_move": pos.to_move,
                "move_number": len(pos.moves) + 1,
                "tool_rounds": rounds,
                "tool_calls": tool_log,
                "variations": variations,
                "allowed_coords": sorted(allowed),
                "language": lang,
                "guard": guard,
            },
        )

    # ---- tools -------------------------------------------------------------------------------------------------

    def _run_tool(self, pos: Position, name: str, arguments: str | None, variations: list[list[str]]) -> dict[str, Any]:
        """Execute one tool call against the engine and the game; errors come back as {"error": ...} JSON."""
        try:
            args = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError as exc:
            return {"error": f"tool arguments are not valid JSON: {exc}"}
        if not isinstance(args, Mapping):
            return {"error": "tool arguments must be a JSON object"}
        try:
            if name == "analyze_position":
                return self.tool_analyze_position(pos, args.get("top_k", DEFAULT_TOP_CANDIDATES))
            if name == "evaluate_move":
                return self.tool_evaluate_move(pos, args.get("move", ""))
            if name == "show_variation":
                result = self.tool_show_variation(pos, args.get("moves", []))
                if "error" not in result:
                    variations.append(list(result["moves"]))
                return result
            if name == "get_learner_profile":
                return self.tool_get_learner_profile()
        except (ValueError, TypeError, KeyError) as exc:
            return {"error": str(exc) or exc.__class__.__name__}
        return {"error": f"unknown tool: {name}"}

    def tool_analyze_position(self, pos: Position, top_k: Any = DEFAULT_TOP_CANDIDATES) -> dict[str, Any]:
        k = min(max(int(top_k), 1), MAX_TOOL_CANDIDATES)
        analysis = self.analysis_for(pos)
        return {
            "to_move": pos.to_move,
            "move_number": len(pos.moves) + 1,
            "root_winrate_pct": _pct(analysis.root_value),
            "candidates": [_candidate_facts(c) for c in analysis.candidates[:k]],
        }

    def tool_evaluate_move(self, pos: Position, move: Any) -> dict[str, Any]:
        played = normalize_move(move)
        if played not in set(self.game.legal_moves(pos)):
            return {"error": f"{played} is not a legal move in the current position"}
        analysis = self.analysis_for(pos)
        best = max(self._reliable(analysis), key=lambda c: (c.value, c.visits))
        candidate = next((c for c in analysis.candidates if c.move == played), None)
        if candidate is not None:
            winrate, score_lead = candidate.value, candidate.score_lead
            visits, source = candidate.visits, "root candidate"
        else:  # not a root candidate: evaluate the position after the move (INTERFACES.md, mistakes)
            after = self.analysis_for(self.game.play(pos, played))
            winrate = 1.0 - after.root_value
            top = after.candidates[0] if after.candidates else None
            score_lead = None if top is None or top.score_lead is None else -top.score_lead
            visits, source = 0, "second query on the position after the move"
        return {
            "move": played,
            "perspective": pos.to_move,
            "winrate_pct": _pct(winrate),
            "score_lead": None if score_lead is None else round(float(score_lead), 1),
            "loss_pct": _pct(max(best.value - winrate, 0.0)),
            "best_move": best.move,
            "best_winrate_pct": _pct(best.value),
            "visits": int(visits),
            "source": source,
        }

    def tool_show_variation(self, pos: Position, moves: Any) -> dict[str, Any]:
        if isinstance(moves, str) or not isinstance(moves, Sequence):
            return {"error": "moves must be a list of GTP coordinates"}
        sequence = [normalize_move(m) for m in moves]
        if not sequence:
            return {"error": "moves must not be empty"}
        if len(sequence) > MAX_VARIATION_LENGTH:
            return {"error": f"at most {MAX_VARIATION_LENGTH} moves per variation"}
        current = pos
        played: list[str] = []
        for index, move in enumerate(sequence, start=1):
            try:
                current = self.game.play(current, move)
            except ValueError as exc:
                return {"error": f"illegal move {move} at step {index}: {exc}", "played": played}
            played.append(move)
        analysis = self.analysis_for(current)
        flipped = len(played) % 2 == 1
        winrate = 1.0 - analysis.root_value if flipped else analysis.root_value
        top = analysis.candidates[0] if analysis.candidates else None
        score_lead = None
        if top is not None and top.score_lead is not None:
            score_lead = -top.score_lead if flipped else top.score_lead
        return {
            "moves": played,
            "to_move_after": current.to_move,
            "perspective": pos.to_move,
            "winrate_pct": _pct(winrate),
            "score_lead": None if score_lead is None else round(float(score_lead), 1),
            "engine_reply": None if top is None else top.move,
            "engine_reply_winrate_pct": None if top is None else _pct(top.value),
            "engine_reply_perspective": current.to_move,
            "game_over": bool(self.game.is_terminal(current)),
        }

    def tool_get_learner_profile(self) -> dict[str, Any]:
        if self.learner_profile:
            return dict(self.learner_profile)
        est = self._last_learner
        if est is None:
            return {"rank_label": None, "weak_topics": [], "target_topic": None, "note": "no learner profile yet"}
        return {
            "rank_label": numeric_to_label(est.rank),
            "rank": round(float(est.rank), 2),
            "rank_sd": round(float(est.rank_sd), 2),
            "n_moves": int(est.n_moves),
            "weak_topics": [],
            "target_topic": None,
        }

    def _rank_label(self) -> str | None:
        label = self.learner_profile.get("rank_label") if self.learner_profile else None
        if label:
            return str(label)
        if self._last_learner is not None:
            return numeric_to_label(self._last_learner.rank)
        return None
