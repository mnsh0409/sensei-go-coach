"""The five GTC 2027 poster figures (TASKS.md T10b), written at 300 dpi to poster/figures/.

    python -m eval.poster_figures                  # clean results only (the poster's rule)
    python -m eval.poster_figures --allow-dirty    # preview with uncommitted results (never for the export)

Every figure reads committed results only: results/latency_*.json (Figure 2), results/turns.jsonl (Figure 3, a
committed copy of the app's logs/turns.jsonl from the recorded games; logs/ is gitignored), the
latest clean pilot and confirmatory C1 files (Figure 4) and results/relevance_baselines.json computed from the
latest clean C2 file (Figure 5). Figure 1 is the system diagram. Sizes match the slots in poster/TOKENS.md.
Needs the poster extra: pip install -e ".[poster]".
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch  # noqa: E402

from eval.tokens import Resolver  # noqa: E402

DPI = 300
SIZES = {  # inches, from poster/TOKENS.md
    "fig1_system.png": (13.1, 5.5),
    "fig2_throughput.png": (13.1, 4.0),
    "fig3_turn_latency.png": (13.1, 3.1),
    "fig4_c1_cost.png": (6.4, 3.8),
    "fig5_c2_uplift.png": (6.4, 3.8),
}
# Validated categorical slots 1 to 3 (dataviz reference palette); text stays in ink colors.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a"]
SEQ = ["#a9c8ef", "#5d9be3", "#1f5fae"]  # one hue, light to dark: 1, 4, 8 concurrent queries
INK, INK2, GRID, SURFACE = "#1c1c1c", "#52514e", "#e4e3df", "#ffffff"
POSTER_BLUE, POSTER_ORANGE = "#0F4C5C", "#D9480F"
GPU_LABEL = {"3090": "RTX 3090", "4090": "RTX 4090", "p6000": "RTX PRO 6000"}
BACKEND_LABEL = {"cuda": "CUDA", "trt": "TensorRT"}
GPU_ORDER = ["3090", "4090", "p6000"]  # keys from eval.tokens.gpu_key
TABLE_ROWS = [(g, b) for g in GPU_ORDER for b in ("cuda", "trt")]
"""The data rows of the poster's Table 1, in order (poster/build_poster.js)."""
TABLE_ROW_IN = 0.46
"""Height of one Table 1 row in inches. render_poster.py removes rows that were not benchmarked and moves Figure 2
up by the freed height, so Figure 2 grows by the same amount here and keeps its aspect ratio in the slot."""
LEVELS = ["1", "4", "8"]


def _style() -> None:
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 15,
            "axes.titlesize": 16,
            "axes.labelsize": 15,
            "xtick.labelsize": 14,
            "ytick.labelsize": 14,
            "legend.fontsize": 14,
            "axes.edgecolor": INK2,
            "axes.labelcolor": INK,
            "xtick.color": INK2,
            "ytick.color": INK2,
            "text.color": INK,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.grid": True,
            "grid.color": GRID,
            "grid.linewidth": 1.0,
            "axes.axisbelow": True,
            "figure.facecolor": SURFACE,
            "axes.facecolor": SURFACE,
            "savefig.facecolor": SURFACE,
        }
    )


def _save(fig: Any, out_dir: Path, name: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / name
    fig.savefig(path, dpi=DPI)
    plt.close(fig)
    return path


def fig2_size(bench: Mapping[tuple[str, str], Any]) -> tuple[float, float]:
    """Figure 2's size: the slot plus the height freed by Table 1 rows that were not benchmarked."""
    w, h = SIZES["fig2_throughput.png"]
    missing = sum(1 for key in TABLE_ROWS if key not in bench)
    return w, h + TABLE_ROW_IN * missing


def _figure(name: str, size: tuple[float, float] | None = None) -> tuple[Any, Any]:
    w, h = size or SIZES[name]
    fig, ax = plt.subplots(figsize=(w, h), layout="constrained")
    return fig, ax


# ---- data loading ------------------------------------------------------------------------------------------------


def load_benchmarks(results: Path, allow_dirty: bool) -> dict[tuple[str, str], dict[str, Any]]:
    """(gpu key, backend) -> latency file; clean files only unless allow_dirty."""
    return {k: v[1] for k, v in Resolver(results, allow_dirty).benchmarks().items()}


def load_turns(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---- Figure 1: system diagram ------------------------------------------------------------------------------------


def fig1_system(out_dir: Path) -> Path:
    name = "fig1_system.png"
    w, h = SIZES[name]
    fig = plt.figure(figsize=(w, h))
    ax = fig.add_axes((0, 0, 1, 1))
    ax.set_xlim(0, w)
    ax.set_ylim(0, h)
    ax.axis("off")

    def box(x: float, y: float, bw: float, bh: float, title: str, body: str, accent: str, fill: str) -> None:
        ax.add_patch(
            FancyBboxPatch(
                (x, y),
                bw,
                bh,
                boxstyle="round,pad=0.02,rounding_size=0.12",
                linewidth=2.2,
                edgecolor=accent,
                facecolor=fill,
            )
        )
        ax.text(x + bw / 2, y + bh - 0.28, title, ha="center", va="top", fontsize=17, fontweight="bold", color=accent)
        ax.text(x + bw / 2, y + bh - 0.75, body, ha="center", va="top", fontsize=13.5, color=INK, linespacing=1.35)

    def arrow(x0: float, y0: float, x1: float, y1: float, label: str = "", dy: float = 0.16) -> None:
        ax.add_patch(
            FancyArrowPatch(
                (x0, y0),
                (x1, y1),
                arrowstyle="-|>",
                mutation_scale=22,
                linewidth=2.0,
                color=INK2,
                shrinkA=0,
                shrinkB=0,
            )
        )
        if label:
            ax.text((x0 + x1) / 2, (y0 + y1) / 2 + dy, label, ha="center", va="bottom", fontsize=11.5, color=INK2)

    gpu_fill, cpu_fill, llm_fill = "#e8f0f2", "#f6f5f2", "#fdf0ea"
    box(
        0.25,
        2.35,
        3.6,
        2.85,
        "KataGo on one GPU",
        "CUDA or TensorRT backend\nmain net: policy, value,\nownership, search\n"
        "HumanSL net: human policy\nat 11 rank profiles",
        POSTER_BLUE,
        gpu_fill,
    )
    box(0.25, 0.3, 3.6, 1.3, "Learner", "plays a full 19x19 game", INK2, SURFACE)
    box(
        4.95,
        3.35,
        3.5,
        1.85,
        "Selector",
        "admissible: V* − V(a) ≤ ε\npick argmax I(a) vs learner",
        POSTER_BLUE,
        cpu_fill,
    )
    box(
        4.95,
        1.15,
        3.5,
        1.8,
        "Learner model",
        "rank by max likelihood\ntopics: LDA on mistakes\nnext: Thompson sampling",
        POSTER_BLUE,
        cpu_fill,
    )
    box(
        9.35,
        3.35,
        3.5,
        1.85,
        "Explanation",
        "language model narrates\nfrom engine facts and tools",
        POSTER_ORANGE,
        llm_fill,
    )
    box(
        9.35,
        1.15,
        3.5,
        1.8,
        "Grounding guard",
        "board points must come from\nfacts or tool results; else\nretry, drop or template",
        POSTER_ORANGE,
        llm_fill,
    )

    arrow(3.85, 4.3, 4.95, 4.3, "candidates")
    arrow(3.85, 2.6, 4.95, 2.6, "human policy")
    arrow(8.45, 4.3, 9.35, 4.3, "move, facts")
    arrow(11.1, 3.35, 11.1, 2.95)
    ax.text(11.25, 3.15, "tool calls to the engine", ha="left", va="center", fontsize=11.5, color=INK2)
    ax.plot([11.1, 11.1], [1.15, 0.65], color=INK2, linewidth=2.0)
    arrow(11.1, 0.65, 3.85, 0.65, "move and explanation", dy=0.08)
    arrow(2.05, 1.6, 2.05, 2.35)
    ax.text(2.2, 1.97, "learner's move", ha="left", va="center", fontsize=11.5, color=INK2)
    return _save(fig, out_dir, name)


# ---- Figure 2: throughput by GPU, backend and concurrency -------------------------------------------------------


def fig2_throughput(bench: Mapping[tuple[str, str], Mapping[str, Any]], out_dir: Path) -> Path:
    name = "fig2_throughput.png"
    fig, ax = _figure(name, fig2_size(bench))
    keys = [(g, b) for g in GPU_ORDER for b in ("cuda", "trt") if (g, b) in bench]
    keys += sorted(k for k in bench if k not in keys)
    if not keys:
        ax.text(0.5, 0.5, "no clean engine benchmark yet", ha="center", va="center", transform=ax.transAxes)
        ax.axis("off")
        return _save(fig, out_dir, name)
    width = 0.26
    for j, level in enumerate(LEVELS):
        xs, ys, errs = [], [], [[], []]
        for i, key in enumerate(keys):
            lv = bench[key].get("levels", {}).get(level)
            if lv:
                xs.append(i + (j - 1) * width)
                y = float(lv["visits_per_s"])
                ys.append(y)
                ci = lv.get("visits_per_s_ci95") or [y, y]
                errs[0].append(y - float(ci[0]))
                errs[1].append(float(ci[1]) - y)
        bars = ax.bar(xs, ys, width=width - 0.03, color=SEQ[j], label=f"{level} concurrent", zorder=2)
        if any(e > 0 for e in errs[0] + errs[1]):
            ax.errorbar(xs, ys, yerr=errs, fmt="none", ecolor=INK, elinewidth=1.3, capsize=3, zorder=3)
        if level == LEVELS[-1]:  # value above the top of its error bar, so the two never overlap
            top = max(y + e for y, e in zip(ys, errs[1], strict=True)) if ys else 0.0
            for x, y, e in zip(xs, ys, errs[1], strict=True):
                ax.text(x, y + e + 0.025 * top, f"{y:,.0f}", ha="center", va="bottom", fontsize=13, color=INK)
        del bars
    ax.set_xticks(range(len(keys)))
    ax.set_xticklabels([f"{GPU_LABEL.get(g, g)}\n{BACKEND_LABEL.get(b, b)}" for g, b in keys])
    ax.set_ylabel("visits per second")
    ax.grid(axis="x", visible=False)
    ax.yaxis.set_major_formatter(matplotlib.ticker.StrMethodFormatter("{x:,.0f}"))
    ax.margins(y=0.15)
    ax.legend(loc="lower left", bbox_to_anchor=(0.0, 1.0), frameon=False, ncols=3)  # above the plot, clear of the bars
    return _save(fig, out_dir, name)


# ---- Figure 3: coaching-turn latency ----------------------------------------------------------------------------


def _p95(values: Sequence[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, round(0.95 * (len(ordered) - 1))))]


def turn_latency_summary(turns: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Median and p95 per component over turns where that component ran (value > 0)."""
    rows = []
    for field, label in (
        ("engine_ms", "engine search"),
        ("human_policy_ms", "human policy (rank profile)"),
        ("explanation_ms", "explanation (language model)"),
    ):
        values = [float(t[field]) for t in turns if isinstance(t.get(field), int | float) and t[field] > 0]
        if values:
            rows.append(
                {
                    "field": field,
                    "label": label,
                    "n": len(values),
                    "median": statistics.median(values),
                    "p95": _p95(values),
                }
            )
    return rows


def fig3_turn_latency(turns: Sequence[Mapping[str, Any]], out_dir: Path) -> Path:
    name = "fig3_turn_latency.png"
    fig, ax = _figure(name)
    rows = turn_latency_summary(turns)
    if not rows:
        ax.text(0.5, 0.5, "no logged coaching turns", ha="center", va="center", transform=ax.transAxes)
        ax.axis("off")
        return _save(fig, out_dir, name)
    ys = list(range(len(rows)))[::-1]
    colors = [POSTER_BLUE, POSTER_BLUE, POSTER_ORANGE]
    for y, row, color in zip(ys, rows, colors, strict=False):
        ax.barh(y, row["p95"], height=0.55, color=color, alpha=0.28, zorder=2)
        ax.barh(y, row["median"], height=0.55, color=color, zorder=3)
        ax.text(
            row["p95"] * 1.08,
            y,
            f"median {_ms(row['median'])}, p95 {_ms(row['p95'])}  (n = {row['n']})",
            va="center",
            fontsize=13.5,
            color=INK,
        )
    ax.set_yticks(ys)
    ax.set_yticklabels([r["label"] for r in rows])
    ax.set_xscale("log")
    ax.set_xlim(20, max(r["p95"] for r in rows) * 90)
    ax.set_xlabel("milliseconds per turn (log scale; dark bar median, light bar p95)")
    ax.grid(axis="y", visible=False)
    return _save(fig, out_dir, name)


def _ms(v: float) -> str:
    return f"{v / 1000:.1f} s" if v >= 1000 else f"{v:.0f} ms"


# ---- Figure 4: C1 strength cost by epsilon ----------------------------------------------------------------------


def fig4_c1_cost(pilot: Mapping[str, Any], conf: Mapping[str, Any], out_dir: Path) -> Path:
    name = "fig4_c1_cost.png"
    fig, ax = _figure(name)
    per = pilot["details"]["per_epsilon"]
    eps = sorted(per, key=float)
    xs = list(range(len(eps)))
    for x, e in zip(xs, eps, strict=True):
        s = per[e]
        lo, hi = s["elo_cost_ci"]
        ax.errorbar(
            x - 0.12,
            s["elo_cost"],
            yerr=[[s["elo_cost"] - lo], [hi - s["elo_cost"]]],
            fmt="o",
            color=SERIES[0],
            markersize=9,
            capsize=5,
            linewidth=2,
            zorder=3,
            label=f"pilot, {s['n_games']} games" if x == 0 else None,
        )
    e_star = conf["epsilon"]
    xc = next(x for x, e in zip(xs, eps, strict=True) if abs(float(e) - float(e_star)) < 1e-12) + 0.12
    lo, hi = conf["ci"]
    ax.errorbar(
        xc,
        conf["estimate"],
        yerr=[[conf["estimate"] - lo], [hi - conf["estimate"]]],
        fmt="D",
        color=SERIES[1],
        markersize=10,
        capsize=6,
        linewidth=2.5,
        zorder=4,
        label=f"confirmatory, {conf['n']} games",
    )
    bound = conf["config"]["C1"]["success_max_cost_elo_upper"]
    ax.axhline(bound, color=INK2, linestyle="--", linewidth=1.5)
    ax.text(0.5, bound + 6, "pre-registered bound", ha="center", va="bottom", fontsize=12.5, color=INK2)
    ax.axhline(0, color=INK2, linewidth=1.0)
    ax.set_xticks(xs)
    ax.set_xticklabels([f"ε = {e}" for e in eps])
    ax.set_xlim(-0.6, len(eps) - 0.4)
    ax.set_ylabel("Elo cost (positive = weaker)")
    ax.grid(axis="x", visible=False)
    ax.set_ylim(top=max(bound * 1.9, ax.get_ylim()[1]))
    ax.legend(loc="upper left", frameon=False, fontsize=12.5)
    return _save(fig, out_dir, name)


# ---- Figure 5: C2 uplift per band step, with baselines ---------------------------------------------------------


def fig5_c2_uplift(base: Mapping[str, Any], out_dir: Path) -> Path:
    name = "fig5_c2_uplift.png"
    fig, ax = _figure(name)
    bands = base["bands"]
    steps = [s for s in base["steps"] if s.get("gap") == 1]
    width = 0.26
    series = (("lesson", "Sensei's lesson"), ("best", "engine's best move"), ("rand", "random admissible move"))
    for j, (key, label) in enumerate(series):
        xs, ys, los, his = [], [], [], []
        for i, step in enumerate(steps):
            d = step[key]["diff"]
            xs.append(i + (j - 1) * width)
            ys.append(100 * d["estimate"])
            los.append(100 * (d["estimate"] - d["ci"][0]))
            his.append(100 * (d["ci"][1] - d["estimate"]))
        ax.bar(xs, ys, width=width - 0.03, color=SERIES[j], label=label, zorder=2)
        ax.errorbar(xs, ys, yerr=[los, his], fmt="none", ecolor=INK, elinewidth=1.4, capsize=3, zorder=3)
    ax.axhline(0, color=INK2, linewidth=1.0)
    ax.set_xticks(range(len(steps)))
    ax.set_xticklabels([f"{bands[s['weaker']]}\n→ {bands[s['stronger']]}" for s in steps], fontsize=12.5)
    ax.set_ylabel("rise in play rate (points)")
    ax.grid(axis="x", visible=False)
    lo, hi = ax.get_ylim()
    ax.set_ylim(lo, hi + (hi - lo) * 0.55)
    ax.legend(loc="upper left", frameon=False, fontsize=11.5)
    return _save(fig, out_dir, name)


# ---- driver ------------------------------------------------------------------------------------------------------


def build_all(root: Path, out_dir: Path, allow_dirty: bool = False) -> list[Path]:
    _style()
    results = root / "results"
    written = [fig1_system(out_dir)]
    written.append(fig2_throughput(load_benchmarks(results, allow_dirty), out_dir))
    turns_path = root / "results" / "turns.jsonl"
    if allow_dirty and not turns_path.is_file():
        turns_path = root / "logs" / "turns.jsonl"  # preview only: the uncommitted app log
    written.append(fig3_turn_latency(load_turns(turns_path), out_dir))
    resolver = Resolver(results, allow_dirty)
    written.append(fig4_c1_cost(resolver.result("pilot_C1"), resolver.result("C1"), out_dir))
    written.append(fig5_c2_uplift(resolver.result("C2base"), out_dir))
    manifest = {"allow_dirty": allow_dirty, "sources": dict(resolver.files), "figures": [p.name for p in written]}
    manifest["turns"] = turns_path.relative_to(root).as_posix() if turns_path.is_file() else None
    manifest["benchmarks"] = sorted(path.name for path, _ in resolver.benchmarks().values())
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return written


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--out", default=None, help="output directory (default: <root>/poster/figures)")
    ap.add_argument("--allow-dirty", action="store_true", help="preview with uncommitted results")
    args = ap.parse_args(argv)
    root = Path(args.root)
    out = Path(args.out) if args.out else root / "poster" / "figures"
    for path in build_all(root, out, args.allow_dirty):
        print("wrote", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
