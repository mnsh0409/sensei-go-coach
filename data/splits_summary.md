# Splits summary

Written by `python scripts/make_splits.py`; do not edit by hand. The games themselves are in `data/raw/` (not committed); `data/splits.json` holds only game IDs and plies.

- created: 2026-10-30T17:04:31+08:00; seed: 20261027 (`seeds.global`); source: ogs; raw_dir: `data/raw/ogs`
- games read: 2900, dated 2024-08-01 to 2026-10-28 (minimum date `C2.game_date_min` = 2024-08-01)
- speeds: all
- C2 candidates per band: 1000, which is 500 more than the 500 positions analysed; eval/c2_relevance.py keeps the first 500 (in list order) that pass its engine checks.
- left out while reading the index: nothing
- openings skipped because an earlier pick was the same up to rotation or reflection: 1

## Counts per split

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

## C2 positions per band

A position is the board before move index `ply` (20 <= ply <= 200); the mover's at-game-time rank is inside the band; at most 5 positions per game; no game in two bands.

| Band | HumanSL profile | Positions | Games | Per game | Mover ranks | Ply min / median / max | Pool games / positions |
|---|---|---|---|---|---|---|---|
| 15k-10k | rank_12k | 1000 | 200 | 5.0 | 15k to 10k | 20 / 93 / 200 | 708 / 3540 |
| 9k-5k | rank_7k | 1000 | 200 | 5.0 | 9k to 5k | 20 / 87 / 200 | 1305 / 6525 |
| 4k-1k | rank_2k | 1000 | 200 | 5.0 | 4k to 1k | 20 / 86 / 200 | 1041 / 5205 |
| 1d-3d | rank_2d | 1000 | 200 | 5.0 | 1d to 3d | 20 / 88 / 200 | 224 / 1120 |

## Explainer positions

| Game | Ply | Phase | Declared rank |
|---|---|---|---|
| ogs-86228104 | 37 | opening | 15k |
| ogs-77324422 | 25 | opening | 5k |
| ogs-73738029 | 80 | middle | 3k |
| ogs-72393442 | 52 | middle | 3d |
| ogs-87962930 | 137 | middle | 10k |
| ogs-85680630 | 132 | middle | 9k |
| ogs-75402568 | 162 | end | 3k |
| ogs-87526519 | 177 | end | 1d |
| ogs-88898324 | 121 | only_move_candidate | 10k |
| ogs-68278207 | 55 | only_move_candidate | 7k |

## Rank distribution (at-game-time labels)

| Rank | pool (players) | openings (players) | topic games (players) | C2 positions (movers) | explainer (movers) |
|---|---|---|---|---|---|
| 25k | 140 | 18 | 15 | 0 | 0 |
| 24k | 13 | 0 | 3 | 0 | 0 |
| 23k | 22 | 4 | 3 | 0 | 0 |
| 22k | 35 | 6 | 3 | 0 | 0 |
| 21k | 33 | 4 | 3 | 0 | 0 |
| 20k | 39 | 3 | 6 | 0 | 0 |
| 19k | 69 | 6 | 9 | 0 | 0 |
| 18k | 61 | 4 | 7 | 0 | 0 |
| 17k | 71 | 7 | 10 | 0 | 0 |
| 16k | 107 | 10 | 20 | 0 | 0 |
| 15k | 106 | 7 | 18 | 82 | 1 |
| 14k | 133 | 9 | 13 | 107 | 0 |
| 13k | 167 | 11 | 11 | 127 | 0 |
| 12k | 186 | 14 | 23 | 168 | 0 |
| 11k | 236 | 20 | 16 | 199 | 0 |
| 10k | 291 | 27 | 18 | 317 | 2 |
| 9k | 298 | 31 | 27 | 130 | 1 |
| 8k | 379 | 33 | 45 | 170 | 0 |
| 7k | 408 | 34 | 53 | 168 | 1 |
| 6k | 485 | 39 | 49 | 235 | 0 |
| 5k | 536 | 45 | 65 | 297 | 1 |
| 4k | 482 | 18 | 66 | 311 | 0 |
| 3k | 453 | 27 | 41 | 282 | 2 |
| 2k | 384 | 29 | 41 | 217 | 0 |
| 1k | 328 | 27 | 32 | 190 | 0 |
| 1d | 167 | 2 | 2 | 554 | 1 |
| 2d | 85 | 3 | 0 | 262 | 0 |
| 3d | 60 | 1 | 0 | 184 | 1 |
| 4d | 18 | 0 | 0 | 0 | 0 |
| 5d | 5 | 1 | 1 | 0 | 0 |
| 6d | 2 | 0 | 0 | 0 | 0 |
| 7d | 1 | 0 | 0 | 0 | 0 |

## Reproduce

```
python scripts/fetch_games.py --ids-from data/splits.json   # re-fetch exactly these games
python scripts/make_splits.py --dry-run                     # check that the games on disk are enough
```
