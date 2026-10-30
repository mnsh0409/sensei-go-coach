"""KataGo analysis-engine wrapper: one persistent subprocess, thread-safe queries matched on `id`.

Field names verified against a copy of KataGo's docs/Analysis_Engine.md (master branch, read 2026-10-29; its companion
analysis_example.cfg refers to v1.16.4). Quoting the doc:

Query ("Queries" section): `id (string)`; `moves (list of [player string, location string] tuples)` with player
`"B"` or `"W"` and GTP locations; `initialStones`; `initialPlayer (player string)`, "the player to use for analyzing
the first turn (turn 0)", "useful if `moves` is an empty list"; `rules (string or JSON)`; `komi (integer or
half-integer)`; `boardXSize (integer)`; `boardYSize (integer)`; `analyzeTurns (list of integers)` where "0 is the
initial position, 1 is the position after `moves[0]`"; `maxVisits (integer)`; `includeOwnership (boolean)`;
`includePolicy (boolean)`; `overrideSettings (object)` holding `reportAnalysisWinratesAs` (BLACK|WHITE|SIDETOMOVE,
otherwise taken from the config file) and `humanSLProfile (string)`. Special actions `query_version` (answered with
`version` and `git_hash`) and `clear_cache` (echoed back) each take `id` and `action`.

Response ("Responses" section): errors are `{"error":...}` or `{"error","field","id"}`; warnings are
`{"warning","field","id"}` and "the query will still proceed to generate analysis responses". A result carries `id`,
`isDuringSearch`, `turnNumber`, `moveInfos` (each with `move`, `visits`, `winrate`, `scoreLead`, `prior`, `pv`,
`order`), `rootInfo` (`currentPlayer`, `winrate`, `scoreLead`, `visits`), `ownership` ("length boardYSize *
boardXSize", "row-major order, starting at the top-left of the board (e.g. A19)"), `policy` ("length boardYSize *
boardXSize + 1", same order, "the last value in the array is the policy value for passing", "-1 indicating illegal
moves") and `humanPolicy` ("the format is the same as `policy`", present "if `includePolicy` was true, and a human
model is available" with a `humanSLProfile`). "All values will be from the perspective of
`reportAnalysisWinratesAs`", so every query here overrides it to SIDETOMOVE and the wrapper checks
`rootInfo.currentPlayer` against `pos.to_move`. Terminated queries carry `noResults: true` and no data fields.

Human policy settings, from the doc's "Human SL Analysis Guide" (read 2026-10-29): set `ignorePreRootHistory` to
false, because "normally analysis ignores history to be unbiased by move order, but humans definitely behave
differently based on recent moves"; and `rootNumSymmetriesToSample` (1 to 8, default 1) "to 2, or to 8 ... to
improve the quality of the human policy by averaging more symmetries ... when relying so heavily on the raw human
policy without any search". `human_policy` sends both (8: all symmetries, so the same position always gets the same
policy instead of one randomly chosen symmetry). `analyze` keeps the analysis defaults (history ignored, one
symmetry, the config's `wideRootNoise`), so engine values do not depend on move order.

Ko rule of the `chinese` preset: simple ko, area scoring, suicide illegal, as recorded in CLAUDE.md from
cpp/game/rules.cpp (`parseRulesHelper`, checked 2026-10-29). That source is not in this copy, so it is not re-checked
here; `GoGame` (T3) implements the same rules.

Process handling follows CLAUDE.md: argument list, all three pipes, UTF-8 text mode, line buffered; a daemon thread
routes stdout lines by `id`, another drains stderr into logs/katago_stderr.log; close() closes stdin, waits up to
10 s, then kills.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import subprocess
import threading
import time
from collections import deque
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from sensei.coords import PASS, rc_to_gtp
from sensei.interfaces import Candidate, EngineAnalysis, Move, Position

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STDERR_LOG = ROOT / "logs" / "katago_stderr.log"
ENV_VARS = ("KATAGO_EXE", "KATAGO_MODEL", "KATAGO_HUMAN_MODEL", "KATAGO_CONFIG")
PERSPECTIVE = "SIDETOMOVE"
ANALYZE_OVERRIDES: dict[str, Any] = {"reportAnalysisWinratesAs": PERSPECTIVE}
HUMAN_POLICY_OVERRIDES: dict[str, Any] = {
    "reportAnalysisWinratesAs": PERSPECTIVE,
    "ignorePreRootHistory": False,
    "rootNumSymmetriesToSample": 8,
}
"""Per-query overrideSettings (plus `humanSLProfile` for human policies); recorded in every result file."""
DEFAULT_RULES = "chinese"
DEFAULT_KOMI = 7.5
DEFAULT_SIZE = 19

_CLOSE_TIMEOUT_S = 10.0
_WAIT_SLICE_S = 0.5  # Event.wait slices keep Ctrl-C responsive on Windows and bound the optional query timeout
_STDERR_TAIL_LINES = 40
_RESULT_KEYS = ("rootInfo", "moveInfos", "noResults", "action")


class _Pending:
    """One in-flight query: the caller waits on `event`; the reader thread fills `result` or `error`."""

    __slots__ = ("error", "event", "result")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: dict[str, Any] | None = None
        self.error: BaseException | None = None


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:  # python-dotenv is a declared dependency; this only guards odd environments
        return
    env_file = ROOT / ".env"
    if env_file.is_file():
        load_dotenv(env_file, override=False)


def _board_size(pos: Position) -> int:
    size = int(pos.meta.get("size", DEFAULT_SIZE))
    if not 2 <= size <= 19:
        raise ValueError(f"unsupported board size {size}; KataGo's official nets and sensei.coords cover 2 to 19")
    return size


def _renormalize(values: Sequence[float], size: int, name: str) -> dict[Move, float]:
    """Flat KataGo policy array (row-major from the top left, pass last, -1 illegal) -> {move: p} over legal moves."""
    expected = size * size + 1
    if len(values) != expected:
        raise RuntimeError(f"{name} has {len(values)} entries, expected {expected} for a {size}x{size} board")
    legal: dict[Move, float] = {}
    total = 0.0
    for index, value in enumerate(values):
        p = float(value)
        if p < 0.0:  # -1 marks an illegal move
            continue
        move = PASS if index == size * size else rc_to_gtp(divmod(index, size), size)
        legal[move] = p
        total += p
    if total <= 0.0:
        raise RuntimeError(f"{name} has no probability mass on legal moves")
    return {move: p / total for move, p in legal.items()}


def _ownership(resp: Mapping[str, Any], size: int) -> tuple[float, ...]:
    values = resp.get("ownership")
    if values is None:
        raise RuntimeError("KataGo response has no ownership although includeOwnership was requested")
    if len(values) != size * size:
        raise RuntimeError(f"ownership has {len(values)} entries, expected {size * size} for a {size}x{size} board")
    return tuple(float(v) for v in values)


def _candidates(resp: Mapping[str, Any]) -> tuple[Candidate, ...]:
    """moveInfos -> Candidates sorted by visits descending (KataGo's `order` only breaks ties)."""
    keyed: list[tuple[int, int, Candidate]] = []
    for index, info in enumerate(resp.get("moveInfos") or ()):
        score = info.get("scoreLead")
        cand = Candidate(
            move=str(info["move"]),
            prior=float(info["prior"]),
            value=float(info["winrate"]),
            visits=int(info["visits"]),
            score_lead=None if score is None else float(score),
            pv=tuple(str(m) for m in info.get("pv") or ()),
        )
        keyed.append((-cand.visits, int(info.get("order", index)), cand))
    keyed.sort(key=lambda item: (item[0], item[1]))
    return tuple(cand for _, _, cand in keyed)


class KataGoEngine:
    """`Engine` and `HumanPolicySource` over one `katago analysis` subprocess (INTERFACES.md, Engine section).

    Command: [exe, *exe_args, "analysis", "-config", config, "-model", model, "-human-model", human_model, *extra_args].
    `exe_args` exists so the tests can run `python tests/fake_katago.py analysis ...` in place of a KataGo binary.
    `query_timeout` (seconds) is off by default: a healthy engine answers every query, and a dead one fails all
    pending queries as soon as its stdout closes.
    """

    QUERY_OVERRIDES: Mapping[str, Mapping[str, Any]] = {
        "analyze": ANALYZE_OVERRIDES,
        "human_policy": HUMAN_POLICY_OVERRIDES,
    }
    """The overrideSettings every query sends, for result files (eval/hardware.py engine_metadata)."""

    def __init__(
        self,
        exe: str | os.PathLike[str],
        model: str | os.PathLike[str],
        human_model: str | os.PathLike[str] | None,
        config: str | os.PathLike[str],
        extra_args: Sequence[str] = (),
        *,
        exe_args: Sequence[str] = (),
        stderr_log: str | os.PathLike[str] | None = None,
        query_timeout: float | None = None,
    ) -> None:
        self.exe = str(exe)
        self.model = str(model)
        self.human_model = None if human_model is None else str(human_model)
        self.config = str(config)
        command = [self.exe, *(str(a) for a in exe_args), "analysis", "-config", self.config, "-model", self.model]
        if self.human_model is not None:
            command += ["-human-model", self.human_model]
        command += [str(a) for a in extra_args]
        self.command: tuple[str, ...] = tuple(command)
        self.stderr_log = DEFAULT_STDERR_LOG if stderr_log is None else Path(stderr_log)
        self.query_timeout = query_timeout

        self._lock = threading.Lock()  # protects _pending, _ids and _closed
        self._write_lock = threading.Lock()  # one query line at a time on stdin
        self._pending: dict[str, _Pending] = {}
        self._ids = itertools.count(1)
        self._closed = False
        self._stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)

        self._proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._stdout_thread = threading.Thread(target=self._read_stdout, name="katago-stdout", daemon=True)
        self._stderr_thread = threading.Thread(target=self._drain_stderr, name="katago-stderr", daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()

    # ----------------------------------------------------------------------------------------------- construction

    @classmethod
    def from_env(cls, **kwargs: Any) -> KataGoEngine:
        """Build from KATAGO_EXE, KATAGO_MODEL, KATAGO_HUMAN_MODEL and KATAGO_CONFIG (environment or the repo .env).

        Every path is checked with Path.is_file(); a missing one raises FileNotFoundError naming the resolved path.
        Keyword arguments go to the constructor (extra_args, stderr_log, query_timeout).
        """
        _load_dotenv()
        paths: dict[str, Path] = {}
        for name in ENV_VARS:
            raw = os.environ.get(name, "").strip()
            if not raw:
                raise FileNotFoundError(f"{name} is not set; fill it in .env (see .env.example)")
            path = Path(raw).expanduser().resolve()
            if not path.is_file():
                raise FileNotFoundError(f"{name}={raw!r} resolves to {path}, which is not a file")
            paths[name] = path
        return cls(
            exe=paths["KATAGO_EXE"],
            model=paths["KATAGO_MODEL"],
            human_model=paths["KATAGO_HUMAN_MODEL"],
            config=paths["KATAGO_CONFIG"],
            **kwargs,
        )

    def __enter__(self) -> KataGoEngine:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ---------------------------------------------------------------------------------------------------- queries

    def analyze(self, pos: Position, max_visits: int) -> EngineAnalysis:
        """Engine analysis for the side to move at the root (winrates, score leads and ownership)."""
        return self.analyze_raw(pos, max_visits)[0]

    def analyze_raw(self, pos: Position, max_visits: int) -> tuple[EngineAnalysis, dict[str, Any]]:
        """`analyze` plus the raw KataGo response, for callers that need rootInfo fields such as scoreLead."""
        size = _board_size(pos)
        query = self._base_query(pos, size)
        query.update(
            {
                "maxVisits": int(max_visits),
                "includeOwnership": True,
                "includePolicy": True,
                "overrideSettings": dict(ANALYZE_OVERRIDES),
            }
        )
        resp = self._send(query)
        root = self._check_root(resp, pos)
        policy = resp.get("policy")
        if policy is None:
            raise RuntimeError("KataGo response has no policy although includePolicy was requested")
        analysis = EngineAnalysis(
            position=pos,
            candidates=_candidates(resp),
            policy=_renormalize(policy, size, "policy"),
            root_value=float(root["winrate"]),
            ownership=_ownership(resp, size),
        )
        return analysis, resp

    def human_policy(self, pos: Position, profile: str) -> Mapping[Move, float]:
        """HumanSL policy at `profile` (for example "rank_10k") over legal moves, from a 1-visit query."""
        size = _board_size(pos)
        query = self._base_query(pos, size)
        query.update(
            {
                "maxVisits": 1,
                "includePolicy": True,
                "overrideSettings": {"humanSLProfile": str(profile), **HUMAN_POLICY_OVERRIDES},
            }
        )
        resp = self._send(query)
        self._check_root(resp, pos)
        values = resp.get("humanPolicy")
        if values is None:
            raise RuntimeError(
                "KataGo response has no humanPolicy: the engine needs -human-model and a valid humanSLProfile"
            )
        return _renormalize(values, size, "humanPolicy")

    def version(self) -> str:
        """KataGo's release version string from the `query_version` action (for result files)."""
        resp = self._send({"action": "query_version"})
        return str(resp.get("version", "unknown"))

    def clear_cache(self) -> None:
        """Empty KataGo's neural-net cache (`clear_cache` action), so repeated positions cost full work again."""
        self._send({"action": "clear_cache"})

    # ----------------------------------------------------------------------------------------------------- state

    @property
    def pid(self) -> int:
        return self._proc.pid

    @property
    def returncode(self) -> int | None:
        return self._proc.poll()

    @property
    def alive(self) -> bool:
        return self._proc.poll() is None

    def close(self) -> None:
        """Close stdin (KataGo finishes queued queries and exits), wait up to 10 s, then kill. Idempotent."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        with self._write_lock:
            try:
                self._proc.stdin.close()
            except OSError:
                pass
        try:
            self._proc.wait(timeout=_CLOSE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            logger.warning("KataGo (pid %d) did not exit %.0f s after stdin closed; killing", self.pid,
                           _CLOSE_TIMEOUT_S)
            self._proc.kill()
            self._proc.wait()
        self._stdout_thread.join(timeout=5.0)
        self._stderr_thread.join(timeout=5.0)
        for stream in (self._proc.stdout, self._proc.stderr):  # the readers saw EOF; release the pipe handles
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass

    # -------------------------------------------------------------------------------------------------- internals

    @staticmethod
    def _base_query(pos: Position, size: int) -> dict[str, Any]:
        moves = [[str(color), str(move)] for color, move in pos.moves]
        rules = pos.meta.get("rules", DEFAULT_RULES)
        query: dict[str, Any] = {
            "moves": moves,
            "initialStones": [],
            "rules": rules if isinstance(rules, Mapping) else str(rules),
            "komi": float(pos.meta.get("komi", DEFAULT_KOMI)),
            "boardXSize": size,
            "boardYSize": size,
            "analyzeTurns": [len(moves)],
        }
        if not moves:
            query["initialPlayer"] = pos.to_move  # turn 0 is Black unless told otherwise
        return query

    @staticmethod
    def _check_root(resp: Mapping[str, Any], pos: Position) -> Mapping[str, Any]:
        if resp.get("noResults"):
            raise RuntimeError(f"KataGo returned no results for query {resp.get('id')!r} (terminated before searching)")
        root = resp.get("rootInfo")
        if not isinstance(root, Mapping):
            raise RuntimeError(f"KataGo response {resp.get('id')!r} has no rootInfo")
        current = root.get("currentPlayer")
        if current != pos.to_move:
            raise RuntimeError(
                f"KataGo analyzed {current!r} to move but the position says {pos.to_move!r} "
                f"(query {resp.get('id')!r}); the side-to-move perspective would be wrong"
            )
        turn = resp.get("turnNumber")
        if turn is not None and int(turn) != len(pos.moves):
            raise RuntimeError(f"KataGo analyzed turn {turn} but the position has {len(pos.moves)} moves")
        return root

    def _register(self) -> tuple[str, _Pending]:
        pending = _Pending()
        with self._lock:
            if self._closed:
                raise RuntimeError("KataGoEngine is closed")
            qid = f"q{next(self._ids)}"
            self._pending[qid] = pending
        return qid, pending

    def _discard(self, qid: str) -> None:
        with self._lock:
            self._pending.pop(qid, None)

    def _stderr_text(self) -> str:
        return "\n".join(self._stderr_tail)

    def _send(self, query: Mapping[str, Any]) -> dict[str, Any]:
        """Write one query line and block until its response (matched on id) arrives. Thread-safe."""
        qid, pending = self._register()
        line = json.dumps({"id": qid, **query}, separators=(",", ":"))
        try:
            with self._write_lock:
                self._proc.stdin.write(line + "\n")
                self._proc.stdin.flush()
        except (OSError, ValueError) as exc:  # broken pipe, or stdin closed by close()
            self._discard(qid)
            raise RuntimeError(
                f"cannot write to KataGo (exit code {self._proc.poll()}): {exc}; stderr tail:\n{self._stderr_text()}"
            ) from exc
        deadline = None if self.query_timeout is None else time.monotonic() + self.query_timeout
        while not pending.event.wait(_WAIT_SLICE_S):
            if deadline is not None and time.monotonic() > deadline:
                self._discard(qid)
                raise TimeoutError(f"KataGo query {qid} got no response within {self.query_timeout} s")
        if pending.error is not None:
            raise pending.error
        assert pending.result is not None
        return pending.result

    def _resolve(self, qid: str, result: dict[str, Any] | None, error: BaseException | None) -> bool:
        with self._lock:
            pending = self._pending.pop(qid, None)
        if pending is None:
            return False
        pending.result = result
        pending.error = error
        pending.event.set()
        return True

    def _fail_all(self, message: str) -> None:
        with self._lock:
            items = list(self._pending.items())
            self._pending.clear()
        for qid, pending in items:
            pending.error = RuntimeError(f"query {qid}: {message}")
            pending.event.set()

    def _handle_line(self, line: str) -> None:
        """Route one stdout line. Errors fail their query, warnings are logged, unrouteable errors fail every query."""
        try:
            msg = json.loads(line)
        except ValueError:
            logger.warning("KataGo wrote a non-JSON line to stdout: %.200s", line)
            return
        if not isinstance(msg, dict):
            logger.warning("KataGo wrote a non-object JSON line to stdout: %.200s", line)
            return
        qid = msg.get("id")
        if not isinstance(qid, str):
            if "error" in msg:
                self._fail_all(f"KataGo reported an error that names no query: {msg['error']}")
            else:
                logger.warning("KataGo wrote a line without an id: %.200s", line)
            return
        if msg.get("isDuringSearch") is True:
            return  # partial report (reportDuringSearchEvery); the final response follows
        if "error" in msg:
            text = f"KataGo error for query {qid} (field {msg.get('field')!r}): {msg['error']}"
            self._resolve(qid, None, RuntimeError(text))
            return
        if "warning" in msg:
            # Per the docs, "in the case of a warning, the query will still proceed to generate analysis responses":
            # a bare warning line is logged and the caller keeps waiting for the result line that follows it.
            logger.warning("KataGo warning for query %s (field %r): %s", qid, msg.get("field"), msg["warning"])
            if not any(key in msg for key in _RESULT_KEYS):
                return
        if not self._resolve(qid, msg, None):
            logger.debug("dropped KataGo response for unknown or finished query %s", qid)

    def _read_stdout(self) -> None:
        stdout = self._proc.stdout
        assert stdout is not None
        try:
            while True:
                line = stdout.readline()
                if line == "":
                    break
                line = line.strip()
                if line:
                    self._handle_line(line)
        finally:
            try:
                self._proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                pass
            self._fail_all(
                f"KataGo closed its stdout (exit code {self._proc.returncode}); stderr tail:\n{self._stderr_text()}"
            )

    def _drain_stderr(self) -> None:
        """Copy stderr to the log file line by line (an undrained pipe would eventually block KataGo)."""
        stderr = self._proc.stderr
        assert stderr is not None
        log = None
        try:
            self.stderr_log.parent.mkdir(parents=True, exist_ok=True)
            log = self.stderr_log.open("a", encoding="utf-8")
            log.write(f"# {time.strftime('%Y-%m-%d %H:%M:%S')} pid {self._proc.pid}: {' '.join(self.command)}\n")
            log.flush()
        except OSError as exc:
            logger.warning("cannot write KataGo stderr log %s: %s", self.stderr_log, exc)
        try:
            for line in iter(stderr.readline, ""):
                self._stderr_tail.append(line.rstrip("\r\n"))
                if log is not None:
                    try:
                        log.write(line)
                        log.flush()
                    except OSError:
                        pass
        finally:
            if log is not None:
                log.close()
