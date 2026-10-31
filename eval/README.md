# Evaluation harness

Claims and parameters: `SCOPE.md` and `configs/prereg.yaml`. The harness never calls the Meta Model API.

## Result file contract

Every run writes one JSON file to `results/` named `<claim>_<YYYYmmdd-HHMMSS>.json`; pilot runs are named
`pilot_<claim>_<YYYYmmdd-HHMMSS>.json`. Each file contains:

```json
{
  "claim": "C1",
  "pilot": false,
  "git_commit": "abc1234",
  "git_dirty": false,
  "config_path": "configs/prereg.yaml",
  "config": { "...": "full copy of the config used" },
  "deviations": [],
  "katago_version": "1.x.y",
  "nets": {"main": {"file": "...", "sha256": "..."}, "human": {"file": "...", "sha256": "..."}},
  "analysis_config": {"file": "...", "sha256": "...", "numAnalysisThreads": 8, "numSearchThreadsPerAnalysisThread": 2},
  "hardware": {"host": "...", "gpu": "NVIDIA GeForce RTX 4090", "driver": "...", "cuda": "...",
               "katago_backend": "cuda", "cpu": "...", "ram_gb": 0},
  "concurrency": 8,
  "epsilon": 0.01,
  "seed": 20261027,
  "started": "2026-11-01T10:00:00+08:00",
  "finished": "2026-11-01T14:00:00+08:00",
  "wall_clock_s": 0.0,
  "engine_queries": 0,
  "n": 400,
  "estimate": 0.0,
  "ci": [0.0, 0.0],
  "success": null,
  "details": {}
}
```

The `hardware` block is filled by `eval/hardware.py` (`nvidia-smi --query-gpu=name,driver_version --format=csv`,
the CUDA version from `nvidia-smi`, `platform` for host and CPU) and is also written by `scripts/bench_engine.py`
and into `logs/turns.jsonl`; it is the basis of the GTC poster's scaling figure (T10b), so it is never optional.

`success` is computed by code from the pre-registered criterion, never set by hand (`null` for pilots and
descriptive results). A run with `git_dirty: true` is allowed for debugging but never feeds the README;
`scripts/render_readme.py` refuses it.

epsilon* (used by C2 and copied into `configs/default.yaml` `play.epsilon`) is read from the most recent clean
`pilot_C1_*.json`, field `details.epsilon_star` (`eval.c1_strength.find_epsilon_star`). If only dirty pilots exist,
the most recent one is used and the run records a deviation (debugging only).

`hardware.katago_backend` comes from `KATAGO_BACKEND` in `.env` (cuda, trt, opencl or eigen).

## C1 and C2 in practice

- `python -m eval.run --claim C1 --config configs/prereg.yaml --pilot --concurrency 8` plays the pilot
  (3 epsilons x 40 games); without `--pilot` it plays the confirmatory 400 games at epsilon*.
  `python -m eval.run --claim C2 --config configs/prereg.yaml --concurrency 8` runs C2 (no pilot stage).
- Progress: one stderr line per finished game (C1) or per chunk of positions (C2), with an ETA. C1 also appends every
  game record to `logs/[pilot_]C1_games_<timestamp>.jsonl` while it runs, so a long run can be watched.
- Sensei's learner policy in C1 is HumanSL at `C1.learner_profile`. Prereg v2 lacks that key; until the human
  approves adding it, the harness uses `rank_10k` and records a deviation in every C1 result.
- A game (C1) or position (C2) that KataGo rejects, or that times out after 300 s, is excluded and listed in
  `deviations` (C1) or counted under `details.candidates` (C2). If KataGo exits, the run stops.
- C2 candidates: `data/splits.json` lists, per band, more (game, ply) candidates than `C2.positions_per_band`, in a
  seeded random order. The harness re-checks each one (date, size, handicap, ply range, mover rank in the band,
  rules), analyzes it, and keeps the first `positions_per_band` eligible ones (admissible set of at least
  `C2.min_admissible` moves, at most `C2.max_positions_per_game` per game). A shortfall is a deviation.

## Concurrency

C1 and C2 keep many games or positions in flight on one `KataGoEngine` (thread pool, `--concurrency`, default 8).
Results must not depend on completion order: each game has a fixed opening, colour and seed, and results are
sorted by game index before analysis. KataGo's multithreaded search is not bit-reproducible, so a rerun of the same
config gives slightly different games; the harness itself is deterministic given the engine's answers.

Engine settings: `analysis_config` in every result records the analysis config's SHA-256 and the settings that
matter (`numAnalysisThreads`, `numSearchThreadsPerAnalysisThread`, `wideRootNoise`, `ignorePreRootHistory`,
`rootNumSymmetriesToSample`, ...; null means KataGo's default) plus the per-query overrides the wrapper sends:
analysis queries only force `reportAnalysisWinratesAs = SIDETOMOVE`; human-policy queries also set
`ignorePreRootHistory = false` and `rootNumSymmetriesToSample = 8`, as KataGo's Human SL Analysis Guide recommends.
Keep `wideRootNoise` at the analysis default in the shared config: the app and C1 use the same search, so C1
measures the cost of the selector as deployed, and root noise spreads visits over more candidates, which is where
the selector's reliable alternatives come from.

## Modules

| Module | Task | Content |
|---|---|---|
| `stats.py` | T4 | `elo_from_score(s)` (clips s to [1e-3, 1 - 1e-3]); `paired_bootstrap_ci(values, n_resamples, ci, seed)` returning (mean, lo, hi); `elo_cost_ci(scores_by_opening, n_resamples, ci, seed)` returning the strength cost (positive = Sensei weaker) with a bootstrap over openings; `cluster_bootstrap_ci(values, clusters, n_resamples, ci, seed)` for C2 |
| `splits.py` | T1b | reads `data/splits.json`; game records, openings and human positions (rules mapped to KataGo presets) |
| `openings.py` | T4 | pilot and confirmatory opening sets from `data/splits.json`, replayed under the prereg rules |
| `c1_strength.py` | T4 | games Sensei vs raw engine with adjudication; `--pilot` runs all pilot epsilons and applies the epsilon* rule |
| `hardware.py` | T2 | the hardware block and the engine metadata (KataGo version, net and config SHA-256, threads) |
| `run.py` | T4, extended in T8 | CLI: `python -m eval.run --claim C1 --config configs/prereg.yaml [--pilot] [--concurrency 8]`; regenerates `results/SUMMARY.md` |
| `c2_relevance.py` | T8 | uplift U (primary): each bootstrap resample draws games with replacement within every band, then recomputes every U_k and U; D_best and D_rand (secondary) per band with `cluster_bootstrap_ci` |
| `c3_curriculum.py` | T8 | the simulation with all five policies; no engine needed |
| `tokens.py` | T10 | `{{token}}` resolution from clean results for the README, the video script and the poster |
| `relevance_baselines.py` | after T8 | descriptive context for C2 (not a claim, added after C2 ran): `python -m eval.relevance_baselines` writes `results/relevance_baselines.json` and `.md` from the latest clean C2 result, comparing the lesson's cross-band rates with the engine's best move and a random admissible move, and (with KataGo) the lesson for every weaker band on every stronger band's positions; token source `C2base` |

## Descriptive results

Files in `results/` without a `claim` field are descriptive: they record `git_commit` and `git_dirty` but carry no
success criterion, and `results/SUMMARY.md` lists them only by name (`learner_check.json`, `topics_v1.json`,
`explanations_v1.json`, `relevance_baselines.json`). Only `relevance_baselines.json` feeds the README, through the
`C2base` token source, which resolves only when the file is clean and was computed from the latest clean C2 result
(same name and SHA-256).
