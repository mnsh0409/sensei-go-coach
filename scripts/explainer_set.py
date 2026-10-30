"""The explainer test set and its live run (TASKS.md T5).

    python scripts/explainer_set.py --build           # eval/explainer_set.json from data/splits.json (no engine)
    python scripts/explainer_set.py --run             # KataGo + Meta Model API: results/explanations_v1.md and .json
    python scripts/explainer_set.py --run --fake-engine   # tests/fake_katago.py with the real API (prompt checks)

For each position: KataGo's analysis at `play.max_visits`, the learner policy at the declared rank, Sensei's choice
at `play.epsilon` and temperature 0 (deterministic, so the human grades exactly what the file shows), the move
explanation, and one Ask Sensei question that exercises the tool loop. The markdown file ends with a grading
table: the human grades each explanation 1 to 5 (correct, grounded, useful at the declared rank); a mean of at
least 3.5 passes the T5 gate. Every API call is logged to logs/api_calls.jsonl by sensei.metaapi.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "src", ROOT):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from eval.c1_strength import make_selector  # noqa: E402
from eval.run import git_info  # noqa: E402
from eval.splits import DEFAULT_SPLITS, load_splits, position_at, raw_dir  # noqa: E402
from sensei import metaapi  # noqa: E402
from sensei.game_go import GoGame  # noqa: E402
from sensei.interfaces import Position  # noqa: E402
from sensei.learner import HumanSLLearner  # noqa: E402

SET_PATH = ROOT / "eval" / "explainer_set.json"
FAKE_ENGINE = ROOT / "tests" / "fake_katago.py"


def build_set(splits_path: Path = DEFAULT_SPLITS, root: Path = ROOT) -> dict[str, Any]:
    """The explainer positions of the splits as self-contained positions (moves, rules, komi, declared rank)."""
    splits = load_splits(splits_path)
    directory = raw_dir(splits, root)
    positions = []
    for i, item in enumerate(splits["explainer_positions"], start=1):
        hp = position_at(str(item["game"]), int(item["ply"]), directory)
        positions.append(
            {
                "id": f"e{i}",
                "game": hp.game,
                "ply": hp.ply,
                "phase": item.get("phase"),
                "declared_rank": str(item.get("declared_rank") or hp.mover_rank or "10k"),
                "to_move": hp.position.to_move,
                "meta": dict(hp.position.meta),
                "moves": [list(m) for m in hp.position.moves],
                "played_by_human": hp.played,
            }
        )
    return {
        "version": 1,
        "created": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": f"{splits_path.name} explainer_positions",
        "positions": positions,
    }


def _position(item: Mapping[str, Any]) -> Position:
    return Position(
        game="go", moves=tuple((str(c), str(m)) for c, m in item["moves"]), to_move=item["to_move"], meta=item["meta"]
    )


def question_for(choice_move: str, top_move: str) -> str:
    if choice_move != top_move:
        return f"Why did you play {choice_move} instead of {top_move}? Show me what happens after {top_move}."
    return f"What is the idea behind {choice_move}, and what should I watch for next?"


def run_set(
    items: Sequence[Mapping[str, Any]],
    engine: Any,
    explainer: Any,
    pre: Mapping[str, Any],
    runtime: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Explain Sensei's choice in every position and ask one question; return one record per position."""
    selector = make_selector(pre, runtime)
    game = GoGame()
    prices = runtime["api"]
    out = []
    for item in items:
        pos = _position(item)
        analysis = engine.analyze(pos, int(runtime["play"]["max_visits"]))
        learner = HumanSLLearner(
            engine, pre["learner"]["profile_grid"], item["declared_rank"], int(pre["learner"]["min_moves_for_estimate"])
        )
        choice = selector.choose(
            analysis, learner.policy(pos, game.legal_moves(pos)), float(runtime["play"]["epsilon"]), 0.0,
            np.random.default_rng(0),
        )  # fmt: skip
        explanation = explainer.explain(analysis, choice, learner.current_estimate)
        explainer.set_learner_profile({"declared_rank": item["declared_rank"]})
        top = analysis.candidates[0].move
        question = question_for(choice.move, top)
        answer = explainer.ask(question, position=pos)

        def cost(e: Any) -> float | None:
            return metaapi.estimate_cost_usd(e.prompt_tokens, e.completion_tokens, 0, prices)

        out.append(
            {
                "id": item["id"],
                "game": item["game"],
                "ply": item["ply"],
                "phase": item.get("phase"),
                "declared_rank": item["declared_rank"],
                "board": game.render(pos),
                "to_move": pos.to_move,
                "choice": {
                    "move": choice.move,
                    "engine_top": top,
                    "value_loss": choice.value_loss,
                    "position_class": choice.position_class.value,
                    "admissible": list(choice.admissible),
                },
                "explanation": {
                    "text": explanation.text,
                    "model": explanation.model,
                    "prompt_tokens": explanation.prompt_tokens,
                    "completion_tokens": explanation.completion_tokens,
                    "cost_usd": cost(explanation),
                    "guard": explanation.grounded_on.get("guard"),
                },
                "ask": {
                    "question": question,
                    "text": answer.text,
                    "prompt_tokens": answer.prompt_tokens,
                    "completion_tokens": answer.completion_tokens,
                    "cost_usd": cost(answer),
                    "tool_calls": [
                        {"name": t["name"], "arguments": t["arguments"]}
                        for t in answer.grounded_on.get("tool_calls", [])
                    ],
                    "variations": answer.grounded_on.get("variations", []),
                    "guard": answer.grounded_on.get("guard"),
                },
            }
        )
    return out


def to_markdown(records: Sequence[Mapping[str, Any]], header: Mapping[str, Any]) -> str:
    total = sum((r["explanation"]["cost_usd"] or 0.0) + (r["ask"]["cost_usd"] or 0.0) for r in records)
    lines = [
        f"# Explanations {header['version']}",
        "",
        f"Generated by `scripts/explainer_set.py` at commit `{header['git_commit']}` (dirty: {header['git_dirty']}) "
        f"on {header['created']} with model `{header['model']}`; epsilon {header['epsilon']}, temperature 0. "
        f"Estimated API cost of this file: ${total:.4f}.",
        "",
    ]
    for r in records:
        c, e, a = r["choice"], r["explanation"], r["ask"]
        calls = ", ".join(f"{t['name']}({t['arguments']})" for t in a["tool_calls"]) or "none"
        lines += [
            f"## {r['id']}: {r['phase'] or 'position'}, {r['declared_rank']} learner, {r['to_move']} to move "
            f"(game `{r['game']}`, ply {r['ply']})",
            "",
            "```",
            r["board"],
            "```",
            "",
            f"Sensei plays **{c['move']}** (engine top {c['engine_top']}), value loss {100 * c['value_loss']:.1f}%, "
            f"{c['position_class']}, admissible {', '.join(c['admissible'])}.",
            "",
            f"> {e['text']}",
            "",
            f"Tokens {e['prompt_tokens']} in / {e['completion_tokens']} out; guard {json.dumps(e['guard'])}.",
            "",
            f"Ask Sensei: *{a['question']}*",
            "",
            f"> {a['text']}",
            "",
            f"Tool calls: {calls}; variations {a['variations']}; "
            f"tokens {a['prompt_tokens']} in / {a['completion_tokens']} out.",
            "",
        ]
    lines += [
        "## Grades (human, T5 gate: mean of at least 3.5 passes)",
        "",
        "| Position | Explanation grade (1 to 5) | Ask Sensei grade (1 to 5) | Notes |",
        "|---|---|---|---|",
        *[f"| {r['id']} | | | |" for r in records],
        "",
    ]
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


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build", action="store_true", help="write eval/explainer_set.json from the splits")
    ap.add_argument("--run", action="store_true", help="explain every position with KataGo and the Meta Model API")
    ap.add_argument("--fake-engine", action="store_true", help="tests/fake_katago.py instead of KataGo")
    ap.add_argument("--version", default="v1", help="results/explanations_<version>.md and .json")
    ap.add_argument("--splits", default=None)
    args = ap.parse_args(argv)
    if not (args.build or args.run):
        ap.error("choose --build, --run or both")
    if args.build:
        data = build_set(Path(args.splits) if args.splits else DEFAULT_SPLITS)
        SET_PATH.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
        print(f"wrote {SET_PATH} ({len(data['positions'])} positions)")
    if args.run:
        from sensei.explainer import MetaExplainer

        pre = yaml.safe_load((ROOT / "configs" / "prereg.yaml").read_text(encoding="utf-8"))
        runtime = yaml.safe_load((ROOT / "configs" / "default.yaml").read_text(encoding="utf-8"))
        items = json.loads(SET_PATH.read_text(encoding="utf-8"))["positions"]
        client, model = metaapi.make_client()
        engine = make_engine(args.fake_engine)
        try:
            explainer = MetaExplainer(client, engine, GoGame(), runtime, model=model)
            records = run_set(items, engine, explainer, pre, runtime)
        finally:
            engine.close()
        commit, dirty = git_info(ROOT)
        header = {
            "version": args.version,
            "created": datetime.now().astimezone().isoformat(timespec="seconds"),
            "git_commit": commit,
            "git_dirty": dirty,
            "model": model,
            "epsilon": runtime["play"]["epsilon"],
            "fake_engine": bool(args.fake_engine),
        }
        out = ROOT / "results"
        out.mkdir(exist_ok=True)
        (out / f"explanations_{args.version}.json").write_text(
            json.dumps({**header, "records": records}, indent=1) + "\n", encoding="utf-8"
        )
        (out / f"explanations_{args.version}.md").write_text(to_markdown(records, header), encoding="utf-8")
        print(f"wrote results/explanations_{args.version}.md and .json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
