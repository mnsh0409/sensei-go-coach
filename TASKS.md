# TASKS

Work top to bottom. One task per Claude Code session. Mark a task DONE only when its acceptance tests
pass (not skip) and the commit is made. HUMAN means the human does it or decides it; Claude Code stops and asks.

Calendar (Asia/Taipei): build Oct 27 to Nov 3, internal deadline Nov 4 09:00, hard deadline Nov 5 09:00.
Machines: development on the Windows PC; evaluation runs (C1, C2, topic fitting) on the evaluation machine
chosen in T0. While the confirmatory C1 runs, nothing else uses that machine's GPU.

## Build status (2026-10-29, prepared in a cloud session; verify on your machines)

Code and offline tests exist for every module below; `pytest -q` runs 180+ offline tests against
`tests/fake_katago.py` and scripted model clients. A task is still not DONE until its own acceptance (real KataGo,
the Meta API, a clean git commit, the human gates) passes on your machines. Resume at the first unchecked step.

| Task | Prebuilt | Still needed on your machines |
|---|---|---|
| T0 | | everything (KataGo + nets, `.env`, rules, data source, git repo) |
| T1 | `metaapi.py`, smoke test script | run the smoke test; write `requirements.lock` |
| T1b | `sgf.py`, `eval/splits.py`, fetcher and splits scripts | fetch the games, write `data/splits.json`, fill `data/SOURCES.md` |
| T2 | `engine_katago.py`, `scripts/bench_engine.py` | `pytest -m engine`; one latency file per machine |
| T3 | `game_go.py`, `cli.py` | a 20-move CLI game against real KataGo |
| T4 | selector, stats, `eval/openings.py`, `eval/c1_strength.py`, C1 in `eval/run.py` | approve `C1.learner_profile`; run the pilot from a clean commit; copy epsilon* to `play.epsilon` |
| T4b | | launch the confirmatory C1 |
| T5 | `explainer.py`, `scripts/explainer_set.py` | build the set, live run, grade (gate), calibrate entropy thresholds |
| T6 | `learner.py`, `scripts/learner_check.py` | run the check |
| T7 | `topics.py`, `curriculum.py`, `session.py`, `scripts/fit_topics.py` | fit the topics; name them (gate) |
| T8 | `eval/c2_relevance.py`, `eval/c3_curriculum.py` | run C2; rerun C3 from a clean commit |
| T9 | `src/sensei/app.py` (live, `--fake` and `--replay` modes), `src/sensei/static/index.html` | live check on the dev PC; record one live game into `results/` for the replay demo |
| T10 | `eval/tokens.py`, `scripts/render_readme.py`, `scripts/check_numbers.py` | write the templates and the video script |

---

## T0 HUMAN setup and decisions (Oct 27)

- [ ] Download KataGo for each machine (CUDA build for NVIDIA GPUs; the OpenCL build is the fallback if the
      CUDA DLLs fail on Windows) and the nets: a recent strong main net and the HumanSL net
      (b18c384nbt-humanv0) from katagotraining.org. Keep them outside the repo.
      As of 2026-10-29 the latest release is v1.18.0 (2026-08-22); its notes recommend the cuDNN 9.8.0 builds
      (for Windows `katago-v1.18.0-cuda12.1-cudnn9.8.0-windows-x64.zip`; the cuDNN 8.9.7 builds are much slower on
      transformer nets) and TensorRT 10 or later for TensorRT builds. The HumanSL net fails on TensorRT in v1.17.0
      and v1.17.1 (fixed in v1.17.2), so never use those two versions with TensorRT.
- [ ] The analysis config ships as `configs/katago_analysis.cfg` (KataGo v1.18.0's `analysis_example.cfg` with
      `numAnalysisThreads = 8`, `numSearchThreadsPerAnalysisThread = 2` and `logDir = logs/katago`); point
      `KATAGO_CONFIG` at it. Its `reportAnalysisWinratesAs = BLACK` is overridden per query by the wrapper
      (CLAUDE.md). Leave `wideRootNoise` at its analysis default: the app and C1 share this config, and every result
      records its settings (eval/README.md, "Engine settings"). Step-by-step commands: `RUNBOOK.md`.
- [ ] Run `katago benchmark` on each machine. Write visits/s into `results/benchmark.md` and estimate the C1 wall
      clock: 120 pilot games plus 400 confirmatory games, about 250 queries of 200 visits per game, games run
      concurrently. If 400 games will not finish by Oct 31, lower `openings.n_openings` (minimum 100) now and log
      it in the `revisions` list of `configs/prereg.yaml`.
- [ ] Get `MODEL_API_KEY`, add a payment method under Billing at dev.meta.ai, claim the $150 credits (claim form,
      deadline Nov 4, 9 PM ET), and fill `.env` from `.env.example` (paths unquoted or with forward slashes).
- [ ] Read the official rules; fill the "Rules check" table in `SCOPE.md`; choose a license.
- [ ] Decide and record: (a) the data source (default in `data/SOURCES.md`; check terms and rate limits);
      (b) the evaluation machine (a separate Linux machine with a strong GPU is best, keeping the dev PC free);
      (c) hosting: replay mode only, or live hosting too, per the rules.
- [ ] Create the GitHub repository (private until submission) and push this kit as the first commit.

## T1 Environment and Meta API smoke test (Oct 27)

Build: editable install, `requirements.lock`; `sensei/metaapi.py` with a client factory reading `.env`, and
`log_call` writing `logs/api_calls.jsonl` with an estimated cost from `configs/default.yaml` `api` prices and a
warning once the logged total passes `api.budget_warn_usd`.
Accept:
- Fast offline tests green (contract, coords and ranks tests).
- `python scripts/meta_smoke_test.py` passes steps 1 to 3; token counts and cost appear in `logs/api_calls.jsonl`.
- `results/smoke_image.json` written (stone-reading accuracy; informs stretch S4; no threshold).

## T1b Public games and SGF parsing (Oct 28, CPU only; can run while KataGo is installed)

Build: `sensei/sgf.py` (`read_game`, INTERFACES.md); `scripts/fetch_games.py` for the T0 source with polite rate
limiting and a cache in `data/raw/`; the splits in `data/splits.json` (disjoint, `seeds.global`): 20 pilot
openings, 200 confirmatory openings, C2 positions for 4 bands, 300 topic-fitting games, 10 explainer positions;
`data/SOURCES.md` filled in, including the rank mapping and the HumanSL overlap reasoning.
Accept: `tests/test_sgf.py` passes; split counts recorded in `data/SOURCES.md`.

## T2 KataGo engine wrapper (Oct 28)

Build: `sensei/engine_katago.py` with `KataGoEngine` as specified in CLAUDE.md (KataGo section) and INTERFACES.md
(Engine): side-to-move perspective forced per query and asserted, thread-safe, stderr drained, `from_env()` path
checks. First verify the field names and the ko rule KataGo uses for `chinese`, and record both in a comment.
Also `scripts/bench_engine.py`: mean and p95 latency of `analyze` at `max_visits`, of `human_policy`, and the
throughput with 1, 4 and 8 concurrent queries on 50 fixed positions, written to
`results/latency_<host>_<backend>.json` with the hardware block from eval/README.md. It must run unchanged on
every machine and KataGo backend (CUDA, TensorRT, OpenCL), because these files are the scaling figure of the
GTC poster (T10b).
Accept: `pytest -q -m engine tests/test_engine_katago.py` passes, including the perspective, ownership and
concurrency tests; `results/latency_<host>_<backend>.json` written for the dev PC.

## T3 Go rules and terminal game loop (Oct 28)

Build: `sensei/game_go.py` (`GoGame`: captures, the same ko rule as KataGo for `chinese`, suicide illegal, pass,
two passes end the game, `board(pos)`, `result()` returns None); `sensei/cli.py` to play Sensei or the raw engine
in the terminal, showing the final score lead.
Accept: `tests/test_game_go.py` passes; a 20-move CLI game against the raw engine completes.

## T4 Selector, statistics and the C1 pilot (Oct 29)

Build: `sensei/selector.py` exactly as in INTERFACES.md; `eval/stats.py`, `eval/openings.py`,
`eval/c1_strength.py` (concurrent games, adjudication) and `eval/run.py` for C1 (eval/README.md).
Run the pilot on the evaluation machine: 3 epsilons x 40 games. The pre-registered rule picks epsilon*; the result
goes to `results/pilot_C1_<timestamp>.json` with `"pilot": true`; copy epsilon* into `configs/default.yaml`
`play.epsilon`.
Accept: `tests/test_selector.py` and `tests/test_stats.py` pass; pilot result written from a clean git state.

## T4b HUMAN: launch the confirmatory C1 (Oct 29 night or Oct 30)

Start `python -m eval.run --claim C1 --config configs/prereg.yaml --concurrency 8` in the background on the
evaluation machine from a clean git state. It runs while T5 to T7 proceed. Check it once a day.

## T5 Explainer and Ask Sensei (Oct 29 to 30)

Build: `sensei/explainer.py` (`MetaExplainer`, `build_explanation_messages`, `TOOLS` with the parameters in
INTERFACES.md, `find_ungrounded_coords`, the tool loop with at most `explainer.max_tool_rounds` rounds);
`eval/explainer_set.json` from the 10 explainer positions in the splits (2 opening, 4 middle game, 2 endgame,
2 only-move), each with a declared learner rank (`python scripts/explainer_set.py --build`, then `--run`).
Accept: offline `tests/test_explainer.py` passes; a live run writes `results/explanations_v1.md` (position,
choice, explanation, tool calls, tokens, cost).
HUMAN gate: the human grades each explanation 1 to 5 (correct, grounded, useful at the declared rank).
Mean of at least 3.5 passes. Otherwise iterate the prompt at most twice, then fall back to a template
explanation that the model only polishes. Calibrate `selector.entropy_low` and `selector.entropy_high` in
`configs/default.yaml` on this set; the human approves.
Deviation (2026-11-04, approved by the human in chat): v1 graded 2.50 (`results/explanations_v1_grades.md`); prompt
v2 and v3 were the two rounds, v3.1 fixed one wrong fact (the engine's top move) and graded 3.25
(`results/explanations_v3.1_grades.md`), below 3.5. Instead of the template, a third round (v4) adds engine-grounded
reasons (what the learner would play if Sensei passed, connections, rescued groups), because every 3 in the grades
asked for the purpose of a move and the template carries the same facts as v3.1. If v4 is below 3.5, the template.
Result (2026-11-04): v4.1 (v4 plus a fix to the facts) graded 3.80, explanations 3.90 and Ask Sensei 3.70
(`results/explanations_v4.1_grades.md`): T5 passes and the explanation prompt is frozen at v4.1 for the submission.
v4.2 (2026-11-05, approved by the human in chat after a live game) changes Ask Sensei only: questions about moves
already played, and no tool field names or instruction echo in answers. Not done:
the entropy thresholds stay at their placeholders (changing them would change the graded texts); disclosed in the
README limitations.

## T6 Learner model (Oct 30)

Build: `sensei/learner.py` (`HumanSLLearner`) using `sensei.ranks.rank_posterior`, with the cache key from
INTERFACES.md.
Accept: offline `tests/test_learner.py` passes; `results/learner_check.json` reports, for 5 held-out games per
band, the estimated rank against the true rank (descriptive only, not a claim): `python scripts/learner_check.py`.

## T7 Mistakes, topics, curriculum and session (Oct 31)

Build: the mistake check and situation featurizer (INTERFACES.md); `sensei/topics.py` (`LdaMistakeTopics`,
scikit-learn LDA) fitted on `topics.fit_games` games at `topics.fit_visits` on the evaluation machine (only if the
confirmatory C1 has finished; otherwise on the dev PC); `sensei/curriculum.py` (`ThompsonCurriculum`);
`sensei/session.py` (per-move check, tagging, immediate explanation for target-topic mistakes, profile JSON,
curriculum update).
Accept: `tests/test_topics.py` and `tests/test_curriculum.py` pass; fitted topics described in
`results/topics_v1.md` (`python scripts/fit_topics.py`, which also writes the model `results/topics_v1.json` that
the app loads through `configs/default.yaml` `topics.model`); a new `tests/test_session.py` with a fake engine and
fake explainer shows that a scripted 30-move game updates the profile JSON and the curriculum, and explains only
target-topic mistakes.
HUMAN gate: readable topics are kept and named; otherwise switch to the hand-coded fallback categories in
`configs/default.yaml`.

## T8 C2, C3 and the summary (Nov 1)

Build: `eval/c2_relevance.py` and `eval/c3_curriculum.py` (eval/README.md); extend `eval/run.py`.
Accept: C3 runs offline; C2 runs on the evaluation machine; the confirmatory C1 has finished;
`results/C1_*.json`, `results/C2_*.json`, `results/C3_*.json` exist from a clean git state; `results/SUMMARY.md`
generated from them.

## T9 Web UI and demo (Nov 2)

Build: `sensei/app.py` (FastAPI) and one HTML page: board, explanation panel, Ask Sensei chat with variations
drawn on the board, session summary, rank dropdown. Replay mode first (plays back a recorded session from
`results/`), then live mode on the dev PC; hosted only if T0 chose hosting.
Accept: replay mode works from a clean browser; the full user journey in `SCOPE.md` works live on the dev PC;
the hosted URL works if T0 chose hosting.

## T10 README, numbers check, video script (Nov 3)

Build: `scripts/render_readme.py` (fills `{{C1.estimate}}`-style tokens inside `<!-- results -->` blocks of
`README.md` and `video_script.md` from `results/*.json`, refusing results with `git_dirty: true`);
`scripts/check_numbers.py` (every number inside those blocks matches the rendered values; numbers outside the
blocks are not checked); README per the checklist in `SCOPE.md`; `video_script.md` (under 3 minutes); the
submission description text.
Both scripts exist (token grammar and aliases in `eval/tokens.py`): write the text in `README.tmpl.md` and
`video_script.tmpl.md` (tokens such as `{{C1.elo}}`, `{{C2.U_pct}}`, `{{C3.gap}}`, or any dotted path like
`{{C1.details.summary.score:.3f}}`); `render_readme.py` writes `README.md` and `video_script.md` from them.
Accept: both scripts pass; every item in the SCOPE checklist ticked except submission.

## T11 Buffer and submission (Nov 3 to 4)

Rehearse the demo from a clean machine, record the video, submit by Nov 4 09:00 Asia/Taipei.

## T10b GTC 2027 poster (Nov 6 to 9; only after the hackathon is submitted; HUMAN decides go or no-go)

Deadline: NVIDIA GTC 2027 poster call, Tuesday Nov 10, 2026, 5 p.m. PT (Wed Nov 11, 09:00 Asia/Taipei).
Review criteria (from the call): original technical work; technical improvement enabled by accelerated computing;
a research framework with a defined problem, use of NVIDIA technology, benchmarking methodology and results.
Format (from the GTC guidelines): 43 x 24 inch print-ready PDF at 300 dpi, 1 inch white margin, no NVIDIA logos or
branding, sections Problem Statement / Goal, Application of NVIDIA Technology, Benchmarking Methods, Results and
Outcomes, plus references; no product promotion or marketing language.
The skeleton already exists: `poster/sensei_gtc2027_poster.pptx` with every number as a `{{token}}` and five figure
slots; `poster/TOKENS.md` maps each token to its result file.
Build: run `scripts/bench_engine.py` on every available GPU and backend (dev PC, Linux 4090, Blackwell RTX PRO 6000;
CUDA and TensorRT); `eval/poster_figures.py` writing the five PNGs listed in `poster/TOKENS.md` from `results/` and
`logs/turns.jsonl` only; `poster/render_poster.py` replacing tokens and placing the PNGs over their slots
(python-pptx; replace `run.text`, never `text_frame.text`), writing `poster/sensei_gtc2027_poster_filled.pptx`.
The Meta Model API is described as a swappable explanation backend; the NVIDIA stack (KataGo CUDA/TensorRT on RTX,
concurrent evaluation on one GPU, batched multi-profile HumanSL rank estimation) carries the poster.
Accept: `scripts/check_numbers.py` passes on the filled file; no `{{` left in it; PDF exported from PowerPoint at
300 dpi; submitted on the GTC portal with whatever the form requires.

## T12 C2b: pre-registered lesson relevance on fresh games (after submission, for the poster; agreed 2026-10-31)

Origin: the descriptive `results/relevance_baselines.md` (commit a231355). Of its six band-to-stronger-band steps,
one beat a random admissible move: the 15k-10k lesson as played by 4k-1k players. It was found after the fact and is
sensitive to KataGo's run-to-run variation, so C2b tests it on games that no split has used.
Data: games in `data/raw/ogs` not named anywhere in `data/splits.json` (1,570 on 2026-10-31), plus the games
`scripts/fetch_games.py --target 4500` adds (started 2026-10-31). Drawn once, with a fixed seed, into
`data/splits_c2b.json`, committed after the prereg entry and before any analysis.
Prereg (human approval): a `C2b` entry in `configs/prereg.yaml` that states the hypothesis's exploratory origin.
Sensei unchanged (C1's epsilon*, C2's bands and profiles). Primary: R = [P_4k-1k(played = s_15k-10k) /
P_15k-10k(played = s_15k-10k)] / [the same ratio for r, a random admissible move]; success: the lower end of the 95%
game-clustered bootstrap CI of R is above 1. One KataGo pass per position, every lesson from that pass. Positions per
band, cap per game and resamples fixed in the entry before the split is drawn. Secondary, descriptive: the engine's
best move as a second baseline, the other kyu steps, positions where the lesson is not the best move, and the dan band
only if the fetch brings enough new dan games.
Build: the fresh-game split script, `eval/c2b_relevance.py` (reusing `c2_relevance.py` and `relevance_baselines.py`)
with tests, `python -m eval.run --claim C2b`, poster tokens.
Accept: tests pass; `results/C2b_*.json` from a clean commit; the poster reports it whatever the verdict.

---

## Stretch (only after T9 is DONE)

S1 to S5 as listed in `SCOPE.md`, in that order, one session each, each with its own tests.
