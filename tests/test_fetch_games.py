"""scripts/fetch_games.py, offline: no test touches the network (the HTTP layer is injected).

The JSON fixtures in tests/fixtures/ogs are RECONSTRUCTIONS of the structure read from the live OGS API on 2026-10-29
(invented players and moves, real field names, a few real rating values), not copies of live responses.
"""

from __future__ import annotations

import copy
import gzip
import importlib.util
import io
import itertools
import json
import math
import sys
import urllib.error
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

pytest.importorskip("sensei.sgf")
from sensei.ranks import label_to_numeric  # noqa: E402
from sensei.sgf import read_game  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "ogs"


def load_script(name: str):
    """Import scripts/<name>.py; dataclasses with postponed annotations need the module in sys.modules."""
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


fg = load_script("fetch_games")

GTP_COLUMNS = "ABCDEFGHJKLMNOPQRST"
MIN_DATE = date(2024, 8, 1)
FILTERS = fg.Filters(min_date=MIN_DATE)


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def mutate(base: dict, **changes) -> dict:
    """A deep copy with dotted-path values set, e.g. mutate(g, **{"players.white.ui_class": "bot"}); None is a value."""
    data = copy.deepcopy(base)
    for path, value in changes.items():
        keys = path.split(".")
        node = data
        for key in keys[:-1]:
            node = node[key]
        node[keys[-1]] = value
    return data


def without(base: dict, *paths: str) -> dict:
    data = copy.deepcopy(base)
    for path in paths:
        keys = path.split(".")
        node = data
        for key in keys[:-1]:
            node = node[key]
        del node[keys[-1]]
    return data


def gtp(x: int, y: int) -> str:
    """GTP point of an OGS move: x is the column from the left, y the row from the top (independent of the script)."""
    return "pass" if (x, y) == (-1, -1) else f"{GTP_COLUMNS[x]}{19 - y}"


# ---------------------------------------------------------------------------------------------------------- ranks


@pytest.mark.parametrize(
    ("rating", "api_rank"),
    [  # rating and rank pairs read from the API (historical_ratings with gamedata.players.*.rank, and current values)
        (627.6884765625, 4.135648160035635),
        (307.8206481933594, -12.35934501840371),
        (984.4461669921875, 14.553963440966106),
        (1030.420166015625, 15.610592008603822),
        (943.2897338867188, 13.565326592156222),
        (1500.0, 24.303382182144386),
        (697.860595703125, 6.5889775923198854),
        (753.2694702148438, 8.357723821311986),
        (1125.8827017028436, 17.66169912212786),
        (828.3973151289197, 10.55859058940781),
    ],
)
def test_rating_to_rank_matches_the_api(rating, api_rank):
    assert fg.rating_to_rank(rating) == pytest.approx(api_rank, abs=1e-5)
    assert fg.rank_to_rating(api_rank) == pytest.approx(rating, rel=1e-6)


def test_rating_to_rank_clamps_the_rating():
    assert fg.rating_to_rank(10.0) == fg.rating_to_rank(100.0) == pytest.approx(math.log(100 / 525) * 23.15)
    assert fg.rating_to_rank(10_000.0) == fg.rating_to_rank(6000.0)


@pytest.mark.parametrize(
    ("rank", "label"),
    [
        (29.0, "1k"), (29.999, "1k"), (30.0, "1d"), (30.999, "1d"), (31.0, "2d"), (28.999, "2k"), (28.0, "2k"),
        (20.0, "10k"), (20.5, "10k"), (19.999, "11k"), (5.0, "25k"), (5.5, "25k"), (4.0, "25k"), (-12.36, "25k"),
        (37.9, "8d"), (38.0, "9d"), (50.0, "9d"),
    ],
)  # fmt: skip
def test_rank_label_follows_ogs_display_rule(rank, label):
    assert fg.rank_label(rank) == label


def test_rating_labels_of_known_players():
    assert fg.rating_label(984.4461669921875) == "16k"
    assert fg.rating_label(1030.420166015625) == "15k"
    assert fg.rating_label(1500.0) == "6k"
    assert fg.rating_label(2012.3) == "2d"
    assert fg.rating_label(1850.7) == "1k"
    assert fg.rating_label(300.0) == "25k"  # OGS shows nothing below 25k


def test_labels_sit_on_the_sensei_rank_scale():
    for rank in (5.0, 12.3, 20.0, 28.9, 29.0, 29.9, 30.0, 33.3, 37.99):
        assert label_to_numeric(fg.rank_label(rank)) == math.floor(rank) - 29


def test_bands():
    bands = ["15k-10k", "9k-5k", "4k-1k", "1d-3d"]
    assert fg.parse_band("15k-10k") == (-14.0, -9.0)
    assert fg.parse_band("1d-3d") == (1.0, 3.0)
    assert fg.parse_band("10k-15k") == (-14.0, -9.0)
    with pytest.raises(ValueError):
        fg.parse_band("12k")
    expected = {
        "15k": "15k-10k", "12k": "15k-10k", "10k": "15k-10k", "9k": "9k-5k", "5k": "9k-5k", "4k": "4k-1k",
        "1k": "4k-1k", "1d": "1d-3d", "3d": "1d-3d", "4d": None, "16k": None, "25k": None, "?": None, "": None,
    }  # fmt: skip
    for label, band in expected.items():
        assert fg.band_of(label, bands) == band, label


# ---------------------------------------------------------------------------------------------------- ids and dates


def test_id_date_anchors_are_consistent():
    ids = [i for i, _ in fg.ID_ANCHORS]
    times = [datetime.fromisoformat(t) for _, t in fg.ID_ANCHORS]
    assert ids == sorted(set(ids)) and times == sorted(set(times))
    for game_id, when in zip(ids, times, strict=True):
        assert fg.estimate_id(when) == game_id
        assert abs((fg.estimate_date(game_id) - when).total_seconds()) < 1.0


def test_default_id_range_covers_the_date_window():
    lo, hi = fg.default_id_range(date(2024, 8, 1), datetime(2026, 10, 29, 12, tzinfo=UTC))
    assert 66_480_000 <= lo <= 66_500_000  # game 66500000 started 2024-08-01T02:55Z
    assert 91_900_000 <= hi <= 92_000_000  # two days before "now" is 2026-10-27T12:00Z, between 91929295 and 92000000
    with pytest.raises(ValueError):
        fg.default_id_range(date(2026, 10, 29), datetime(2026, 10, 29, 12, tzinfo=UTC))


def test_sample_ids_is_seeded_distinct_and_prefix_stable():
    a = list(zip(range(60), fg.sample_ids(1000, 10**7, seed=7), strict=False))
    b = list(zip(range(60), fg.sample_ids(1000, 10**7, seed=7), strict=False))
    c = list(zip(range(60), fg.sample_ids(1000, 10**7, seed=8), strict=False))
    ids = [i for _, i in a]
    assert ids == [i for _, i in b] and ids != [i for _, i in c]
    assert len(set(ids)) == 60 and all(1000 <= i <= 10**7 for i in ids)
    assert sorted(fg.sample_ids(5, 9, seed=1)) == [5, 6, 7, 8, 9]  # a small range is exhausted exactly once


def test_game_names():
    assert fg.game_name(69000000) == "ogs-69000000"
    assert fg.parse_game_name("ogs-12") == 12
    for bad in ("ogs-", "kgs-12", "ogs-1a", "12"):
        with pytest.raises(ValueError):
            fg.parse_game_name(bad)


# ---------------------------------------------------------------------------------------------- reading a game JSON


def test_recorded_games_are_accepted_with_the_expected_record():
    resign = fg.evaluate_game(69000000, fixture("game_resign"), FILTERS)
    assert isinstance(resign, fg.AcceptedGame)
    rec = resign.record
    assert (rec["game"], rec["id"], rec["date"]) == ("ogs-69000000", 69000000, "2024-10-27")
    assert (rec["black_rank"], rec["white_rank"]) == ("16k", "15k")  # from historical_ratings, not from players.*
    assert (rec["komi"], rec["rules"], rec["moves"], rec["outcome"], rec["winner"]) == (
        6.5,
        "japanese",
        71,
        "Resignation",
        "B",
    )
    assert rec["speed"] == "live" and rec["started"] == "2024-10-27T08:55:49Z"
    assert {"game", "id", "date", "black_rank", "white_rank", "komi", "rules", "moves", "outcome"} <= set(rec)
    json.dumps(rec)

    score = fg.evaluate_game(79876543, fixture("game_score_passes"), FILTERS)
    assert isinstance(score, fg.AcceptedGame)
    assert (score.record["black_rank"], score.record["white_rank"]) == ("2d", "1k")
    assert (score.record["winner"], score.record["outcome"], score.record["speed"]) == (
        "W",
        "13.5 points",
        "correspondence",
    )


@pytest.mark.parametrize(("name", "game_id"), [("game_resign", 69000000), ("game_score_passes", 79876543)])
def test_sgf_written_by_the_fetcher_round_trips_through_read_game(name, game_id):
    data = fixture(name)
    accepted = fg.evaluate_game(game_id, data, FILTERS)
    rec = read_game(accepted.sgf.encode("utf-8"))
    assert rec.size == 19 and rec.handicap == 0 and rec.setup == ()
    assert rec.komi == accepted.record["komi"] and rec.rules == accepted.record["rules"]
    assert (rec.black_rank, rec.white_rank) == (accepted.record["black_rank"], accepted.record["white_rank"])
    assert rec.date == accepted.record["date"]
    raw = data["gamedata"]["moves"]
    assert len(rec.moves) == len(raw) == accepted.record["moves"]
    for i, ((color, move), (x, y, *_)) in enumerate(zip(rec.moves, raw, strict=True)):
        assert color == ("B" if i % 2 == 0 else "W")
        assert move == gtp(x, y)


def test_passes_survive_the_round_trip_and_results_are_written():
    accepted = fg.evaluate_game(79876543, fixture("game_score_passes"), FILTERS)
    rec = read_game(accepted.sgf.encode("utf-8"))
    assert [m for _, m in rec.moves[-2:]] == ["pass", "pass"]
    assert "RE[W+13.5]" in accepted.sgf and "RU[Chinese]" in accepted.sgf and "HA[0]" in accepted.sgf
    assert "PB[" not in accepted.sgf and "PW[" not in accepted.sgf  # no user names
    assert "black_demo" not in accepted.sgf


def test_labels_come_from_the_historical_rating_not_the_current_one():
    data = mutate(
        fixture("game_resign"), **{"players.black.ratings.overall.rating": 3000.0, "players.black.ranking": 33.0}
    )
    data["historical_ratings"]["black"]["ranking"] = 33.0
    accepted = fg.evaluate_game(69000000, data, FILTERS)
    assert accepted.record["black_rank"] == "16k"


@pytest.mark.parametrize(
    ("changes", "deletions", "reason"),
    [
        ({"ended": None}, (), "not_finished"),
        ({"annulled": True}, (), "annulled"),
        ({"outcome": "Cancellation"}, (), "cancelled"),
        ({"width": 9, "height": 9}, (), "not_19x19"),
        ({"width": 13, "height": 13}, (), "not_19x19"),
        ({"handicap": 2}, (), "handicap"),
        ({"gamedata.handicap": 3}, (), "handicap"),
        ({"gamedata.initial_state": {"black": "pddp", "white": ""}}, (), "setup_stones"),
        ({"gamedata.initial_player": "white"}, (), "white_first"),
        ({"ranked": False}, (), "unranked"),
        ({"rengo": True}, (), "rengo"),
        ({"players.white.ui_class": "bot"}, (), "bot"),
        ({"players.black.ui_class": "bot aquatic-9"}, (), "bot"),
        ({"historical_ratings.black.ui_class": "supporter bot"}, (), "bot"),
        ({"players.black.professional": True}, (), "professional"),
        ({"gamedata.players.white.professional": True}, (), "professional"),
        ({"outcome": "Disconnection"}, (), "outcome:Disconnection"),
        ({"outcome": "Moderator Decision"}, (), "outcome:Moderator Decision"),
        ({"outcome": ""}, (), "outcome:empty"),
        ({"black_lost": False, "white_lost": False}, (), "no_winner"),
        ({"black_lost": True, "white_lost": True}, (), "no_winner"),
        ({"started": "2024-07-31T23:59:59.999999Z"}, (), "before_min_date"),
        ({"rules": "tromp-taylor"}, (), "rules:tromp-taylor"),
        ({"gamedata.moves": [[3, 3, 100, None, None]] * 59}, (), "too_short"),
        ({"gamedata.moves": []}, (), "too_short"),
        ({"gamedata.moves": [[19, 3, 100, None, None]] * 70}, (), "bad_moves"),
        ({"gamedata.moves": [[3, -2, 100, None, None]] * 70}, (), "bad_moves"),
        ({"gamedata.moves": [["a", 3, 100]] * 70}, (), "bad_moves"),
        ({"gamedata.moves": [[3, 3, 100, 1, None]] * 70}, (), "bad_moves"),  # Black again at ply 1
        ({"gamedata.moves": [[3, 3, 100, 0, None]] * 70}, (), "bad_moves"),  # an edit that removes a stone
        ({"historical_ratings.white.ratings.overall.deviation": 160.0}, (), "provisional"),
        ({"historical_ratings.black.ratings.overall.deviation": 350.0}, (), "provisional"),
        ({"gamedata.players.black.rank": 16.2}, (), "rank_unstable"),
        ({}, ("historical_ratings.black.ratings.overall.rating",), "no_historical_ratings"),
        ({}, ("historical_ratings",), "no_historical_ratings"),
    ],
)
def test_filters_reject_with_the_right_reason(changes, deletions, reason):
    data = without(mutate(fixture("game_resign"), **changes), *deletions)
    verdict = fg.evaluate_game(69000000, data, FILTERS)
    assert isinstance(verdict, fg.Rejected), verdict
    assert verdict.reason == reason


@pytest.mark.parametrize(
    ("changes", "check"),
    [
        ({"outcome": "Timeout"}, lambda a: "RE[B+T]" in a.sgf and a.record["outcome"] == "Timeout"),
        ({"outcome": "Resigniation"}, lambda a: "RE[B+R]" in a.sgf),  # the 2012 spelling
        ({"outcome": "0.5 points"}, lambda a: "RE[B+0.5]" in a.sgf),
        ({"black_lost": True, "white_lost": False}, lambda a: "RE[W+R]" in a.sgf and a.record["winner"] == "W"),
        ({"started": "2024-08-01T00:00:00Z"}, lambda a: a.record["date"] == "2024-08-01"),
        ({"rules": "nz"}, lambda a: "RU[NZ]" in a.sgf),
        ({"rules": "ing"}, lambda a: "RU[Ing]" in a.sgf),
        ({"rules": "aga", "komi": "7.50", "gamedata.komi": 7.0}, lambda a: "RU[AGA]" in a.sgf and "KM[7]" in a.sgf),
        ({"gamedata.moves": [[3, 3, 100, None, None]] * 60}, lambda a: a.record["moves"] == 60),
        (  # explicit colours that agree with alternate play, and an extra dict, are fine
            {"gamedata.moves": [[i % 19, 3, 100, 1 + i % 2, {"blur": 4000}] for i in range(70)]},
            lambda a: a.record["moves"] == 70,
        ),
        ({"historical_ratings.white.ratings.overall.deviation": 159.9}, lambda a: True),
        ({"players.white.ui_class": "supporter"}, lambda a: True),
        ({"players.white.username": "noob_bot"}, lambda a: True),  # a user name is not a bot marker
        ({"gamedata.players.black.rank": 14.554 + 0.045}, lambda a: True),  # the 0.045 seen in a real game
        ({"gamedata.players.black.rank": None}, lambda a: True),
    ],
)
def test_borderline_games_that_must_pass(changes, check):
    verdict = fg.evaluate_game(69000000, mutate(fixture("game_resign"), **changes), FILTERS)
    assert isinstance(verdict, fg.AcceptedGame), verdict
    assert check(verdict)


def test_provisional_players_pass_when_allowed():
    data = mutate(fixture("game_resign"), **{"historical_ratings.white.ratings.overall.deviation": 350.0})
    assert isinstance(fg.evaluate_game(1, data, fg.Filters(MIN_DATE, allow_provisional=True)), fg.AcceptedGame)


def test_min_moves_and_min_date_are_parameters():
    data = fixture("game_resign")
    assert fg.evaluate_game(1, data, fg.Filters(MIN_DATE, min_moves=72)).reason == "too_short"
    assert fg.evaluate_game(1, data, fg.Filters(date(2024, 10, 28))).reason == "before_min_date"
    assert isinstance(fg.evaluate_game(1, data, fg.Filters(date(2024, 10, 27))), fg.AcceptedGame)


@pytest.mark.parametrize("broken", [[], "text", {"id": 1}, {"started": "yesterday"}])
def test_unreadable_json_is_a_schema_error_not_a_reject(broken):
    with pytest.raises(fg.SchemaError):
        fg.evaluate_game(1, broken, FILTERS)


def test_missing_komi_is_a_schema_error():
    with pytest.raises(fg.SchemaError, match="komi"):
        fg.evaluate_game(1, without(fixture("game_resign"), "komi", "gamedata.komi"), FILTERS)
    # either place is enough
    assert isinstance(fg.evaluate_game(1, without(fixture("game_resign"), "gamedata.komi"), FILTERS), fg.AcceptedGame)
    assert isinstance(fg.evaluate_game(1, without(fixture("game_resign"), "komi"), FILTERS), fg.AcceptedGame)


@pytest.mark.parametrize("key", fg.REQUIRED_KEYS)
def test_a_finished_game_without_a_required_key_is_a_schema_error(key):
    """A renamed field must stop the run, not reject every game quietly (or, for ui_class, let bots through)."""
    with pytest.raises(fg.SchemaError, match=key):
        fg.evaluate_game(1, without(fixture("game_resign"), key), FILTERS)


def test_a_running_game_needs_none_of_the_required_keys():
    running = without(mutate(fixture("game_resign"), ended=None), "outcome", "gamedata", "black_lost", "white_lost")
    assert fg.evaluate_game(1, running, FILTERS) == fg.Rejected("not_finished")
    no_ended_key = without(fixture("game_resign"), "ended")
    assert fg.evaluate_game(1, no_ended_key, FILTERS) == fg.Rejected("not_finished")


def test_players_need_a_ui_class_field_but_it_may_be_empty():
    missing = without(fixture("game_resign"), "players.white.ui_class")
    with pytest.raises(fg.SchemaError, match="players.white.ui_class"):
        fg.evaluate_game(1, missing, FILTERS)
    empty = mutate(fixture("game_resign"), **{"players.white.ui_class": ""})
    assert isinstance(fg.evaluate_game(1, empty, FILTERS), fg.AcceptedGame)


def test_outcome_kinds():
    assert fg.outcome_kind("Resignation") == ("R", "")
    assert fg.outcome_kind("Timeout") == ("T", "")
    assert fg.outcome_kind("12.5 points") == ("S", "12.5")
    assert fg.outcome_kind("3 points") == ("S", "3")
    for other in ("", "Cancellation", "Disconnection", "points", "x points", "Abandonment"):
        assert fg.outcome_kind(other) is None


# ------------------------------------------------------------------------------------------------------------- HTTP


def ok(data) -> fg.HttpResponse:
    return fg.HttpResponse(200, {}, json.dumps(data).encode("utf-8"))


def status(code: int, body: bytes = b"", **headers: str) -> fg.HttpResponse:
    return fg.HttpResponse(code, headers, body)


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


class Script:
    """An injected getter that plays back a list of responses (or exceptions) for every URL, and records the URLs."""

    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.urls: list[str] = []

    def __call__(self, url: str) -> fg.HttpResponse:
        self.urls.append(url)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, BaseException):
            raise item
        return item


def make_client(get, clock: FakeClock | None = None, **kwargs) -> tuple[fg.Client, FakeClock]:
    clock = clock or FakeClock()
    kwargs.setdefault("rate", 1000.0)
    return fg.Client(get, sleep=clock.sleep, clock=clock.now, log=lambda _line: None, **kwargs), clock


def test_client_reads_a_game_and_builds_the_url():
    get = Script(ok({"id": 5}))
    client, _ = make_client(get, api_base="https://example.invalid/api/v1/")
    assert client.get_game(5) == ("ok", {"id": 5})
    assert get.urls == ["https://example.invalid/api/v1/games/5"]


@pytest.mark.parametrize(
    "response",
    [
        status(404, b'{"detail":"Not found."}'),
        status(410),
        status(200, b"Error: Game 68123457 not found in Scylla."),
        status(500, b"Error: Game 68123457 not found in Scylla."),
        status(400, b"Not Found"),
    ],
)
def test_missing_games_are_permanent_and_not_retried(response):
    get = Script(response)
    client, clock = make_client(get)
    assert client.get_game(68123457) == ("missing", None)
    assert len(get.urls) == 1 and clock.sleeps == []


def test_client_backs_off_exponentially_on_429_and_5xx_and_network_errors():
    get = Script(status(503), urllib.error.URLError("reset"), status(500), ok({"id": 1}))
    client, clock = make_client(get, rate=1000.0)
    assert client.get_game(1) == ("ok", {"id": 1})
    assert len(get.urls) == 4
    backoffs = [s for s in clock.sleeps if s >= 1.0]
    assert backoffs == [2.0, 4.0, 8.0]


def test_client_honours_retry_after():
    get = Script(status(429, b"", **{"retry-after": "30"}), ok({"id": 1}))
    client, clock = make_client(get)
    assert client.get_game(1)[0] == "ok"
    assert [s for s in clock.sleeps if s >= 1.0] == [30.0]


def test_client_gives_up_after_the_retries_and_caps_the_delay():
    get = Script(status(502))
    client, clock = make_client(get, retries=3, max_delay=5.0)
    with pytest.raises(fg.FetchError, match="HTTP 502"):
        client.get_game(1)
    assert len(get.urls) == 4  # the first try and three retries
    assert [s for s in clock.sleeps if s >= 1.0] == [2.0, 4.0, 5.0]


def test_client_retries_a_200_that_is_not_json_and_rejects_other_client_errors():
    client, _ = make_client(Script(status(200, b"<html>maintenance</html>"), ok({"id": 1})))
    assert client.get_game(1) == ("ok", {"id": 1})
    client, _ = make_client(Script(status(405, b"nope")))
    with pytest.raises(fg.FetchError, match="HTTP 405"):
        client.get_game(1)


@pytest.mark.parametrize("code", [401, 403])
def test_client_reports_denied_without_retrying(code):
    get = Script(status(code, b"forbidden"))
    client, _ = make_client(get)
    assert client.get_game(1) == ("denied", None)
    assert len(get.urls) == 1


def test_client_spaces_requests_at_the_given_rate():
    get = Script(ok({"id": 1}))
    client, clock = make_client(get, rate=2.0)
    for game_id in range(6):
        client.get_game(game_id)
    assert client.requests == 6
    assert clock.t == pytest.approx(5 * 0.5)  # six request starts, half a second apart
    with pytest.raises(ValueError):
        fg.Client(get, rate=0)


def test_urllib_getter_sends_the_headers_and_returns_error_statuses(monkeypatch):
    seen = {}

    class Reply:
        status = 200
        headers = {"Content-Encoding": "gzip", "X-Test": "1"}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return gzip.compress(b'{"id": 7}')

    def fake_urlopen(request, timeout):
        seen["headers"] = {k.lower(): v for k, v in request.header_items()}
        seen["timeout"] = timeout
        if request.full_url.endswith("/9"):
            raise urllib.error.HTTPError(
                request.full_url, 404, "Not Found", {"Content-Type": "text/plain"}, io.BytesIO(b"gone")
            )
        return Reply()

    monkeypatch.setattr(fg.urllib.request, "urlopen", fake_urlopen)
    get = fg.make_urllib_get("test-agent/1.0", timeout=12.0)
    response = get("https://example.invalid/api/v1/games/7")
    assert (response.status, response.body) == (200, b'{"id": 7}')
    assert seen["headers"]["user-agent"] == "test-agent/1.0" and seen["headers"]["accept"] == "application/json"
    assert seen["headers"]["accept-encoding"] == "gzip" and seen["timeout"] == 12.0
    missing = get("https://example.invalid/api/v1/games/9")
    assert (missing.status, missing.body) == (404, b"gone")


def test_user_agent_is_descriptive_and_overridable():
    args = fg.build_parser().parse_args(["--target", "1"])
    assert fg.user_agent_for(args).startswith("sensei-research/0.1 (") and "contact" not in fg.user_agent_for(args)
    args = fg.build_parser().parse_args(["--target", "1", "--contact", "https://github.com/me/sensei"])
    assert fg.user_agent_for(args).endswith("contact: https://github.com/me/sensei)")
    args = fg.build_parser().parse_args(["--target", "1", "--user-agent", "custom/2"])
    assert fg.user_agent_for(args) == "custom/2"


def test_the_default_log_flushes_so_that_redirected_output_shows_progress(monkeypatch):
    class Recorder:
        def __init__(self) -> None:
            self.written: list[str] = []
            self.flushes = 0

        def write(self, text: str) -> int:
            self.written.append(text)
            return len(text)

        def flush(self) -> None:
            self.flushes += 1

    recorder = Recorder()
    monkeypatch.setattr(sys, "stdout", recorder)
    fg.say("progress")
    assert "".join(recorder.written) == "progress\n" and recorder.flushes >= 1
    assert fg.main.__kwdefaults__["log"] is fg.say


# ------------------------------------------------------------------------------------- the whole run, with a fake OGS


class FakeOgs:
    """Serves ids from a rule: id % 5 == 0 is missing, 1 and 4 are good games, 2 is a 9x9 game, 3 is unranked."""

    def __init__(self, ok_fixture: str = "game_resign") -> None:
        self.base = fixture(ok_fixture)
        self.calls: list[int] = []
        self.overrides: dict[int, fg.HttpResponse] = {}

    def __call__(self, url: str) -> fg.HttpResponse:
        game_id = int(url.rsplit("/", 1)[1])
        self.calls.append(game_id)
        if game_id in self.overrides:
            return self.overrides[game_id]
        kind = game_id % 5
        if kind == 0:
            return status(404, b'{"detail":"Not found."}')
        data = copy.deepcopy(self.base)
        data["id"] = game_id
        if kind == 2:
            data["width"] = data["height"] = 9
        if kind == 3:
            data["ranked"] = False
        return ok(data)


def run(tmp_path, *args, fake=None, **kwargs):
    fake = fake or FakeOgs()
    lines: list[str] = []
    argv = ["--out", str(tmp_path / "ogs"), "--rate", "1000", "--min-date", "2024-08-01", *args]
    code = fg.main(argv, http_get=fake, sleep=lambda _s: None, log=lines.append, **kwargs)
    return code, fake, lines


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_run_writes_sgf_index_and_rejects(tmp_path):
    code, fake, lines = run(tmp_path, "--target", "6", "--id-range", "1000", "1999", "--seed", "3")
    out = tmp_path / "ogs"
    index, rejects = read_jsonl(out / "index.jsonl"), read_jsonl(out / "rejects.jsonl")
    assert code == 0 and len(index) == 6
    assert len({r["id"] for r in index}) == 6 and all(1000 <= r["id"] <= 1999 for r in index)
    for rec in index:
        assert {"game", "id", "date", "black_rank", "white_rank", "komi", "rules", "moves", "outcome"} <= set(rec)
        parsed = read_game((out / f"{rec['game']}.sgf").read_bytes())
        assert (parsed.black_rank, parsed.white_rank, parsed.komi, parsed.rules) == ("16k", "15k", 6.5, "japanese")
        assert rec["id"] % 5 in (1, 4)
    assert sorted(fake.calls) == sorted([r["id"] for r in index] + [r["id"] for r in rejects])
    reasons: dict[int, set[str]] = {}
    for r in rejects:
        reasons.setdefault(r["id"] % 5, set()).add(r["reason"])
    assert reasons == {0: {"not_found"}, 2: {"not_19x19"}, 3: {"unranked"}}
    assert not list(out.glob("*.tmp"))
    summary = "\n".join(lines)
    assert "accepted this run: 6" in summary and "target of 6 accepted games reached" in summary
    assert "15k-10k" in summary and "outside every band" in summary


def test_second_run_skips_seen_ids_and_a_larger_target_continues_the_sequence(tmp_path):
    args = ("--id-range", "1000", "1999", "--seed", "3")
    _, first, _ = run(tmp_path, "--target", "6", *args)
    _, second, lines = run(tmp_path, "--target", "6", *args)
    assert second.calls == []  # nothing left to do: no request at all
    assert "target of 6 accepted games reached" in "\n".join(lines)

    _, third, lines = run(tmp_path, "--target", "9", *args)
    assert third.calls and not set(third.calls) & set(first.calls)  # only unseen ids are requested
    assert len(read_jsonl(tmp_path / "ogs" / "index.jsonl")) == 9
    assert "already seen and skipped" in "\n".join(lines)

    # The sequence is the same as one uninterrupted run with the larger target.
    _, whole, _ = run(tmp_path / "fresh", "--target", "9", *args)
    assert first.calls + third.calls == whole.calls


def test_seed_decides_the_sequence(tmp_path):
    _, a, _ = run(tmp_path / "a", "--target", "3", "--id-range", "1000", "1999", "--seed", "1")
    _, b, _ = run(tmp_path / "b", "--target", "3", "--id-range", "1000", "1999", "--seed", "1")
    _, c, _ = run(tmp_path / "c", "--target", "3", "--id-range", "1000", "1999", "--seed", "2")
    assert a.calls == b.calls and a.calls != c.calls


def test_default_seed_is_the_global_seed_of_the_prereg_config(tmp_path):
    _, fake, lines = run(tmp_path, "--target", "2", "--id-range", "1000", "1999")
    assert "seed 20261027" in lines[0]
    _, expected, _ = run(tmp_path / "explicit", "--target", "2", "--id-range", "1000", "1999", "--seed", "20261027")
    assert fake.calls == expected.calls


def test_default_range_comes_from_the_dates(tmp_path):
    now = datetime(2026, 10, 29, 12, tzinfo=UTC)
    _, _, lines = run(tmp_path, "--target", "1", "--max-requests", "1", now=now)
    assert "Sampling ids 66496577..91969137" in lines[0] and "2024-08-01" in lines[0]


def test_max_requests_and_range_exhaustion_stop_the_run(tmp_path):
    code, fake, lines = run(tmp_path / "a", "--target", "50", "--id-range", "1000", "1999", "--max-requests", "7")
    assert code == 0 and len(fake.calls) == 7 and "--max-requests 7 reached" in "\n".join(lines)
    code, fake, lines = run(tmp_path / "b", "--target", "500", "--id-range", "1000", "1019")
    assert code == 0 and sorted(fake.calls) == list(range(1000, 1020)) and "id range is exhausted" in "\n".join(lines)


def read_jsonl_tolerant(path: Path) -> tuple[list[dict], int]:
    """(readable rows, number of unreadable lines)"""
    rows, damaged = [], 0
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            damaged += 1
    return rows, damaged


def test_a_run_killed_in_the_middle_of_a_line_resumes(tmp_path):
    args = ("--target", "3", "--id-range", "1000", "1999", "--seed", "3")
    _, first, _ = run(tmp_path, *args)
    index = tmp_path / "ogs" / "index.jsonl"
    before = read_jsonl(index)
    last = before[-1]["id"]
    assert first.calls[-1] == last  # the run stopped as soon as the third game was in
    index.write_bytes(index.read_bytes()[:-40])  # the last line is cut short and has no newline

    code, second, lines = run(tmp_path, *args)
    assert code == 0 and "1 unreadable lines" in "\n".join(lines)
    assert second.calls == [last]  # only the game whose index line was lost is fetched again
    rows, damaged = read_jsonl_tolerant(index)
    assert damaged == 1 and [r["id"] for r in rows] == [r["id"] for r in before]
    assert rows == before  # the new line is on a line of its own, not glued to the fragment

    code, third, lines = run(tmp_path, *args)  # and the next run is quiet
    assert code == 0 and third.calls == []


def test_an_sgf_without_an_index_line_is_fetched_again_and_the_index_gets_one_line(tmp_path):
    args = ("--target", "2", "--id-range", "1000", "1999", "--seed", "3")
    run(tmp_path, *args)
    index = tmp_path / "ogs" / "index.jsonl"
    lines_before = index.read_text(encoding="utf-8").splitlines()
    index.write_text(lines_before[0] + "\n", encoding="utf-8")  # the process died before the second append
    code, second, _ = run(tmp_path, *args)
    assert code == 0 and len(second.calls) == 1
    assert index.read_text(encoding="utf-8").splitlines() == lines_before


def test_unreadable_server_answers_are_not_recorded_and_stop_a_hopeless_run(tmp_path):
    fake = FakeOgs()
    fake.overrides = {i: status(500) for i in range(1000, 2000)}
    code, fake, lines = run(tmp_path, "--target", "5", "--id-range", "1000", "1999", "--retries", "2", fake=fake)
    assert code == 2 and "ids in a row failed" in "\n".join(lines)
    assert not (tmp_path / "ogs" / "rejects.jsonl").exists() and not (tmp_path / "ogs" / "index.jsonl").exists()
    assert len(fake.calls) == 5 * 3  # five ids, each tried three times


def test_schema_changes_stop_the_run_without_recording(tmp_path):
    fake = FakeOgs()
    renamed = without(fixture("game_resign"), "ranked")  # as if the API had renamed a field
    fake.overrides = {i: ok({**renamed, "id": i}) for i in range(1000, 2000)}
    code, fake, lines = run(tmp_path, "--target", "5", "--id-range", "1000", "1999", fake=fake)
    assert code == 2 and "unexpected JSON shapes" in "\n".join(lines)
    assert any("missing keys: ranked" in line for line in lines)
    assert len(fake.calls) == fg.MAX_CONSECUTIVE_ERRORS
    assert not (tmp_path / "ogs" / "rejects.jsonl").exists()


def test_schema_streak_counts_through_missing_ids_and_resets_on_a_readable_game(tmp_path):
    n = fg.MAX_CONSECUTIVE_ERRORS
    draws = list(itertools.islice(fg.sample_ids(1000, 1999, fg.DEFAULT_SEED), 3 * n))
    broken = without(fixture("game_resign"), "ranked")

    def serve(bad: list[int], good: list[int], gone: list[int]) -> FakeOgs:
        fake = FakeOgs()
        fake.overrides = {i: ok({**broken, "id": i}) for i in bad}
        fake.overrides |= {i: ok({**fixture("game_resign"), "id": i}) for i in good}
        fake.overrides |= {i: status(404, b'{"detail":"Not found."}') for i in gone}
        return fake

    # n-1 broken games, a readable one, n-1 broken again: never n in a row, so the run goes on
    fake = serve(draws[: n - 1] + draws[n : 2 * n - 1], [draws[n - 1]], [])
    code, fake, lines = run(
        tmp_path / "reset", "--target", "50", "--id-range", "1000", "1999", "--max-requests", str(2 * n - 1), fake=fake
    )
    assert code == 0 and "unexpected JSON shapes" not in "\n".join(lines)

    # n broken games with a not-found id in between still make n in a row
    fake = serve(draws[: n - 2] + draws[n - 1 : n + 1], [], [draws[n - 2]])
    code, fake, lines = run(tmp_path / "gap", "--target", "50", "--id-range", "1000", "1999", fake=fake)
    assert code == 2 and "unexpected JSON shapes" in "\n".join(lines)
    assert len(fake.calls) == n + 1


def test_a_block_aborts_without_polluting_the_rejects(tmp_path):
    fake = FakeOgs()
    fake.overrides = {i: status(403, b"blocked") for i in range(1000, 2000)}
    code, fake, lines = run(tmp_path, "--target", "5", "--id-range", "1000", "1999", fake=fake)
    assert code == 2 and "looks like a block" in "\n".join(lines)
    assert len(fake.calls) == fg.MAX_CONSECUTIVE_DENIED
    assert not (tmp_path / "ogs" / "rejects.jsonl").exists()


def test_isolated_refusals_are_recorded_once_the_server_answers_again(tmp_path):
    fake = FakeOgs()
    code, probe, _ = run(tmp_path / "probe", "--target", "4", "--id-range", "1000", "1999", "--seed", "5")
    second_id = probe.calls[1]
    fake.overrides = {second_id: status(403, b"private")}
    code, fake, _ = run(tmp_path / "real", "--target", "4", "--id-range", "1000", "1999", "--seed", "5", fake=fake)
    rejects = {r["id"]: r["reason"] for r in read_jsonl(tmp_path / "real" / "ogs" / "rejects.jsonl")}
    assert code == 0 and rejects[second_id] == "forbidden"


def test_argument_errors(tmp_path):
    code, fake, lines = run(tmp_path)
    assert code == 2 and "--target" in lines[0] and fake.calls == []
    code, _, lines = run(tmp_path, "--target", "1", "--min-date", "yesterday")
    assert code == 2 and "YYYY-MM-DD" in lines[0]


def test_ids_from_refetches_exactly_the_named_games(tmp_path):
    _, source, _ = run(tmp_path / "source", "--target", "8", "--id-range", "1000", "1999", "--seed", "11")
    accepted = sorted(r["id"] for r in read_jsonl(tmp_path / "source" / "ogs" / "index.jsonl"))
    names = [f"ogs-{i}" for i in accepted]
    explainer = {"game": names[7], "ply": 30, "phase": "opening", "declared_rank": "15k"}
    splits = {
        "version": 1,
        "seed": 1,
        "source": "ogs",
        "raw_dir": "data/raw/ogs",
        "pilot_openings": names[:2],
        "confirmatory_openings": names[2:4],
        "c2_positions": {"15k-10k": [{"game": names[4], "ply": 30}], "9k-5k": [{"game": names[5], "ply": 40}]},
        "topic_games": [names[6]],
        "explainer_positions": [explainer],
    }
    path = tmp_path / "splits.json"
    path.write_text(json.dumps(splits), encoding="utf-8")

    code, fake, lines = run(tmp_path / "repro", "--ids-from", str(path))
    assert code == 0 and fake.calls == accepted
    out = tmp_path / "repro" / "ogs"
    assert sorted(p.name for p in out.glob("*.sgf")) == sorted(f"{n}.sgf" for n in names)
    for n in names:
        assert (out / f"{n}.sgf").read_bytes() == (tmp_path / "source" / "ogs" / f"{n}.sgf").read_bytes()
    assert f"names {len(names)} games" in lines[0]

    code, again, _ = run(tmp_path / "repro", "--ids-from", str(path))
    assert code == 0 and again.calls == []  # all on disk already

    broken = FakeOgs()
    broken.overrides = {accepted[0]: ok({**fixture("game_resign"), "id": accepted[0], "annulled": True})}
    code, _, lines = run(tmp_path / "repro2", "--ids-from", str(path), fake=broken)
    assert code == 1 and "NOT reproduced (1)" in "\n".join(lines)
    assert not (tmp_path / "repro2" / "ogs" / f"ogs-{accepted[0]}.sgf").exists()


def test_ids_from_rejects_foreign_game_names(tmp_path):
    splits = {
        "pilot_openings": ["kgs-5"], "confirmatory_openings": [], "c2_positions": {}, "topic_games": [],
        "explainer_positions": [],
    }  # fmt: skip
    path = tmp_path / "splits.json"
    path.write_text(json.dumps(splits), encoding="utf-8")
    code, fake, lines = run(tmp_path, "--ids-from", str(path))
    assert code == 2 and "not an OGS game name" in lines[0] and fake.calls == []


def test_band_coverage_counts_games_and_players():
    bands = ["15k-10k", "9k-5k", "4k-1k", "1d-3d"]
    records = [
        {"black_rank": "12k", "white_rank": "11k"},  # two players in one band: one game, two players
        {"black_rank": "12k", "white_rank": "8k"},
        {"black_rank": "2d", "white_rank": "20k"},
    ]
    assert fg.band_coverage(records, bands) == {"15k-10k": (2, 3), "9k-5k": (1, 1), "4k-1k": (0, 0), "1d-3d": (1, 1)}


def test_output_is_utf8_without_carriage_returns(tmp_path):
    run(tmp_path, "--target", "2", "--id-range", "1000", "1999")
    for path in (tmp_path / "ogs").iterdir():
        assert b"\r" not in path.read_bytes(), path.name


def test_a_challenge_that_never_started_is_rejected_not_a_schema_error():
    never = mutate(fixture("game_resign"), started=None, ended=None)
    assert fg.evaluate_game(1, never, FILTERS) == fg.Rejected("not_started")
    with pytest.raises(fg.SchemaError, match="started"):  # the key itself gone: the API changed
        fg.evaluate_game(1, without(fixture("game_resign"), "started"), FILTERS)
    with pytest.raises(fg.SchemaError, match="started"):  # an end without a start makes no sense
        fg.evaluate_game(1, mutate(fixture("game_resign"), started=None), FILTERS)
