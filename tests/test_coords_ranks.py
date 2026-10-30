from __future__ import annotations

import math

import pytest

from sensei.coords import find_coords, gtp_to_rc, neighbors, rc_to_gtp, region
from sensei.ranks import (
    label_to_numeric,
    nearest_profile,
    numeric_to_label,
    profile_to_numeric,
    rank_posterior,
)


def test_gtp_roundtrip_every_point():
    for size in (9, 19):
        for row in range(size):
            for col in range(size):
                assert gtp_to_rc(rc_to_gtp((row, col), size), size) == (row, col)


def test_gtp_known_points():
    assert gtp_to_rc("A19") == (0, 0)
    assert gtp_to_rc("T1") == (18, 18)
    assert gtp_to_rc("J10") == (9, 8)  # no I column
    assert gtp_to_rc("pass") is None
    assert rc_to_gtp(None) == "pass"


@pytest.mark.parametrize("bad", ["I5", "Z3", "A0", "A20", "Q", "16Q"])
def test_gtp_rejects_bad(bad):
    with pytest.raises(ValueError):
        gtp_to_rc(bad)


def test_find_coords():
    text = "Black Q16 is big; I think D4 or R14, not I5 or A20. Then T1."
    assert find_coords(text) == ["Q16", "D4", "R14", "T1"]
    assert find_coords("E5 and J9 and K3", size=9) == ["E5", "J9"]


def test_neighbors_and_region():
    assert sorted(neighbors((0, 0))) == [(0, 1), (1, 0)]
    assert len(neighbors((9, 9))) == 4
    assert region(gtp_to_rc("D4")) == "corner"
    assert region(gtp_to_rc("K4")) == "edge"
    assert region(gtp_to_rc("K10")) == "center"


def test_rank_scale():
    assert label_to_numeric("1k") == 0.0
    assert label_to_numeric("10k") == -9.0
    assert label_to_numeric("1d") == 1.0
    assert profile_to_numeric("rank_5k") == -4.0
    assert profile_to_numeric("preaz_3d") == 3.0
    assert numeric_to_label(0.4) == "1k"
    assert numeric_to_label(0.6) == "1d"
    assert numeric_to_label(-9.2) == "10k"
    with pytest.raises(ValueError):
        profile_to_numeric("pro_9d")


def test_nearest_profile():
    grid = ["rank_20k", "rank_10k", "rank_5k", "rank_1d"]
    assert nearest_profile(-8.0, grid) == "rank_10k"
    assert nearest_profile(0.9, grid) == "rank_1d"


def test_rank_posterior_peaks_at_true_profile():
    grid = ["rank_15k", "rank_10k", "rank_5k", "rank_1d"]
    ll = {"rank_15k": -120.0, "rank_10k": -105.0, "rank_5k": -100.0, "rank_1d": -110.0}
    mean, sd, post = rank_posterior(ll)
    assert max(post, key=post.get) == "rank_5k"
    assert abs(sum(post.values()) - 1.0) < 1e-12
    assert -6.0 < mean < -3.0
    assert sd > 0
    # Equal likelihoods give the grid mean.
    mean_u, _, _ = rank_posterior({p: 0.0 for p in grid})
    assert math.isclose(mean_u, sum(profile_to_numeric(p) for p in grid) / len(grid))
