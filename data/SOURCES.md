# Data sources

Decided in T0 (HUMAN), filled in during T1b. AI CUP competition data is never used in this repository.

Default unless T0 decides otherwise, and what the code does: OGS (online-go.com) public games, 19x19, even games,
no handicap, ranked, both players human, played on or after 2024-08-01.

Status of this file: the pipeline (`scripts/fetch_games.py`, `scripts/make_splits.py`) is written and tested offline,
but the shell that built it could not reach online-go.com, so no game has been fetched yet and the counts below are
the targets. `python scripts/make_splits.py` writes the counts that were actually drawn between the
`splits:begin` and `splits:end` markers (and `data/splits_summary.md`). Statements marked UNVERIFIED were not
confirmed; the list is at the end.

| Use | Source | URL | License / terms | Date range | Count | Notes |
|---|---|---|---|---|---|---|
| Pilot openings (C1) | OGS | `https://online-go.com/api/v1/games/<id>` | ToS silent on bulk use; permission UNVERIFIED (see Terms of use) | started 2024-08-01 or later | 20 | first 12 moves; distinct up to rotation and reflection |
| Confirmatory openings (C1) | OGS | same | same | same | 200 | disjoint from the pilot openings |
| C2 positions (per band) | OGS | same | same | same | 500 analysed x 4 bands (1000 candidates per band) | at most 5 per game; mover's at-game-time rank in the band; plies 20 to 200; no game in two bands; reserve: see Reproduce |
| Topic fitting (T7) | OGS | same | same | same | 300 games | |
| Explainer test positions (T5) | OGS | same | same | same | 10 | 2 opening, 4 middle game, 2 endgame, 2 only-move candidates; declared rank = mover's rank |

All splits are disjoint, drawn once with `seeds.global`, and stored in `data/splits.json` (game IDs and
positions only). Raw games stay in `data/raw/` (gitignored). Lists in the file are in drawing order, which is random,
so a prefix of a list (for example if `openings.n_openings` is lowered) is itself a random subset.

## Split counts (recorded by `scripts/make_splits.py`)

<!-- splits:begin -->
Recorded by `python scripts/make_splits.py` (created 2026-10-30T17:04:31+08:00, seed 20261027) from 2900 usable games fetched from ogs (2024-08-01 to 2026-10-28; minimum date 2024-08-01).

| Split | Target | Drawn | Games | Game dates |
|---|---|---|---|---|
| pilot_openings | 20 | 20 | 20 | 2024-08-18 to 2026-10-03 |
| confirmatory_openings | 200 | 200 | 200 | 2024-08-04 to 2026-10-26 |
| c2_positions 15k-10k (rank_12k) | 1000 | 1000 | 200 | 2024-08-12 to 2026-10-25 |
| c2_positions 9k-5k (rank_7k) | 1000 | 1000 | 200 | 2024-08-03 to 2026-10-21 |
| c2_positions 4k-1k (rank_2k) | 1000 | 1000 | 200 | 2024-08-02 to 2026-10-24 |
| c2_positions 1d-3d (rank_2d) | 1000 | 1000 | 200 | 2024-08-02 to 2026-10-22 |
| topic_games | 300 | 300 | 300 | 2024-08-01 to 2026-10-28 |
| explainer_positions | 10 | 10 | 10 | 2024-10-02 to 2026-07-19 |

C2 candidates per band: 1000, which is 500 more than the 500 positions analysed; eval/c2_relevance.py keeps the first 500 (in list order) that pass its engine checks.
<!-- splits:end -->

## How the games are obtained

- Endpoint: `GET https://online-go.com/api/v1/games/<id>` returns the game as JSON without any login or token. One
  request per game; `scripts/fetch_games.py` builds the SGF from that JSON (main line only; SZ, KM, RU, HA, DT, BR, WR
  and the result; no user names), so there is no second request.
- There is no public list of games by date (`/api/v1/games/` cannot be read, and `/api/v1/players/<id>/games/` lists one
  player at a time, which would bias the sample). The fetcher therefore draws game IDs uniformly from an ID range with a
  seeded generator (`seeds.global`, so the same seed gives the same sequence) and keeps the ones that pass the filters.
  The range is mapped from dates with 19 (id, start time) pairs read from the API; about 29,000 to 32,000 new IDs a day.
  Roughly one random ID in four does not exist and roughly one existing game in five passes the filters, so expect
  about 6 requests per accepted game: 1600 games take about 2.7 hours at the default 1 request per second.
- Filters, each rejected ID is logged with its reason in `data/raw/ogs/rejects.jsonl`: finished and not annulled or
  cancelled; ended by resignation, score or time (not disconnection); 19x19; handicap 0, no setup stones, Black first;
  ranked; not rengo; neither player a bot (OGS marks bot accounts with the `bot` token in `ui_class`) nor a
  professional; rules one of japanese, chinese, aga, korean, nz, ing; at least 60 moves (passes included), none with
  an explicit colour that breaks alternate play (an edited stone); started (UTC) on or after `--min-date` (default
  `C2.game_date_min`, 2024-08-01); both players' rating deviation below 160 (OGS's own cut-off for a provisional
  rank); the rank computed from the historical rating within one rank of OGS's own value for that game.
- Rules: OGS has six rule sets (`chinese`, `aga`, `japanese`, `korean`, `ing`, `nz`; type `GobanEngineRules` in the OGS
  `goban` library) and the SGF carries the name as `RU[Japanese]`, `RU[Chinese]`, `RU[AGA]`, `RU[Korean]`, `RU[NZ]` or
  `RU[Ing]`. KataGo accepts all six (`parseRulesHelper` in `cpp/game/rules.cpp`: `japanese`, `korean`, `chinese`,
  `aga`, `nz`, and `goe`/`ing` on its line 372; master branch, raw file read on 2026-10-29). `eval/splits.py`
  maps every one of them to its KataGo preset (`katago_rules`, Ing included); `make_splits.py` asks that table and
  leaves out any game whose rules it cannot map (reason `rules_or_komi` in the summary), so every position it draws
  can be built. Go rules inside Sensei (`GoGame`) are simple ko with suicide illegal; a C2 position whose history
  breaks those rules (rare: a multi-stone suicide under NZ or Ing rules) is skipped by the C2 harness and counted.
- Resumable and reproducible: `index.jsonl` holds one line per accepted game (game, id, date, black_rank, white_rank,
  komi, rules, moves, outcome, and a few more); a rerun skips every ID in `index.jsonl` or `rejects.jsonl`, and a larger
  `--target` continues the same sequence. `python scripts/fetch_games.py --ids-from data/splits.json` re-fetches exactly
  the games a splits file names (it reports any that no longer pass the filters).
- Games can be left out of the splits later with `python scripts/make_splits.py --speeds live,rapid,blitz`. The
  `speed` of each game (live, blitz, rapid or correspondence) is in the index. Correspondence games are included by
  default; whether to exclude them is a human decision (their ranks and move quality are no less valid, but they
  differ in style, and 19x19 correspondence games are a small share).

## Terms of use and access

What was checked on 2026-10-29. The terms were read in their raw source file (the shell that built the pipeline can
read raw files from GitHub but not online-go.com), so those quotations are exact. `robots.txt` and the developer
pages were readable only through a web fetcher that returns a summary of a page, so they are as the summary gave them.

- Terms of service (`src/views/docs/legal.tsx` in the OGS repository `online-go/online-go.com`, "Effective: Oct 30,
  2012"): no section on automated access, scraping, robots, API use or research use (a search of the file for those
  words finds nothing). The Code of Conduct lists cheating, threats and harassment, unlawful or abusive content,
  impersonation, breaking the law, and acting "in a manner that negatively affects other users' ability to communicate
  through or use Services provided by Online-Go.com", which is one reason for the low request rate. "Account
  Limitations": OGS "may establish general practices and limits concerning the use of the Service". For user content,
  OGS "does not claim ownership of any Content you submit", while users grant it a worldwide, royalty-free,
  non-exclusive "perpetual, irrevocable, and fully sublicensable license to use, distribute, reproduce, modify, adapt,
  publish, translate, perform, and display such Content". The licence runs to OGS, not to third parties, so the terms
  neither grant nor refuse permission to copy game records.
- "No Cheating or Computer Help" (same terms) forbids using Go programs or neural networks "to analyze current ongoing
  games". The pipeline reads finished games only (a game without an end time is rejected as `not_finished`); the
  later analysis of those games happens offline, in this repository.
- Privacy policy (same file, "Effective: Sep 25, 2021"): OGS may share "non-private, aggregated or otherwise
  non-personal information, such as your public user profile information, game information (both ongoing and
  historical), friends, public and game chats, and forum posts".
- `https://online-go.com/robots.txt` (summary only) contains just `User-agent: *` and `Disallow: /bad-robots`; it does
  not mention `/api/`.
- No rate limit is published (the "Getting started" page of the developer documentation at `https://ogs.readme.io`
  has none; summary only). Whether anonymous clients are throttled, and with which status code, is UNVERIFIED.
- Permission for bulk reading has not been asked for. UNVERIFIED, and a human decision (T0 (a)): the code is
  deliberately gentle (1 request per second by default, exponential backoff on HTTP 429, 5xx and network errors with
  `Retry-After` honoured, a descriptive User-Agent, a stop after 20 refusals in a row), reads only what an anonymous
  browser could read, and should be run with `--contact <repository URL or e-mail>` so that OGS can reach the operator.
  Do not raise `--rate` above 2.
- What is kept: no user names or user IDs are written to the SGFs or to `index.jsonl`; the raw files stay in the
  gitignored `data/raw/`; only game IDs and plies are committed (`data/splits.json`). Game IDs are public, so a game can
  be looked up by ID. This repository does not commit or redistribute the raw SGFs.

## Rank mapping

- Source ranks are computed, not copied. For each game the fetcher takes the players' ratings at the time of the game
  (`historical_ratings.<colour>.ratings.overall.rating`; the `ranking` field next to it is the player's current rank
  and is not used) and converts them with OGS's own formula (`rating_to_rank` and `rankString` in
  `src/lib/rank_utils.ts`, read as a raw file on 2026-10-29): rank = ln(rating / 525) x 23.15, where 29 is 1k and 30 is
  1d; the label is `ceil(30 - rank)k` below 30 and `floor(rank - 29)d` from 30 up, limited to 25k to 9d. The formula
  reproduces ten rating and rank pairs read from the API to better than 1e-5.
  The labels become BR and WR in the SGF, so `sensei.sgf.read_game` and `eval.splits.position_at` return the rank the
  player had in that game. UNVERIFIED: whether OGS stores the rating before or after the game's own update. The fetcher
  rejects a game when the computed rank differs from OGS's own rank for that game
  (`gamedata.players.<colour>.rank`) by more than one rank, which bounds the effect; one of ten slots differed by 0.045.
- On the scale of `sensei.ranks` the OGS number minus 29 is the numeric rank (1k = 0, 1d = 1), so a label is
  identical on both scales.
- C2 bands (`configs/prereg.yaml`) and the HumanSL profile used for the learner policy in each: 15k-10k with
  `rank_12k`, 9k-5k with `rank_7k`, 4k-1k with `rank_2k`, 1d-3d with `rank_2d`. A position belongs to a band when the
  mover's at-game-time label is inside it (both ends included); 16k and weaker, and 4d and stronger, are in no band.
  Declared ranks of explainer positions are limited to 20k to 5d, the app's dropdown.
- Offset between OGS ranks and the HumanSL ranks: UNVERIFIED and not corrected. The HumanSL documentation
  (`https://katagotraining.org/extra_networks/` and KataGo's `docs/Analysis_Engine.md`) says the net was "trained on a
  large number of human games ... across players of all different ranks and time periods" and does not name the
  sources or define the rank scale, so an OGS 12k need not be a `rank_12k` player; a difference of a rank or two is
  plausible. C2's primary metric compares neighbouring bands of the same source, so a constant offset changes which
  profile stands for a band, not the ordering of the bands. The T6 check (`results/learner_check.json`, estimated
  against labelled rank for held-out games) is the place to look for an offset; it is descriptive only.
- OGS rank labels have a floor of 25k; provisional accounts (rating deviation 160 or more) are excluded by default
  (`--allow-provisional` keeps them), because their labels are the least reliable.

## HumanSL overlap reasoning (C2)

The HumanSL net (`b18c384nbt-humanv0`) is listed on `https://katagotraining.org/extra_networks/` as "KataGo Human SL
Network (July 2024)", "originally released with v1.15.0"; the v1.15.0 release page shows 19 July (no year in the text
that was read). The page says only that it "was trained on a large number of human games"; sources, servers and the
last training date are not given. A net that was released in July 2024 cannot have been trained on games played
after its release, so games that started on or after 2024-08-01 cannot be in its training data, whatever the sources
were. 2024-08-01 is `C2.game_date_min` in `configs/prereg.yaml`: it is 13 days after the release page's date and is
rounded to a month start.

- Applied filter: `scripts/fetch_games.py --min-date 2024-08-01` compares each game's own `started` time (UTC) with the
  date, not the ID estimate, for all splits and not only C2; `scripts/make_splits.py` applies the same date again to
  everything it reads and counts the games it leaves out. The date range actually drawn is in the generated block above
  and in `data/splits_summary.md`.
- What the filter does not exclude: players who are also in the training data (games from the same players before the
  release), and positions that recur in older games (openings, joseki). Only the games themselves are known to be new.
- If any game older than 2024-08-01 is ever used (`--min-date` lowered), say why here and flag it in the C2 result.
- The C1 openings and the topic-fitting games use the same date window for simplicity; HumanSL overlap does not matter
  for them.

## Reproduce

Windows (PowerShell, from the repository root; the engine is not needed for either step):

```powershell
.\.venv\Scripts\python.exe scripts/fetch_games.py --target 5 --max-requests 60 --contact "<repository URL or e-mail>"   # trial, about a minute
.\.venv\Scripts\python.exe scripts/fetch_games.py --target 1600 --contact "<repository URL or e-mail>"
.\.venv\Scripts\python.exe scripts/make_splits.py --dry-run     # are the games on disk enough?
.\.venv\Scripts\python.exe scripts/make_splits.py               # writes data/splits.json, data/splits_summary.md, the block above
.\.venv\Scripts\python.exe scripts/fetch_games.py --ids-from data/splits.json   # on another machine: exactly these games
```

If `make_splits.py` says a band is short it names the band and how many more games to fetch; rerun the first command
with the larger `--target` (it continues where it stopped). `data/splits.json` is written once and never regenerated
after a C1 or C2 run (`make_splits.py` refuses to overwrite it without `--force`).

C2 reserve (decided before the first draw): `eval/c2_relevance.py` re-checks every candidate with the engine and keeps
the first 500 per band that pass (an admissible set of at least `C2.min_admissible` = 2 moves); it records a deviation
from the pre-registration when fewer pass. Only-move positions fail that check, and more fail at a small epsilon*,
so `make_splits.py` draws `splits.c2_reserve_factor` (2 in `configs/default.yaml`) times 500 = 1000 candidates per
band by default; the harness still uses the first 500 eligible ones in list order, which is the random drawing order,
so the reserve changes no pre-registered quantity. A reserve needs games (at least 200 per band at 5 per game); if a
band is short, `make_splits.py --dry-run` says how many more games to fetch, and `--positions-per-band N` (for example
750) is the fallback when fetching more is not practical. Every run of `make_splits.py` prints which case it is in.

## UNVERIFIED

- That the SGF endpoint `https://online-go.com/api/v1/games/<id>/sgf` carries historical rather than current ranks
  (not used; the SGF is built from the game JSON).
- The HTTP status of a non-existent game id, the rate limit for anonymous clients, and whether private games answer
  401 or 403.
- Whether the historical rating is the value before or after the game's own rating update.
- The OGS to HumanSL rank offset (above), and OGS's agreement for bulk reading (above).
- The exact text of `robots.txt`, of the developer documentation and of the HumanSL release date: read only through a
  web fetcher that summarizes pages (the shell cannot reach online-go.com, and github.com pages and its API are
  closed to it). The OGS and KataGo source files quoted above were read as raw files.
- Real counts: no game has been fetched here. The 1d-3d band is expected to be the scarcest (an expectation, not a
  measurement); `make_splits.py --dry-run` shows how many games each band has.
