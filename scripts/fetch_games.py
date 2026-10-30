"""Fetch public OGS (online-go.com) games into data/raw/ogs as SGF files (TASKS.md T1b).

    python scripts/fetch_games.py --target 1200
    python scripts/fetch_games.py --target 1200 --min-date 2024-08-01 --rate 1.0 --out data/raw/ogs
    python scripts/fetch_games.py --target 1200 --id-range 66500000 91900000
    python scripts/fetch_games.py --ids-from data/splits.json      # re-fetch exactly the games a splits file names

Game IDs are sampled uniformly (seeded, reproducible) from an ID range that is mapped to dates, because OGS offers no
public list of games by date. Each ID costs one request (the game JSON); the SGF is built from that JSON, so no second
request is needed. Accepted games are written to <out>/ogs-<id>.sgf and one line each to <out>/index.jsonl; every
rejected ID goes to <out>/rejects.jsonl with its reason. A rerun skips every ID in either file (resumable), and a
larger --target continues the same ID sequence. Politeness: 1 request per second by default, exponential backoff on
HTTP 429, 5xx and network errors (honouring Retry-After), a descriptive User-Agent (add --contact).
"""

# ======================================================================================================================
# What this script relies on, as checked on 2026-10-29 (see data/SOURCES.md for the terms of use).
#
# How it was checked: the web fetcher available while writing this script returns a model-written summary of a page,
# not the raw bytes, and this container cannot reach online-go.com from the shell. It can read raw files from GitHub
# (OGS's and KataGo's source, quoted exactly below). Field names and values of the API were read on many games and
# cross-checked against each other; tests/fixtures/ogs holds RECONSTRUCTIONS of the observed structure (invented
# players and moves), not copies of live responses. VERIFIED means seen in a live response or in OGS's own source;
# UNVERIFIED means assumed or not readable.
#
# VERIFIED, game JSON. GET https://online-go.com/api/v1/games/<id> answers without any login or token (games 1,
#   66000000, 66400000, 66500000, 66600000, 67000000, 69000000, 70000000, 75000000, 79876543, 88000000, 92000000 and
#   others were read). Top-level keys: id, all_players, name, players, related, creator, mode ("game"), source
#   ("play"), black, white (player ids), width, height, rules (lower case: "japanese", "chinese", "aga" seen), ranked,
#   handicap, handicap_rank_difference, komi (a STRING such as "6.50"), time_control, black_player_rank,
#   black_player_rating, white_player_rank, white_player_rating (0 and "0.000" in every game from 2024 on that was
#   read: not usable), time_per_move, time_control_parameters, disable_analysis, tournament, tournament_round, ladder,
#   pause_on_weekends, outcome, black_lost, white_lost, annulled, annulment_reason, started, ended, historical_ratings,
#   gamedata, auth, rengo, flags, bot_detection_results, simul_black, simul_white. started and ended are ISO 8601 in
#   UTC with microseconds and a trailing Z; ended is null and outcome "" while a game is running.
# VERIFIED, players. players.<black|white> holds id, username, country, icon, ratings{version, overall{rating,
#   deviation, volatility}}, ranking, professional and ui_class. These are the player's CURRENT values, not the values
#   at game time. ui_class is a space-separated token list ("", "provisional", "supporter", "bot", "bot aquatic-9",
#   "supporter moderator admin" seen); a bot account has the token "bot" (game 88000000, white; five bot accounts in
#   the list of one player's games). User names are not used to spot bots ("noob_bot" in game 66000000 is not a bot).
# VERIFIED, rating at game time. historical_ratings.<black|white>.ratings.overall.{rating, deviation} differ from the
#   current values (game 75000000, black: 627.7 then, 828.4 now). The ranking field inside historical_ratings is the
#   CURRENT ranking and must not be used. The rank computed from the historical rating with the formula below equals
#   gamedata.players.<color>.rank (OGS's own rank at game time) to 5 decimals for 9 of 10 player slots checked; the
#   tenth (game 66000000, black, a 21-minute game) differs by 0.045 of a rank (3.3 rating points), which fits a rating
#   update from another game finishing in between. UNVERIFIED: whether the historical rating is the value before or
#   after the game's own update; the cross-check against gamedata.players.*.rank (rank_unstable below) bounds the
#   difference either way.
# VERIFIED, gamedata. gamedata is a TOP-LEVEL key (a first reading wrongly put it inside historical_ratings; the bracket
#   structure of a second reading settled it). It holds, among others: initial_player ("black"), initial_state
#   {"black": "", "white": ""}, handicap, komi (a number), moves, outcome, phase ("finished"), players.<color>.{id,
#   username, rank, professional}, ranked, rengo, rules, start_time and end_time (unix seconds), time_control.speed
#   ("live", "blitz", "rapid" and "correspondence" seen), width, height, winner (a player id).
# VERIFIED, moves. gamedata.moves is a list of [x, y, time_ms, null, null]; x is the column counted from the left and y
#   the row counted from the top, both 0-based (the real-time API page says: "Coordinates are encoded like SGF's, a-z
#   to specify each coordinate with aa being the top left"); a pass is [-1, -1, time_ms] (game 70000000 ends with two).
#   Game 1 (2012) has an empty moves list; games from 2024 on carry them (39, 63, 71, 249 and 255 moves seen). The
#   entry layout is [x, y, time delta in ms, colour, extra] (AdHocPackedMove in online-go/goban, src/engine/formats/
#   AdHocFormat.ts; decodeMoves and encodeMoveToArray in src/engine/util/move_encoding.ts; raw files read 2026-10-29):
#   the colour (1 black, 2 white, 0 empty) is written only for edited stones and extra holds blur, played_by and the
#   like; both were null in every game read. parse_moves refuses a colour that breaks alternate play.
# VERIFIED, outcome strings seen: "Resignation" ("Resigniation" in game 1, a 2012 typo), "Timeout", "<n> points"
#   (n a multiple of 0.5), "Cancellation", "Disconnection". black_lost / white_lost give the loser; annulled is a
#   separate boolean (an annulled "Timeout" game was seen), so it is checked on its own.
# VERIFIED, rank formula. OGS source, src/lib/rank_utils.ts on branch main (AGPL-3.0, read through the GitHub API on
#   2026-10-29): rank_to_rating(rank) = 525 * exp(rank / 23.15); rating_to_rank(rating) = ln(clamp(rating, 100, 6000) /
#   525) * 23.15; MinRank = 5 (25k), MaxRank = 38 (9d); rank 29 = 1k and rank 30 = 1d; the label is ceil(30 - rank)
#   + "k" below 30 and floor(rank - 29) + "d" from 30 up; PROVISIONAL_RATING_CUTOFF = 160 (deviation below it is not
#   provisional). Checked on ten rating/rank pairs from the API (differences below 1e-5; the API's players.ranking is
#   clamped at 5, gamedata.players.*.rank is not). The numeric rank (30 = 1d) is the sensei.ranks scale plus 29.
# VERIFIED, listing. https://online-go.com/api/v1/games/ cannot be read (the fetcher reports a client error), so
#   there is no public list of all games by date. /api/v1/players/<id>/games/ works (DRF paging: count, next, previous,
#   results; page_size, ordering and ended__gt / ended__lt filters took effect) but only lists one player's games,
#   which would bias the sample. Hence uniform sampling of game IDs.
# VERIFIED, ID to date (game id: start time, UTC), used by ID_ANCHORS below: 66000000 2024-07-14, 66400000 2024-07-28,
#   66500000 2024-08-01T02:55, 66600000 2024-08-04, 67000000 2024-08-18, 69000000 2024-10-27, 70000000 2024-11-29,
#   71234567 2025-01-10, 73456789 2025-03-19, 75000000 2025-05-06, 76543210 2025-06-21, 79876543 2025-10-02,
#   81234568 2025-11-14, 83333333 2026-01-19, 88000000 2026-06-18, 89012345 2026-07-22, 90123456 2026-08-26,
#   91929295 2026-10-26, 92000000 2026-10-28. About 29,000 to 32,000 new IDs per day. Not every ID exists: 68123457 and
#   99999999 were reported as not found, and other IDs as client errors, roughly one in four of the random IDs tried.
#   A random ID was an accepted game (19x19, ranked, even, human, finished, 60+ moves) in about 1 of 5 cases that
#   existed, so expect roughly 6 requests per accepted game (about 2 hours at 1 request per second for 1200 games).
# VERIFIED, rules. OGS has six rule sets, "chinese", "aga", "japanese", "korean", "ing" and "nz" (type
#   GobanEngineRules in online-go/goban, src/engine/GobanEngine.ts on main, raw file read 2026-10-29). KataGo's
#   parseRulesHelper (cpp/game/rules.cpp on master, raw file read 2026-10-29, line 372 for "goe" and "ing") accepts all
#   six names (lower-cased), so the RU property written here (Japanese, Chinese, AGA, Korean, NZ, Ing) reaches KataGo
#   unchanged once sensei.sgf lower-cases it. KataGo also has "chinese-ogs" (positional superko); it is not used, see
#   data/SOURCES.md. A newer eval/splits.py maps RU through katago_rules(), which has no entry for "ing": position_at
#   then raises ValueError for Ing games, and make_splits.py leaves them out of the pool (reason rules_or_komi).
# VERIFIED, access. https://online-go.com/robots.txt contains only "User-agent: *" and "Disallow: /bad-robots". The
#   terms of service say nothing about automated access, the API or research use (details in data/SOURCES.md).
#
# UNVERIFIED, SGF endpoint. https://online-go.com/api/v1/games/<id>/sgf exists (the fetcher got a non-text body,
#   shown as "[binary data]") but its text could not be read, so it is unknown whether its BR and WR are historical
#   or current ranks. A 2020 sample in KaTrain's tests shows the header style (PC[OGS: https://online-go.com/game/<id>],
#   PB, PW, BR[4k], WR[5k], TM, OT, RE, SZ, KM[6.5], RU[Japanese], DT). This script therefore does not use the
#   endpoint: it builds the SGF from the game JSON and writes the labels computed from historical_ratings.
# UNVERIFIED, status codes. The HTTP status of a missing ID is not visible through the fetcher (it showed "client error"
#   and, once, the text "Error: Game 68123457 not found in Scylla."). The client treats 404 and 410, and any non-JSON
#   or error body that says "not found", as a permanent "not_found". 401 and 403 are recorded as "forbidden" only
#   after a later success in the same run (a block would give a long unbroken run of them, which aborts the run).
# UNVERIFIED, rate limits. No limit is published (ogs.readme.io "Getting started" has none); whether anonymous clients
#   are throttled, and with which status or Retry-After, is unknown. Defaults: 1 request per second, backoff 2, 4, 8,
#   ... up to 120 s on HTTP 429, 5xx and network errors, at most 6 retries, then the ID is skipped (and not recorded).
# UNVERIFIED, whether private games answer 403.
# ======================================================================================================================

from __future__ import annotations

import argparse
import gzip
import http.client
import json
import math
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "src", ROOT):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import yaml  # noqa: E402

from sensei.ranks import label_to_numeric  # noqa: E402

SOURCE = "ogs"
GAME_PREFIX = "ogs-"
API_BASE = "https://online-go.com/api/v1"
GAME_PAGE = "OGS: https://online-go.com/game/"  # PC property, in the style of an OGS SGF export
DEFAULT_OUT = ROOT / "data" / "raw" / "ogs"
PREREG = ROOT / "configs" / "prereg.yaml"
INDEX_NAME = "index.jsonl"
REJECTS_NAME = "rejects.jsonl"
DEFAULT_SEED = 20261027
DEFAULT_MIN_DATE = "2024-08-01"
DEFAULT_USER_AGENT = "sensei-research/0.1 (Go coaching research; sequential low-rate sampling of public games)"

BOARD_SIZE = 19
MIN_MOVES = 60

# OGS rank scale (src/lib/rank_utils.ts): rank = ln(rating / 525) * 23.15, 29 = 1k, 30 = 1d.
OGS_A = 525.0
OGS_C = 23.15
MIN_RATING, MAX_RATING = 100.0, 6000.0
MIN_RANK, MAX_RANK = 5.0, 38.0
PROVISIONAL_DEVIATION = 160.0
MAX_RANK_DISCREPANCY = 1.0  # between the historical-rating rank and gamedata.players.*.rank, in ranks

RULES_SGF = {"japanese": "Japanese", "chinese": "Chinese", "aga": "AGA", "korean": "Korean", "nz": "NZ", "ing": "Ing"}
_POINTS_RE = re.compile(r"^(\d+(?:\.\d+)?) points$")

# (game id, start time in UTC) pairs read from the API on 2026-10-29. Sorted by id and by time.
ID_ANCHORS: tuple[tuple[int, str], ...] = (
    (66_000_000, "2024-07-14T23:15:43+00:00"),
    (66_400_000, "2024-07-28T13:37:14+00:00"),
    (66_500_000, "2024-08-01T02:55:12+00:00"),
    (66_600_000, "2024-08-04T17:12:51+00:00"),
    (67_000_000, "2024-08-18T22:37:11+00:00"),
    (69_000_000, "2024-10-27T08:55:49+00:00"),
    (70_000_000, "2024-11-29T21:28:19+00:00"),
    (71_234_567, "2025-01-10T00:24:16+00:00"),
    (73_456_789, "2025-03-19T07:28:57+00:00"),
    (75_000_000, "2025-05-06T13:24:39+00:00"),
    (76_543_210, "2025-06-21T19:18:00+00:00"),
    (79_876_543, "2025-10-02T18:52:25+00:00"),
    (81_234_568, "2025-11-14T05:00:31+00:00"),
    (83_333_333, "2026-01-19T15:02:02+00:00"),
    (88_000_000, "2026-06-18T06:33:11+00:00"),
    (89_012_345, "2026-07-22T18:47:33+00:00"),
    (90_123_456, "2026-08-26T15:56:04+00:00"),
    (91_929_295, "2026-10-26T03:29:01+00:00"),
    (92_000_000, "2026-10-28T13:11:19+00:00"),
)

# Top-level keys every finished game carries (all seen in live responses). `ended` is left out on purpose: it is null
# while a game runs, and a missing one is treated the same way.
REQUIRED_KEYS = (
    "width",
    "height",
    "rules",
    "ranked",
    "handicap",
    "outcome",
    "black_lost",
    "white_lost",
    "annulled",
    "rengo",
    "players",
    "gamedata",
)

MAX_CONSECUTIVE_ERRORS = 5  # IDs in a row that failed after all retries or had an unreadable shape
MAX_CONSECUTIVE_DENIED = 20  # 401/403 answers in a row: this looks like a block, not like private games


def say(line: str) -> None:
    """print() that flushes, so that a run of hours shows its progress even when the output is piped or redirected."""
    print(line, flush=True)


# ----------------------------------------------------------------------------------------------------------------------
# Ranks
# ----------------------------------------------------------------------------------------------------------------------


def rating_to_rank(rating: float) -> float:
    """OGS numeric rank of a Glicko-2 rating (30 = 1d, 29 = 1k), without the 5..38 display clamp."""
    return math.log(min(MAX_RATING, max(MIN_RATING, float(rating))) / OGS_A) * OGS_C


def rank_to_rating(rank: float) -> float:
    return OGS_A * math.exp(rank / OGS_C)


def rank_label(rank: float) -> str:
    """The label OGS shows for a numeric rank, from 25k to 9d: '12k', '1d'."""
    r = min(MAX_RANK, max(MIN_RANK, float(rank)))
    if r < 30:
        return f"{math.ceil(30 - r)}k"
    return f"{math.floor(r - 29)}d"


def rating_label(rating: float) -> str:
    return rank_label(rating_to_rank(rating))


def parse_band(name: str) -> tuple[float, float]:
    """'15k-10k' -> (-14.0, -9.0) and '1d-3d' -> (1.0, 3.0) on the sensei.ranks scale; both ends are inside the band."""
    left, sep, right = name.partition("-")
    if not sep:
        raise ValueError(f"bad band name {name!r}: expected two rank labels joined by '-'")
    a, b = label_to_numeric(left), label_to_numeric(right)
    return min(a, b), max(a, b)


def band_of(label: str, bands: Sequence[str]) -> str | None:
    """The band whose range contains a rank label such as '12k', or None (also for labels that are not ranks)."""
    try:
        x = label_to_numeric(label)
    except ValueError:
        return None
    for name in bands:
        lo, hi = parse_band(name)
        if lo <= x <= hi:
            return name
    return None


# ----------------------------------------------------------------------------------------------------------------------
# Game IDs and dates
# ----------------------------------------------------------------------------------------------------------------------

_ANCHOR_TIMES = [datetime.fromisoformat(t) for _, t in ID_ANCHORS]
_ANCHOR_IDS = [i for i, _ in ID_ANCHORS]


def estimate_id(when: datetime) -> int:
    """Approximate id of the first game started at `when`: linear between anchors, the nearest slope outside them."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    n = len(ID_ANCHORS)
    j = 1
    while j < n - 1 and when > _ANCHOR_TIMES[j]:
        j += 1
    t0, t1 = _ANCHOR_TIMES[j - 1], _ANCHOR_TIMES[j]
    i0, i1 = _ANCHOR_IDS[j - 1], _ANCHOR_IDS[j]
    per_second = (i1 - i0) / (t1 - t0).total_seconds()
    return round(i0 + (when - t0).total_seconds() * per_second)


def estimate_date(game_id: int) -> datetime:
    """Inverse of estimate_id: the approximate start time of a game id."""
    n = len(ID_ANCHORS)
    j = 1
    while j < n - 1 and game_id > _ANCHOR_IDS[j]:
        j += 1
    t0, t1 = _ANCHOR_TIMES[j - 1], _ANCHOR_TIMES[j]
    i0, i1 = _ANCHOR_IDS[j - 1], _ANCHOR_IDS[j]
    return t0 + (t1 - t0) * ((game_id - i0) / (i1 - i0))


def default_id_range(min_date: date, now: datetime, margin_days: int = 2) -> tuple[int, int]:
    """(lo, hi): from the start of min_date to `margin_days` before now (games that recent may still be running)."""
    lo = estimate_id(datetime(min_date.year, min_date.month, min_date.day, tzinfo=UTC))
    hi = estimate_id(now - timedelta(days=margin_days))
    if hi <= lo:
        raise ValueError(f"empty id range {lo}..{hi}: min date {min_date} is too close to now")
    return lo, hi


def sample_ids(lo: int, hi: int, seed: int) -> Iterator[int]:
    """Distinct ids drawn uniformly from lo..hi (inclusive); the first n are the same however long the run goes."""
    rng = random.Random(seed)
    seen: set[int] = set()
    span = hi - lo + 1
    while len(seen) < span:
        game_id = rng.randrange(lo, hi + 1)
        if game_id in seen:
            continue
        seen.add(game_id)
        yield game_id


def game_name(game_id: int) -> str:
    return f"{GAME_PREFIX}{game_id}"


def parse_game_name(name: str) -> int:
    m = re.fullmatch(rf"{re.escape(GAME_PREFIX)}(\d+)", name)
    if not m:
        raise ValueError(f"not an OGS game name: {name!r} (expected {GAME_PREFIX}<digits>)")
    return int(m.group(1))


# ----------------------------------------------------------------------------------------------------------------------
# Reading one game JSON: filters, ranks and the SGF
# ----------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Filters:
    min_date: date
    min_moves: int = MIN_MOVES
    allow_provisional: bool = False
    max_rank_discrepancy: float = MAX_RANK_DISCREPANCY


class Rejected(NamedTuple):
    reason: str
    detail: str = ""


class SchemaError(ValueError):
    """The JSON lacks something a finished game must have. Not recorded: it may mean that the API changed."""


@dataclass(frozen=True)
class AcceptedGame:
    record: dict[str, Any]  # one line of index.jsonl
    sgf: str


def _get(obj: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(obj, Mapping) or key not in obj:
            return None
        obj = obj[key]
    return obj


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _iso_z(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def ui_tokens(*players: Any) -> set[str]:
    out: set[str] = set()
    for player in players:
        text = _get(player, "ui_class")
        if isinstance(text, str):
            out.update(text.split())
    return out


def parse_moves(raw: Any, size: int = BOARD_SIZE) -> list[tuple[int, int] | None]:
    """gamedata.moves -> (x, y) per move with x the column from the left and y the row from the top; None = pass.

    An entry is [x, y, time_delta_ms, colour, extra] (goban's AdHocPackedMove). The colour (1 black, 2 white, 0 empty)
    is set only on edited stones and `extra` holds things like "blur"; neither is read, except that a colour that
    breaks alternate play (Black first) is refused, because build_sgf writes the colours by turn.
    """
    if not isinstance(raw, list):
        raise ValueError("moves is not a list")
    out: list[tuple[int, int] | None] = []
    for i, item in enumerate(raw):
        if not isinstance(item, list | tuple) or len(item) < 2:
            raise ValueError(f"move {i} is not a coordinate list")
        x, y = item[0], item[1]
        if isinstance(x, bool) or isinstance(y, bool) or not isinstance(x, int) or not isinstance(y, int):
            raise ValueError(f"move {i} has non-integer coordinates")
        colour = item[3] if len(item) > 3 else None
        if colour is not None and colour != (1 if i % 2 == 0 else 2):
            raise ValueError(f"move {i} has colour {colour!r}, which is not alternate play")
        if x == -1 and y == -1:
            out.append(None)
        elif 0 <= x < size and 0 <= y < size:
            out.append((x, y))
        else:
            raise ValueError(f"move {i} is off the board: {x},{y}")
    return out


def outcome_kind(outcome: str) -> tuple[str, str] | None:
    """('R', '') resignation, ('T', '') timeout, ('S', '13.5') score; None for anything else."""
    text = outcome.strip()
    if text.lower() in ("resignation", "resigniation"):
        return "R", ""
    if text.lower() == "timeout":
        return "T", ""
    m = _POINTS_RE.match(text)
    if m:
        return "S", f"{float(m.group(1)):g}"
    return None


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("]", "\\]")


def build_sgf(
    game_id: int,
    started: datetime,
    komi: float,
    rules: str,
    black_label: str,
    white_label: str,
    result: str,
    moves: Sequence[tuple[int, int] | None],
) -> str:
    """The game as one SGF (FF4, main line only). Black moves first and the colours alternate; no user names."""
    head = (
        f"(;GM[1]FF[4]CA[UTF-8]SZ[{BOARD_SIZE}]\n"
        f"PC[{_escape(GAME_PAGE + str(game_id))}]\n"
        f"DT[{started.astimezone(UTC).date().isoformat()}]KM[{komi:g}]RU[{RULES_SGF[rules]}]HA[0]\n"
        f"BR[{black_label}]WR[{white_label}]RE[{result}]\n"
    )
    nodes = []
    for i, move in enumerate(moves):
        color = "B" if i % 2 == 0 else "W"
        coord = "" if move is None else chr(ord("a") + move[0]) + chr(ord("a") + move[1])
        nodes.append(f";{color}[{coord}]")
    lines = ["".join(nodes[k : k + 10]) for k in range(0, len(nodes), 10)]
    return head + "\n".join(lines) + ")\n"


def _rating_of(data: Mapping[str, Any], color: str) -> tuple[float, float] | None:
    rating = _get(data, "historical_ratings", color, "ratings", "overall", "rating")
    deviation = _get(data, "historical_ratings", color, "ratings", "overall", "deviation")
    ok = all(isinstance(v, int | float) and not isinstance(v, bool) for v in (rating, deviation))
    return (float(rating), float(deviation)) if ok else None


def evaluate_game(game_id: int, data: Any, filters: Filters) -> AcceptedGame | Rejected:
    """Apply the filters to one game JSON. Returns the first reason for rejection, or the accepted game.

    Raises SchemaError when the JSON is not shaped like a game at all.
    """
    if not isinstance(data, Mapping):
        raise SchemaError("the JSON is not an object")
    started = parse_time(data.get("started"))
    if started is None:
        # A challenge that never started has "started": null (and no end); that is a game to reject, not a changed
        # API. Rerunning after 2026-10-29 showed it: such ids were retried first and tripped the schema stop.
        if "started" in data and data.get("started") in (None, "") and parse_time(data.get("ended")) is None:
            return Rejected("not_started")
        raise SchemaError("no readable 'started' time")
    gamedata = data.get("gamedata")
    if not isinstance(gamedata, Mapping):
        gamedata = {}

    ended = parse_time(data.get("ended"))
    if ended is None:
        return Rejected("not_finished")
    missing = [key for key in REQUIRED_KEYS if key not in data]
    if missing:  # a renamed field would otherwise reject every game quietly (or let bots through)
        raise SchemaError("missing keys: " + ", ".join(missing))
    outcome = str(data.get("outcome") or "")
    if data.get("annulled"):
        return Rejected("annulled")
    if outcome == "Cancellation":
        return Rejected("cancelled")
    width, height = data.get("width"), data.get("height")
    if width is None or height is None:
        raise SchemaError("no board size")
    if (width, height) != (BOARD_SIZE, BOARD_SIZE):
        return Rejected("not_19x19", f"{width}x{height}")
    if data.get("handicap") != 0 or gamedata.get("handicap", 0) != 0:
        return Rejected("handicap", str(data.get("handicap")))
    initial = gamedata.get("initial_state")
    if isinstance(initial, Mapping) and (initial.get("black") or initial.get("white")):
        return Rejected("setup_stones")
    if gamedata.get("initial_player", "black") != "black":
        return Rejected("white_first")
    if data.get("ranked") is not True:
        return Rejected("unranked")
    if data.get("rengo") or gamedata.get("rengo"):
        return Rejected("rengo")
    players = {c: _get(data, "players", c) for c in ("black", "white")}
    historical = {c: _get(data, "historical_ratings", c) for c in ("black", "white")}
    for color, player in players.items():
        if _get(player, "ui_class") is None:  # "" is fine (an ordinary account); a missing field is not
            raise SchemaError(f"players.{color}.ui_class is missing: bot accounts could not be told apart")
    if "bot" in ui_tokens(*players.values(), *historical.values()):
        return Rejected("bot")
    if any(_get(p, "professional") for p in players.values()) or any(
        _get(gamedata, "players", c, "professional") for c in ("black", "white")
    ):
        return Rejected("professional")
    kind = outcome_kind(outcome)
    if kind is None:
        return Rejected(f"outcome:{outcome or 'empty'}")
    black_lost, white_lost = data.get("black_lost"), data.get("white_lost")
    if bool(black_lost) == bool(white_lost):
        return Rejected("no_winner")
    if started.astimezone(UTC).date() < filters.min_date:
        return Rejected("before_min_date", started.date().isoformat())
    rules = str(data.get("rules") or "").lower()
    if rules not in RULES_SGF:
        return Rejected(f"rules:{rules or 'empty'}")
    try:
        moves = parse_moves(gamedata.get("moves"))
    except ValueError as exc:
        return Rejected("bad_moves", str(exc))
    if len(moves) < filters.min_moves:
        return Rejected("too_short", str(len(moves)))

    black_pair, white_pair = _rating_of(data, "black"), _rating_of(data, "white")
    if black_pair is None or white_pair is None:
        return Rejected("no_historical_ratings")
    (black_rating, black_dev), (white_rating, white_dev) = black_pair, white_pair
    if not filters.allow_provisional and max(black_dev, white_dev) >= PROVISIONAL_DEVIATION:
        return Rejected("provisional", f"RD {max(black_dev, white_dev):.0f}")
    black_rank, white_rank = rating_to_rank(black_rating), rating_to_rank(white_rating)
    for color, rank in (("black", black_rank), ("white", white_rank)):
        at_start = _get(gamedata, "players", color, "rank")
        if isinstance(at_start, int | float) and not isinstance(at_start, bool):
            if abs(rank - float(at_start)) > filters.max_rank_discrepancy:
                return Rejected("rank_unstable", f"{color} {rank:.2f} vs {float(at_start):.2f}")

    komi_raw = gamedata.get("komi", data.get("komi"))
    try:
        komi = float(komi_raw)
    except (TypeError, ValueError) as exc:
        raise SchemaError(f"no readable komi: {komi_raw!r}") from exc
    winner = "W" if black_lost else "B"
    code, points = kind
    result = f"{winner}+{points if code == 'S' else code}"
    black_label, white_label = rank_label(black_rank), rank_label(white_rank)
    speed = _get(gamedata, "time_control", "speed")
    record = {
        "game": game_name(game_id),
        "id": game_id,
        "source": SOURCE,
        "date": started.astimezone(UTC).date().isoformat(),
        "started": _iso_z(started),
        "ended": _iso_z(ended),
        "black_rank": black_label,
        "white_rank": white_label,
        "black_ogs_rank": round(black_rank, 3),
        "white_ogs_rank": round(white_rank, 3),
        "black_rating": round(black_rating, 1),
        "white_rating": round(white_rating, 1),
        "black_rd": round(black_dev, 1),
        "white_rd": round(white_dev, 1),
        "komi": komi,
        "rules": rules,
        "moves": len(moves),
        "outcome": outcome,
        "winner": winner,
        "speed": speed if isinstance(speed, str) else "",
    }
    sgf = build_sgf(game_id, started, komi, rules, black_label, white_label, result, moves)
    return AcceptedGame(record, sgf)


# ----------------------------------------------------------------------------------------------------------------------
# HTTP: an injected getter, a rate limiter and retries with backoff
# ----------------------------------------------------------------------------------------------------------------------


class HttpResponse(NamedTuple):
    status: int
    headers: Mapping[str, str]  # keys in lower case
    body: bytes


HttpGet = Callable[[str], HttpResponse]


def decode_body(headers: Mapping[str, str], body: bytes) -> bytes:
    if headers.get("content-encoding", "").lower() == "gzip":
        return gzip.decompress(body)
    return body


def make_urllib_get(user_agent: str, timeout: float = 30.0) -> HttpGet:
    """The real getter. HTTP error statuses come back as responses; network errors raise OSError."""

    def get(url: str) -> HttpResponse:
        request = urllib.request.Request(
            url, headers={"User-Agent": user_agent, "Accept": "application/json", "Accept-Encoding": "gzip"}
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status, headers, body = response.status, response.headers, response.read()
        except urllib.error.HTTPError as err:
            status, headers, body = err.code, err.headers, err.read() or b""
        lowered = {k.lower(): v for k, v in headers.items()} if headers else {}
        try:
            body = decode_body(lowered, body)
        except (OSError, EOFError):  # a truncated gzip stream
            body = b""
        return HttpResponse(status, lowered, body)

    return get


class FetchError(Exception):
    """A request failed after all retries (network, 429, 5xx, unreadable body). Not recorded: a rerun tries again."""


def _says_not_found(body: bytes) -> bool:
    return b"not found" in body[:2000].lower()


def _retry_after(headers: Mapping[str, str]) -> float | None:
    value = headers.get("retry-after", "").strip()
    return float(value) if re.fullmatch(r"\d+(?:\.\d+)?", value) else None


class Client:
    """One game per request at a fixed maximum rate; backs off on 429, 5xx and network errors."""

    def __init__(
        self,
        get: HttpGet,
        *,
        api_base: str = API_BASE,
        rate: float = 1.0,
        retries: int = 6,
        base_delay: float = 2.0,
        max_delay: float = 120.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        log: Callable[[str], None] = say,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self._get = get
        self.api_base = api_base.rstrip("/")
        self.rate = rate
        self._interval = 1.0 / rate
        self.retries = retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self._sleep = sleep
        self._clock = clock
        self._log = log
        self._last_start: float | None = None
        self.requests = 0

    def _throttle(self) -> None:
        now = self._clock()
        if self._last_start is not None:
            wait = self._last_start + self._interval - now
            if wait > 0:
                self._sleep(wait)
                now = self._clock()
        self._last_start = now

    def get_game(self, game_id: int) -> tuple[str, Any]:
        """('ok', json), ('missing', None) or ('denied', None). Raises FetchError after the last retry."""
        url = f"{self.api_base}/games/{game_id}"
        attempt = 0
        while True:
            self._throttle()
            self.requests += 1
            retry_after: float | None = None
            try:
                response = self._get(url)
            except (OSError, http.client.HTTPException) as exc:
                problem = f"network error ({type(exc).__name__}: {exc})"
            else:
                status = response.status
                if status in (404, 410):
                    return "missing", None
                if status == 200:
                    try:
                        return "ok", json.loads(response.body.decode("utf-8"))
                    except ValueError:
                        if _says_not_found(response.body):
                            return "missing", None
                        problem = "HTTP 200 with a body that is not JSON"
                elif status in (401, 403):
                    return "denied", None
                elif status == 429 or status >= 500:
                    if _says_not_found(response.body):
                        return "missing", None
                    problem = f"HTTP {status}"
                    retry_after = _retry_after(response.headers)
                elif _says_not_found(response.body):
                    return "missing", None
                else:
                    raise FetchError(f"HTTP {status} for {url}")
            if attempt >= self.retries:
                raise FetchError(f"{problem} for {url} after {self.retries} retries")
            delay = min(self.max_delay, self.base_delay * 2**attempt)
            if retry_after is not None:
                delay = min(max(delay, retry_after), 600.0)
            self._log(f"  {problem}; retry {attempt + 1}/{self.retries} in {delay:.0f} s")
            self._sleep(delay)
            attempt += 1


# ----------------------------------------------------------------------------------------------------------------------
# The output directory: SGF files, index.jsonl, rejects.jsonl
# ----------------------------------------------------------------------------------------------------------------------


def _append_line(path: Path, obj: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    needs_newline = False
    if path.is_file() and path.stat().st_size > 0:
        with path.open("rb") as handle:
            handle.seek(-1, os.SEEK_END)
            needs_newline = handle.read(1) != b"\n"  # a run killed in the middle of a line
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        if needs_newline:
            handle.write("\n")
        handle.write(json.dumps(obj, separators=(",", ":")) + "\n")
        handle.flush()


def _read_lines(path: Path) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    damaged = 0
    if not path.is_file():
        return rows, damaged
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            damaged += 1
            continue
        if isinstance(row, dict) and isinstance(row.get("id"), int):
            rows.append(row)
        else:
            damaged += 1
    return rows, damaged


class Store:
    def __init__(self, directory: Path) -> None:
        self.dir = Path(directory)
        self.index_path = self.dir / INDEX_NAME
        self.rejects_path = self.dir / REJECTS_NAME
        index_rows, bad_index = _read_lines(self.index_path)
        reject_rows, bad_rejects = _read_lines(self.rejects_path)
        self.damaged_lines = bad_index + bad_rejects
        self.index: dict[int, dict[str, Any]] = {r["id"]: r for r in index_rows}  # the last line of an id wins
        self.rejects: dict[int, dict[str, Any]] = {r["id"]: r for r in reject_rows}

    def sgf_path(self, game_id: int) -> Path:
        return self.dir / f"{game_name(game_id)}.sgf"

    def has_game(self, game_id: int) -> bool:
        return game_id in self.index and self.sgf_path(game_id).is_file()

    def seen(self, game_id: int) -> bool:
        return game_id in self.rejects or self.has_game(game_id)

    def accepted_count(self) -> int:
        return sum(1 for game_id in self.index if self.sgf_path(game_id).is_file())

    def add_accepted(self, game_id: int, accepted: AcceptedGame) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.sgf_path(game_id)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(accepted.sgf, encoding="utf-8", newline="\n")
        os.replace(tmp, path)
        if game_id not in self.index:
            _append_line(self.index_path, accepted.record)
        self.index[game_id] = accepted.record

    def add_reject(self, game_id: int, reason: str, detail: str = "") -> None:
        row: dict[str, Any] = {"id": game_id, "reason": reason}
        if detail:
            row["detail"] = detail
        _append_line(self.rejects_path, row)
        self.rejects[game_id] = row

    def accepted_records(self) -> list[dict[str, Any]]:
        return [self.index[i] for i in sorted(self.index) if self.sgf_path(i).is_file()]


# ----------------------------------------------------------------------------------------------------------------------
# Configuration, summary and the run loop
# ----------------------------------------------------------------------------------------------------------------------


def load_prereg(path: Path = PREREG) -> dict[str, Any]:
    """configs/prereg.yaml as a dict (empty when the file is missing)."""
    if not Path(path).is_file():
        return {}
    cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return dict(cfg) if isinstance(cfg, Mapping) else {}


def band_names(prereg: Mapping[str, Any]) -> list[str]:
    return [str(b["name"]) for b in (_get(prereg, "C2", "bands") or [])]


def band_coverage(records: Iterable[Mapping[str, Any]], bands: Sequence[str]) -> dict[str, tuple[int, int]]:
    """band -> (games with at least one player in the band, player slots in the band)."""
    games: Counter[str] = Counter()
    slots: Counter[str] = Counter()
    for rec in records:
        in_game: set[str] = set()
        for key in ("black_rank", "white_rank"):
            band = band_of(str(rec.get(key, "")), bands)
            if band:
                slots[band] += 1
                in_game.add(band)
        games.update(in_game)
    return {b: (games[b], slots[b]) for b in bands}


@dataclass
class RunStats:
    drawn: int = 0
    skipped_seen: int = 0
    accepted: int = 0
    rejected: Counter[str] = field(default_factory=Counter)
    errors: Counter[str] = field(default_factory=Counter)
    requests: int = 0
    seconds: float = 0.0
    stop_reason: str = ""
    aborted: bool = False  # stopped by a block, a dead server or a changed API
    interrupted: bool = False
    not_reproduced: list[str] = field(default_factory=list)


def format_summary(stats: RunStats, store: Store, prereg: Mapping[str, Any], target: int | None) -> list[str]:
    records = store.accepted_records()
    lines = [
        "",
        "Summary",
        f"  requests this run: {stats.requests} in {stats.seconds / 60:.1f} min; ids drawn: {stats.drawn}; "
        f"already seen and skipped: {stats.skipped_seen}",
        f"  accepted this run: {stats.accepted}; accepted in {store.dir}: {len(records)}"
        + (f" (target {target})" if target else ""),
    ]
    if stats.rejected:
        parts = ", ".join(f"{k}={v}" for k, v in stats.rejected.most_common())
        lines.append(f"  rejected this run ({sum(stats.rejected.values())}): {parts}")
    if stats.errors:
        parts = ", ".join(f"{k}={v}" for k, v in stats.errors.most_common())
        lines.append(f"  errors this run, not recorded and retried on a rerun: {parts}")
    on_disk = Counter(str(r.get("reason", "")).split(":")[0] for r in store.rejects.values())
    if on_disk:
        parts = ", ".join(f"{k}={v}" for k, v in on_disk.most_common())
        lines.append(f"  all rejects on disk ({len(store.rejects)}): {parts}")
    if store.damaged_lines:
        lines.append(f"  WARNING: {store.damaged_lines} unreadable lines in index.jsonl / rejects.jsonl were ignored")
    if records:
        dates = sorted(str(r["date"]) for r in records)
        lines.append(f"  date range of accepted games: {dates[0]} to {dates[-1]}")
        speeds = Counter(str(r.get("speed") or "?") for r in records)
        lines.append("  time control speed: " + ", ".join(f"{k}={v}" for k, v in speeds.most_common()))
    bands = band_names(prereg)
    if bands and records:
        positions = _get(prereg, "C2", "positions_per_band") or 0
        cap = _get(prereg, "C2", "max_positions_per_game") or 1
        need = math.ceil(positions / cap) if positions else 0
        lines.append(
            f"  C2 rank bands (games with a player in the band / players in the band; need {need}+ games each):"
        )
        coverage = band_coverage(records, bands)
        for band in bands:
            games, slots = coverage[band]
            flag = "" if games >= need else "  <- fetch more"
            lines.append(f"    {band:>8}: {games:5d} games / {slots:5d} players{flag}")
        outside = sum(1 for r in records for k in ("black_rank", "white_rank") if band_of(str(r[k]), bands) is None)
        lines.append(f"    outside every band: {outside} players")
    if stats.not_reproduced:
        lines.append(f"  NOT reproduced ({len(stats.not_reproduced)}): " + ", ".join(stats.not_reproduced[:20]))
    if stats.stop_reason:
        lines.append(f"  stopped: {stats.stop_reason}")
    return lines


@dataclass(frozen=True)
class RunConfig:
    out_dir: Path
    filters: Filters
    target: int | None = None
    id_range: tuple[int, int] | None = None
    ids: tuple[int, ...] | None = None  # --ids-from: fetch exactly these
    seed: int = DEFAULT_SEED
    max_requests: int | None = None


class FetchRun:
    """One run: draw ids, fetch, judge, store. Everything is saved as it goes, so a stopped run resumes."""

    def __init__(self, cfg: RunConfig, client: Client, log: Callable[[str], None] = say) -> None:
        if cfg.ids is None and (cfg.id_range is None or cfg.target is None):
            raise ValueError("a run needs either ids or both an id range and a target")
        self.cfg, self.client, self.log = cfg, client, log
        self.store = Store(cfg.out_dir)
        self.stats = RunStats()
        self.accepted_total = self.store.accepted_count()
        self.goal = len(cfg.ids) if cfg.ids is not None else int(cfg.target or 0)
        self._pending_denied: list[int] = []  # recorded only once a later request succeeds, so a block leaves no trace
        self._fetch_streak = 0  # ids in a row whose request failed after all retries
        self._schema_streak = 0  # games in a row whose JSON was not shaped like a finished game
        self._t0 = time.monotonic()

    def _ids(self) -> Iterable[int]:
        if self.cfg.ids is not None:
            return self.cfg.ids
        assert self.cfg.id_range is not None
        return sample_ids(self.cfg.id_range[0], self.cfg.id_range[1], self.cfg.seed)

    def _limit_reached(self) -> bool:
        if self.cfg.ids is None and self.accepted_total >= self.goal:
            self.stats.stop_reason = f"target of {self.goal} accepted games reached"
            return True
        if self.cfg.max_requests is not None and self.client.requests >= self.cfg.max_requests:
            self.stats.stop_reason = f"--max-requests {self.cfg.max_requests} reached"
            return True
        return False

    def _abort(self, reason: str) -> bool:
        self.stats.aborted = True
        self.stats.stop_reason = reason
        return False

    def _flush_denied(self) -> None:
        for game_id in self._pending_denied:
            self.store.add_reject(game_id, "forbidden")
            self.stats.rejected["forbidden"] += 1
        self._pending_denied.clear()

    def _progress(self) -> None:
        requests = self.client.requests
        per_game = requests / max(self.stats.accepted, 1)
        minutes_left = max(self.goal - self.accepted_total, 0) * per_game / self.client.rate / 60
        self.log(
            f"  progress: {requests} requests, {self.stats.accepted} accepted this run "
            f"({100 * self.stats.accepted / max(requests, 1):.0f}% of requests), about {minutes_left:.0f} min to go"
        )

    def _process(self, game_id: int) -> bool:
        """Fetch and judge one id. False means: stop the run."""
        name = game_name(game_id)
        try:
            status, data = self.client.get_game(game_id)
        except FetchError as exc:
            self.log(f"  {name}: {exc}")
            self.stats.errors["fetch_failed"] += 1
            self._fetch_streak += 1
            if self._fetch_streak >= MAX_CONSECUTIVE_ERRORS:
                return self._abort(
                    f"{MAX_CONSECUTIVE_ERRORS} ids in a row failed after all retries: the server may be down"
                )
            return True
        if status == "denied":
            self._pending_denied.append(game_id)
            if len(self._pending_denied) >= MAX_CONSECUTIVE_DENIED:
                self.stats.errors["denied"] += len(self._pending_denied)
                self._pending_denied.clear()
                return self._abort(
                    f"{MAX_CONSECUTIVE_DENIED} requests in a row were refused (HTTP 401/403): this looks like a block;"
                    " check --user-agent and --contact and wait before retrying (those ids were not recorded)"
                )
            return True
        self._fetch_streak = 0
        self._flush_denied()
        if status == "missing":
            self.store.add_reject(game_id, "not_found")
            self.stats.rejected["not_found"] += 1
            return True
        try:
            verdict = evaluate_game(game_id, data, self.cfg.filters)
        except SchemaError as exc:
            self.log(f"  {name}: unexpected JSON shape ({exc})")
            self.stats.errors["schema"] += 1
            self._schema_streak += 1
            if self._schema_streak >= MAX_CONSECUTIVE_ERRORS:
                return self._abort(
                    f"{MAX_CONSECUTIVE_ERRORS} unexpected JSON shapes in a row: the API may have changed"
                )
            return True
        self._schema_streak = 0  # not-found answers neither reset nor extend this streak: only a game that was read
        if isinstance(verdict, Rejected):
            self.store.add_reject(game_id, verdict.reason, verdict.detail)
            self.stats.rejected[verdict.reason.split(":")[0]] += 1
            if self.cfg.ids is not None:
                self.stats.not_reproduced.append(f"{name} ({verdict.reason})")
                self.log(f"  WARNING {name} is named in the splits file but is now rejected: {verdict.reason}")
            return True
        had_file = self.store.has_game(game_id)
        self.store.add_accepted(game_id, verdict)
        self.stats.accepted += 1
        self.accepted_total += 0 if had_file else 1
        rec = verdict.record
        self.log(
            f"  accepted {name}: {rec['black_rank']} vs {rec['white_rank']}, {rec['moves']} moves, {rec['date']}"
            f" [{self.accepted_total}/{self.goal}]"
        )
        if self.stats.accepted % 25 == 0:
            self._progress()
        return True

    def execute(self) -> tuple[RunStats, Store]:
        self.log(
            f"Output directory {self.store.dir}: {self.accepted_total} games and {len(self.store.rejects)} rejects"
            " already there."
        )
        try:
            for game_id in self._ids():
                if self._limit_reached():
                    break
                self.stats.drawn += 1
                if self.store.seen(game_id):
                    self.stats.skipped_seen += 1
                    continue
                if not self._process(game_id):
                    break
            else:
                if self.cfg.ids is None:
                    self.stats.stop_reason = "the id range is exhausted"
        except KeyboardInterrupt:
            self.stats.interrupted = True
            self.stats.stop_reason = "interrupted (Ctrl-C); everything fetched so far is saved and a rerun resumes"
        self.stats.requests = self.client.requests
        self.stats.seconds = time.monotonic() - self._t0
        return self.stats, self.store


def positive_float(text: str) -> float:
    value = float(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return value


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", type=int, help="stop at this many accepted games in --out (needed without --ids-from)")
    ap.add_argument(
        "--min-date", help=f"earliest start date, UTC, YYYY-MM-DD (default C2.game_date_min: {DEFAULT_MIN_DATE})"
    )
    ap.add_argument(
        "--id-range",
        type=int,
        nargs=2,
        metavar=("LO", "HI"),
        help="sample ids from LO..HI instead of the date-mapped range",
    )
    ap.add_argument(
        "--rate", type=positive_float, default=1.0, help="requests per second (default 1.0; please stay low)"
    )
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="output directory (default data/raw/ogs)")
    ap.add_argument("--ids-from", metavar="SPLITS_JSON", help="re-fetch exactly the games a splits file names")
    ap.add_argument("--seed", type=int, help="seed of the id sequence (default seeds.global in configs/prereg.yaml)")
    ap.add_argument("--max-requests", type=int, help="stop after this many HTTP requests (for a trial run)")
    ap.add_argument(
        "--min-moves", type=int, default=MIN_MOVES, help="minimum number of moves, passes included (default 60)"
    )
    ap.add_argument(
        "--allow-provisional",
        action="store_true",
        help=f"also accept players whose rating deviation is {PROVISIONAL_DEVIATION:g} or more (unreliable ranks)",
    )
    ap.add_argument(
        "--retries", type=int, default=6, help="retries per id on HTTP 429/5xx or network errors (default 6)"
    )
    ap.add_argument("--user-agent", help="replace the User-Agent header")
    ap.add_argument("--contact", help="repository URL or e-mail added to the User-Agent so that OGS can reach you")
    ap.add_argument("--api-base", default=API_BASE, help=argparse.SUPPRESS)
    return ap


def user_agent_for(args: argparse.Namespace) -> str:
    if args.user_agent:
        return str(args.user_agent)
    if args.contact:
        return f"{DEFAULT_USER_AGENT[:-1]}; contact: {args.contact})"
    return DEFAULT_USER_AGENT


def main(
    argv: Sequence[str] | None = None,
    *,
    http_get: HttpGet | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    now: datetime | None = None,
    log: Callable[[str], None] = say,
    prereg_path: Path = PREREG,
) -> int:
    args = build_parser().parse_args(argv)
    prereg = load_prereg(prereg_path)
    seed = args.seed if args.seed is not None else int(_get(prereg, "seeds", "global") or DEFAULT_SEED)
    min_date_text = args.min_date or str(_get(prereg, "C2", "game_date_min") or DEFAULT_MIN_DATE)
    try:
        min_date = date.fromisoformat(min_date_text)
    except ValueError:
        log(f"error: --min-date {min_date_text!r} is not YYYY-MM-DD")
        return 2
    filters = Filters(min_date=min_date, min_moves=args.min_moves, allow_provisional=args.allow_provisional)

    ids: tuple[int, ...] | None = None
    id_range: tuple[int, int] | None = None
    if args.ids_from:
        from eval.splits import games_in, load_splits  # only this mode needs the splits schema

        names = sorted(set().union(*games_in(load_splits(Path(args.ids_from))).values()))
        try:
            ids = tuple(sorted(parse_game_name(n) for n in names))
        except ValueError as exc:
            log(f"error: {exc}")
            return 2
        log(f"{args.ids_from} names {len(ids)} games.")
    else:
        if args.target is None or args.target < 1:
            log("error: give --target N (N >= 1) or --ids-from SPLITS_JSON")
            return 2
        try:
            id_range = (
                tuple(sorted(args.id_range)) if args.id_range else default_id_range(min_date, now or datetime.now(UTC))
            )
        except ValueError as exc:
            log(f"error: {exc}")
            return 2
        lo, hi = id_range
        log(
            f"Sampling ids {lo}..{hi} (about {estimate_date(lo):%Y-%m-%d} to {estimate_date(hi):%Y-%m-%d}),"
            f" seed {seed}, target {args.target}, {args.rate:g} request/s, games started on or after {min_date}."
        )
    if args.rate > 2:
        log(f"note: {args.rate:g} requests per second is a lot to ask of a volunteer-run server.")

    get = http_get or make_urllib_get(user_agent_for(args))
    client = Client(
        get, api_base=args.api_base, rate=args.rate, retries=args.retries, sleep=sleep, clock=clock, log=log
    )
    cfg = RunConfig(Path(args.out), filters, args.target, id_range, ids, seed, args.max_requests)
    stats, store = FetchRun(cfg, client, log).execute()
    for line in format_summary(stats, store, prereg, args.target):
        log(line)
    if stats.interrupted:
        return 130
    if stats.aborted:
        return 2
    if ids is not None and (stats.not_reproduced or stats.errors):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
