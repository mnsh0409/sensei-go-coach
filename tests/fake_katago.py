"""A stand-in for `katago analysis`, used by the offline tests and by `scripts/bench_engine.py --fake`.

Speaks the protocol of KATAGO_Analysis_Engine.md on stdin/stdout: one JSON query per line in, one JSON response per
line out, possibly out of order, from a pool of worker threads that sleep a little before answering. The numbers are
synthetic but have the documented shape and the documented perspective rules:

- rootInfo.currentPlayer is the player to move after the move list (Black at turn 0 unless initialPlayer says
  otherwise; otherwise the opponent of the last mover).
- A Black-perspective winrate and score lead come from the stone-count difference and the komi. They are reported
  from the perspective named by overrideSettings.reportAnalysisWinratesAs, defaulting to BLACK exactly like the
  shipped analysis_example.cfg, so a wrapper that forgets the SIDETOMOVE override reports the wrong side whenever
  White is to move. Ownership follows the same rule: positive means the reporting side owns the point.
- policy and humanPolicy have boardYSize * boardXSize + 1 entries, row-major from the top left (A19), pass last,
  -1 on occupied points. humanPolicy flattens toward uniform for weaker humanSLProfile ranks.
- moveInfos cover a few legal empty points, listed in an order that is NOT sorted by visits (KataGo's `order`
  field is by playSelectionValue, so a consumer has to sort itself). A 1-visit query has no moveInfos, as in KataGo.
- Errors use the documented {"error", "field", "id"} shape (illegal move, unknown profile, bad field, ...); a query
  without a string id gets {"error": ...} with no id.
- stderr receives a startup banner and an occasional warning line. The process exits when stdin closes, after
  answering every queued query, like KataGo without -quit-without-waiting.

Usage: python tests/fake_katago.py analysis -config x -model y -human-model z   (every argument is ignored).
The sleep per query grows with maxVisits (20 microseconds per visit), so a huge maxVisits makes a slow query.
"""

from __future__ import annotations

import hashlib
import json
import math
import queue
import random
import re
import sys
import threading
import time

COLUMNS = "ABCDEFGHJKLMNOPQRST"
VERSION = "1.16.4-fake"
WORKERS = 4
DEFAULT_MAX_VISITS = 500  # analysis_example.cfg
RULES_PRESETS = {
    "chinese",
    "japanese",
    "korean",
    "tromp-taylor",
    "aga",
    "bga",
    "new-zealand",
    "stone-scoring",
    "ancient-area",
    "ancient-territory",
    "chinese-ogs",
    "chinese-kgs",
    "aga-button",
    "ing",
    "goe",
}
PROFILE_RE = re.compile(r"^(rank|preaz)_(\d{1,2}[kd])(?:_(\d{1,2}[kd]))?$|^proyear_(\d{4})$")
WARN_EVERY = 7

_out_lock = threading.Lock()
_query_counter = [0]


# ------------------------------------------------------------------------------------------------------ helpers


def rank_value(label: str) -> float:
    """'20k' -> -19, '1k' -> 0, '5d' -> 5 (the numeric scale of sensei.ranks)."""
    n, kind = int(label[:-1]), label[-1]
    return float(1 - n) if kind == "k" else float(n)


def parse_profile(profile: str, current: str) -> float:
    """Strength of `current`'s imitated player, or raise ValueError for an unknown profile."""
    m = PROFILE_RE.match(profile)
    if not m:
        raise ValueError(f"unknown humanSLProfile {profile!r}")
    if m.group(4):
        year = int(m.group(4))
        if not 1800 <= year <= 2023:
            raise ValueError(f"proyear out of range in {profile!r}")
        return 7.0
    labels = [m.group(2)] + ([m.group(3)] if m.group(3) else [])
    for label in labels:
        value = rank_value(label)
        if not -19.0 <= value <= 9.0:
            raise ValueError(f"rank out of range in {profile!r}")
    if len(labels) == 2:
        return rank_value(labels[0] if current == "B" else labels[1])
    return rank_value(labels[0])


def parse_location(text: str, xs: int, ys: int) -> tuple[int, int] | None:
    """GTP location -> (row, col) with row 0 at the top; 'pass' -> None; ValueError when off the board."""
    s = str(text).strip().upper()
    if s == "PASS":
        return None
    m = re.fullmatch(r"([A-HJ-T])(\d{1,2})", s)
    if not m:
        raise ValueError(f"could not parse location {text!r}")
    col = COLUMNS.index(m.group(1))
    n = int(m.group(2))
    if col >= xs or not 1 <= n <= ys:
        raise ValueError(f"location {text!r} is off the board")
    return ys - n, col


def to_gtp(rc: tuple[int, int], ys: int) -> str:
    row, col = rc
    return f"{COLUMNS[col]}{ys - row}"


def unit_hash(*parts: object) -> float:
    """Deterministic pseudo-random number in [0, 1) from the parts."""
    digest = hashlib.md5("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()
    return int(digest[:8], 16) / 2**32


def error(qid: str | None, field: str, message: str) -> dict:
    out = {"error": message, "field": field}
    if qid is not None:
        out["id"] = qid
    return out


class QueryError(Exception):
    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


# ---------------------------------------------------------------------------------------------- synthetic numbers


def engine_weights(stones: dict[tuple[int, int], str], xs: int, ys: int) -> dict[tuple[int, int] | None, float]:
    """Unnormalized 'engine' preference over empty points plus pass: corners, star points and contact moves."""
    weights: dict[tuple[int, int] | None, float] = {}
    hoshi = {3, ys - 4} if ys >= 9 else set()
    hoshi_c = {3, xs - 4} if xs >= 9 else set()
    for r in range(ys):
        for c in range(xs):
            if (r, c) in stones:
                continue
            w = 0.02
            dr, dc = min(r, ys - 1 - r), min(c, xs - 1 - c)
            if r in hoshi and c in hoshi_c:
                w += 1.0
            elif (r in hoshi and dc == 2) or (c in hoshi_c and dr == 2):
                w += 0.5
            elif dr == 2 and dc == 2:
                w += 0.3
            if ys % 2 == 1 and xs % 2 == 1 and r == ys // 2 and c == xs // 2:
                w += 0.2
            if dr == 0 or dc == 0:
                w *= 0.25
            for nr, nc in ((r - 1, c), (r + 1, c), (r, c - 1), (r, c + 1)):
                if (nr, nc) in stones:
                    w += 0.3
            w *= 0.85 + 0.3 * unit_hash("w", r, c, len(stones))
            weights[(r, c)] = w
    weights[None] = 0.001
    return weights


def flat_policy(weights: dict, stones: dict, xs: int, ys: int, blend_uniform: float = 0.0) -> list[float]:
    """Row-major array of length ys*xs+1 (pass last), -1 on occupied points, legal values summing to 1."""
    total = sum(weights.values())
    legal_count = len(weights)
    out: list[float] = []
    for r in range(ys):
        for c in range(xs):
            if (r, c) in stones:
                out.append(-1.0)
            else:
                p = weights[(r, c)] / total
                out.append((1.0 - blend_uniform) * p + blend_uniform / legal_count)
    p_pass = weights[None] / total
    out.append((1.0 - blend_uniform) * p_pass + blend_uniform / legal_count)
    return out


def ownership_black(stones: dict[tuple[int, int], str], xs: int, ys: int, lead_black: float) -> list[float]:
    baseline = 0.15 * math.tanh(lead_black / 10.0)
    out: list[float] = []
    for r in range(ys):
        for c in range(xs):
            influence = 0.0
            for (sr, sc), color in stones.items():
                d = abs(sr - r) + abs(sc - c)
                if d <= 3:
                    influence += (1.0 if color == "B" else -1.0) * (1.0 - d / 4.0)
            out.append(0.95 * max(-1.0, min(1.0, influence + baseline)))
    return out


def analysis_response(
    qid: str,
    turn: int,
    stones: dict[tuple[int, int], str],
    current: str,
    xs: int,
    ys: int,
    komi: float,
    max_visits: int,
    report_as: str,
    profile: str | None,
    include_policy: bool,
    include_ownership: bool,
    rng: random.Random,
) -> dict:
    n_black = sum(1 for v in stones.values() if v == "B")
    n_white = len(stones) - n_black
    lead_black = 2.0 * (n_black - n_white) - 0.1 * komi
    wr_black = 1.0 / (1.0 + math.exp(-lead_black / 4.0))
    pov = "B" if report_as == "BLACK" else "W" if report_as == "WHITE" else current
    sign = 1.0 if pov == "B" else -1.0
    root_wr = wr_black if pov == "B" else 1.0 - wr_black
    root_lead = sign * lead_black

    weights = engine_weights(stones, xs, ys)
    policy = flat_policy(weights, stones, xs, ys)
    total_w = sum(weights.values())
    strength = None if profile is None else parse_profile(profile, current)
    blend = None if strength is None else min(0.95, max(0.05, (9.0 - strength) / 30.0))
    human = None if blend is None else flat_policy(weights, stones, xs, ys, blend_uniform=blend)

    move_infos: list[dict] = []
    if max_visits >= 2:
        points = [p for p in weights if p is not None]
        points.sort(key=lambda p: weights[p], reverse=True)
        top = points[: min(6, len(points), max_visits - 1)]
        raw = [weights[p] ** 2 for p in top]
        budget = max_visits - 1
        visits = [max(1, round(budget * x / sum(raw))) for x in raw]
        visits[0] += budget - sum(visits) if sum(visits) <= budget else 0
        ranked = sorted(range(len(top)), key=lambda i: (-visits[i], -weights[top[i]]))
        for order, i in enumerate(ranked):
            move = to_gtp(top[i], ys)
            h = unit_hash("mi", move, turn, len(stones))
            wr = min(1.0, max(0.0, root_wr + 0.06 * (h - 0.5)))
            lead = root_lead + (h - 0.5)
            prior = weights[top[i]] / total_w
            reply = to_gtp(top[(i + 1) % len(top)], ys) if len(top) > 1 else "pass"
            info = {
                "move": move,
                "visits": visits[i],
                "edgeVisits": visits[i],
                "weight": float(visits[i]),
                "edgeWeight": float(visits[i]),
                "winrate": wr,
                "lcb": wr - 0.02,
                "scoreLead": lead,
                "scoreMean": lead,
                "scoreSelfplay": lead * 1.2,
                "scoreStdev": 20.0 + 10.0 * h,
                "utility": 2.0 * wr - 1.0,
                "utilityLcb": 2.0 * wr - 1.04,
                "prior": prior,
                "order": order,
                "playSelectionValue": float(visits[i]),
                "pv": [move, reply] if visits[i] > 1 else [move],
            }
            if human is not None:
                idx = top[i][0] * xs + top[i][1]
                info["humanPrior"] = human[idx]
            move_infos.append(info)
        rng.shuffle(move_infos)  # KataGo's list order is by playSelectionValue, not by visits; consumers must sort

    root_info = {
        "currentPlayer": current,
        "winrate": root_wr,
        "lcb": root_wr - 0.01,
        "scoreLead": root_lead,
        "scoreSelfplay": root_lead * 1.2,
        "scoreStdev": 24.0,
        "utility": 2.0 * root_wr - 1.0,
        "visits": max_visits,
        "rawWinrate": root_wr,
        "rawLead": root_lead,
        "thisHash": hashlib.md5(f"{sorted(stones.items())}|{current}".encode()).hexdigest().upper(),
        "symHash": hashlib.md5(f"{len(stones)}|{current}".encode()).hexdigest().upper(),
    }
    if human is not None:
        root_info["humanWinrate"] = root_wr
    resp: dict = {
        "id": qid,
        "isDuringSearch": False,
        "turnNumber": turn,
        "moveInfos": move_infos,
        "rootInfo": root_info,
    }
    if include_ownership:
        own = ownership_black(stones, xs, ys, lead_black)
        resp["ownership"] = [sign * o for o in own]
    if include_policy:
        resp["policy"] = policy
        if human is not None:
            resp["humanPolicy"] = human
    return resp


# ------------------------------------------------------------------------------------------------- query parsing


def parse_stones(items: object, field: str, xs: int, ys: int, board: dict[tuple[int, int], str]) -> list:
    """Place [player, location] pairs on `board`; raise QueryError for malformed or illegal entries."""
    if not isinstance(items, list):
        raise QueryError(field, f"{field} must be a list of [player, location] pairs")
    placed = []
    for k, item in enumerate(items):
        if not (isinstance(item, list) and len(item) == 2):
            raise QueryError(field, f"{field}[{k}] must be a [player, location] pair")
        player, loc = item
        if player not in ("B", "W"):
            raise QueryError(field, f"{field}[{k}]: player must be \"B\" or \"W\", got {player!r}")
        try:
            rc = parse_location(loc, xs, ys)
        except ValueError as exc:
            raise QueryError(field, f"{field}[{k}]: {exc}") from exc
        if rc is not None:
            if rc in board:
                raise QueryError(field, f"Illegal move {k}: {player} {loc} (point is occupied)")
            board[rc] = player
        placed.append((player, rc))
    return placed


def handle_query(query: object, rng: random.Random) -> list[dict]:
    """Responses for one query line (several for several analyzeTurns)."""
    if not isinstance(query, dict):
        return [{"error": "Query must be a JSON object"}]
    qid = query.get("id")
    if not isinstance(qid, str):
        return [{"error": 'Must specify "id" as a string'}]
    action = query.get("action")
    if action is not None:
        if action == "query_version":
            return [{**query, "version": VERSION, "git_hash": "<omitted>"}]
        if action in ("clear_cache", "terminate", "terminate_all", "set_focus"):
            return [dict(query)]
        return [error(qid, "action", f"Unknown action {action!r}")]
    try:
        return analyze_query(qid, query, rng)
    except QueryError as exc:
        return [error(qid, exc.field, str(exc))]


def _int_field(query: dict, name: str, default: int | None, lo: int, hi: int) -> int:
    value = query.get(name, default)
    if value is None:
        raise QueryError(name, f"Must specify {name}")
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise QueryError(name, f"{name} must be an integer from {lo} to {hi}")
    return value


def analyze_query(qid: str, query: dict, rng: random.Random) -> list[dict]:
    xs = _int_field(query, "boardXSize", None, 2, 19)
    ys = _int_field(query, "boardYSize", None, 2, 19)
    rules = query.get("rules")
    if isinstance(rules, str):
        if rules.lower() not in RULES_PRESETS:
            raise QueryError("rules", f"Unknown rules {rules!r}")
    elif not isinstance(rules, dict):
        raise QueryError("rules", "Must specify rules as a string or a JSON object")
    komi = query.get("komi", 7.5)
    if isinstance(komi, bool) or not isinstance(komi, int | float) or (komi * 2) != int(komi * 2):
        raise QueryError("komi", "komi must be an integer or half-integer")
    komi = float(komi)
    if not -400.0 <= komi <= 400.0:
        raise QueryError("komi", "komi out of range")
    max_visits = _int_field(query, "maxVisits", DEFAULT_MAX_VISITS, 1, 2**31 - 1)
    include_policy = bool(query.get("includePolicy", False))
    include_ownership = bool(query.get("includeOwnership", False))
    overrides = query.get("overrideSettings", {})
    if not isinstance(overrides, dict):
        raise QueryError("overrideSettings", "overrideSettings must be an object")
    report_as = str(overrides.get("reportAnalysisWinratesAs", "BLACK")).upper()
    if report_as not in ("BLACK", "WHITE", "SIDETOMOVE"):
        raise QueryError("overrideSettings", f"Could not parse reportAnalysisWinratesAs {report_as!r}")
    profile = overrides.get("humanSLProfile")
    if profile is not None and not isinstance(profile, str):
        raise QueryError("overrideSettings", "humanSLProfile must be a string")
    initial_player = query.get("initialPlayer")
    if initial_player is not None and initial_player not in ("B", "W"):
        raise QueryError("initialPlayer", 'initialPlayer must be "B" or "W"')
    if "moves" not in query:
        raise QueryError("moves", "Must specify moves")

    board: dict[tuple[int, int], str] = {}
    parse_stones(query.get("initialStones", []), "initialStones", xs, ys, board)
    initial_board = dict(board)
    moves = parse_stones(query["moves"], "moves", xs, ys, board)

    turns = query.get("analyzeTurns", [len(moves)])
    if not isinstance(turns, list) or not all(isinstance(t, int) and 0 <= t <= len(moves) for t in turns):
        raise QueryError("analyzeTurns", f"analyzeTurns must be a list of integers from 0 to {len(moves)}")
    if initial_player is None:
        initial_player = moves[0][0] if moves else "B"
    if profile is not None:  # validate before answering anything (parse_profile checks every rank label)
        try:
            parse_profile(profile, "B")
        except ValueError as exc:
            raise QueryError("overrideSettings", f"Could not set override settings: {exc}") from exc

    responses = []
    for turn in sorted(set(turns)):
        stones = dict(initial_board)
        for player, rc in moves[:turn]:
            if rc is not None:
                stones[rc] = player
        current = initial_player if turn == 0 else ("W" if moves[turn - 1][0] == "B" else "B")
        responses.append(
            analysis_response(
                qid, turn, stones, current, xs, ys, komi, max_visits, report_as, profile,
                include_policy, include_ownership, rng,
            )
        )
    return responses


# --------------------------------------------------------------------------------------------------- process loop


def write_response(resp: dict) -> None:
    line = json.dumps(resp, sort_keys=True, separators=(",", ":"))
    with _out_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def worker(tasks: queue.Queue, seed: int) -> None:
    rng = random.Random(seed)
    while True:
        item = tasks.get()
        if item is None:
            return
        raw_line, sequence = item
        try:
            query = json.loads(raw_line)
        except ValueError as exc:
            write_response({"error": f"Could not parse json: {exc}"})
            continue
        visits = query.get("maxVisits", DEFAULT_MAX_VISITS) if isinstance(query, dict) else 0
        visits = visits if isinstance(visits, int) and not isinstance(visits, bool) else 0
        time.sleep(rng.uniform(0.001, 0.015) + 2e-5 * max(0, visits))
        if sequence % WARN_EVERY == 0:
            qid = query.get("id") if isinstance(query, dict) else None
            sys.stderr.write(f"WARNING: fake engine synthetic warning while handling query {qid!r}\n")
            sys.stderr.flush()
        for resp in handle_query(query, rng):
            try:
                write_response(resp)
            except OSError:  # parent went away
                return


def main() -> int:
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    for line in (
        f"KataGo v{VERSION} (tests/fake_katago.py, a stand-in for the analysis engine)",
        "Loaded config (ignored)",
        "Loaded model (ignored)",
        "Loaded human SL model (ignored)",
        "Started, ready to begin handling requests",
    ):
        sys.stderr.write(line + "\n")
    sys.stderr.flush()

    tasks: queue.Queue = queue.Queue()
    threads = [threading.Thread(target=worker, args=(tasks, i), daemon=True) for i in range(WORKERS)]
    for t in threads:
        t.start()
    while True:
        raw = sys.stdin.readline()
        if raw == "":
            break
        raw = raw.strip()
        if not raw:
            continue
        _query_counter[0] += 1
        tasks.put((raw, _query_counter[0]))
    for _ in threads:
        tasks.put(None)
    for t in threads:
        t.join()
    sys.stderr.write("stdin closed, all queries answered, exiting\n")
    sys.stderr.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
