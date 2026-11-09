"""Result tokens for README.md, video_script.md and the GTC poster (TASKS.md T10 and T10b).

A token is `{{<source>.<path>[:<format>]}}`:

- <source> names a result: `C1`, `C2`, `C3` (the most recent CLEAN confirmatory file `results/<claim>_*.json`) or
  `pilot_C1` (the most recent clean pilot). Clean means `git_dirty` is false and `git_commit` is a real commit.
  A DERIVED source is a descriptive file computed from a claim's result: `C2base` is
  `results/relevance_baselines.json` (eval/relevance_baselines.py), which resolves only when it is clean and was
  computed from the very file the `C2` source resolves to (same name and SHA-256).
- <path> is an alias from ALIASES (the names in poster/TOKENS.md) or a dotted path into the result JSON
  (`estimate`, `ci.1`, `details.summary.score`, `hardware.gpu`).
- <format> is a Python format spec (`.1f`, `,`, `.0%`); an alias brings its own default.

The `hw` source (poster, TASKS.md T10b) reads the CLEAN engine benchmarks `results/latency_*.json` written by
scripts/bench_engine.py, keyed by GPU (`3090`, `4090`, `p6000`, from `hardware.gpu`) and backend (`cuda`, `trt`):
`hw.<gpu>.<backend>.{vps,pm,p50,p95,hp,cu}` (visits per second at 8 concurrent queries, the mean over repeats; the
half-width of its 95% CI as "± 18"; analyze p50 and p95 and human-policy mean in ms at 1 query; "driver / CUDA"),
and the summaries `hw.hp_ms` (human-policy mean ms on the C1 GPU and backend), `hw.conc_gain` (visits per second at
8 over 1 concurrent query there), `hw.trt_gain` (TensorRT over CUDA visits per second at 8 concurrent queries, as
"1.3×" or the range "1.2× to 1.5×" over GPUs), `hw.runs` (the repeats per file, which must agree across the
files used) and `hw.builds` (the distinct KataGo build labels of the files used, comma-separated).

Only clean results resolve, so a number in the README can always be traced to a commit. A missing result or a
null value raises TokenError naming the token.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = ROOT / "results"

TOKEN_RE = re.compile(r"\{\{\s*([A-Za-z_]\w*)\.([\w.]+?)(?::([^{}]*?))?\s*\}\}")
BLOCK_RE = re.compile(r"<!--\s*results\s*-->(.*?)<!--\s*/results\s*-->", re.DOTALL)
SOURCES = {"C1": ("C1", False), "C2": ("C2", False), "C3": ("C3", False), "pilot_C1": ("C1", True)}
DERIVED: dict[str, tuple[str, str]] = {"C2base": ("relevance_baselines.json", "C2")}
"""Descriptive result files: token source -> (file name in results/, the source it was computed from)."""


class TokenError(ValueError):
    """A token that cannot be rendered from clean results."""


HW_SOURCE = "hw"
HW_METRICS: dict[str, tuple[str, str]] = {
    "vps": ("levels.8.visits_per_s", ",.0f"),
    "p50": ("levels.1.analyze.p50_ms", ".0f"),
    "p95": ("levels.1.analyze.p95_ms", ".0f"),
    "hp": ("levels.1.human_policy.mean_ms", ".0f"),
}
"""Benchmark metric -> (path in a latency file, default format)."""


def eol_digests(raw: bytes) -> set[str]:
    """SHA-256 of a text file as stored and with LF or CRLF line endings. Git converts line endings between the
    Windows dev machine (CRLF) and a Linux checkout or CI (LF), so a recorded digest must match either form."""
    lf = raw.replace(b"\r\n", b"\n")
    return {hashlib.sha256(b).hexdigest() for b in (raw, lf, lf.replace(b"\n", b"\r\n"))}


def gpu_key(name: str | None) -> str:
    """Short GPU key used in poster tokens: "NVIDIA GeForce RTX 3090" -> "3090", "RTX PRO 6000 ..." -> "p6000"."""
    if not name:
        return ""
    text = str(name).upper()
    if "PRO 6000" in text:
        return "p6000"
    m = re.search(r"RTX\s+(\d{4})", text)
    if m:
        return m.group(1)
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _hours(r: Mapping[str, Any]) -> float:
    return float(r["wall_clock_s"]) / 3600.0


def _games_per_hour(r: Mapping[str, Any]) -> float:
    return float(r["n"]) / _hours(r)


def _pct(path: str) -> Callable[[Mapping[str, Any]], float]:
    return lambda r: 100.0 * float(lookup(r, path))


def _verdict(r: Mapping[str, Any]) -> str:
    """ "pass" or "fail" from the code-computed success field (never typed by hand)."""
    if r.get("success") is None:
        raise KeyError("success")
    return "pass" if r["success"] else "fail"


def _rise_verdict(path: str) -> Callable[[Mapping[str, Any]], str]:
    """How a ratio of ratios compares with 1, from its CI (never typed by hand): "faster than", "slower than" or
    "at a rate not distinguishable from"."""

    def get(r: Mapping[str, Any]) -> str:
        ci = lookup(r, path)["ci"]
        if not ci:
            raise KeyError(path)
        lo, hi = float(ci[0]), float(ci[1])
        if lo > 1.0:
            return "faster than"
        if hi < 1.0:
            return "slower than"
        return "at a rate not distinguishable from"

    return get


Alias = tuple[str | Callable[[Mapping[str, Any]], Any], str]

ALIASES: dict[str, dict[str, Alias]] = {
    "C1": {
        "n_deviations": (lambda r: len(r.get("deviations") or []), "d"),
        "elo": ("estimate", ".0f"),
        "lo": ("ci.0", ".0f"),
        "hi": ("ci.1", ".0f"),
        "eps": ("epsilon", "g"),
        "n": ("n", "d"),
        "queries": ("engine_queries", ","),
        "hours": (_hours, ".1f"),
        "gph": (_games_per_hour, ".0f"),
        "gpu": ("hardware.gpu", ""),
        "conc": ("concurrency", "d"),
        "score": ("details.summary.score", ".3f"),
        "score_pct": (_pct("details.summary.score"), ".1f"),
        "loss_pct": (_pct("details.summary.mean_winrate_loss_per_move"), ".2f"),
        "left_top_pct": (_pct("details.summary.left_engine_top_rate"), ".0f"),
        "ci_pct": (lambda r: 100.0 * float(r["config"]["C1"]["ci"]), ".0f"),
        "threshold": ("config.C1.success_max_cost_elo_upper", "g"),
        "profile": ("details.learner_profile", ""),
        "verdict": (_verdict, ""),
        "commit": ("git_commit", ""),
    },
    "pilot_C1": {
        "n_deviations": (lambda r: len(r.get("deviations") or []), "d"),
        "eps": ("details.epsilon_star", "g"),
        "n": ("n", "d"),
        "commit": ("git_commit", ""),
    },
    "C2": {
        "n_deviations": (lambda r: len(r.get("deviations") or []), "d"),
        "U": ("estimate", ".3f"),
        "lo": ("ci.0", ".3f"),
        "hi": ("ci.1", ".3f"),
        "U_pct": (_pct("estimate"), ".1f"),
        "lo_pct": (_pct("ci.0"), ".1f"),
        "hi_pct": (_pct("ci.1"), ".1f"),
        "n": ("n", ","),
        "eps": ("epsilon", "g"),
        "ci_pct": (lambda r: 100.0 * float(r["config"]["C2"]["ci"]), ".0f"),
        "verdict": (_verdict, ""),
        "commit": ("git_commit", ""),
    },
    "C2base": {
        "lesson_x": ("adjacent.lesson_ratio_gmean.estimate", ".2f"),
        "best_x": ("adjacent.best_ratio_gmean.estimate", ".2f"),
        "rand_x": ("adjacent.rand_ratio_gmean.estimate", ".2f"),
        "lesson_over_rand": ("adjacent.lesson_over_rand_gmean.estimate", ".2f"),
        "lesson_over_rand_lo": ("adjacent.lesson_over_rand_gmean.ci.0", ".2f"),
        "lesson_over_rand_hi": ("adjacent.lesson_over_rand_gmean.ci.1", ".2f"),
        "lesson_vs_rand": (_rise_verdict("adjacent.lesson_over_rand_gmean"), ""),
        "lesson_gain_pct": (_pct("adjacent.lesson_diff_mean.estimate"), ".1f"),
        "best_gain_pct": (_pct("adjacent.best_diff_mean.estimate"), ".1f"),
        "rand_gain_pct": (_pct("adjacent.rand_diff_mean.estimate"), ".1f"),
        "not_best_gain_pct": (_pct("adjacent.lesson_not_best_diff_mean.estimate"), ".1f"),
        "not_best_share_pct": (_pct("not_best_share"), ".0f"),
        "weakest_band": ("bands.0", ""),
        "strongest_band": ("bands.-1", ""),
        "weakest_lesson_own_pct": (_pct("rates.lesson.0.0.estimate"), ".1f"),
        "weakest_lesson_strongest_pct": (_pct("rates.lesson.0.-1.estimate"), ".1f"),
        "best_weakest_pct": (_pct("rates.best.0.estimate"), ".1f"),
        "best_strongest_pct": (_pct("rates.best.-1.estimate"), ".1f"),
        "rand_weakest_pct": (_pct("rates.rand.0.estimate"), ".1f"),
        "rand_strongest_pct": (_pct("rates.rand.-1.estimate"), ".1f"),
        "ci_pct": (lambda r: 100.0 * float(r["bootstrap"]["ci"]), ".0f"),
        "commit": ("git_commit", ""),
    },
    "C3": {
        "n_deviations": (lambda r: len(r.get("deviations") or []), "d"),
        "gap": ("details.gap_closure.estimate", ".2f"),
        "gap_lo": ("details.gap_closure.ci.0", ".2f"),
        "gap_hi": ("details.gap_closure.ci.1", ".2f"),
        "gap_pct": (_pct("details.gap_closure.estimate"), ".0f"),
        "auc_diff": ("estimate", ".3f"),
        "lo": ("ci.0", ".3f"),
        "hi": ("ci.1", ".3f"),
        "learners": ("n", "d"),
        "sessions": ("config.C3.sessions", "d"),
        "ident_pct": (_pct("details.identification_share.thompson"), ".0f"),
        "ident_session": ("details.identification_session", "d"),
        "ci_pct": (lambda r: 100.0 * float(r["config"]["C3"]["ci"]), ".0f"),
        "verdict": (_verdict, ""),
        "commit": ("git_commit", ""),
    },
}


def lookup(data: Any, path: str) -> Any:
    """Follow a dotted path through mappings and lists (integer parts index lists)."""
    value = data
    for part in path.split("."):
        if isinstance(value, Mapping) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.lstrip("-").isdigit() and -len(value) <= int(part) < len(value):
            value = value[int(part)]
        else:
            raise KeyError(path)
    return value


def is_clean(result: Mapping[str, Any]) -> bool:
    return result.get("git_dirty") is False and result.get("git_commit") not in (None, "", "no-git")


def latest_clean(
    results_dir: Path, claim: str, pilot: bool, allow_dirty: bool = False
) -> tuple[Path, dict[str, Any]]:
    """The most recent clean result file of a claim (file names sort chronologically); allow_dirty is for
    previews only and accepts uncommitted results too."""
    pattern = f"pilot_{claim}_*.json" if pilot else f"{claim}_*.json"
    for path in sorted(Path(results_dir).glob(pattern), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, Mapping) and data.get("claim") == claim and bool(data.get("pilot")) == pilot:
            if allow_dirty or is_clean(data):
                return path, dict(data)
    kind = "pilot " if pilot else ""
    raise TokenError(f"no clean {kind}{claim} result in {results_dir} (git_dirty false and a real git commit)")


class Resolver:
    """Resolves tokens against the clean results in one directory, caching each source's file."""

    def __init__(self, results_dir: Path = RESULTS_DIR, allow_dirty: bool = False) -> None:
        self.results_dir = Path(results_dir)
        self.allow_dirty = allow_dirty
        """Previews only (poster drafts): accept results from uncommitted trees. Never for a checked output."""
        self._cache: dict[str, tuple[Path, dict[str, Any]]] = {}
        self.files: dict[str, str] = {}
        """source -> result file name, for the generated-file header."""
        self._bench: dict[tuple[str, str], tuple[Path, dict[str, Any]]] | None = None

    def result(self, source: str) -> dict[str, Any]:
        if source in DERIVED:
            if source not in self._cache:
                self._cache[source] = self._derived(source)
                self.files[source] = self._cache[source][0].name
            return self._cache[source][1]
        if source not in SOURCES:
            raise TokenError(f"unknown token source {source!r}; known: {', '.join([*SOURCES, *DERIVED, HW_SOURCE])}")
        if source not in self._cache:
            claim, pilot = SOURCES[source]
            self._cache[source] = latest_clean(self.results_dir, claim, pilot, self.allow_dirty)
            self.files[source] = self._cache[source][0].name
        return self._cache[source][1]

    def _derived(self, source: str) -> tuple[Path, dict[str, Any]]:
        """A descriptive file: clean, and computed from the very file its parent source resolves to."""
        name, parent = DERIVED[source]
        path = self.results_dir / name
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as err:
            raise TokenError(f"{source}: cannot read {path} ({type(err).__name__}); run its script first") from err
        if not isinstance(data, Mapping) or not (self.allow_dirty or is_clean(data)):
            raise TokenError(f"{source}: {name} is not from a clean git commit (git_dirty false and a real commit)")
        self.result(parent)
        parent_path = self._cache[parent][0]
        origin = data.get("source") if isinstance(data.get("source"), Mapping) else {}
        if origin.get("file") != parent_path.name or origin.get("sha256") not in eol_digests(parent_path.read_bytes()):
            raise TokenError(
                f"{source}: {name} was computed from {origin.get('file')}, not from the latest clean {parent} result "
                f"{parent_path.name}: rerun its script"
            )
        return path, dict(data)

    def value(self, source: str, path: str, fmt: str | None) -> str:
        token = f"{{{{{source}.{path}{':' + fmt if fmt is not None else ''}}}}}"
        if source == HW_SOURCE:
            return self._hw_value(token, path, fmt)
        data = self.result(source)
        alias = ALIASES.get(source, {}).get(path)
        try:
            if alias is not None:
                getter, default_fmt = alias
                raw = getter(data) if callable(getter) else lookup(data, getter)
                fmt = default_fmt if fmt is None else fmt
            else:
                raw = lookup(data, path)
        except (KeyError, TypeError, ValueError) as err:
            raise TokenError(f"{token}: {path} is not in {self.files.get(source)}") from err
        if raw is None:
            raise TokenError(f"{token}: {path} is null in {self.files.get(source)}")
        if isinstance(raw, Mapping | list):
            raise TokenError(f"{token}: {path} is not a single value")
        if fmt:
            try:
                text = format(raw, fmt)
            except (TypeError, ValueError) as err:
                raise TokenError(f"{token}: cannot format {raw!r} with {fmt!r}") from err
            return text[1:] if re.fullmatch(r"-0(?:\.0+)?%?", text) else text  # "-0" reads as a sign error
        if isinstance(raw, bool):
            return "yes" if raw else "no"
        if isinstance(raw, float):
            return format(raw, ".4g")
        return str(raw)

    # ---- engine benchmarks (the `hw` source) ------------------------------------------------------------------

    def benchmarks(self) -> dict[tuple[str, str], tuple[Path, dict[str, Any]]]:
        """(gpu key, backend) -> the most recently finished clean latency file."""
        if self._bench is None:
            found: dict[tuple[str, str], tuple[Path, dict[str, Any]]] = {}
            for path in sorted(self.results_dir.glob("latency_*.json")):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if not isinstance(data, Mapping) or data.get("kind") != "engine_benchmark":
                    continue
                if not (self.allow_dirty or is_clean(data)):
                    continue
                hw = data.get("hardware") or {}
                key = (gpu_key(hw.get("gpu")), str(hw.get("katago_backend") or "").lower())
                if not key[0] or not key[1] or key[1] == "fake":
                    continue
                if key not in found or str(data.get("finished", "")) >= str(found[key][1].get("finished", "")):
                    found[key] = (path, dict(data))
            self._bench = found
        return self._bench

    def _bench_file(self, token: str, gpu: str, backend: str) -> dict[str, Any]:
        entry = self.benchmarks().get((gpu, backend))
        if entry is None:
            raise TokenError(f"{token}: no clean results/latency_*.json for GPU {gpu!r} and backend {backend!r}")
        self.files[f"hw.{gpu}.{backend}"] = entry[0].name
        return entry[1]

    def _hw_value(self, token: str, path: str, fmt: str | None) -> str:
        parts = path.split(".")
        try:
            if len(parts) == 3:
                gpu, backend, metric = parts
                data = self._bench_file(token, gpu, backend)
                if metric == "cu":
                    hw = data["hardware"]
                    if not hw.get("driver"):
                        raise KeyError("hardware.driver")
                    return f"{hw['driver']} / {hw.get('cuda') or 'n/a'}"
                if metric == "pm":
                    ci = lookup(data, "levels.8.visits_per_s_ci95")
                    if not ci:
                        raise TokenError(f"{token}: no CI in the benchmark; rerun bench_engine.py with --repeats")
                    return "± " + format((float(ci[1]) - float(ci[0])) / 2, fmt or ",.0f")
                if metric not in HW_METRICS:
                    known = ", ".join(["cu", *HW_METRICS])
                    raise TokenError(f"{token}: unknown benchmark metric {metric!r}; known: {known}")
                getter, default_fmt = HW_METRICS[metric]
                raw = lookup(data, getter)
            elif path == "hp_ms":
                data = self._bench_file(token, *self._c1_gpu())
                raw, default_fmt = lookup(data, "levels.1.human_policy.mean_ms"), ".0f"
            elif path == "conc_gain":
                data = self._bench_file(token, *self._c1_gpu())
                raw = float(lookup(data, "levels.8.visits_per_s")) / float(lookup(data, "levels.1.visits_per_s"))
                default_fmt = ".1f"
            elif path == "trt_gain":
                return self._trt_gain(token, fmt or ".1f")
            elif path == "runs":
                return self._runs(token)
            elif path == "builds":
                return self._builds(token)
            else:
                raise TokenError(f"{token}: unknown hw token {path!r}")
        except KeyError as err:
            raise TokenError(f"{token}: {err.args[0]} is missing from the benchmark file") from err
        fmt = default_fmt if fmt is None else fmt
        try:
            return format(raw, fmt)
        except (TypeError, ValueError) as err:
            raise TokenError(f"{token}: cannot format {raw!r} with {fmt!r}") from err

    def _c1_gpu(self) -> tuple[str, str]:
        hw = self.result("C1").get("hardware") or {}
        return gpu_key(hw.get("gpu")), str(hw.get("katago_backend") or "").lower()

    def _runs(self, token: str) -> str:
        counts = {int(data.get("repeats") or 1) for _, data in self.benchmarks().values()}
        if len(counts) != 1:
            raise TokenError(f"{token}: the benchmark files differ in repeats ({sorted(counts)}); rerun them alike")
        return str(counts.pop())

    def _builds(self, token: str) -> str:
        labels = []
        for (_gpu, _backend), (path, data) in sorted(self.benchmarks().items(), key=lambda kv: (kv[0][1], kv[0][0])):
            label = (data.get("katago_build") or {}).get("label")
            if not label:
                raise TokenError(f"{token}: {path.name} has no KataGo build label (rerun with --build-label)")
            if label not in labels:
                labels.append(label)
        if not labels:
            raise TokenError(f"{token}: no clean benchmark files")
        return ", ".join(labels)

    def _trt_gain(self, token: str, fmt: str) -> str:
        bench = self.benchmarks()
        gains = []
        for gpu in sorted({g for g, _ in bench}):
            if (gpu, "cuda") in bench and (gpu, "trt") in bench:
                cuda = float(lookup(bench[(gpu, "cuda")][1], "levels.8.visits_per_s"))
                trt = float(lookup(bench[(gpu, "trt")][1], "levels.8.visits_per_s"))
                gains.append(trt / cuda)
        if not gains:
            raise TokenError(f"{token}: no GPU has clean CUDA and TensorRT benchmarks")
        lo, hi = format(min(gains), fmt), format(max(gains), fmt)
        return f"{lo}×" if lo == hi else f"{lo}× to {hi}×"

    def render(self, text: str) -> str:
        """Replace every token in `text`."""
        return TOKEN_RE.sub(lambda m: self.value(m.group(1), m.group(2), m.group(3)), text)


def render_blocks(text: str, resolver: Resolver) -> str:
    """Render the tokens inside `<!-- results -->` blocks; a token outside a block is an error."""
    outside = BLOCK_RE.sub("", text)
    stray = TOKEN_RE.findall(outside)
    if stray:
        names = ", ".join(f"{s}.{p}" for s, p, _ in stray[:5])
        raise TokenError(f"tokens outside <!-- results --> blocks: {names}")

    def fill(m: re.Match[str]) -> str:
        whole, start = m.group(0), m.start(0)
        return whole[: m.start(1) - start] + resolver.render(m.group(1)) + whole[m.end(1) - start :]

    return BLOCK_RE.sub(fill, text)


NUMBER_RE = re.compile(r"(?<![\w.{])[-+]?\d(?:[\d,]*\d)?(?:\.\d+)?")
"""A numeric literal not glued to a preceding word (C1, U_k and x2 are names, not numbers)."""


def literal_numbers(block: str) -> list[str]:
    """Numbers typed by hand in a template block (anything numeric that is not part of a token or a word)."""
    return NUMBER_RE.findall(TOKEN_RE.sub("", block))
