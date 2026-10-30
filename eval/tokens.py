"""Result tokens for README.md, video_script.md and the GTC poster (TASKS.md T10 and T10b).

A token is `{{<source>.<path>[:<format>]}}`:

- <source> names a result: `C1`, `C2`, `C3` (the most recent CLEAN confirmatory file `results/<claim>_*.json`) or
  `pilot_C1` (the most recent clean pilot). Clean means `git_dirty` is false and `git_commit` is a real commit.
- <path> is an alias from ALIASES (the names in poster/TOKENS.md) or a dotted path into the result JSON
  (`estimate`, `ci.1`, `details.summary.score`, `hardware.gpu`).
- <format> is a Python format spec (`.1f`, `,`, `.0%`); an alias brings its own default.

Only clean results resolve, so a number in the README can always be traced to a commit. A missing result or a
null value raises TokenError naming the token.
"""

from __future__ import annotations

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
        if source not in SOURCES:
            raise TokenError(f"unknown token source {source!r}; known: {', '.join(SOURCES)}")
        if source not in self._cache:
            claim, pilot = SOURCES[source]
            self._cache[source] = latest_clean(self.results_dir, claim, pilot)
            self.files[source] = self._cache[source][0].name
        return self._cache[source][1]

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
