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


def _hours(r: Mapping[str, Any]) -> float:
    return float(r["wall_clock_s"]) / 3600.0


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


def latest_clean(results_dir: Path, claim: str, pilot: bool) -> tuple[Path, dict[str, Any]]:
    """The most recent clean result file of a claim (file names sort chronologically)."""
    pattern = f"pilot_{claim}_*.json" if pilot else f"{claim}_*.json"
    for path in sorted(Path(results_dir).glob(pattern), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, Mapping) and data.get("claim") == claim and bool(data.get("pilot")) == pilot:
            if is_clean(data):
                return path, dict(data)
    kind = "pilot " if pilot else ""
    raise TokenError(f"no clean {kind}{claim} result in {results_dir} (git_dirty false and a real git commit)")


class Resolver:
    """Resolves tokens against the clean results in one directory, caching each source's file."""

    def __init__(self, results_dir: Path = RESULTS_DIR) -> None:
        self.results_dir = Path(results_dir)
        self._cache: dict[str, tuple[Path, dict[str, Any]]] = {}
        self.files: dict[str, str] = {}
        """source -> result file name, for the generated-file header."""

    def result(self, source: str) -> dict[str, Any]:
        if source in DERIVED:
            if source not in self._cache:
                self._cache[source] = self._derived(source)
                self.files[source] = self._cache[source][0].name
            return self._cache[source][1]
        if source not in SOURCES:
            raise TokenError(f"unknown token source {source!r}; known: {', '.join([*SOURCES, *DERIVED])}")
        if source not in self._cache:
            claim, pilot = SOURCES[source]
            self._cache[source] = latest_clean(self.results_dir, claim, pilot)
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
        if not isinstance(data, Mapping) or not is_clean(data):
            raise TokenError(f"{source}: {name} is not from a clean git commit (git_dirty false and a real commit)")
        self.result(parent)
        parent_path = self._cache[parent][0]
        origin = data.get("source") if isinstance(data.get("source"), Mapping) else {}
        digest = hashlib.sha256(parent_path.read_bytes()).hexdigest()
        if origin.get("file") != parent_path.name or origin.get("sha256") != digest:
            raise TokenError(
                f"{source}: {name} was computed from {origin.get('file')}, not from the latest clean {parent} result "
                f"{parent_path.name}: rerun its script"
            )
        return path, dict(data)

    def value(self, source: str, path: str, fmt: str | None) -> str:
        token = f"{{{{{source}.{path}{':' + fmt if fmt is not None else ''}}}}}"
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
