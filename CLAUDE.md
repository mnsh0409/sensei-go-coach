# Sensei: working brief for Claude Code

## What this project is

Sensei is a coaching agent for Go that teaches by playing. KataGo is a tool; a Meta model
(Muse Spark through Meta Model API) explains, answers questions and plans sessions through
tool calls. On each of its turns Sensei asks KataGo for candidate moves, picks the most
instructive move whose value loss stays within a bound epsilon, and explains it in plain
language grounded only on engine numbers. Across sessions, a Thompson-sampling curriculum
targets the learner's weaknesses, which are discovered as topics over move features.

Built for the Meta Global AI Developer Hackathon, indie track.
Hard deadline: 2026-11-05 09:00 Asia/Taipei (06:30 IST). Internal deadline: 2026-11-04 09:00 Asia/Taipei.

Design principle: **the model never chooses moves and never states a variation it did not get
from a tool.** Move selection is deterministic, tested code. The model narrates, answers and plans.

## Read first, every session

1. This file.
2. `SCOPE.md`: frozen scope, stretch list, pre-registered claims.
3. `TASKS.md`: find the first task not marked DONE and work on that task only.
4. `src/sensei/interfaces.py`: the contracts. `INTERFACES.md` explains them and gives the exact math.

## Ground rules

- One task per session. A task is DONE only when its acceptance tests pass (not skip) and its
  commit is made. End every session by running the fast offline tests and pasting the summary line verbatim.
- Do not edit `src/sensei/interfaces.py`, `SCOPE.md`, or `configs/prereg.yaml` without the
  human's explicit approval in chat. If a contract looks wrong, stop and explain why.
- Do not build anything from the stretch list in `SCOPE.md` unless `TASKS.md` says so.
- No number enters `README.md`, the video script or any UI text unless an `eval/` script produced
  it and wrote it to `results/` together with the git commit hash and config. Never type a result by hand.
- Secrets: `MODEL_API_KEY` lives in `.env` (gitignored). Never print, log or commit it.
- Every Meta Model API call goes through `sensei.metaapi` and is logged to `logs/api_calls.jsonl`
  (model, prompt tokens, completion tokens, latency, estimated cost from the prices in `configs/default.yaml`).
  Credit budget is $150; warn the human once the logged total passes `budget_warn_usd`.
- The evaluation harness (`eval/`) never calls the Meta Model API.
- Every result file, latency file and the app's per-turn log carry the `hardware` block from `eval/README.md`.
  The app appends one line per coaching turn to `logs/turns.jsonl` (engine ms, human-policy ms, explanation ms,
  tokens, cost). These feed the GTC poster (TASKS.md T10b) as well as the demo.
- Data boundary: never read, copy or train on AI CUP competition data in this repository.
  All data must be public, with source, license and date range recorded in `data/SOURCES.md`.
- Keep research theory out of the repository: no proofs or theorem statements. The selector is a
  constrained re-ranking rule over engine candidates, nothing more.
- Prefer boring code: standard library, numpy, scipy, scikit-learn. A new dependency needs a reason
  in the commit message.
- Windows first: the dev machine runs Windows. Use `pathlib`, pass subprocess arguments as lists,
  never rely on shell syntax inside Python.
- A test skipped for a missing prerequisite (KataGo binary, API key) means the task is not DONE.
  Say which prerequisite is missing and stop.
- When unsure between two designs, pick the simpler one and note the alternative in the commit message.

## Environment

- Python 3.12 (`requires-python = ">=3.11,<3.13"`). On the dev machine the venv is created with `py -3.12`
  (the python.org install). Never use the Microsoft Store Python 3.13 (the launcher's default on this machine),
  Python 3.10, or any global Python.
- Interpreter for every command: `.venv/Scripts/python.exe` (forward slashes work in both PowerShell and Git Bash;
  `.venv/bin/python` on Linux). Do not rely on an activated venv.
- Setup (PowerShell, done by the human):

  ```powershell
  py -3.12 -m venv .venv
  .\.venv\Scripts\python.exe -m pip install -U pip
  .\.venv\Scripts\python.exe -m pip install -e ".[dev,web]"
  Copy-Item .env.example .env   # then fill it in
  ```

- After the first green smoke test, write the lock file as plain text and commit it. Git Bash:
  `.venv/Scripts/python.exe -m pip freeze --exclude-editable > requirements.lock`. Windows PowerShell 5.1's `>`
  writes UTF-16, so there use `... --exclude-editable | Out-File -Encoding ascii requirements.lock`.

## Commands

| Purpose | Command |
|---|---|
| Fast offline tests | `.venv/Scripts/python.exe -m pytest -q` |
| Tests needing KataGo or the API | `.venv/Scripts/python.exe -m pytest -q -m "engine or network"` |
| Meta API smoke test | `.venv/Scripts/python.exe scripts/meta_smoke_test.py` |
| Evaluations | `.venv/Scripts/python.exe -m eval.run --claim C1 --config configs/prereg.yaml` (C1, C2 or C3) |
| Lint | `.venv/Scripts/python.exe -m ruff check .` |

## Layout

```
src/sensei/      package: interfaces, coords, ranks, sgf, engine_katago, game_go, selector, metaapi, explainer,
                 learner, topics, curriculum, session, cli, app (prebuilt with offline tests; see TASKS.md
                 "Build status" for what still needs real KataGo, the API and the human gates)
scripts/         meta_smoke_test, bench_engine, fetch_games, make_splits, explainer_set, learner_check,
                 fit_topics, render_readme, check_numbers
eval/            evaluation harness (splits, openings, c1_strength, c2_relevance, c3_curriculum, stats, tokens,
                 hardware, run); every result is written to results/
configs/         prereg.yaml (frozen), default.yaml (runtime)
data/            SOURCES.md (committed); raw/, processed/ and cache/ are gitignored
results/         evaluation outputs, committed
logs/            API call log and KataGo stderr, gitignored
profiles/        learner profiles (JSON), gitignored
tests/           acceptance tests; module tests skip until the module exists
```

## Data

- Public games only, from the source decided in T0 and recorded in `data/SOURCES.md` (default: OGS, games
  played on or after 2024-08-01, after the HumanSL net's July 2024 release). 19x19, even games, no handicap.
- All splits (pilot openings, confirmatory openings, C2 positions, topic-fitting games, explainer positions)
  are disjoint and written once to `data/splits.json` (committed; game IDs and positions only, no game data)
  with a fixed seed. Never regenerate it after an evaluation run.

## Meta Model API (public docs, checked 2026-10-27; re-check if a call fails)

- OpenAI-compatible. Base URL `https://api.meta.ai/v1`, Bearer token `MODEL_API_KEY`.
- Model: `muse-spark-1.3`, which Meta lists as recommended for new work; it supports every `reasoning_effort`
  level. `META_MODEL` in `.env` overrides it. `muse-spark-1.1` is the original version.
- Chat completions, tool calling (parallel by default), structured output, image input (`image_url` with a
  base64 data URL) and `reasoning_effort`.
- Use `tool_choice="auto"` with explicit instructions rather than forcing a named function.
- Prices and rate limits: see `configs/default.yaml` (prices) and dev.meta.ai/docs/pricing-rate-limits.
- Use the `openai` Python SDK with `base_url`. Do not use Llama Stack clients.

## KataGo

- Run the analysis engine as one persistent subprocess:
  `katago analysis -config <cfg> -model <main net> -human-model <HumanSL net>`.
  One JSON query per line on stdin, one JSON response per line on stdout; responses can arrive out of order,
  so match on `id`.
- **Perspective (critical).** The shipped `analysis_example.cfg` sets `reportAnalysisWinratesAs = BLACK`. Every
  query must send `"overrideSettings": {"reportAnalysisWinratesAs": "SIDETOMOVE"}`, and the wrapper must assert
  `rootInfo.currentPlayer == pos.to_move`. All winrates, score leads and ownership in Sensei are then for the side
  to move at the root (`ownership > 0` means the side to move owns the point). Getting this wrong inverts the
  selector whenever Sensei plays White; `tests/test_engine_katago.py` checks it.
- Human policy: put `humanSLProfile` (for example `rank_10k`, `rank_1d`, `preaz_5k`) inside `overrideSettings`
  and send `includePolicy: true`; the response then contains `humanPolicy`. A 1-visit query is enough.
- `policy` and `humanPolicy` are flat arrays of length size*size + 1 in row-major order from A19 (top left),
  pass last, with -1 for illegal moves. Renormalize over legal moves.
- Every query sends `rules` and `komi` from `pos.meta` (defaults `chinese` and 7.5) and the board size.
- Process handling on Windows: `subprocess.Popen` with an argument list and `stdin`, `stdout` and `stderr` all
  `PIPE`, `text=True, encoding="utf-8", bufsize=1`. Read stdout on a daemon thread and route each response by
  `id`; drain stderr on a second daemon thread into `logs/katago_stderr.log` (an undrained stderr pipe eventually
  blocks KataGo). Write each query as one line and flush. Shutdown: close stdin (KataGo exits), wait up to 10 s,
  then kill.
- `analyze()` and `human_policy()` are thread-safe so the evaluation harness can keep many games in flight on one
  engine. For evaluation runs set `numAnalysisThreads` to 4 to 8 and `numSearchThreadsPerAnalysisThread` to 2 to 4
  in the analysis config (the example's 16 search threads per position make 200-visit values noisier);
  `configs/katago_analysis.cfg` ships with 8 and 2.
- The main net gives policy, winrate, score lead and ownership. The HumanSL net is `b18c384nbt-humanv0`.
- **Verify the exact query and response field names against KataGo's `docs/Analysis_Engine.md` before writing
  the parser**, and record in a code comment what you verified and from which KataGo version.
- Ko: KataGo's `chinese` preset is simple ko, area scoring, suicide illegal (verified in `cpp/game/rules.cpp`,
  `parseRulesHelper`, 2026-10-29), which is what `GoGame` implements. If KataGo ever rejects a move as illegal,
  the harness logs a deviation.
- Coordinates are GTP style: columns A to T skipping I, rows 1 to 19 from the bottom, plus `pass`.
  Use `sensei.coords`; its `(row, col)` has row 0 at the top, matching KataGo's ownership order.
  sgfmill puts row 0 at the bottom; convert at that boundary only (`sensei.sgf`).

## Definition of done for the hackathon

See "Submission checklist" in `SCOPE.md`.
