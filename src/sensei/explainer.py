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
from sensei.coords import PASS, find_coords, gtp_to_rc, neighbors, rc_to_gtp
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
PROMPT_VERSION = "v4.1"
"""Version of the explanation and Ask Sensei prompts; recorded with every explanation set (results/explanations_*).

v2 (2026-11-04, after the T5 grading of v1): the prompts name the learner's color and Sensei's ("I" and "you"),
give no engine internals (prior, visits, instructiveness, admissible set), describe every point in plain Go words
(`describe_point`), state what a move does to the stones next to it (`local_facts`), and talk in points when one
side is far ahead. Tool results drop prior and visits and label every number with its side.
v3 (2026-11-04, after a review of the v2 outputs): "my new stone" instead of the ambiguous "the stone", the nearest
stones for quiet moves, no winning percentages at all once one side is far ahead (v2's instruction to talk in points
leaked into the text), "about even" for a score near zero, the points given up against the engine's top move as a
required part of the reason, and every move in a tool result labelled with who plays it.
v3.1 (2026-11-04, a fix to one fact, the prompt wording is v3's): "the engine's top move" is the engine's
most-searched move (`_engine_top`) in the explanation facts, the template and `evaluate_move`, not the
admissible move with the highest winrate, and the facts say so when my move scores more points than it.
v4 (2026-11-04, after the T5 grading of v3.1, a deviation from TASKS.md's two-round cap): the reason for a move
comes from the engine, not the model: what the other side would play near it if Sensei passed and what that move
would do to Sensei's stones (`threat_facts`, one extra engine query), and how many points the move is worth against
passing; moves that connect two groups say so; the position type no longer contradicts an only move; a move that
gives up 2 points or more is not called the best; "a likely answer for you is"; location words for at most two points.
v4.1 (2026-11-04, a fix to the facts after a review of the v4 outputs, the prompt wording is v4's): the other side's
move after a pass is a reason only when it captures, ataris, rescues or connects (`threat_sharp`), and a forcing
move (the expected answer next to it, then the engine's top move) is named as one (`forcing_exchange`).
"""
DECIDED = 0.95
"""Above this winning chance (or below 1 - DECIDED) the prompts ask for points instead of percentages."""

# ---- tool schemas (OpenAI function-calling format) -------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "analyze_position",
            "description": (
                "Engine analysis of the current position: the top candidate moves, best first, each with where it is "
                "on the board, its winning chance and its score lead in points for the side to move ('for')."
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
                "The engine's evaluation of one legal move in the current position: where it is, the winning chance "
                "and score lead for the side to move after it ('for'), its loss against the engine's top move, what "
                "it does to the stones next to it (captures, ataris, liberties, connections), what the other side "
                "would play near it if the side to move passed instead ('if_not_played'), and how many points the "
                "move is worth compared with passing."
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
                        "description": 'GTP coordinates in playing order, for example ["D4", "C3", "C4"].',
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
    """ "q16" -> "Q16", "PASS" -> "pass". Raises ValueError for a non-string."""
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


_COLOR_NAMES = {"B": "Black", "W": "White"}
_CLASS_WORDS = {
    "only_move": "a must-play (only move) situation: every other move gives up ground",
    "tactical": "a fighting (tactical) position where exact moves matter",
    "strategic": "a quiet (strategic) position with several reasonable plans",
}


def _color_name(color: Any) -> str:
    return _COLOR_NAMES.get(str(color), str(color))


def _opponent(color: Any) -> str:
    return "W" if str(color) == "B" else "B"


def _ordinal(n: int) -> str:
    return {1: "1st", 2: "2nd", 3: "3rd"}.get(int(n), f"{int(n)}th")


def describe_point(move: Any, size: int = 19) -> str | None:
    """A point in plain Go words: "upper-left corner (3-4 point)", "right side, 2nd line, lower half", "center".

    Lines count from the nearest edge (the edge itself is the 1st line). A point within 5 lines of two edges (3 on
    boards smaller than 13x13) is a corner point, within 5 lines of one edge a side point, anything else the center.
    None for a pass or anything that is not a point on the board.
    """
    if not isinstance(move, str) or move.strip().lower() == PASS:
        return None
    try:
        rc = gtp_to_rc(move.strip().upper(), size)
    except (ValueError, KeyError, IndexError):
        return None
    if rc is None:
        return None
    row, col = rc
    top, bottom, left, right = row + 1, size - row, col + 1, size - col
    v_line, h_line = min(top, bottom), min(left, right)
    vert = "upper" if top < bottom else "lower" if bottom < top else "middle"
    horiz = "left" if left < right else "right" if right < left else "middle"
    near = 5 if size >= 13 else 3
    if v_line <= near and h_line <= near:
        a, b = sorted((v_line, h_line))
        return f"{vert}-{horiz} corner ({a}-{b} point)"
    if h_line <= near:
        half = "" if vert == "middle" else f", {vert} half"
        return f"{horiz} side, {_ordinal(h_line)} line{half}"
    if v_line <= near:
        half = "" if horiz == "middle" else f", {horiz} half"
        return f"{'top' if vert == 'upper' else 'bottom'} side, {_ordinal(v_line)} line{half}"
    toward = "-".join(x for x in ({"upper": "top", "lower": "bottom"}.get(vert, vert), horiz) if x != "middle")
    return "center" if not toward else f"center, toward the {toward}"


def _chain(board: Mapping[tuple[int, int], str], start: tuple[int, int], size: int) -> tuple[set, set]:
    """The stones of the group at start and its liberties, on a board keyed by (row, col)."""
    color = board[start]
    stones, libs, stack = {start}, set(), [start]
    while stack:
        point = stack.pop()
        for q in neighbors(point, size):
            c = board.get(q)
            if c is None:
                libs.add(q)
            elif c == color and q not in stones:
                stones.add(q)
                stack.append(q)
    return stones, libs


_SHAPES = (  # (offsets, relation, which side's stones, the points between that must be empty)
    (((1, 1), (1, -1), (-1, 1), (-1, -1)), "diagonal to", "opponent", 0),
    (((1, 1), (1, -1), (-1, 1), (-1, -1)), "diagonal to", "own", 0),
    (((2, 0), (-2, 0), (0, 2), (0, -2)), "a one-space jump from", "own", 1),
    (((1, 2), (2, 1), (-1, 2), (-2, 1), (1, -2), (2, -1), (-1, -2), (-2, -1)), "a knight's move from", "own", 0),
    (((3, 0), (-3, 0), (0, 3), (0, -3)), "a two-space jump from", "own", 2),
)
MAX_SHAPES = 3
NEAREST_RADIUS = 4
"""For a move that touches nothing: the nearest stone of each color within this many points (Chebyshev distance)."""


def _shape_relations(
    board: Mapping[tuple[int, int], str], rc: tuple[int, int], me: str, other: str, size: int
) -> list[dict[str, str]]:
    """Standard shape words linking a move to nearby stones before it (diagonal, one-space jump, knight's move,
    two-space jump), at most MAX_SHAPES, in that order."""
    row, col = rc
    out: list[dict[str, str]] = []
    for offsets, relation, side, gap in _SHAPES:
        color = other if side == "opponent" else me
        for dr, dc in offsets:
            q = (row + dr, col + dc)
            if not (0 <= q[0] < size and 0 <= q[1] < size) or board.get(q) != color:
                continue
            steps = max(abs(dr), abs(dc))
            between = [(row + dr * k // steps, col + dc * k // steps) for k in range(1, steps)] if gap else []
            if any(board.get(m) is not None for m in between):
                continue
            out.append({"relation": relation, "side": side, "stone": rc_to_gtp(q, size)})
            if len(out) >= MAX_SHAPES:
                return out
    return out


def local_facts(game: Any, pos: Position, move: Any) -> dict[str, Any] | None:
    """What a move by the side to move does to the stones around it (None for a pass or when the game cannot say).

    touches_opponent / touches_own: stones next to it before the move; captured: opponent stones it removes; atari:
    one stone of every adjacent opponent group left with a single liberty; adjacent_opponent: every adjacent opponent
    group with 3 liberties or fewer after the move (a stone of it, its size, its liberties); own_group: size and
    liberties of the mover's group after the move; joins: one stone of each of the mover's groups it connects (v4),
    empty unless it connects two or more; own_before: each of the mover's groups next to it before the move (a
    stone, its size, its liberties), so a move that rescues a short-of-liberties group can say so.
    """
    if not isinstance(move, str) or move.strip().lower() == PASS:
        return None
    size = _board_size(pos)
    try:
        rc = gtp_to_rc(move, size)
        before = game.board(pos)
        after = game.board(game.play(pos, move))
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    if rc is None:
        return None
    b = {gtp_to_rc(k, size): v for k, v in before.items()}
    a = {gtp_to_rc(k, size): v for k, v in after.items()}
    me, other = str(pos.to_move), _opponent(pos.to_move)
    around = neighbors(rc, size)
    counted: set[tuple[int, int]] = set()
    adjacent: list[dict[str, Any]] = []
    atari: list[str] = []
    for q in around:
        if a.get(q) != other or q in counted:
            continue
        stones, libs = _chain(a, q, size)
        counted |= stones
        entry = {"stone": rc_to_gtp(q, size), "stones": len(stones), "liberties": len(libs)}
        if len(libs) <= 3:
            adjacent.append(entry)
        if len(libs) == 1:
            atari.append(entry["stone"])
    own_stones, own_libs = _chain(a, rc, size) if rc in a else (set(), set())
    joined: list[str] = []  # one stone of every separate own group next to the move, when there are two or more
    own_before: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for q in around:
        if b.get(q) == me and q not in seen:
            stones, libs = _chain(b, q, size)
            seen |= stones
            joined.append(rc_to_gtp(q, size))
            own_before.append({"stone": rc_to_gtp(q, size), "stones": len(stones), "liberties": len(libs)})
    nearest: dict[str, dict[str, Any] | None] = {"own": None, "opponent": None}
    for point, color in b.items():
        if point is None:
            continue
        d = max(abs(point[0] - rc[0]), abs(point[1] - rc[1]))
        key = "own" if color == me else "opponent"
        if d <= NEAREST_RADIUS and (
            nearest[key] is None or (d, point) < (nearest[key]["distance"], nearest[key]["rc"])
        ):
            nearest[key] = {"stone": rc_to_gtp(point, size), "distance": d, "rc": point}
    for entry in nearest.values():
        if entry is not None:
            entry.pop("rc")
    return {
        "nearest": nearest,
        "shape": _shape_relations(b, rc, me, other, size),
        "touches_opponent": any(b.get(q) == other for q in around),
        "touches_own": any(b.get(q) == me for q in around),
        "captured": sum(1 for p, v in b.items() if v == other and p not in a),
        "atari": atari,
        "adjacent_opponent": adjacent,
        "own_group": {"stones": len(own_stones), "liberties": len(own_libs)},
        "joins": joined if len(joined) >= 2 else [],
        "own_before": own_before,
    }


def rescued(local: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """The mover's groups next to the move that had 2 liberties or fewer before it and more after it (v4)."""
    if not local:
        return []
    after = int((local.get("own_group") or {}).get("liberties") or 0)
    return [g for g in local.get("own_before", ()) if int(g["liberties"]) <= 2 and after > int(g["liberties"])]


def local_text(local: Mapping[str, Any] | None, mine: str, theirs: str) -> str:
    """local_facts as plain sentences. mine and theirs are the possessives of the mover and the other side
    ("my"/"your", "your"/"my", or color names such as "Black's")."""
    if not local:
        return ""
    parts: list[str] = []
    if local.get("captured"):
        n = int(local["captured"])
        parts.append(f"It captures {n} of {theirs} stone{'s' if n != 1 else ''}.")
    if local.get("atari"):
        parts.append(f"It puts {theirs} stones at {', '.join(local['atari'])} in atari (one liberty left).")
    for g in local.get("adjacent_opponent", ()):
        if g["stone"] in local.get("atari", ()):
            continue
        parts.append(
            f"{theirs.capitalize()} {'group' if g['stones'] > 1 else 'stone'} at {g['stone']} next to it has "
            f"{g['liberties']} liberties."
        )
    joins = list(local.get("joins") or ())
    if joins:
        parts.append(f"It connects {mine} stones at {', '.join(joins[:-1])} and {joins[-1]} into one group.")
    after_libs = int((local.get("own_group") or {}).get("liberties") or 0)
    for g in rescued(local):
        parts.append(f"It gives {mine} stones at {g['stone']} {after_libs} liberties instead of {g['liberties']}.")
    if not local.get("captured") and not local.get("atari"):
        if local.get("touches_opponent"):
            parts.append(f"It touches {theirs} stones.")
        elif local.get("touches_own") and not joins and not rescued(local):
            parts.append(f"It is played next to {mine} own stones.")
        elif not local.get("shape"):
            parts.append("It touches no stones.")
    for s in local.get("shape", ()):
        whose = mine if s["side"] == "own" else theirs
        parts.append(f"It is {s['relation']} {whose} stone at {s['stone']}.")
    quiet = not (local.get("touches_opponent") or local.get("touches_own") or local.get("captured"))
    if quiet:
        named = {s["stone"] for s in local.get("shape", ())}
        near = local.get("nearest") or {}
        bits = []
        for side, whose in (("opponent", theirs), ("own", mine)):
            n = near.get(side)
            if n and n["stone"] not in named:
                bits.append(f"{whose} stone at {n['stone']}, {n['distance']} points away")
        if bits:
            parts.append("The nearest other stones are " + " and ".join(bits) + ".")
        elif not near.get("own") and not near.get("opponent"):
            parts.append(f"No stone is within {NEAREST_RADIUS} points of it: it is in an open area.")
    own = local.get("own_group") or {}
    if own.get("stones"):
        if own["stones"] > 1:
            parts.append(f"After it, {mine} group there has {own['stones']} stones and {own['liberties']} liberties.")
        else:
            parts.append(f"After it, {mine} new stone has {own['liberties']} liberties.")
    return " ".join(parts)


THREAT_RADIUS = 4
"""The other side's best move after a pass is "here" (a reason for my move) when it is within this many points
(Chebyshev distance) of my move."""


def _distance(a: str, b: str, size: int) -> int | None:
    try:
        ra, rb = gtp_to_rc(a, size), gtp_to_rc(b, size)
    except (ValueError, KeyError, IndexError):
        return None
    if ra is None or rb is None:
        return None
    return max(abs(ra[0] - rb[0]), abs(ra[1] - rb[1]))


def threat_facts(
    game: Any, pos: Position, move: Any, after_pass: EngineAnalysis | None, my_score_lead: float | None
) -> dict[str, Any] | None:
    """What the other side would do if the side to move passed instead of playing `move` (v4).

    after_pass is the engine's analysis of the position after a pass by the side to move. Returns the other side's
    top move there, its distance from `move`, whether it is local (within THREAT_RADIUS) or the same point, its
    local_facts from the other side's view, and worth_points: the score after `move` minus the score after the pass
    and that reply, both for the side to move (None when a score is missing). None when there is nothing to say
    (a pass, no analysis, the other side would pass too, or the game cannot replay it).
    """
    if not isinstance(move, str) or move.strip().lower() == PASS or after_pass is None:
        return None
    top = _engine_top(after_pass)
    if top is None or str(top.move).lower() == PASS:
        return None
    size = _board_size(pos)
    distance = _distance(str(top.move), move, size)
    if distance is None:
        return None
    worth = None
    if my_score_lead is not None and top.score_lead is not None:
        worth = round(float(my_score_lead) + float(top.score_lead), 1)  # top.score_lead is for the other side
    return {
        "move": top.move,
        "same_point": top.move == move,
        "distance": distance,
        "local": distance <= THREAT_RADIUS,
        "facts": local_facts(game, after_pass.position, top.move),
        "worth_points": worth,
    }


def forcing_exchange(move: Any, pv: Sequence[str] | None, top_move: Any, size: int) -> dict[str, str] | None:
    """A forcing move (sente) by the engine's own line (v4.1): the line starts with `move`, the other side's
    expected answer is within THREAT_RADIUS of it, and the mover's next move in the line is the engine's top move,
    which `move` is not. Returns {"reply", "then"} or None."""
    if not pv or len(pv) < 3 or pv[0] != move or not top_move or move == top_move:
        return None
    reply, then = str(pv[1]), str(pv[2])
    if PASS in (reply.lower(), then.lower()) or then != top_move:
        return None
    distance = _distance(reply, str(move), size)
    if distance is None or distance > THREAT_RADIUS:
        return None
    return {"reply": reply, "then": then}


def threat_sharp(threat: Mapping[str, Any] | None) -> bool:
    """A local threat that captures, ataris, leaves a group of the passer with 2 liberties or fewer, connects, or
    rescues a group of its own that had 2 liberties or fewer."""
    if not threat or not threat.get("local"):
        return False
    f = threat.get("facts") or {}
    return bool(
        f.get("captured")
        or f.get("atari")
        or f.get("joins")
        or rescued(f)
        or any(int(g["liberties"]) <= 2 for g in f.get("adjacent_opponent", ()))
    )


def threat_text(local: Mapping[str, Any] | None, passer: str, mover: str) -> str:
    """The other side's threat move as clauses in the conditional ("it would put my stones at D4 in atari").
    passer and mover are possessives of the side that passed and the side that plays the threat."""
    if not local:
        return ""
    parts: list[str] = []
    if local.get("captured"):
        n = int(local["captured"])
        parts.append(f"it would capture {n} of {passer} stone{'s' if n != 1 else ''}")
    if local.get("atari"):
        parts.append(f"it would put {passer} stones at {', '.join(local['atari'])} in atari")
    for g in local.get("adjacent_opponent", ()):
        if g["stone"] in local.get("atari", ()):
            continue
        kind = "group" if g["stones"] > 1 else "stone"
        parts.append(f"{passer} {kind} at {g['stone']} next to it would have {g['liberties']} liberties")
    joins = list(local.get("joins") or ())
    if joins:
        parts.append(f"it would connect {mover} stones at {', '.join(joins[:-1])} and {joins[-1]}")
    after_libs = int((local.get("own_group") or {}).get("liberties") or 0)
    for g in rescued(local):
        parts.append(f"it would give {mover} stones at {g['stone']} {after_libs} liberties instead of {g['liberties']}")
    if not parts:
        if local.get("touches_opponent"):
            parts.append(f"it would touch {passer} stones")
        elif local.get("touches_own"):
            parts.append(f"it would be played next to {mover} own stones")
    return "; ".join(parts)


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


def _engine_top(analysis: EngineAnalysis) -> Candidate | None:
    """The engine's top move: its most-searched candidate (candidates come in visit order), the move KataGo and
    every Go interface built on it show first. v3 used the admissible candidate with the highest winrate instead,
    which is a different move whenever two winrates are within noise and, in decided games, can hide a gap of
    several points (v3.1)."""
    return analysis.candidates[0] if analysis.candidates else None


def explanation_facts(
    analysis: EngineAnalysis,
    choice: Choice,
    learner: LearnerEstimate,
    top_n: int = DEFAULT_TOP_CANDIDATES,
    game: Any = None,
    threat: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The exact facts behind one explanation (also stored as Explanation.grounded_on).

    The numeric fields of v1 stay for the app and the logs; the prompt (v2) uses the plain ones: the colors, where
    each point is, what the move does to the stones next to it (needs `game`), the points against the engine's best
    move and the engine's expected continuation.
    """
    pos = analysis.position
    size = _board_size(pos)
    shown: list[Candidate] = list(analysis.candidates[:top_n])
    if choice.move not in {c.move for c in shown}:
        chosen = next((c for c in analysis.candidates if c.move == choice.move), None)
        if chosen is not None:
            shown.append(chosen)
    last = pos.moves[-1] if pos.moves else None
    mine = next((c for c in analysis.candidates if c.move == choice.move), None)
    best = _engine_top(analysis)
    points_vs_best = None
    if mine is not None and best is not None and mine.score_lead is not None and best.score_lead is not None:
        points_vs_best = round(float(best.score_lead) - float(mine.score_lead), 1)
    continuation: list[str] = []
    if mine is not None and mine.pv and mine.pv[0] == choice.move:
        continuation = [m for m in mine.pv[1:3] if m != PASS]
    local = local_facts(game, pos, choice.move) if game is not None else None
    forcing = None
    if mine is not None and best is not None:
        forcing = forcing_exchange(choice.move, mine.pv, best.move, size)
    points = [choice.move, *(c.move for c in shown), *continuation]
    if best is not None:
        points.append(best.move)
    if last is not None:
        points.append(last[1])
    points += [g["stone"] for g in (local or {}).get("adjacent_opponent", ())]
    points += [s["stone"] for s in (local or {}).get("shape", ())]
    points += [n["stone"] for n in ((local or {}).get("nearest") or {}).values() if n]
    points += list((local or {}).get("joins") or ()) + [g["stone"] for g in rescued(local)]
    if threat and threat.get("local"):
        points.append(threat["move"])
        tf = threat.get("facts") or {}
        points += list(tf.get("atari", ())) + [g["stone"] for g in tf.get("adjacent_opponent", ())]
        points += list(tf.get("joins") or ()) + [g["stone"] for g in rescued(tf)]
    where = {m: w for m in dict.fromkeys(points) if (w := describe_point(m, size))}
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
        "prompt_version": PROMPT_VERSION,
        "sensei_color": str(pos.to_move),
        "learner_color": _opponent(pos.to_move),
        "where": where,
        "my_winrate_pct": None if mine is None else _pct(mine.value),
        "my_score_lead": None if mine is None or mine.score_lead is None else round(float(mine.score_lead), 1),
        "best_move": None if best is None else best.move,
        "best_winrate_pct": None if best is None else _pct(best.value),
        "points_vs_best": points_vs_best,
        "continuation": continuation,
        "local": local,
        "threat": None if threat is None else dict(threat),
        "forcing": forcing,
    }


def allowed_coords_from_facts(facts: Mapping[str, Any]) -> set[str]:
    allowed = {str(c["move"]).upper() for c in facts.get("candidates", ())}
    allowed.add(str(facts["move"]).upper())
    last = facts.get("last_move")
    if last:
        allowed.add(str(last["move"]).upper())
    if facts.get("best_move"):
        allowed.add(str(facts["best_move"]).upper())
    allowed |= {str(m).upper() for m in facts.get("continuation") or ()}
    local = facts.get("local") or {}
    allowed |= {str(g["stone"]).upper() for g in local.get("adjacent_opponent", ())}
    allowed |= {str(s).upper() for s in local.get("atari", ())}
    allowed |= {str(s["stone"]).upper() for s in local.get("shape", ())}
    allowed |= {str(n["stone"]).upper() for n in (local.get("nearest") or {}).values() if n}
    allowed |= {str(s).upper() for s in local.get("joins") or ()}
    allowed |= {str(g["stone"]).upper() for g in rescued(local)}
    threat = facts.get("threat") or {}
    if threat.get("local"):
        allowed.add(str(threat["move"]).upper())
        tf = threat.get("facts") or {}
        allowed |= {str(s).upper() for s in tf.get("atari", ())}
        allowed |= {str(g["stone"]).upper() for g in tf.get("adjacent_opponent", ())}
        allowed |= {str(s).upper() for s in tf.get("joins") or ()}
        allowed |= {str(g["stone"]).upper() for g in rescued(tf)}
    allowed.discard(PASS.upper())
    return allowed


def _system_prompt(
    language: str, max_sentences: int, rank_label: str, sensei_color: str = "W", learner_color: str = "B"
) -> str:
    me, you = _color_name(sensei_color), _color_name(learner_color)
    return (
        f"You are Sensei, a Go coach who teaches by playing against the learner. You play {me}; the learner plays "
        f'{you} and is about {rank_label}. Say "I" and "my" for {me}\'s stones and moves, and "you" and '
        f"\"your\" for {you}'s; never call {me}'s stones the learner's. "
        f"Write for a {rank_label} player: plain Go words such a player knows, no engine internals (never mention "
        "prior, policy, visits, instructiveness, admissible moves, a value bound, or decimals such as 0.005). "
        "Use only the facts in the user message and never invent moves, coordinates, variations or numbers: mention "
        "only the coordinates given there, describe where my move is with the location words given (for example "
        '"right side, 2nd line"; other points can be named by coordinate alone), and do not claim shapes, threats, '
        "life and death or sequences that the facts do not state. Round numbers (13.7 points: about 14 points). "
        f"Write in {language}, in plain prose without markdown, in at most {max_sentences} sentences."
    )


def _at(move: Any, where: Mapping[str, str]) -> str:
    w = where.get(str(move))
    return f"{move} ({w})" if w else str(move)


def _user_prompt(facts: Mapping[str, Any], max_sentences: int) -> str:
    me, you = _color_name(facts["sensei_color"]), _color_name(facts["learner_color"])
    where = facts.get("where") or {}
    rank = facts["learner"]["label"]
    root = float(facts["root_winrate_pct"])
    decided = root >= 100 * DECIDED or root <= 100 * (1 - DECIDED)
    lines = [f"Move {facts['move_number']}."]
    last = facts.get("last_move")
    if last:
        who = "you" if last["color"] == facts["learner_color"] else "I"
        lines.append(f"Last move: {_color_name(last['color'])} ({who}) played {_at(last['move'], where)}.")
    lines.append(f"I ({me}) now play {_at(facts['move'], where)}.")
    text = local_text(facts.get("local"), "my", "your")
    if text:
        lines.append(f"What my move does to the stones around it: {text}")
    if not decided:
        lines.append(
            f"Winning chances before my move: {me} (me) about {root:.0f}%, {you} (you) about {100 - root:.0f}%."
        )
    lead = facts.get("my_score_lead")
    if lead is not None:
        if abs(float(lead)) < 0.5:
            lines.append("Score after my move, by the engine's count: about even.")
        else:
            leader = f"{me} (me)" if lead > 0 else f"{you} (you)"
            lines.append(f"Score after my move, by the engine's count: {leader} ahead by about {abs(lead):.1f} points.")
    threat = facts.get("threat") or {}
    if threat_sharp(threat):  # v4.1: a threat that does nothing to the stones is no reason for my move
        clause = threat_text(threat.get("facts"), "my", "your")
        if threat.get("same_point"):
            head = f"If I had passed instead, your best move would be this same point, {facts['move']}"
        else:
            head = (
                f"If I had passed instead, your best move would be {_at(threat['move'], where)}, "
                f"{threat['distance']} points from my move"
            )
        lines.append(head + (f": {clause}." if clause else "."))
    worth = threat.get("worth_points")
    if worth is not None and float(worth) >= 0.5:
        lines.append(f"Compared with passing, my move is worth about {float(worth):.1f} points by the engine's count.")
    best = facts.get("best_move")
    gap = facts.get("points_vs_best")
    if best and best != facts["move"]:
        detail = []
        if gap is not None:
            if gap >= 0.5:
                detail.append(f"about {gap:.1f} points better for me")
            elif gap <= -0.5:
                detail.append(f"my move scores about {-gap:.1f} points more by the engine's count")
            else:
                detail.append("about the same in points")
        if not decided and facts.get("best_winrate_pct") is not None and facts.get("my_winrate_pct") is not None:
            best_wr, my_wr = float(facts["best_winrate_pct"]), float(facts["my_winrate_pct"])
            loss = f" (a loss of {best_wr - my_wr:.1f}%)" if best_wr - my_wr >= 0.05 else ""
            detail.append(f"winning chances {best_wr:.1f}% for it against {my_wr:.1f}% for my move{loss}")
        lines.append(f"The engine's top move was {_at(best, where)}: {'; '.join(detail) or 'close to mine'}.")
    else:
        mine_wr = facts.get("my_winrate_pct")
        tail = "." if mine_wr is None or decided else f" (winning chances {float(mine_wr):.1f}% for me after it)."
        lines.append("My move is also the engine's top move" + tail)
    only = facts.get("position_class") == "only_move" or int(facts.get("n_admissible") or 0) <= 1
    rare = float(facts.get("instructiveness") or 0.0) >= 0.3
    big_gap = bool(best and best != facts["move"] and gap is not None and gap >= 2.0)
    if only:
        why = "every other move gives up ground here"
    elif big_gap:
        found = "rarely find my move" if rare else "often find my move too"
        why = (
            f"the engine counts my move and {best} as both keeping my winning chances, and players around {rank} "
            f"{found}; but {best} gains about {gap:.1f} points more, so for points {best} is the better move: say so "
            "plainly and do not call my move the best one"
        )
    elif rare:
        why = (
            f"players around {rank} rarely find this move, and among the moves that keep my winning chances it is the "
            "one most worth learning"
        )
    else:
        why = f"it keeps my winning chances, and players around {rank} often find it too"
    if threat_sharp(threat):
        why += "; the main reason to play here is what you would do here if I passed (above)"
    forcing = facts.get("forcing")
    if forcing:
        why += (
            f"; it is a forcing move (sente): the engine expects your answer at {forcing['reply']} next to it, and "
            f"then I still play {forcing['then']}, the engine's top move, so I play my move first"
        )
    lines.append(f"Why I chose it: {why}.")
    cont = facts.get("continuation") or []
    if cont:
        line = f"A likely answer for you is {_at(cont[0], where)}"
        if len(cont) > 1:
            line += f", and then I would play {_at(cont[1], where)}"
        lines.append(line + ".")
    kind = "only_move" if only else str(facts["position_class"])
    lines.append(f"Position type: {_CLASS_WORDS.get(kind, kind)}.")
    lines.append(
        f"Write at most {max_sentences} sentences for a {rank} player: first the reason for my move (what you would "
        "do here if I passed, when that is given; otherwise what my move does to the stones around it), then why I "
        'chose it, with any points it gives up, then a likely answer for you. Say "a likely answer for you is" and '
        "never tell the learner to watch for their own move. Give location words for my move and at most one other "
        "point. Use only these facts."
    )
    return "\n".join(lines)


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
    system = _system_prompt(
        language, max_sentences, facts["learner"]["label"], facts["sensei_color"], facts["learner_color"]
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": _user_prompt(facts, max_sentences)}]


def mistake_facts(
    analysis: EngineAnalysis,
    played: Move,
    best: Move,
    loss: float,
    learner: LearnerEstimate,
    topic: str | None = None,
    position_class: Any = None,
    top_n: int = DEFAULT_TOP_CANDIDATES,
    game: Any = None,
) -> dict[str, Any]:
    """The exact facts behind a mistake explanation (Explanation.grounded_on); the learner is the side to move."""
    pos = analysis.position
    size = _board_size(pos)
    shown: list[Candidate] = list(analysis.candidates[:top_n])
    for move in (best, played):
        if move not in {c.move for c in shown}:
            extra = next((c for c in analysis.candidates if c.move == move), None)
            if extra is not None:
                shown.append(extra)
    last = pos.moves[-1] if pos.moves else None
    by_move = {c.move: c for c in analysis.candidates}
    points_lost = None
    if played in by_move and best in by_move:
        lead_played, lead_best = by_move[played].score_lead, by_move[best].score_lead
        if lead_played is not None and lead_best is not None:
            points_lost = round(float(lead_best) - float(lead_played), 1)
    local_played = local_facts(game, pos, played) if game is not None else None
    local_best = local_facts(game, pos, best) if game is not None else None
    points = [played, best, *(c.move for c in shown)]
    if last is not None:
        points.append(last[1])
    for local in (local_played, local_best):
        points += [g["stone"] for g in (local or {}).get("adjacent_opponent", ())]
        points += [s["stone"] for s in (local or {}).get("shape", ())]
        points += [n["stone"] for n in ((local or {}).get("nearest") or {}).values() if n]
    where = {m: w for m in dict.fromkeys(points) if (w := describe_point(m, size))}
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
        "prompt_version": PROMPT_VERSION,
        "learner_color": str(pos.to_move),
        "sensei_color": _opponent(pos.to_move),
        "where": where,
        "points_lost": points_lost,
        "local_played": local_played,
        "local_best": local_best,
    }


def build_mistake_messages(facts: Mapping[str, Any], lang: str, max_sentences: int) -> list[dict[str, str]]:
    you, me = _color_name(facts["learner_color"]), _color_name(facts["sensei_color"])
    rank = facts["learner"]["label"]
    where = facts.get("where") or {}
    topic_text = "" if not facts.get("topic") else " This kind of situation is the learner's current focus topic."
    system = (
        f"You are Sensei, a Go coach playing {me} against the learner, who plays {you} and is about {rank}. "
        f'The learner just made a mistake. Say "you" and "your" for {you}\'s stones and "I" and "my" '
        f"for {me}'s. Write for a {rank} player, encouraging and concrete, with no engine internals (never mention "
        "prior, policy, visits or decimals such as 0.05). Use only the facts in the user message and never invent "
        "moves, coordinates, variations or numbers: mention only the coordinates given there, describe where a point "
        "is with the location words given, and do not claim shapes, "
        "threats or sequences that the facts do not state. "
        f"Write in {language_name(lang)}, in plain prose without markdown, in at most {max_sentences} sentences."
    )
    lines = [f"Move {facts['move_number']}."]
    last = facts.get("last_move")
    if last:
        who = "you" if last["color"] == facts["learner_color"] else "I"
        lines.append(f"Last move: {_color_name(last['color'])} ({who}) played {_at(last['move'], where)}.")
    lines.append(
        f"You ({you}) played {_at(facts['played'], where)}; "
        f"the engine's best move was {_at(facts['best_move'], where)}."
    )
    cost = f"Your move cost {float(facts['loss_pct']):.1f}% of your winning chances"
    if facts.get("points_lost") is not None and float(facts["points_lost"]) >= 0.5:
        cost += f", about {float(facts['points_lost']):.1f} points"
    lines.append(cost + ".")
    played_text = local_text(facts.get("local_played"), "your", "my")
    best_text = local_text(facts.get("local_best"), "your", "my")
    if played_text:
        lines.append(f"What your move does to the stones next to it: {played_text}")
    if best_text:
        lines.append(f"What {facts['best_move']} would have done: {best_text}")
    lines.append(
        f"In at most {max_sentences} sentences, say what {facts['best_move']} achieves that {facts['played']} does "
        f"not, using only these facts.{topic_text}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": "\n".join(lines)}]


def template_mistake(facts: Mapping[str, Any]) -> str:
    return f"{facts['played']} cost {facts['loss_pct']:.1f}% winrate. The engine preferred {facts['best_move']} here."


def template_explanation(facts: Mapping[str, Any]) -> str:
    """A deterministic explanation from the facts alone; used when the guard drops every sentence."""
    where = facts.get("where") or {}
    parts = [f"I played {_at(facts['move'], where)}."]
    best = facts.get("best_move")
    gap = facts.get("points_vs_best")
    if best and best != facts["move"]:
        if gap is not None and gap >= 0.5:
            parts.append(f"The engine's top move was {best}, about {gap:.1f} points better by its count.")
        elif gap is not None and gap <= -0.5:
            parts.append(f"The engine's top move was {best}; mine scores about {-gap:.1f} points more by its count.")
        else:
            parts.append(f"The engine's top move was {best}, about as good as mine.")
    else:
        parts.append("It is also the engine's top move.")
    threat = facts.get("threat") or {}
    clause = threat_text(threat.get("facts"), "my", "your") if threat_sharp(threat) else ""
    if clause:
        parts.append(f"If I had passed, your best move would be {threat['move']}: {clause}.")
    return " ".join(parts)


# ---- Ask Sensei prompt ---------------------------------------------------------------------------------------------


def _ask_system_prompt(
    pos: Position, language: str, max_sentences: int, rank_label: str | None, learner_color: str | None = None
) -> str:
    who = f" of about {rank_label} strength" if rank_label else ""
    number = len(pos.moves) + 1
    to_move = _color_name(pos.to_move)
    if learner_color in ("B", "W"):
        you, me = _color_name(learner_color), _color_name(_opponent(learner_color))
        turn = "your turn" if pos.to_move == learner_color else "my turn"
        opening = (
            f'You are Sensei, a Go coach playing {me} against the learner{who}, who plays {you}. Say "I" and '
            f'"my" for {me}\'s stones and moves, and "you" and "your" for {you}\'s; never call {me}\'s stones '
            f"the learner's. It is move {number}, {to_move} to play ({turn}). "
        )
    else:
        opening = (
            f"You are Sensei, a Go coach answering a learner{who}. Refer to the players as Black and White. "
            f"It is move {number}, {to_move} to play. "
        )
    return (
        opening + f"The board is {_board_size(pos)}x{_board_size(pos)}. "
        "Tools: analyze_position (the engine's top moves), evaluate_move (one move: winning chance, points, and what "
        "it does to the stones next to it), show_variation (plays a sequence on the learner's board and evaluates the"
        " end position) and get_learner_profile (rank estimate and weak topics). Every tool result says which side "
        "its numbers are for ('for') and where each point is ('where'); use those location words. They also say who "
        "plays each move ('by'): a move by Sensei's color is always my move and a move by the learner's color always "
        "yours, also inside a variation. Answer the question that was asked, first. When asked about the idea of a "
        "move, start from evaluate_move's 'if_not_played' (what the other side would play there if the mover passed),"
        " 'forcing_exchange' (a forcing move: the other side answers next to it and the mover then plays the engine's"
        " top move) and 'worth_vs_pass_points' when they are given; mention shapes only to support that, never as the"
        " idea itself. When asked what happens after a move, call show_variation with that move and describe the line"
        " it returns. Explain with what the tools return (locations, captures, ataris, liberties, points), not with "
        "generic advice such as 'stay calm', 'hang on' or 'keep it simple'. Never mention engine internals (prior, "
        "policy, visits, instructiveness), and never invent moves, coordinates, variations or numbers: mention only "
        "coordinates that a tool returned or the question named. When one side's winning chance is above 95%, talk in"
        " points. A score at the end of a variation comes from a separate search, so say it is the score after the "
        "whole line and do not compare it with a score from evaluate_move. "
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
        self.learner_color: str | None = None
        self.learner_profile: dict[str, Any] = {}
        self._last_learner: LearnerEstimate | None = None
        self._analyses: OrderedDict[tuple[Hashable, ...], EngineAnalysis] = OrderedDict()

    # ---- state used by Ask Sensei ----------------------------------------------------------------------------------

    def set_position(self, pos: Position, analysis: EngineAnalysis | None = None) -> None:
        """The position Ask Sensei tools refer to, with its analysis if already known."""
        self.position = pos
        if analysis is not None:
            self._remember(analysis)

    def set_learner_color(self, color: str | None) -> None:
        """The learner's color ("B" or "W"), so prompts and tool results can say "you" and "I" (set by the session;
        `explain` sets it too, to the side that is not Sensei)."""
        self.learner_color = color if color in ("B", "W") else None

    def _label(self, color: Any) -> str:
        """A side as tool results name it: "Black (you)", "White (me)", or the bare color while the learner's is
        unknown."""
        name = _color_name(color)
        if self.learner_color not in ("B", "W"):
            return name
        return f"{name} (you)" if str(color) == self.learner_color else f"{name} (me)"

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

    def threat_after_pass(self, pos: Position, move: Any, my_score_lead: float | None) -> dict[str, Any] | None:
        """threat_facts for `move` in pos: one extra engine query on the position after a pass by the side to move
        (v4). None when passing would end the game or the engine or the game cannot answer."""
        if pos.moves and str(pos.moves[-1][1]).lower() == PASS:
            return None
        try:
            passed = self.game.play(pos, PASS)
            if self.game.is_terminal(passed):
                return None
            return threat_facts(self.game, pos, move, self.analysis_for(passed), my_score_lead)
        except (ValueError, KeyError, TypeError, AttributeError, RuntimeError):
            return None

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
        mine = next((c for c in analysis.candidates if c.move == choice.move), None)
        threat = self.threat_after_pass(analysis.position, choice.move, None if mine is None else mine.score_lead)
        facts = explanation_facts(analysis, choice, learner, game=self.game, threat=threat)
        messages = build_explanation_messages(
            analysis, choice, learner, lang, max_sentences=self.max_sentences, facts=facts
        )
        self.set_position(analysis.position, analysis)
        self.learner_color = facts["learner_color"]
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
        facts = mistake_facts(analysis, played, best, loss, learner, topic, position_class, game=self.game)
        messages = build_mistake_messages(facts, lang, self.max_sentences)
        size = _board_size(analysis.position)
        allowed = {str(c["move"]).upper() for c in facts["candidates"]} | {played.upper(), best.upper()}
        if facts.get("last_move"):
            allowed.add(str(facts["last_move"]["move"]).upper())
        for local in (facts.get("local_played"), facts.get("local_best")):
            allowed |= {str(g["stone"]).upper() for g in (local or {}).get("adjacent_opponent", ())}
            allowed |= {str(s["stone"]).upper() for s in (local or {}).get("shape", ())}
            allowed |= {str(n["stone"]).upper() for n in ((local or {}).get("nearest") or {}).values() if n}
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
        learner_color: str | None = None,
    ) -> Explanation:
        """Answer a learner question with the tool loop (at most explainer.max_tool_rounds tool rounds).

        learner_color ("B" or "W") overrides the color set by the session or by the last `explain`; without either,
        the prompt refers to the players by color only.

        history holds earlier user/assistant messages of the Ask Sensei conversation. grounded_on carries the tool
        calls with their results, the variations the model showed (for the UI to draw), the allowed coordinates and
        what the guard did.
        """
        pos = position if position is not None else self.position
        if pos is None:
            raise ValueError("no current position: call set_position() or explain() before ask()")
        size = _board_size(pos)
        rank_label = self._rank_label()
        if learner_color is not None:
            self.set_learner_color(learner_color)
        system = _ask_system_prompt(pos, language_name(lang), self.max_sentences, rank_label, self.learner_color)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
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
                "prompt_version": PROMPT_VERSION,
                "learner_color": self.learner_color,
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
        size = _board_size(pos)
        return {
            "to_move": pos.to_move,
            "for": self._label(pos.to_move),
            "move_number": len(pos.moves) + 1,
            "root_winrate_pct": _pct(analysis.root_value),
            "candidates": [
                {
                    "move": c.move,
                    "where": describe_point(c.move, size),
                    "winrate_pct": _pct(c.value),
                    "score_lead": None if c.score_lead is None else round(float(c.score_lead), 1),
                }
                for c in analysis.candidates[:k]
            ],
        }

    def tool_evaluate_move(self, pos: Position, move: Any) -> dict[str, Any]:
        played = normalize_move(move)
        if played not in set(self.game.legal_moves(pos)):
            return {"error": f"{played} is not a legal move in the current position"}
        analysis = self.analysis_for(pos)
        best = _engine_top(analysis) or max(self._reliable(analysis), key=lambda c: (c.value, c.visits))
        candidate = next((c for c in analysis.candidates if c.move == played), None)
        if candidate is not None:
            winrate, score_lead = candidate.value, candidate.score_lead
            source = "root candidate"
        else:  # not a root candidate: evaluate the position after the move (INTERFACES.md, mistakes)
            after = self.analysis_for(self.game.play(pos, played))
            winrate = 1.0 - after.root_value
            top = after.candidates[0] if after.candidates else None
            score_lead = None if top is None or top.score_lead is None else -top.score_lead
            source = "second query on the position after the move"
        size = _board_size(pos)
        mover, other = self._label(pos.to_move), self._label(_opponent(pos.to_move))
        on_board = local_text(local_facts(self.game, pos, played), f"{mover}'s", f"{other}'s")
        threat = self.threat_after_pass(pos, played, score_lead) if played != PASS else None
        if_not_played = None
        if threat_sharp(threat):
            if_not_played = {
                "by": other,
                "move": threat["move"],
                "where": describe_point(threat["move"], size),
                "same_point": threat["same_point"],
                "what_it_would_do": threat_text(threat.get("facts"), f"{mover}'s", f"{other}'s") or None,
            }
        worth = None if threat is None else threat.get("worth_points")
        return {
            "move": played,
            "by": mover,
            "where": describe_point(played, size),
            "perspective": pos.to_move,
            "for": mover,
            "winrate_pct": _pct(winrate),
            "score_lead": None if score_lead is None else round(float(score_lead), 1),
            "loss_pct": _pct(max(best.value - winrate, 0.0)),
            "best_move": best.move,
            "best_where": describe_point(best.move, size),
            "best_winrate_pct": _pct(best.value),
            "on_the_board": on_board or None,
            "if_not_played": if_not_played,
            "worth_vs_pass_points": worth if worth is not None and worth >= 0.5 else None,
            "forcing_exchange": None
            if candidate is None or best is None
            else forcing_exchange(played, candidate.pv, best.move, size),
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
        size = _board_size(pos)
        steps, color = [], pos.to_move
        for move in played:
            steps.append({"move": move, "by": self._label(color), "where": describe_point(move, size)})
            color = _opponent(color)
        return {
            "moves": played,
            "steps": steps,
            "to_move_after": current.to_move,
            "perspective": pos.to_move,
            "for": self._label(pos.to_move),
            "winrate_pct": _pct(winrate),
            "score_lead": None if score_lead is None else round(float(score_lead), 1),
            "engine_reply": None if top is None else top.move,
            "engine_reply_where": None if top is None else describe_point(top.move, size),
            "engine_reply_by": self._label(current.to_move),
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
