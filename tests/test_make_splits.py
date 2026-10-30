"""scripts/make_splits.py on synthetic games written to tmp_path: no network, no KataGo.

The checks below do not call the functions under test to decide what is right: ranks, bands, openings and the
capture/atari test are re-implemented here in a few lines, and the harness's own readers (eval.splits) are used to
read the result back.
"""

from __future__ import annotations

import importlib.util
import json
import random
import shutil
import sys
from pathlib import Path

import pytest
import yaml

pytest.importorskip("sgfmill")
from eval import splits as harness  # noqa: E402
from sensei.game_go import GoGame  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CREATED = "2026-10-30T10:00:00+08:00"
COLS = "ABCDEFGHJKLMNOPQRST"


def load_script(name: str):
    """Import scripts/<name>.py; dataclasses with postponed annotations need the module in sys.modules."""
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ms = load_script("make_splits")
fg = sys.modules["fetch_games"]  # the module make_splits imported: one rank and band implementation for both

BANDS = {"15k-10k": (-14, -9), "9k-5k": (-8, -4), "4k-1k": (-3, 0), "1d-3d": (1, 3)}
PROFILES = {"15k-10k": "rank_12k", "9k-5k": "rank_7k", "4k-1k": "rank_2k", "1d-3d": "rank_2d"}


def numeric(label: str) -> int:
    """Independent of sensei.ranks: '12k' -> -11, '1k' -> 0, '1d' -> 1."""
    n = int(label[:-1])
    return 1 - n if label[-1] == "k" else n


def band_of_label(label: str) -> str | None:
    return next((name for name, (lo, hi) in BANDS.items() if lo <= numeric(label) <= hi), None)


# ------------------------------------------------------------------------------------------------ synthetic games


def gtp_of(col: int, row: int) -> str:
    return f"{COLS[col]}{19 - row}"


def sgf_of(col: int, row: int) -> str:
    return chr(97 + col) + chr(97 + row)


def random_moves(rng: random.Random, n: int, prefix=(), passes=()):
    """n legal moves of a random game, Black first: (col, row) per move, None for a pass."""
    game = GoGame()
    pos = game.initial_position()
    out: list[tuple[int, int] | None] = []
    for move in prefix:
        pos = game.play(pos, "pass" if move is None else gtp_of(*move))
        out.append(move)
    while len(out) < n:
        if len(out) in passes:
            pos = game.play(pos, "pass")
            out.append(None)
            continue
        col, row = rng.randrange(19), rng.randrange(19)
        try:
            pos = game.play(pos, gtp_of(col, row))
        except ValueError:  # occupied, ko or suicide
            continue
        out.append((col, row))
    return out


def scattered_moves(rng: random.Random, n: int):
    """n distinct points in random order: cheap, and not necessarily a legal game."""
    points = [(c, r) for r in range(19) for c in range(19)]
    return rng.sample(points, n)


def sgf_text(moves, black: str, white: str, *, date="2025-03-01", size=19, komi=6.5, rules="Japanese", handicap=0):
    nodes = "".join(f";{'B' if i % 2 == 0 else 'W'}[{'' if m is None else sgf_of(*m)}]" for i, m in enumerate(moves))
    head = f"(;GM[1]FF[4]CA[UTF-8]SZ[{size}]KM[{komi}]RU[{rules}]HA[{handicap}]DT[{date}]BR[{black}]WR[{white}]"
    if handicap:
        head += "AB[dd][pp]"
    return head + nodes + ")\n"


class Pool:
    """Writes SGF files and index.jsonl rows into one directory."""

    def __init__(self, directory: Path) -> None:
        self.dir = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.rows: list[dict] = []

    def add(self, game_id, moves, black, white, *, date="2025-03-01", speed="live", **sgf_args) -> str:
        name = f"ogs-{game_id}"
        (self.dir / f"{name}.sgf").write_text(sgf_text(moves, black, white, date=date, **sgf_args), encoding="utf-8")
        self.rows.append(
            {"game": name, "id": game_id, "date": date, "black_rank": black, "white_rank": white, "komi": 6.5,
             "rules": "japanese", "moves": len(moves), "outcome": "Resignation", "speed": speed}
        )  # fmt: skip
        return name

    def finish(self, extra_lines=()) -> Path:
        index = self.dir / "index.jsonl"
        lines = [json.dumps(r) for r in self.rows] + list(extra_lines)
        index.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return index


RANK_PAIRS = [
    ("12k", "11k"),
    ("8k", "7k"),
    ("3k", "2k"),
    ("1d", "2d"),
    ("13k", "6k"),
    ("2k", "1d"),
    ("10k", "9k"),
    ("5k", "4k"),
]
SHARED_OPENING = [(15, 3), (3, 15), (15, 15), (3, 3)]  # pd dp pp dd


def symmetric(points, k: int):
    """The k-th of the eight rotations and reflections of a list of (col, row) points."""
    out = []
    for c, r in points:
        c, r = (18 - c if k & 1 else c), (18 - r if k & 2 else r)
        out.append((r, c) if k & 4 else (c, r))
    return out


def build_small_pool(directory: Path, *, ranks=RANK_PAIRS, n_games=60, seed=1, shared_every=5, special=None) -> Path:
    """About 60 small games. A third are long (160 to 229 moves); every `shared_every`-th starts with the same four
    moves up to symmetry; two start with a pass; some have passes in the middle. `special` maps a game number to
    its (black, white) ranks."""
    rng = random.Random(seed)
    pool = Pool(directory)
    for i in range(1, n_games + 1):
        n = 160 + (i * 7) % 70 if i % 3 == 0 else 62 + (i * 5) % 60
        prefix = symmetric(SHARED_OPENING, (i // shared_every) % 8) if shared_every and i % shared_every == 0 else ()
        passes = {1, 2} if i in (7, 8) else ({8, 13, 21, 30} if i % 4 == 1 else set())
        black, white = (special or {}).get(i) or ranks[i % len(ranks)]
        pool.add(i, random_moves(rng, n, prefix, passes), black, white, date=f"2025-0{1 + i % 6}-1{i % 10}")
    return pool.finish()


def write_configs(directory: Path, **c2) -> tuple[Path, Path]:
    prereg = {
        "seeds": {"global": 123},
        "openings": {"n_openings": 6, "pilot_n_openings": 2, "plies": 4},
        "C2": {
            "game_date_min": "2024-08-01",
            "bands": [{"name": n, "profile": PROFILES[n]} for n in BANDS],
            "positions_per_band": 6,
            "max_positions_per_game": 2,
            "ply_min": 4,
            "ply_max": 20,
            **c2,
        },
    }
    prereg_path, default_path = directory / "prereg.yaml", directory / "default.yaml"
    prereg_path.write_text(yaml.safe_dump(prereg), encoding="utf-8")
    default_path.write_text(yaml.safe_dump({"topics": {"fit_games": 8}}), encoding="utf-8")
    return prereg_path, default_path


def run_cli(index: Path, out: Path, configs: tuple[Path, Path], *extra: str, sources: bool = False):
    lines: list[str] = []
    argv = [
        "--index", str(index), "--out", str(out), "--prereg", str(configs[0]), "--default-config", str(configs[1]),
        "--created", CREATED, *extra,
    ]  # fmt: skip
    if not sources:
        argv.append("--no-update-sources")
    code = ms.main(argv, log=lines.append)
    return code, lines


@pytest.fixture(scope="module")
def small(tmp_path_factory):
    """One reduced run shared by the checks below: 2 + 6 openings, 4 x 6 C2 positions, 8 topic games."""
    base = tmp_path_factory.mktemp("small")
    index = build_small_pool(base / "pool")
    configs = write_configs(base)
    out = base / "out" / "splits.json"
    code, lines = run_cli(index, out, configs)
    assert code == 0, "\n".join(lines)
    return {"index": index, "configs": configs, "out": out, "lines": lines, "pool": base / "pool", "base": base}


def read_points(path: Path) -> list[tuple[int, int] | None]:
    """The moves of an SGF as (col, row), read with a regular expression instead of sensei.sgf."""
    import re

    out = []
    for coord in re.findall(r";[BW]\[([a-s]{0,2})\]", path.read_text(encoding="utf-8")):
        out.append(None if not coord else (ord(coord[0]) - 97, ord(coord[1]) - 97))
    return out


# ---------------------------------------------------------------------------------------- a reduced run end to end


def test_schema_and_counts(small):
    raw = json.loads(small["out"].read_text(encoding="utf-8"))
    assert list(raw) == [
        "version", "seed", "source", "created", "raw_dir", "pilot_openings", "confirmatory_openings",
        "c2_positions", "topic_games", "explainer_positions",
    ]  # fmt: skip
    assert (raw["version"], raw["seed"], raw["source"], raw["created"]) == (1, 123, "ogs", CREATED)
    assert Path(raw["raw_dir"]) == small["pool"].resolve()
    splits = harness.load_splits(small["out"])
    harness.check_disjoint(splits)
    assert harness.raw_dir(splits) == small["pool"].resolve()
    assert len(splits["pilot_openings"]) == 2 and len(splits["confirmatory_openings"]) == 6
    assert list(splits["c2_positions"]) == list(BANDS)
    assert all(len(p) == 6 for p in splits["c2_positions"].values())
    assert len(splits["topic_games"]) == 8 and len(splits["explainer_positions"]) == 10
    assert any("Wrote" in line for line in small["lines"])


def test_no_game_is_in_two_splits_or_two_bands(small):
    s = harness.load_splits(small["out"])
    groups = {
        "pilot": set(s["pilot_openings"]),
        "confirmatory": set(s["confirmatory_openings"]),
        "topics": set(s["topic_games"]),
        "explainer": {p["game"] for p in s["explainer_positions"]},
        **{f"c2 {band}": {p["game"] for p in items} for band, items in s["c2_positions"].items()},
    }
    sizes = {name: len(games) for name, games in groups.items()}
    assert sizes["pilot"] == 2 and sizes["confirmatory"] == 6 and sizes["topics"] == 8 and sizes["explainer"] == 10
    everything = [g for games in groups.values() for g in games]
    assert len(everything) == len(set(everything))  # nobody appears twice anywhere
    assert set(everything) <= {r["game"] for r in map(json.loads, small["index"].read_text().splitlines())}


def test_openings_are_distinct_alternating_and_pass_free(small):
    s = harness.load_splits(small["out"])
    raw = harness.raw_dir(s)
    seen = set()
    for game in s["pilot_openings"] + s["confirmatory_openings"]:
        moves = harness.opening_moves(game, 4, raw)  # raises on a pass or a colour out of turn
        assert [c for c, _ in moves] == ["B", "W", "B", "W"]
        points = read_points(raw / f"{game}.sgf")[:4]
        assert None not in points
        canon = min(tuple(symmetric(points, k)) for k in range(8))
        assert canon not in seen, f"{game} repeats an opening up to symmetry"
        seen.add(canon)
    assert len(seen) == 8


def test_c2_positions_are_in_band_in_range_and_capped(small):
    s = harness.load_splits(small["out"])
    raw = harness.raw_dir(s)
    for band, items in s["c2_positions"].items():
        per_game: dict[str, int] = {}
        for p in items:
            hp = harness.position_at(p["game"], p["ply"], raw)
            assert 4 <= p["ply"] <= 20
            assert hp.played != "pass"
            assert band_of_label(hp.mover_rank) == band, (band, p, hp.mover_rank)
            per_game[p["game"]] = per_game.get(p["game"], 0) + 1
        assert max(per_game.values()) <= 2
        assert len({(p["game"], p["ply"]) for p in items}) == len(items)


def test_explainer_set_has_the_four_phases_and_the_mover_rank(small):
    s = harness.load_splits(small["out"])
    raw = harness.raw_dir(s)
    items = s["explainer_positions"]
    assert [p["phase"] for p in items] == ["opening"] * 2 + ["middle"] * 4 + ["end"] * 2 + ["only_move_candidate"] * 2
    assert len({p["game"] for p in items}) == 10
    for p in items:
        hp = harness.position_at(p["game"], p["ply"], raw)
        assert p["declared_rank"] == hp.mover_rank
        assert -19 <= numeric(hp.mover_rank) <= 5  # on the app's dropdown, 20k to 5d
        assert hp.played != "pass"
        if p["phase"] == "opening":
            assert 10 <= p["ply"] < 50
        elif p["phase"] == "middle":
            assert 50 <= p["ply"] <= 149
        elif p["phase"] == "end":
            assert 150 <= p["ply"] <= 200
        else:
            assert 20 <= p["ply"] <= 200


class MiniBoard:
    """A tiny Go board with captures, written for this test; no ko or suicide rules."""

    def __init__(self) -> None:
        self.stones: dict[tuple[int, int], str] = {}

    @staticmethod
    def around(p):
        c, r = p
        return [(x, y) for x, y in ((c + 1, r), (c - 1, r), (c, r + 1), (c, r - 1)) if 0 <= x < 19 and 0 <= y < 19]

    def group(self, p):
        color, stones, todo, libs = self.stones[p], {p}, [p], set()
        while todo:
            for q in self.around(todo.pop()):
                if q not in self.stones:
                    libs.add(q)
                elif self.stones[q] == color and q not in stones:
                    stones.add(q)
                    todo.append(q)
        return stones, libs

    def play(self, color, p):
        """Place a stone; returns the number of captured stones."""
        self.stones[p] = color
        captured = 0
        for q in self.around(p):
            if q in self.stones and self.stones[q] != color:
                group, libs = self.group(q)
                if not libs:
                    captured += len(group)
                    for g in group:
                        del self.stones[g]
        return captured


def previous_move_forces(points, ply: int) -> bool:
    board = MiniBoard()
    captured = 0
    for i, p in enumerate(points[:ply]):
        if p is not None:
            captured = board.play("B" if i % 2 == 0 else "W", p)
    last = points[ply - 1]
    if last is None:
        return False
    if captured:
        return True
    enemy = "W" if (ply - 1) % 2 == 0 else "B"
    return any(board.stones.get(q) == enemy and len(board.group(q)[1]) == 1 for q in board.around(last))


def test_only_move_candidates_follow_a_capture_or_an_atari(small):
    s = harness.load_splits(small["out"])
    raw = harness.raw_dir(s)
    only = [p for p in s["explainer_positions"] if p["phase"] == "only_move_candidate"]
    assert len(only) == 2
    for p in only:
        assert previous_move_forces(read_points(raw / f"{p['game']}.sgf"), p["ply"]), p


def test_the_same_inputs_give_the_same_bytes_and_another_seed_gives_another_draw(small, tmp_path):
    again = tmp_path / "again.json"
    code, _ = run_cli(small["index"], again, small["configs"])
    assert code == 0 and again.read_bytes() == small["out"].read_bytes()
    other = tmp_path / "other.json"
    code, _ = run_cli(small["index"], other, small["configs"], "--seed", "124")
    assert code == 0
    a, b = json.loads(small["out"].read_text()), json.loads(other.read_text())
    assert a["seed"] == 123 and b["seed"] == 124
    assert a["confirmatory_openings"] != b["confirmatory_openings"] and a["topic_games"] != b["topic_games"]


def test_a_missing_created_stamp_is_filled_with_the_current_time(small, tmp_path):
    out = tmp_path / "now.json"
    code = ms.main(
        ["--index", str(small["index"]), "--out", str(out), "--prereg", str(small["configs"][0]),
         "--default-config", str(small["configs"][1]), "--no-update-sources"],
        log=lambda _line: None,
    )  # fmt: skip
    assert code == 0
    stamp = json.loads(out.read_text())["created"]
    assert stamp.startswith("20") and ("+" in stamp[10:] or "-" in stamp[10:] or stamp.endswith("Z"))


def test_openings_that_repeat_an_earlier_one_up_to_symmetry_are_skipped_and_counted(tmp_path):
    index = build_small_pool(tmp_path / "pool", shared_every=2)  # half of the games start alike
    cfg = ms.load_config(*write_configs(tmp_path))
    games, _ = ms.load_pool(index, cfg.min_date)
    drawn = ms.draw_splits(games, cfg, created=CREATED, raw_dir="x")
    picked = drawn.splits["pilot_openings"] + drawn.splits["confirmatory_openings"]
    shared = min(tuple(symmetric(SHARED_OPENING, k)) for k in range(8))
    raw = index.parent
    in_class = [
        g for g in picked if min(tuple(symmetric(read_points(raw / f"{g}.sgf")[:4], k)) for k in range(8)) == shared
    ]
    assert len(picked) == 8 and len(in_class) <= 1
    assert drawn.duplicate_openings >= 1  # thirty games start alike: some of them were met and passed over


# ------------------------------------------------------------------------------------------------- not enough games


def test_a_band_without_players_fails_with_a_clear_message(tmp_path):
    ranks = [pair for pair in RANK_PAIRS if "d" not in pair[0] + pair[1]]  # nobody in 1d-3d
    index = build_small_pool(tmp_path / "pool", ranks=ranks)
    out = tmp_path / "splits.json"
    code, lines = run_cli(index, out, write_configs(tmp_path))
    text = "\n".join(lines)
    assert code == 2 and not out.exists()
    assert "Not enough games to fill the splits: 60 usable games" in text
    assert "c2_positions 1d-3d: 0 of 6 positions" in text
    assert "with a player ranked 1d-3d" in text and "no estimate" in text
    assert "fetch_games.py --target" in text and "Nothing was written." in text
    assert "c2_positions 15k-10k" not in text.split("Not enough games")[1]  # the other bands are fine


def test_a_scarce_band_reports_games_to_fetch_and_the_share_of_such_games(tmp_path):
    ranks = [pair for pair in RANK_PAIRS if "d" not in pair[0] + pair[1]]
    special = {11: ("1d", "10k"), 12: ("1d", "10k")}  # two games with a dan player: 2 positions each, 4 of the 6 needed
    index = build_small_pool(tmp_path / "pool", ranks=ranks, special=special)
    code, lines = run_cli(index, tmp_path / "splits.json", write_configs(tmp_path))
    text = "\n".join(lines)
    assert code == 2
    assert "c2_positions 1d-3d: 4 of 6 positions (from 2 games, at most 2 per game)" in text
    assert "Need about 1 more games with a player ranked 1d-3d; they are 3.3% of the games fetched" in text
    assert "fetch about 30 more games in total" in text
    assert "--target 90" in text  # 60 games now plus 30


def test_openings_run_short_when_every_game_starts_alike(tmp_path):
    rng = random.Random(3)
    pool = Pool(tmp_path / "pool")
    for i in range(1, 61):
        black, white = RANK_PAIRS[i % len(RANK_PAIRS)]
        pool.add(i, random_moves(rng, 70, prefix=symmetric(SHARED_OPENING, i % 8)), black, white)
    code, lines = run_cli(pool.finish(), tmp_path / "splits.json", write_configs(tmp_path))
    text = "\n".join(lines)
    assert code == 2
    assert "openings (pilot + confirmatory): 1 of 8 games" in text and "no other selected game has" in text


def test_the_other_splits_report_what_is_left_after_the_bands(tmp_path):
    index = build_small_pool(tmp_path / "pool", n_games=36)  # the bands and the explainer take most of it
    cfg = ms.load_config(*write_configs(tmp_path))
    games, skipped = ms.load_pool(index, cfg.min_date)
    drawn = ms.draw_splits(games, cfg, created=CREATED, raw_dir="x")
    assert not skipped and len(games) == 36
    topic = [s for s in drawn.shortages if s.split == "topic_games"]
    assert topic and topic[0].need == 8 and topic[0].have == len(drawn.splits["topic_games"]) < 8
    splits = drawn.splits
    c2_games = {p["game"] for items in splits["c2_positions"].values() for p in items}
    used = [*splits["pilot_openings"], *splits["confirmatory_openings"], *splits["topic_games"], *c2_games]
    used += [p["game"] for p in splits["explainer_positions"]]
    assert len(used) == len(set(used)) <= 36  # the shortfall comes from the pool running out, not from reuse


# ------------------------------------------------------------------------------------------------ files and options


def test_existing_splits_are_not_overwritten_without_force(small, tmp_path):
    out = tmp_path / "splits.json"
    out.write_text("{}", encoding="utf-8")
    code, lines = run_cli(small["index"], out, small["configs"])
    assert code == 2 and "already exists" in lines[0] and out.read_text() == "{}"
    code, _ = run_cli(small["index"], out, small["configs"], "--force")
    assert code == 0 and out.read_bytes() == small["out"].read_bytes()


def test_dry_run_checks_and_writes_nothing(small, tmp_path):
    out = tmp_path / "splits.json"
    code, lines = run_cli(small["index"], out, small["configs"], "--dry-run")
    assert code == 0 and "Dry run: the games on disk are enough" in "\n".join(lines)
    assert not out.exists() and not (tmp_path / "splits_summary.md").exists()
    ranks = [pair for pair in RANK_PAIRS if "d" not in pair[0] + pair[1]]
    index = build_small_pool(tmp_path / "pool", ranks=ranks)
    code, lines = run_cli(index, out, small["configs"], "--dry-run")
    assert code == 2 and "Not enough games" in "\n".join(lines)


def test_summary_lists_counts_dates_bands_and_ranks(small):
    text = (small["out"].parent / "splits_summary.md").read_text(encoding="utf-8")
    for heading in ("# Splits summary", "## Counts per split", "## C2 positions per band", "## Explainer positions",
                    "## Rank distribution"):  # fmt: skip
        assert heading in text
    for band, profile in PROFILES.items():
        assert f"| c2_positions {band} ({profile}) | 6 | 6 |" in text
    assert "| pilot_openings | 2 | 2 | 2 |" in text and "| confirmatory_openings | 6 | 6 | 6 |" in text
    assert "| topic_games | 8 | 8 | 8 |" in text and "| explainer_positions | 10 | 10 | 10 |" in text
    assert "games read: 60" in text and "2025-01-" in text  # the date range of the synthetic pool
    assert "| Rank | pool (players) |" in text and "| 12k |" in text and "| 2d |" in text
    assert "seed: 123" in text and CREATED in text


SOURCES = (
    "# Data sources\r\n\r\nbefore\r\n\r\n<!-- splits:begin -->\r\nold table\r\n<!-- splits:end -->\r\n\r\nafter\r\n"
)


def test_sources_block_is_replaced_between_the_markers_and_line_endings_survive(small, tmp_path):
    sources = tmp_path / "SOURCES.md"
    sources.write_bytes(SOURCES.encode("utf-8"))
    out = tmp_path / "splits.json"
    code, lines = run_cli(small["index"], out, small["configs"], "--sources", str(sources), sources=True)
    raw = sources.read_bytes().decode("utf-8")
    assert code == 0 and "Updated the splits table" in "\n".join(lines)
    assert raw.startswith("# Data sources\r\n\r\nbefore\r\n\r\n<!-- splits:begin -->\r\n")
    assert raw.endswith("<!-- splits:end -->\r\n\r\nafter\r\n") and "old table" not in raw
    assert "\r\n" in raw and "\n" not in raw.replace("\r\n", "")  # no bare line feeds crept in
    assert "| c2_positions 1d-3d (rank_2d) | 6 | 6 |" in raw and "from 60 usable games" in raw
    code, _ = run_cli(small["index"], out, small["configs"], "--sources", str(sources), "--force", sources=True)
    again = sources.read_bytes().decode("utf-8")
    assert code == 0 and again == raw and again.count("<!-- splits:begin -->") == 1  # replaced, not appended to


def test_sources_next_to_the_output_are_found_by_default_and_missing_markers_only_warn(small, tmp_path):
    out_dir = tmp_path / "data"
    out_dir.mkdir()
    (out_dir / "SOURCES.md").write_text("no markers here\n", encoding="utf-8")
    out = out_dir / "splits.json"
    code, lines = run_cli(small["index"], out, small["configs"], "--sources", str(out_dir / "SOURCES.md"), sources=True)
    assert code == 0 and "has no <!-- splits:begin -->" in "\n".join(lines)
    assert (out_dir / "SOURCES.md").read_text() == "no markers here\n"
    (out_dir / "SOURCES.md").write_text("x\n<!-- splits:begin -->\n<!-- splits:end -->\n", encoding="utf-8")
    code, lines = run_cli(small["index"], out, small["configs"], "--force", sources=True)  # no --sources: same folder
    assert code == 0 and "Updated the splits table" in "\n".join(lines)
    assert "| topic_games | 8 | 8 | 8 |" in (out_dir / "SOURCES.md").read_text()


def test_speed_filter(small, tmp_path):
    pool = tmp_path / "pool"
    shutil.copytree(small["pool"], pool)
    rows = [json.loads(line) for line in (pool / "index.jsonl").read_text().splitlines()]
    for i, row in enumerate(rows):
        row["speed"] = "correspondence" if i % 2 else "live"
    (pool / "index.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    cfg = ms.load_config(*small["configs"])
    games, skipped = ms.load_pool(pool / "index.jsonl", cfg.min_date, ["live"])
    assert len(games) == 30 and skipped["speed_not_selected"] == 30
    assert {g.speed for g in games} == {"live"}
    code, lines = run_cli(pool / "index.jsonl", tmp_path / "s.json", small["configs"], "--speeds", "live,rapid")
    assert code == 2 and "30 usable games" in "\n".join(lines)  # 30 games cannot fill the reduced config's 38 slots
    text = "\n".join(lines)
    assert "speed_not_selected" in text


# ---------------------------------------------------------------------------------------------------- reading games


def test_unusable_games_are_left_out_with_a_reason(tmp_path):
    rng = random.Random(9)
    pool = Pool(tmp_path / "pool")
    for i in (1, 2, 3):
        pool.add(i, random_moves(rng, 62), "12k", "11k")
    pool.add(101, random_moves(rng, 62), "12k", "11k", date="2024-07-31")  # before the HumanSL release
    pool.add(102, [(i % 9, i // 9) for i in range(62)], "12k", "11k", size=9)
    pool.add(103, random_moves(rng, 62), "12k", "11k", handicap=2)
    pool.add(104, random_moves(rng, 62), "12k", "11k", date="")
    (pool.dir / "ogs-105.sgf").write_text("(;GM[1]SZ[19];B[pd", encoding="utf-8")  # cut off
    pool.rows.append({"game": "ogs-105", "id": 105, "date": "2025-03-01"})
    pool.rows.append({"game": "ogs-106", "id": 106, "date": "2025-03-01"})  # the file is missing
    index = pool.finish(extra_lines=["{not json", ""])
    games, skipped = ms.load_pool(index, "2024-08-01")
    assert [g.name for g in games] == ["ogs-1", "ogs-2", "ogs-3"]
    assert dict(skipped) == {
        "before_min_date": 1, "not_19x19": 1, "handicap_or_setup": 1, "no_date": 1, "unreadable_sgf": 1,
        "missing_sgf": 1, "unreadable_index_line": 1,
    }  # fmt: skip


def test_a_c2_reserve_is_drawn_on_request_and_every_run_says_which_it_is(tmp_path):
    index = build_small_pool(tmp_path / "pool")
    configs = write_configs(tmp_path)  # the analysis uses 6 positions per band
    out = tmp_path / "splits.json"
    code, plain = run_cli(index, out, configs, "--dry-run")  # these configs set no splits.c2_reserve_factor
    assert code == 0 and any("6, no reserve" in line and "records a deviation" in line for line in plain)
    code, fewer = run_cli(index, out, configs, "--dry-run", "--positions-per-band", "4")
    assert code == 0 and any("4, fewer than the 6 that C2.positions_per_band asks for" in line for line in fewer)
    code, more = run_cli(index, out, configs, "--positions-per-band", "10")
    note = "C2 candidates per band: 10, which is 4 more than the 6 positions analysed"
    assert code == 0 and any(note in line for line in more)
    sizes = {band: len(items) for band, items in harness.load_splits(out)["c2_positions"].items()}
    assert sizes == dict.fromkeys(BANDS, 10)
    assert note in (tmp_path / "splits_summary.md").read_text(encoding="utf-8")


def test_games_the_harness_cannot_build_a_position_from_are_left_out(tmp_path, monkeypatch):
    assert ms.katago_rules is getattr(harness, "katago_rules", None)  # wired to the harness when it has the function
    rng = random.Random(4)
    pool = Pool(tmp_path / "pool")
    for number, rules in ((1, "Japanese"), (2, "Ing"), (3, "NZ"), (4, "AGA")):
        pool.add(number, random_moves(rng, 62), "12k", "11k", rules=rules)
    no_komi = pool.dir / "ogs-4.sgf"
    no_komi.write_text(no_komi.read_text(encoding="utf-8").replace("KM[6.5]", ""), encoding="utf-8")
    index = pool.finish()

    def newer_harness(rules: str) -> str:  # the shape of eval.splits.katago_rules: ValueError without a preset
        if rules.lower() not in {"japanese", "nz", "aga"}:
            raise ValueError(f"rules {rules!r} have no KataGo preset")
        return rules.lower()

    monkeypatch.setattr(ms, "katago_rules", None)  # an older eval/splits.py: only a missing komi matters
    games, skipped = ms.load_pool(index, "2024-08-01")
    assert [g.name for g in games] == ["ogs-1", "ogs-2", "ogs-3"] and dict(skipped) == {"rules_or_komi": 1}
    monkeypatch.setattr(ms, "katago_rules", newer_harness)
    games, skipped = ms.load_pool(index, "2024-08-01")
    assert [g.name for g in games] == ["ogs-1", "ogs-3"] and dict(skipped) == {"rules_or_komi": 2}
    assert ms.playable("Ing", 7.5) is False and ms.playable("AGA", 7.5) is True and ms.playable("AGA", None) is False


def test_the_index_may_hold_a_game_twice_and_the_last_line_wins(tmp_path):
    pool = Pool(tmp_path / "pool")
    pool.add(1, random_moves(random.Random(1), 62), "12k", "11k", date="2025-03-01")
    first = dict(pool.rows[0])
    pool.rows.append({**first, "speed": "rapid"})
    games, skipped = ms.load_pool(pool.finish(), "2024-08-01")
    assert [g.name for g in games] == ["ogs-1"] and games[0].speed == "rapid" and not skipped


def test_a_missing_index_is_reported_with_the_way_out(tmp_path):
    lines: list[str] = []
    code = ms.main(["--index", str(tmp_path / "nope.jsonl"), "--out", str(tmp_path / "s.json")], log=lines.append)
    assert code == 2 and "run scripts/fetch_games.py first" in lines[0]


def test_what_the_fetcher_writes_is_what_make_splits_reads(tmp_path):
    fixture = json.loads((ROOT / "tests" / "fixtures" / "ogs" / "game_resign.json").read_text(encoding="utf-8"))

    def fake_ogs(url: str):
        game_id = int(url.rsplit("/", 1)[1])
        if game_id % 3 == 0:
            return fg.HttpResponse(404, {}, b'{"detail":"Not found."}')
        return fg.HttpResponse(200, {}, json.dumps({**fixture, "id": game_id}).encode("utf-8"))

    out = tmp_path / "ogs"
    code = fg.main(
        ["--out", str(out), "--target", "4", "--id-range", "100", "199", "--rate", "1000", "--min-date", "2024-08-01"],
        http_get=fake_ogs, sleep=lambda _s: None, log=lambda _line: None,
    )  # fmt: skip
    assert code == 0
    games, skipped = ms.load_pool(out / "index.jsonl", "2024-08-01")
    assert len(games) == 4 and not skipped
    first = games[0]
    assert (first.black_rank, first.white_rank, first.komi, first.rules) == ("16k", "15k", 6.5, "japanese")
    assert first.date == "2024-10-27" and first.speed == "live" and len(first.moves) == 71
    assert [m for _, m in first.moves[:3]] == [gtp_of(*p) for p in read_points(out / f"{first.name}.sgf")[:3]]


# --------------------------------------------------------------------------------------------------------- helpers


def pool_game(points, black="12k", white="11k", name="ogs-1"):
    moves = tuple(("B" if i % 2 == 0 else "W", "pass" if p is None else gtp_of(*p)) for i, p in enumerate(points))
    return ms.PoolGame(name, "2025-03-01", "live", moves, black, white, 6.5, "japanese")


def test_canonical_opening_identifies_rotations_and_reflections():
    base = [gtp_of(*p) for p in SHARED_OPENING] + [gtp_of(9, 9)]
    forms = {ms.canonical_opening([gtp_of(*p) for p in symmetric(SHARED_OPENING + [(9, 9)], k)]) for k in range(8)}
    assert len(forms) == 1 and forms == {ms.canonical_opening(base)}
    other = [gtp_of(*p) for p in SHARED_OPENING] + [gtp_of(9, 8)]
    assert ms.canonical_opening(other) != ms.canonical_opening(base)
    assert ms.canonical_opening(["Q16", "D4"]) != ms.canonical_opening(["Q16", "Q4"])  # a different shape, not a flip


def test_opening_of_needs_alternation_no_pass_and_enough_moves():
    rng = random.Random(2)
    good = pool_game(random_moves(rng, 20))
    assert ms.opening_of(good, 12) == tuple(m for _, m in good.moves[:12])
    assert ms.opening_of(pool_game(random_moves(rng, 11)), 12) is None
    assert ms.opening_of(pool_game(random_moves(rng, 20, passes={5})), 12) is None
    assert ms.opening_of(pool_game(random_moves(rng, 20, passes={15})), 12) is not None  # a late pass is fine
    swapped = ms.PoolGame("ogs-2", "2025-03-01", "", (("W", "D4"), ("B", "Q16"), *good.moves[2:20]), "", "", 6.5, "")
    assert ms.opening_of(swapped, 12) is None


# A white stone on D4 is put into atari (move 4), captured (move 6); then a pass; then a black self-atari.
FORCING = [
    (3, 14),  # 0  B D5
    (3, 15),  # 1  W D4
    (2, 15),  # 2  B C4
    (15, 3),  # 3  W Q16
    (4, 15),  # 4  B E4: D4 has one liberty left (D3)
    (15, 4),  # 5  W Q15
    (3, 16),  # 6  B D3: captures D4
    None,  # 7  W pass
    (15, 15),  # 8  B Q4
    (0, 16),  # 9  W A3
    (15, 16),  # 10 B Q3
    (1, 17),  # 11 W B2
    (0, 17),  # 12 B A2: its own group has one liberty (A1); no white group is in atari
]


@pytest.mark.parametrize(
    ("ply", "expected"),
    [(1, None), (4, None), (5, "atari"), (6, None), (7, "capture"), (8, None), (9, None), (13, None)],
)
def test_forcing_previous_move_finds_ataris_and_captures(ply, expected):
    game = pool_game(FORCING)
    assert ms.forcing_previous_move(game, ply) == expected
    assert previous_move_forces(FORCING, ply) == (expected is not None)  # the test's own board agrees


@pytest.mark.parametrize(
    ("black", "white", "usable"),
    [
        ("12k", "11k", True),
        ("20k", "5d", True),
        ("21k", "25k", False),
        ("6d", "7d", False),
        ("21k", "1d", True),
        ("6d", "?", False),
    ],
)
def test_declared_ranks_stay_on_the_apps_dropdown_from_20k_to_5d(black, white, usable):
    game = pool_game(random_moves(random.Random(4), 80), black, white)
    hit = ms._explainer_choice(game, "middle", None, list(BANDS), random.Random(1))
    assert (hit is not None) == usable
    if hit:
        assert hit[1] in (black, white) and -19 <= numeric(hit[1]) <= 5
        assert game.rank_of(game.moves[hit[0]][0]) == hit[1]


def test_a_wanted_band_restricts_the_mover():
    game = pool_game(random_moves(random.Random(4), 80), "12k", "8k")
    ply, label = ms._explainer_choice(game, "middle", "9k-5k", list(BANDS), random.Random(1))
    assert label == "8k" and game.moves[ply][0] == "W"
    assert ms._explainer_choice(game, "middle", "1d-3d", list(BANDS), random.Random(1)) is None


def test_an_illegal_record_is_refused_and_its_game_is_not_used_for_an_only_move_position(monkeypatch):
    monkeypatch.setitem(ms.PHASE_PLIES, "only_move_candidate", (1, 12))  # the scripted game is short
    bands = list(BANDS)
    assert ms._explainer_choice(pool_game(FORCING), "only_move_candidate", None, bands, random.Random(1)) == (5, "11k")
    cheat = pool_game([FORCING[0], FORCING[0], *FORCING[2:]])  # White's first move lands on a black stone
    with pytest.raises(ValueError):
        ms.forcing_previous_move(cheat, 3)
    assert ms._explainer_choice(cheat, "only_move_candidate", None, bands, random.Random(1)) is None


def test_dump_splits_is_valid_json_with_one_position_per_line():
    splits = {
        "version": 1, "seed": 5, "source": "ogs", "created": CREATED, "raw_dir": "data/raw/ogs",
        "pilot_openings": ["ogs-1", "ogs-2"], "confirmatory_openings": [],
        "c2_positions": {"15k-10k": [{"game": "ogs-3", "ply": 30}, {"game": "ogs-3", "ply": 42}], "9k-5k": []},
        "topic_games": ["ogs-4"],
        "explainer_positions": [{"game": "ogs-5", "ply": 12, "phase": "opening", "declared_rank": "9k"}],
    }  # fmt: skip
    text = ms.dump_splits(splits)
    assert json.loads(text) == splits and text.endswith("}\n") and "\r" not in text
    assert '      {"game": "ogs-3", "ply": 30},' in text and '    "9k-5k": []' in text
    assert '  "pilot_openings": ["ogs-1", "ogs-2"],' in text
    assert json.loads(ms.dump_splits({**splits, "explainer_positions": []}))["explainer_positions"] == []


def test_dump_splits_wraps_long_lists_of_names_and_leaves_short_ones_alone():
    names = [f"ogs-{80_000_000 + 7 * n}" for n in range(200)]
    splits = {"version": 1, "pilot_openings": names[:3], "confirmatory_openings": names, "topic_games": names[3:]}
    text = ms.dump_splits(splits)
    lines = text.splitlines()
    assert json.loads(text) == splits  # same content in the same order
    assert max(len(line) for line in lines) <= ms.JSON_WIDTH
    assert '  "pilot_openings": ["ogs-80000000", "ogs-80000007", "ogs-80000014"],' in lines
    start = lines.index('  "confirmatory_openings": [')
    block = lines[start + 1 : lines.index("  ],", start)]
    assert 1 < len(block) < len(names) // 3  # wrapped, and each line holds several names
    assert all(line.startswith("    ") and line.endswith(",") for line in block[:-1]) and not block[-1].endswith(",")
    wide = "x" * 300  # an over-wide item stands on a line of its own
    assert ms._wrapped([wide, "y"], "  ") == [f'  "{wide}",', '  "y"']


def test_the_generator_does_not_depend_on_the_hash_seed():
    # A regression value: it changes only if rng_for or the hashing scheme changes, and then every split would change.
    assert ms.rng_for(20261027, "c2", "15k-10k").random() == pytest.approx(0.22241291030550625, abs=1e-12)
    assert ms.rng_for(1, "a").random() != ms.rng_for(1, "b").random() != ms.rng_for(2, "b").random()


def test_the_repository_configs_give_the_documented_sizes():
    cfg = ms.load_config()
    assert (cfg.seed, cfg.min_date) == (20261027, "2024-08-01")
    assert [b.name for b in cfg.bands] == list(BANDS) and [b.profile for b in cfg.bands] == list(PROFILES.values())
    assert (cfg.c2_target, cfg.positions_per_band) == (500, 1000)  # splits.c2_reserve_factor 2 in default.yaml
    assert (cfg.max_per_game, cfg.ply_min, cfg.ply_max) == (5, 20, 200)
    assert (cfg.pilot_openings, cfg.openings, cfg.opening_plies, cfg.topic_games, cfg.explainer_total) == (
        20,
        200,
        12,
        300,
        10,
    )
    with pytest.raises(FileNotFoundError):
        ms.load_config(ROOT / "configs" / "nope.yaml")


# ------------------------------------------------------------------------------------------------------ full size


def test_the_documented_sizes_can_be_drawn_from_about_fifteen_hundred_games(tmp_path):
    """20 + 200 openings, 4 x 1000 C2 candidates (500 analysed plus the default reserve; at most 5 per game), 300
    topic games and 10 explainer positions."""
    rng = random.Random(11)
    pool = Pool(tmp_path / "pool")
    pairs = [
        ("12k", "11k"),
        ("8k", "7k"),
        ("3k", "2k"),
        ("1d", "2d"),
        ("13k", "14k"),
        ("6k", "5k"),
        ("1k", "1d"),
        ("4d", "5d"),
    ]
    for i in range(1, 1501):
        black, white = pairs[i % len(pairs)]
        if i % 20 == 0:
            moves = random_moves(rng, 160 + i % 60)  # legal and long: the end and only-move explainer positions
        else:
            moves = scattered_moves(rng, 62 + i % 50)
        pool.add(i, moves, black, white, date=f"2025-0{1 + i % 9}-1{i % 9}")
    index = pool.finish()
    out = tmp_path / "out" / "splits.json"
    lines: list[str] = []
    code = ms.main(
        ["--index", str(index), "--out", str(out), "--created", CREATED, "--no-update-sources"], log=lines.append
    )
    assert code == 0, "\n".join(lines)
    s = harness.load_splits(out)
    harness.check_disjoint(s)
    assert s["seed"] == 20261027 and s["source"] == "ogs"
    assert (len(s["pilot_openings"]), len(s["confirmatory_openings"])) == (20, 200)
    assert {band: len(p) for band, p in s["c2_positions"].items()} == dict.fromkeys(BANDS, 1000)
    assert len(s["topic_games"]) == 300 and len(s["explainer_positions"]) == 10
    raw = harness.raw_dir(s)
    for items in s["c2_positions"].values():
        counts: dict[str, int] = {}
        for p in items:
            counts[p["game"]] = counts.get(p["game"], 0) + 1
        assert max(counts.values()) <= 5
    for game in s["pilot_openings"] + s["confirmatory_openings"]:
        harness.opening_moves(game, 12, raw)
    sample = s["c2_positions"]["1d-3d"][0]
    assert band_of_label(harness.position_at(sample["game"], sample["ply"], raw).mover_rank) == "1d-3d"
