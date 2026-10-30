"""Fit the mistake topics on the topic-fitting games and describe them (TASKS.md T7).

    python scripts/fit_topics.py --concurrency 8           # KataGo from .env, data/splits.json topic_games
    python scripts/fit_topics.py --fake --games 3          # offline plumbing check against tests/fake_katago.py

Every move of every game (passes skipped) is checked exactly as the app checks a learner move
(`sensei.session.check_move`): the position before it is analyzed at `topics.fit_visits` under the game's own
rules and komi, the loss is V* - V(played), and a loss above `play.mistake_threshold` is a mistake with its
situation bag. Documents are windows of `topics.window` consecutive mistakes by the same player
(`sensei.topics.window_documents`). The LDA model (`topics.n_topics` topics, `seeds.global`) goes to
results/topics_<version>.json with the corpus statistics, the hardware block and the git commit, and
results/topics_<version>.md describes each topic for the human gate in T7: name the readable topics in
configs/default.yaml `topics.names`, or switch to `topics.fallback_categories`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "src", ROOT):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from eval.c1_strength import make_selector  # noqa: E402
from eval.hardware import engine_metadata, hardware_block  # noqa: E402
from eval.run import git_info  # noqa: E402
from eval.splits import DEFAULT_SPLITS, game_record, katago_rules, load_splits, raw_dir  # noqa: E402
from sensei.coords import PASS  # noqa: E402
from sensei.game_go import GoGame  # noqa: E402
from sensei.interfaces import EngineAnalysis, Position  # noqa: E402
from sensei.session import check_move  # noqa: E402
from sensei.topics import LdaMistakeTopics, window_documents  # noqa: E402

FAKE_ENGINE = ROOT / "tests" / "fake_katago.py"


class CountingEngine:
    """Thread-safe query counter around the shared engine."""

    def __init__(self, engine: Any) -> None:
        self.engine = engine
        self.queries = 0
        self._lock = threading.Lock()

    def analyze(self, pos: Position, visits: int) -> EngineAnalysis:
        with self._lock:
            self.queries += 1
        return self.engine.analyze(pos, visits)


def analyze_game(
    game_id: str, directory: Path, engine: CountingEngine, runtime: Mapping[str, Any], selector: Any
) -> dict[str, Any]:
    """Check every move of one game. Returns the mistakes (in game order, with their bags) and counts."""
    out: dict[str, Any] = {"game": game_id, "moves": 0, "mistakes": [], "error": None}
    try:
        rec = game_record(game_id, directory)
        if rec.komi is None or rec.handicap or rec.setup:
            raise ValueError("no komi, or a handicap game")
        game = GoGame()
        pos = game.initial_position(size=rec.size, komi=float(rec.komi), rules=katago_rules(rec.rules))
    except (OSError, ValueError) as err:
        out["error"] = f"skipped: {err}"
        return out
    visits = int(runtime["topics"]["fit_visits"])
    threshold = float(runtime["play"]["mistake_threshold"])
    bins = runtime["topics"]["loss_bins"]

    def analyze(p: Position) -> EngineAnalysis:
        return engine.analyze(p, visits)

    try:
        for ply, (color, move) in enumerate(rec.moves):
            if color != pos.to_move:
                raise ValueError(f"ply {ply}: {color} moves out of turn")
            if move == PASS:
                pos = game.play(pos, move)
                continue
            before = analyze(pos)
            after = game.play(pos, move)
            if before.candidates:
                check = check_move(game, analyze, selector, pos, before, move, after, threshold, bins)
                out["moves"] += 1
                if check.is_mistake:
                    out["mistakes"].append(
                        {"ply": ply, "color": color, "move": move, "best": check.best, "loss": round(check.loss, 4),
                         "severity": check.severity, "bag": check.bag}
                    )  # fmt: skip
            pos = after
    except Exception as err:  # noqa: BLE001 - keep what was checked; one bad game must not sink the fit
        if not getattr(engine.engine, "alive", True):
            raise
        out["error"] = f"stopped at ply {len(pos.moves)}: {type(err).__name__}: {err}"
    return out


def topic_label(model: LdaMistakeTopics, k: int) -> str:
    """A suggested name from the top three tokens, for the human to accept or replace."""
    return " / ".join(token.split(":", 1)[-1] for token, _ in model.describe(k, 3))


def describe(model: LdaMistakeTopics, games: Sequence[Mapping[str, Any]], meta: Mapping[str, Any]) -> str:
    mistakes = [(g["game"], m) for g in games for m in g["mistakes"]]
    assigned: dict[int, list[tuple[str, Mapping[str, Any], float]]] = {k: [] for k in range(model.n_topics)}
    for game_id, m in mistakes:
        dist = model.assign(m["bag"])
        k = max(range(len(dist)), key=lambda i: dist[i])
        assigned[k].append((game_id, m, dist[k]))
    corpus = meta["corpus"]
    lines = [
        f"# Mistake topics {meta['version']}",
        "",
        f"Generated by `scripts/fit_topics.py` at commit `{meta['git_commit']}` (dirty: {meta['git_dirty']}) on "
        f"{meta['created']}. Model: `{meta['model_file']}`.",
        "",
        f"Corpus: {corpus['games_used']} games ({corpus['games_skipped']} skipped), {corpus['moves']} moves checked "
        f"at {meta['settings']['fit_visits']} visits, {corpus['mistakes']} mistakes "
        f"(loss above {meta['settings']['mistake_threshold']}), {corpus['documents']} documents of up to "
        f"{meta['settings']['window']} mistakes, {corpus['engine_queries']} engine queries in "
        f"{corpus['wall_clock_s']:.0f} s on {meta['hardware'].get('gpu') or 'no GPU'}.",
        "",
        "HUMAN gate (TASKS.md T7): read each topic below. If they are readable, put one short name per topic, in this",
        "order, into `configs/default.yaml` `topics.names`; otherwise set `topics.names` to the hand-coded",
        "`topics.fallback_categories`. The suggested names are only the top tokens.",
        "",
    ]
    for k in range(model.n_topics):
        rows = assigned[k]
        share = len(rows) / len(mistakes) if mistakes else 0.0
        mean_loss = sum(float(m["loss"]) for _, m, _ in rows) / len(rows) if rows else 0.0
        severity = Counter(m["severity"] for _, m, _ in rows)
        lines += [
            f'## Topic {k}: suggested name "{topic_label(model, k)}"',
            "",
            f"- Top tokens: {', '.join(f'{t} ({w:.2f})' for t, w in model.describe(k, 8))}",
            f"- Mistakes assigned (argmax): {len(rows)} ({100 * share:.1f}%), mean loss {mean_loss:.3f}, "
            f"severity {dict(sorted(severity.items()))}",
        ]
        for game_id, m, p in sorted(rows, key=lambda r: -r[2])[:3]:
            lines.append(
                f"- Example: game `{game_id}` ply {m['ply']}: {m['color']} {m['move']} (engine: {m['best']}), "
                f"loss {m['loss']:.3f}, p = {p:.2f}"
            )
        lines.append("")
    return "\n".join(lines)


def make_engine(fake: bool) -> Any:
    from sensei.engine_katago import KataGoEngine

    if fake:
        return KataGoEngine(
            exe=sys.executable,
            exe_args=[str(FAKE_ENGINE)],
            model="fake-main.bin.gz",
            human_model="fake-human.bin.gz",
            config="fake-analysis.cfg",
            query_timeout=120.0,
        )
    return KataGoEngine.from_env(query_timeout=300.0)


def run(args: argparse.Namespace) -> tuple[Path, Path]:
    root = Path(args.root)
    pre = yaml.safe_load((root / "configs" / "prereg.yaml").read_text(encoding="utf-8"))
    runtime = yaml.safe_load((root / "configs" / "default.yaml").read_text(encoding="utf-8"))
    splits_path = Path(args.splits) if args.splits else DEFAULT_SPLITS
    splits = load_splits(splits_path)
    directory = raw_dir(splits, root)
    games = [str(g) for g in splits["topic_games"]][: args.games or int(runtime["topics"]["fit_games"])]
    if not games:
        raise SystemExit(f"{splits_path} has no topic_games")
    out_dir = Path(args.out) if args.out else root / "results"
    model_path = out_dir / f"topics_{args.version}.json"
    md_path = out_dir / f"topics_{args.version}.md"
    if model_path.exists() and not args.force:
        raise SystemExit(f"{model_path} exists; pass --force to overwrite, or choose another --version")

    selector = make_selector(pre, runtime)
    started = datetime.now().astimezone()
    t0 = time.perf_counter()
    engine = make_engine(args.fake)
    try:
        meta_engine = engine_metadata(engine)
        counting = CountingEngine(engine)
        with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
            results = list(pool.map(lambda g: analyze_game(g, directory, counting, runtime, selector), games))
    finally:
        engine.close()
    wall = time.perf_counter() - t0

    window = int(runtime["topics"]["window"])
    documents: list[dict[str, int]] = []
    for g in results:  # game order, not completion order
        for color in ("B", "W"):
            documents += window_documents([m["bag"] for m in g["mistakes"] if m["color"] == color], window)
    if not documents:
        raise SystemExit("no mistakes found: nothing to fit (check the games and play.mistake_threshold)")
    model = LdaMistakeTopics(int(runtime["topics"]["n_topics"]), int(pre["seeds"]["global"]))
    model.fit(documents)

    commit, dirty = git_info(root)
    used = [g for g in results if g["moves"] > 0]
    meta = {
        "version": args.version,
        "created": started.isoformat(timespec="seconds"),
        "git_commit": commit,
        "git_dirty": dirty,
        "model_file": model_path.name,
        "settings": {
            "fit_visits": int(runtime["topics"]["fit_visits"]),
            "window": window,
            "mistake_threshold": float(runtime["play"]["mistake_threshold"]),
            "loss_bins": list(runtime["topics"]["loss_bins"]),
            "entropy_low": float(runtime["selector"]["entropy_low"]),
            "entropy_high": float(runtime["selector"]["entropy_high"]),
            "seed": int(pre["seeds"]["global"]),
        },
        "corpus": {
            "games_listed": len(games),
            "games_used": len(used),
            "games_skipped": len(games) - len(used),
            "games_with_errors": [g["game"] for g in results if g["error"]][:50],
            "moves": sum(g["moves"] for g in results),
            "mistakes": sum(len(g["mistakes"]) for g in results),
            "documents": len(documents),
            "engine_queries": counting.queries,
            "wall_clock_s": round(wall, 1),
        },
        "hardware": hardware_block("fake" if args.fake else (os.environ.get("KATAGO_BACKEND") or None)),
        **meta_engine,
        "suggested_names": [topic_label(model, k) for k in range(model.n_topics)],
    }
    model.save(model_path, **meta)
    md_path.write_text(describe(model, results, meta) + "\n", encoding="utf-8")
    return model_path, md_path


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--concurrency", type=int, default=8, help="games in flight on one engine")
    ap.add_argument("--games", type=int, default=None, help="use only the first N topic games (default: fit_games)")
    ap.add_argument("--version", default="v1", help="output names: results/topics_<version>.json and .md")
    ap.add_argument("--force", action="store_true", help="overwrite an existing model of this version")
    ap.add_argument("--fake", action="store_true", help="use tests/fake_katago.py instead of KataGo")
    ap.add_argument("--splits", default=None, help="splits file (default: data/splits.json)")
    ap.add_argument("--out", default=None, help="output directory (default: results/)")
    ap.add_argument("--root", default=str(ROOT), help=argparse.SUPPRESS)
    return ap


def main(argv: Sequence[str] | None = None) -> int:
    model_path, md_path = run(build_parser().parse_args(argv))
    data = json.loads(model_path.read_text(encoding="utf-8"))
    c = data["corpus"]
    print(f"{c['games_used']} games, {c['moves']} moves, {c['mistakes']} mistakes, {c['documents']} documents")
    for k, name in enumerate(data["suggested_names"]):
        print(f"topic {k}: {name}")
    print(f"wrote {model_path} and {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
