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
| `author_1`, `author_2`, `affiliation_2` | human | authors and the second affiliation |
| `repo_url` | human | public repository URL (also the QR slot) |
| `C1.queries` | `results/C1_*.json` `engine_queries` | engine searches in the confirmatory C1 run |
| `C1.eps` | `results/C1_*.json` `epsilon` | epsilon* used in C1, C2 and the app |
| `C1.elo`, `C1.lo`, `C1.hi` | `results/C1_*.json` `estimate`, `ci` | strength cost in Elo and its 95% CI |
| `C1.hours` | `results/C1_*.json` `wall_clock_s` / 3600 | wall clock of the confirmatory run |
| `C1.gpu`, `C1.conc` | `results/C1_*.json` `hardware.gpu`, `concurrency` | GPU and games in flight |
| `C2.U`, `C2.lo`, `C2.hi` | `results/C2_*.json` `estimate`, `ci` | cross-band uplift U and its 95% CI |
| `C2.U_pct` | `results/C2_*.json` `estimate` x 100 | U in percentage points |
| `data.n_games` | `data/SOURCES.md` counts or `results/C2_*.json` `details.n_games` | public games used by C2 |
| `hw.<gpu>.<backend>.cu` | `results/latency_<host>_<backend>.json` `hardware.driver`, `hardware.cuda` | "driver / CUDA" |
| `hw.<gpu>.<backend>.vps` | same file, visits per second at 8 concurrent queries | Table 1 |
| `hw.<gpu>.<backend>.p50` | same file, `analyze` p50 latency in ms | Table 1 |
| `hw.conc` | `results/C1_*.json` `concurrency` | concurrent games in the NVIDIA section |
| `hw.hp_ms` | `results/latency_*.json`, `human_policy` mean ms on the dev PC | per-profile human-policy latency |
| `hw.serial_h` | `C1.engine_queries` x p50 analyze latency (1 concurrent) / 3600 | estimated serial wall clock |
| `hw.gph`, `hw.best.gpu`, `hw.best.backend` | best `results/latency_*.json` by throughput, games/h = 3600 x vps / (200 x 250) | headline throughput |
| `hw.trt_speedup` | ratio of TensorRT to CUDA `vps` on the same GPU | TensorRT gain |

`<gpu>` is one of `3090`, `4090`, `p6000`; `<backend>` is `cuda` or `trt`. A GPU or backend that was not
benchmarked keeps its token, so the gap is visible, and the human removes that row before export.

Figures (`poster/figures/`): `fig1_system.png` (hand-drawn system diagram, 13.1 x 5.5 in),
`fig2_throughput.png` (13.1 x 4.0), `fig3_turn_latency.png` (13.1 x 3.1), `fig4_c1_cost.png` (6.4 x 3.8),
`fig5_c2_uplift.png` (6.4 x 3.8).

Rebuilding the skeleton itself (only if the layout changes): `npm install pptxgenjs`, then
`node poster/build_poster.js && python poster/fix_pptx.py poster/sensei_gtc2027_poster.pptx`.
