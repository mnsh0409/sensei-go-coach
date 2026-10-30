"""Situation featurizer tests (INTERFACES.md "Mistakes and mistake topics"). Skip until sensei.topics exists."""

from __future__ import annotations

import numpy as np
import pytest

from sensei.coords import gtp_to_rc
from sensei.interfaces import PositionClass

topics_mod = pytest.importorskip("sensei.topics")
situation_bag = topics_mod.situation_bag
loss_bin = topics_mod.loss_bin
window_documents = topics_mod.window_documents
LdaMistakeTopics = topics_mod.LdaMistakeTopics

BINS = [0.05, 0.10, 0.20]


def bag(**overrides):
    kwargs = dict(
        ply=10,
        played_rc=gtp_to_rc("D4"),
        best_rc=gtp_to_rc("K10"),
        previous_rc=gtp_to_rc("Q16"),
        position_class=PositionClass.TACTICAL,
        local_tags=(),
        size=19,
    )
    kwargs.update(overrides)
    return situation_bag(**kwargs)


def test_tokens_of_a_plain_move():
    b = bag()
    assert b == {
        "phase:opening": 1,
        "region:corner": 1,
        "best_region:center": 1,
        "class:tactical": 1,
        "local:tenuki": 1,  # Q16 to D4 is far more than 5 lines
    }
    assert all(isinstance(v, int) for v in b.values())
    assert not any(t.startswith("loss:") for t in b)


@pytest.mark.parametrize(
    ("ply", "phase"), [(0, "opening"), (49, "opening"), (50, "middle"), (149, "middle"), (150, "end"), (300, "end")]
)
def test_phase_boundaries(ply, phase):
    assert f"phase:{phase}" in bag(ply=ply)


@pytest.mark.parametrize(("move", "reg"), [("D4", "corner"), ("K4", "edge"), ("A10", "edge"), ("K10", "center")])
def test_region_tokens(move, reg):
    b = bag(played_rc=gtp_to_rc(move), best_rc=gtp_to_rc(move), previous_rc=gtp_to_rc(move))
    assert f"region:{reg}" in b
    assert f"best_region:{reg}" in b


def test_pass_produces_no_region_token():
    b = bag(played_rc=None)
    assert not any(t.startswith("region:") for t in b)
    assert "best_region:center" in b
    assert "local:tenuki" not in b  # no distance without a played point
    b2 = bag(best_rc=None)
    assert not any(t.startswith("best_region:") for t in b2)


def test_class_token_accepts_enum_and_string():
    assert "class:only_move" in bag(position_class=PositionClass.ONLY_MOVE)
    assert "class:strategic" in bag(position_class="strategic")
    with pytest.raises(ValueError):
        bag(position_class="brilliant")


def test_tenuki_rule_is_more_than_five_lines():
    near = bag(played_rc=(9, 9), previous_rc=(4, 4))  # 5 lines away: not a tenuki
    far = bag(played_rc=(9, 9), previous_rc=(3, 9))  # 6 lines away
    assert "local:tenuki" not in near
    assert "local:tenuki" in far
    assert "local:tenuki" not in bag(played_rc=(9, 9), previous_rc=None)
    assert "local:tenuki" in bag(played_rc=(9, 9), previous_rc=(4, 4), local_tags=("tenuki",))


def test_local_tags():
    b = bag(played_rc=(9, 9), previous_rc=(9, 10), local_tags=("capture", "atari", "contact"))
    assert {"local:capture", "local:atari", "local:contact"} <= set(b)
    assert "local:tenuki" not in b
    with pytest.raises(ValueError):
        bag(local_tags=("ladder",))


def test_loss_bins():
    assert loss_bin(0.05, BINS) == "small"
    assert loss_bin(0.0999, BINS) == "small"
    assert loss_bin(0.10, BINS) == "medium"
    assert loss_bin(0.199, BINS) == "medium"
    assert loss_bin(0.20, BINS) == "large"
    assert loss_bin(0.9, BINS) == "large"
    with pytest.raises(ValueError):
        loss_bin(0.04, BINS)  # not a mistake
    with pytest.raises(ValueError):
        loss_bin(0.1, [0.2, 0.1, 0.05])


def test_window_documents_merges_consecutive_bags():
    bags = [bag(ply=p) for p in (0, 60, 160, 10, 20, 30, 40)]
    docs = window_documents(bags, window=5)
    assert len(docs) == 2
    assert docs[0]["phase:opening"] == 3 and docs[0]["phase:middle"] == 1 and docs[0]["phase:end"] == 1
    assert docs[0]["class:tactical"] == 5
    assert docs[1]["phase:opening"] == 2
    assert window_documents([], window=5) == []
    with pytest.raises(ValueError):
        window_documents(bags, window=0)


def test_featurized_bags_fit_the_topic_model():
    rng = np.random.default_rng(3)
    bags = []
    for _ in range(120):
        if rng.random() < 0.5:
            b = bag(ply=int(rng.integers(0, 50)), played_rc=(3, 3), previous_rc=(15, 15), position_class="strategic")
        else:
            b = bag(ply=int(rng.integers(150, 300)), played_rc=(0, 9), previous_rc=(0, 10), local_tags=("atari",))
        bags.append(b)
    model = LdaMistakeTopics(n_topics=2, seed=0)
    model.fit(window_documents(bags, window=3))
    dist = model.assign(bags[0])
    assert len(dist) == 2 and abs(sum(dist) - 1.0) < 1e-6
    assert model.assign({"never:seen": 2}) == [0.5, 0.5]
    with pytest.raises(RuntimeError):
        LdaMistakeTopics(n_topics=2, seed=0).assign(bags[0])
