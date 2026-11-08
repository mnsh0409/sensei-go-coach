"""Evaluation CLI: `python -m eval.run --claim C3 --config configs/prereg.yaml [--pilot] [--concurrency 8]`.

Every run writes one result file to results/ following the contract in eval/README.md and regenerates
results/SUMMARY.md from all result files there. The harness never calls the Meta Model API.

Claims are dispatched through `CLAIMS`; a claim module exposes
`run_claim(cfg, *, seed, pilot, concurrency, results_dir) -> dict` returning the claim-specific fields (`n`,
`estimate`, `ci`, `success`, `details`, and any of `engine_queries`, `katago_version`, `nets`, `analysis_config`,
`concurrency`, `epsilon`, `deviations` it can fill, plus `katago_backend` for the hardware block). `results_dir` is
where this run writes, so C1's confirmatory stage can read epsilon* from the pilot result there.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from eval import c1_strength, c2_relevance, c3_curriculum
from eval.hardware import hardware_block

ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = ROOT / "results"

ClaimRunner = Callable[..., dict[str, Any]]

CLAIMS: dict[str, ClaimRunner] = {
    "C1": c1_strength.run_claim,
    "C2": c2_relevance.run_claim,
    "C3": c3_curriculum.run_claim,
}

CLAIM_FIELDS = (
    "katago_version",
    "nets",
    "analysis_config",
    "concurrency",
    "epsilon",
    "engine_queries",
    "n",
    "estimate",
    "ci",
    "success",
    "details",
)
"""Fields a claim runner may return; everything else in the file is filled here."""


def _git(root: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def git_info(root: Path, ignore_untracked: tuple[str, ...] = ()) -> tuple[str, bool]:
    """(short commit, dirty). ("no-git", True) when `root` itself is not a git checkout or git is missing.

    A checkout of a parent directory does not count: a copy of the project inside some other repository must not
    borrow that repository's commit. Without a commit the run counts as dirty, so it never feeds the README.
    `ignore_untracked` lists path prefixes whose UNTRACKED files do not make the tree dirty (the engine benchmark
    passes "results/latency_" so its second backend run is not marked dirty by the first run's output file).
    """
    toplevel = _git(root, "rev-parse", "--show-toplevel")
    if not toplevel or Path(toplevel).resolve() != root.resolve():
        return "no-git", True
    head = _git(root, "rev-parse", "--short", "HEAD")
    if not head:
        return "no-git", True
    status = _git(root, "status", "--porcelain", "--untracked-files=all")
    if status is None:
        return head, True
    lines = [
        line
        for line in status.splitlines()
        if line.strip() and not (line.startswith("?? ") and line[3:].strip('"').startswith(ignore_untracked))
    ]
    return head, bool(lines)


def load_config(path: Path) -> dict[str, Any]:
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(cfg, Mapping):
        raise ValueError(f"{path} does not hold a mapping")
    return dict(cfg)


def _jsonable(value: Any) -> Any:
    """Convert numpy scalars and arrays, tuples and paths for json.dumps."""
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, set | frozenset | tuple):
        return list(value)
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def _format_number(value: Any, digits: int = 4) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int | float):
        return f"{value:.{digits}g}" if isinstance(value, float) else str(value)
    return str(value)


def _success_cell(value: Any) -> str:
    if value is None:
        return "n/a"
    return "PASS" if value else "FAIL"


def regenerate_summary(results_dir: Path) -> Path:
    """Write results/SUMMARY.md as a markdown table over every contract-following JSON file in results/."""
    rows: list[dict[str, Any]] = []
    skipped: list[str] = []
    for path in sorted(results_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            skipped.append(path.name)
            continue
        if not isinstance(data, Mapping) or "claim" not in data or "estimate" not in data:
            skipped.append(path.name)
            continue
        rows.append({"file": path.name, **data})
    rows.sort(key=lambda r: (str(r.get("claim")), bool(r.get("pilot")), str(r.get("finished", ""))))

    lines = [
        "# Evaluation summary",
        "",
        "Generated by `python -m eval.run`; do not edit by hand. One row per result file in `results/`.",
        "Claims and success criteria: `SCOPE.md`; parameters: `configs/prereg.yaml`.",
        "A `dirty` run or a run without a git commit never feeds the README (`scripts/render_readme.py`).",
        "C3 is a closed-loop simulation with assumed learning dynamics, not a human study.",
        "",
        "| Claim | Pilot | n | Estimate | CI | Success | Headline | Commit | Dirty | Finished | File |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        claim = str(r.get("claim"))
        details = r.get("details") or {}
        if isinstance(details, Mapping) and details.get("simulation"):
            claim += " (simulation)"
        ci = r.get("ci")
        ci_cell = ""
        if isinstance(ci, Sequence) and len(ci) == 2:
            level = None
            cfg = r.get("config")
            if isinstance(cfg, Mapping) and isinstance(cfg.get(r.get("claim")), Mapping):
                level = cfg[r.get("claim")].get("ci")
            label = f"{round(level * 100)}% " if isinstance(level, int | float) else ""
            ci_cell = f"{label}[{_format_number(ci[0])}, {_format_number(ci[1])}]"
        headline = ""
        if isinstance(details, Mapping) and isinstance(details.get("headline"), Mapping):
            h = details["headline"]
            h_ci = h.get("ci")
            headline = f"{h.get('name')} = {_format_number(h.get('estimate'))}"
            if isinstance(h_ci, Sequence) and len(h_ci) == 2:
                headline += f" [{_format_number(h_ci[0])}, {_format_number(h_ci[1])}]"
        lines.append(
            "| "
            + " | ".join(
                [
                    claim,
                    "yes" if r.get("pilot") else "no",
                    _format_number(r.get("n")),
                    _format_number(r.get("estimate")),
                    ci_cell,
                    _success_cell(r.get("success")),
                    headline,
                    str(r.get("git_commit", "")),
                    "yes" if r.get("git_dirty") else "no",
                    str(r.get("finished", "")),
                    f"`{r['file']}`",
                ]
            )
            + " |"
        )
    if not rows:
        lines.append("| (no results yet) | | | | | | | | | | |")
    if skipped:
        lines += ["", "Files in `results/` that are not claim results: " + ", ".join(f"`{s}`" for s in skipped) + "."]
    out = results_dir / "SUMMARY.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def run(
    claim: str,
    config_path: Path,
    *,
    pilot: bool = False,
    concurrency: int | None = None,
    seed: int | None = None,
    results_dir: Path = RESULTS_DIR,
    config_label: str | None = None,
) -> Path:
    """Run one claim and write its result file; returns the file's path."""
    if claim not in CLAIMS:
        raise SystemExit(f"unknown claim {claim!r}; available: {', '.join(sorted(CLAIMS))}")
    cfg = load_config(config_path)
    deviations: list[dict[str, Any]] = []
    global_seed = int(cfg["seeds"]["global"])
    if seed is None:
        seed = global_seed
    elif seed != global_seed:
        deviations.append(
            {
                "field": "seed",
                "value": int(seed),
                "reason": f"--seed on the command line instead of seeds.global ({global_seed}); not confirmatory",
            }
        )

    commit, dirty = git_info(ROOT)
    started = datetime.now().astimezone()
    clock = time.perf_counter()
    outcome = dict(CLAIMS[claim](cfg, seed=int(seed), pilot=pilot, concurrency=concurrency, results_dir=results_dir))
    wall = time.perf_counter() - clock
    finished = datetime.now().astimezone()
    katago_backend = outcome.pop("katago_backend", None)

    unknown = set(outcome) - set(CLAIM_FIELDS) - {"deviations"}
    if unknown:
        raise ValueError(f"claim {claim} returned unexpected fields {sorted(unknown)!r}")
    deviations.extend(outcome.get("deviations", []))

    result: dict[str, Any] = {
        "claim": claim,
        "pilot": bool(pilot),
        "git_commit": commit,
        "git_dirty": dirty,
        "config_path": config_label if config_label is not None else config_path.as_posix(),
        "config": cfg,
        "deviations": deviations,
        "katago_version": None,
        "nets": None,
        "analysis_config": None,
        "hardware": hardware_block(katago_backend),
        "concurrency": None,
        "epsilon": None,
        "seed": int(seed),
        "started": started.isoformat(timespec="seconds"),
        "finished": finished.isoformat(timespec="seconds"),
        "wall_clock_s": round(wall, 3),
        "engine_queries": 0,
        "n": None,
        "estimate": None,
        "ci": None,
        "success": None,
        "details": {},
    }
    for key in CLAIM_FIELDS:
        if key in outcome:
            result[key] = outcome[key]
    if pilot:
        result["success"] = None  # pilots never carry a verdict (eval/README.md)

    results_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{'pilot_' if pilot else ''}{claim}_{started.strftime('%Y%m%d-%H%M%S')}"
    out = results_dir / f"{stem}.json"
    n = 1
    while out.exists():  # two runs within one second
        n += 1
        out = results_dir / f"{stem}_{n}.json"
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=_jsonable) + "\n", encoding="utf-8")
    regenerate_summary(results_dir)
    return out


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m eval.run", description="Run one pre-registered claim.")
    parser.add_argument("--claim", required=True, choices=sorted(CLAIMS), help="which claim to run")
    parser.add_argument("--config", required=True, help="YAML config, normally configs/prereg.yaml")
    parser.add_argument("--pilot", action="store_true", help="pilot run (C1): labelled, never pooled")
    parser.add_argument("--concurrency", type=int, default=8, help="games or positions in flight (C1, C2)")
    parser.add_argument("--seed", type=int, default=None, help="override seeds.global (logged as a deviation)")
    parser.add_argument("--results-dir", default=None, help="where to write (default: results/ in the repo)")
    return parser


def main(argv: Sequence[str] | None = None) -> Path:
    args = build_parser().parse_args(argv)
    config_path = Path(args.config)
    if not config_path.is_file():
        raise SystemExit(f"config not found: {config_path.resolve()}")
    results_dir = Path(args.results_dir) if args.results_dir else RESULTS_DIR
    out = run(
        args.claim,
        config_path,
        pilot=args.pilot,
        concurrency=args.concurrency,
        seed=args.seed,
        results_dir=results_dir,
        config_label=args.config.replace("\\", "/"),
    )
    data = json.loads(out.read_text(encoding="utf-8"))
    print(f"wrote {out}")
    print(
        f"{data['claim']}: n={data['n']} estimate={data['estimate']} ci={data['ci']} success={data['success']} "
        f"commit={data['git_commit']} dirty={data['git_dirty']} wall_clock_s={data['wall_clock_s']}"
    )
    headline = (data.get("details") or {}).get("headline")
    if headline:
        print(f"headline {headline['name']}={headline['estimate']} ci={headline['ci']}")
    return out


if __name__ == "__main__":
    main()
