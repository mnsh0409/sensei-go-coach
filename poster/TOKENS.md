# Poster tokens

`poster/sensei_gtc2027_poster.pptx` is the 43 x 24 inch skeleton for the GTC 2027 poster (TASKS.md T10b).
Every number and name on it is a `{{token}}`, set in orange. `poster/render_poster.py` (built in T10b) replaces
each token from the sources below and writes `poster/sensei_gtc2027_poster_filled.pptx`; the human exports that
file to PDF (300 dpi) from PowerPoint. `scripts/check_numbers.py` treats the filled file like the README: every
value must match a clean-state result file.

Figure slots are gray boxes with a dashed border; `eval/poster_figures.py` writes the PNGs listed under
`poster/figures/` at 300 dpi and `render_poster.py` places each one over its slot (same x, y, w, h).

| Token | Source | Meaning |
|---|---|---|
| `author_1`, `affiliation_1`, `author_2`, `affiliation_2`, `repo_url` | human, `poster/poster_meta.yaml` | names; an empty `author_2` drops the second author; `repo_url` also feeds the QR code |
| `C1.queries` | `results/C1_*.json` `engine_queries` | engine queries in the confirmatory C1 run |
| `C1.eps` | `results/C1_*.json` `epsilon` | epsilon* used in C1, C2 and the app |
| `C1.elo`, `C1.lo`, `C1.hi`, `C1.threshold` | `results/C1_*.json` `estimate`, `ci`, prereg bound | strength cost in Elo, its 95% CI and the pre-registered limit |
| `C1.hours`, `C1.gph` | `results/C1_*.json` `wall_clock_s`, `n` | wall clock of the confirmatory run and games per hour |
| `C1.gpu`, `C1.conc` | `results/C1_*.json` `hardware.gpu`, `concurrency` | GPU and games in flight |
| `C2.U_pct`, `C2.lo_pct`, `C2.hi_pct` | `results/C2_*.json` `estimate`, `ci` | uplift U in percentage points and its 95% CI |
| `C2.details.n_games` | `results/C2_*.json` | public games used by C2 |
| `C2base.lesson_vs_rand` | `results/relevance_baselines.json` (from the same C2 file) | how the lesson's rise compares with a random admissible move's |
| `C3.learners`, `C3.sessions`, `C3.gap_pct`, `C3.details.gap_closure.ci.*` | `results/C3_*.json` | simulated learners, sessions, gap closure to the oracle and its CI |
| `hw.<gpu>.<backend>.cu` | `results/latency_*.json` `hardware.driver`, `hardware.cuda` | "driver / CUDA" (Table 1) |
| `hw.<gpu>.<backend>.vps` | same file, `levels.8.visits_per_s` | visits per second at 8 concurrent queries, mean over repeats (Table 1) |
| `hw.<gpu>.<backend>.pm` | same file, `levels.8.visits_per_s_ci95` | "± half-width" of its 95% t-interval over repeats (Table 1) |
| `hw.runs` | every benchmark file used, `repeats` | runs per GPU and backend; the files must agree |
| `hw.builds` | every benchmark file used, `katago_build.label` | the KataGo release builds, comma-separated (Table 1 caption) |
| `hw.<gpu>.<backend>.p50` | same file, `levels.1.analyze.p50_ms` | analyze p50 latency at 1 query, ms (Table 1) |
| `hw.hp_ms` | the latency file of C1's GPU and backend, `levels.1.human_policy.mean_ms` | human-policy query latency |
| `hw.conc_gain` | same file, visits per second at 8 over 1 concurrent query | concurrency gain on one GPU |
| `hw.trt_gain` | every GPU with clean CUDA and TensorRT files, ratio of `levels.8.visits_per_s` | "1.3×", or the range over GPUs |

`<gpu>` is `3090`, `4090` or `p6000`, taken from `hardware.gpu` (eval.tokens.gpu_key), so the `--host` label does
not matter; `<backend>` is `hardware.katago_backend` (`cuda` or `trt`). Only clean latency files count (written from
a committed tree); the newest per GPU and backend wins. `render_poster.py` removes a Table 1 row whose GPU or
backend has no clean file and prints which rows it removed.

Figures (`poster/figures/`): `fig1_system.png` (hand-drawn system diagram, 13.1 x 5.5 in),
`fig2_throughput.png` (13.1 x 4.0), `fig3_turn_latency.png` (13.1 x 3.1), `fig4_c1_cost.png` (6.4 x 3.8),
`fig5_c2_uplift.png` (6.4 x 3.8). `eval/poster_figures.py` also writes `manifest.json` naming its sources;
`render_poster.py` refuses figures built with `--allow-dirty`.

Build order: `pip install -e ".[poster]"`; `python -m eval.poster_figures`; `python poster/render_poster.py`;
`python scripts/check_numbers.py`; open `poster/sensei_gtc2027_poster_filled.pptx` in PowerPoint and export a PDF.

Rebuilding the skeleton itself (only if the layout changes): `npm install pptxgenjs`, then
`node poster/build_poster.js && python poster/fix_pptx.py poster/sensei_gtc2027_poster.pptx`.
