"""Acceptance tests for T7 (topics). Skip until sensei.topics exists."""

from __future__ import annotations

import numpy as np
import pytest

from sensei.interfaces import MistakeTopics

topics_mod = pytest.importorskip("sensei.topics")
LdaMistakeTopics = topics_mod.LdaMistakeTopics

TOPIC_X = ["phase:opening", "region:corner", "loss:small", "class:strategic", "local:tenuki"]
TOPIC_Y = ["phase:end", "region:edge", "loss:large", "class:only_move", "local:atari"]


def synthetic_bags(rng, n=200):
    bags, labels = [], []
    for i in range(n):
        vocab = TOPIC_X if i % 2 == 0 else TOPIC_Y
        bag: dict[str, int] = {}
        for tok in rng.choice(vocab, size=12):
            bag[str(tok)] = bag.get(str(tok), 0) + 1
        bags.append(bag)
        labels.append(i % 2)
    return bags, labels


def test_fit_assign_describe():
    rng = np.random.default_rng(0)
    bags, labels = synthetic_bags(rng)
    model = LdaMistakeTopics(n_topics=2, seed=0)
    assert isinstance(model, MistakeTopics)
    model.fit(bags)
    dists = [np.asarray(model.assign(b)) for b in bags]
    for d in dists:
        assert d.shape == (2,)
        assert abs(d.sum() - 1.0) < 1e-6
    argmax = [int(d.argmax()) for d in dists]
    agree = sum(a == lab for a, lab in zip(argmax, labels, strict=True)) / len(labels)
    assert max(agree, 1 - agree) > 0.95
    desc = model.describe(0, top_n=3)
    assert len(desc) == 3 and all(isinstance(t, str) for t, _ in desc)


def test_unknown_tokens_are_ignored():
    rng = np.random.default_rng(1)
    bags, _ = synthetic_bags(rng)
    model = LdaMistakeTopics(n_topics=2, seed=0)
    model.fit(bags)
    d = np.asarray(model.assign({"never:seen": 3, "phase:end": 1}))
    assert abs(d.sum() - 1.0) < 1e-6
