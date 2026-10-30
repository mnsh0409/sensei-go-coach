"""Mistake situations as token bags and unsupervised mistake topics over them (INTERFACES.md).

`situation_bag` turns one learner move into the bag of tokens defined in INTERFACES.md ("Mistakes and mistake
topics"); `loss_bin` names the severity of a mistake, which is stored separately from the bag;
`window_documents` builds the fitting documents (windows of consecutive mistakes); `LdaMistakeTopics` is the
scikit-learn LDA model over such bags. This is a runtime model, not a pre-registered claim.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
from scipy.special import psi
from sklearn.decomposition import LatentDirichletAllocation
from sklearn.feature_extraction import DictVectorizer

from sensei.coords import region
from sensei.interfaces import PositionClass

PHASE_BOUNDS = (50, 150)
"""ply < 50 is the opening, ply < 150 the middle game, anything else the endgame."""

TENUKI_LINES = 5
"""A move more than this many lines (Chebyshev distance) from the previous move is a tenuki."""

LOCAL_TAGS = ("capture", "atari", "contact", "tenuki")
LOSS_BIN_NAMES = ("small", "medium", "large")


def phase_of(ply: int) -> str:
    """'opening', 'middle' or 'end' from the ply (0-based move count before the move)."""
    if ply < 0:
        raise ValueError(f"ply must be >= 0, got {ply!r}")
    if ply < PHASE_BOUNDS[0]:
        return "opening"
    if ply < PHASE_BOUNDS[1]:
        return "middle"
    return "end"


def is_tenuki(played_rc: tuple[int, int] | None, previous_rc: tuple[int, int] | None) -> bool:
    """More than TENUKI_LINES lines from the previous move. False when either move is a pass or unknown."""
    if played_rc is None or previous_rc is None:
        return False
    return max(abs(played_rc[0] - previous_rc[0]), abs(played_rc[1] - previous_rc[1])) > TENUKI_LINES


def situation_bag(
    ply: int,
    played_rc: tuple[int, int] | None,
    best_rc: tuple[int, int] | None,
    previous_rc: tuple[int, int] | None,
    position_class: PositionClass | str,
    local_tags: Iterable[str] = (),
    size: int = 19,
) -> dict[str, int]:
    """The situation bag of one learner move (every token counted once).

    Tokens: `phase:{opening,middle,end}`, `region:{corner,edge,center}` of the played move (a pass produces no
    region token), `best_region:{...}` of the engine's best move, `class:{only_move,tactical,strategic}` and
    `local:{capture,atari,contact,tenuki}`. `capture`, `atari` and `contact` need the board and come from
    `local_tags`; `tenuki` is derived here from `previous_rc` (and accepted in `local_tags` too). The bag carries
    no loss token: mistakes and non-mistakes are tagged alike, and the severity goes through `loss_bin`.
    """
    tokens = [f"phase:{phase_of(ply)}"]
    if played_rc is not None:
        tokens.append(f"region:{region(played_rc, size)}")
    if best_rc is not None:
        tokens.append(f"best_region:{region(best_rc, size)}")
    tokens.append(f"class:{PositionClass(position_class).value}")
    local = set()
    for tag in local_tags:
        if tag not in LOCAL_TAGS:
            raise ValueError(f"unknown local tag {tag!r}; expected one of {LOCAL_TAGS}")
        local.add(tag)
    if is_tenuki(played_rc, previous_rc):
        local.add("tenuki")
    tokens.extend(f"local:{tag}" for tag in LOCAL_TAGS if tag in local)
    return dict.fromkeys(tokens, 1)


def loss_bin(loss: float, bins: Sequence[float]) -> str:
    """'small', 'medium' or 'large' for a mistake's winrate loss.

    `bins` = (mistake threshold, medium edge, large edge), for example [0.05, 0.10, 0.20]: small is
    [0.05, 0.10), medium [0.10, 0.20), large from 0.20 up. A loss below the threshold is not a mistake and
    raises ValueError.
    """
    if len(bins) != 3 or not (bins[0] < bins[1] < bins[2]):
        raise ValueError(f"bins must be three increasing edges, got {list(bins)!r}")
    if loss < bins[0]:
        raise ValueError(f"loss {loss!r} is below the mistake threshold {bins[0]!r}")
    if loss < bins[1]:
        return "small"
    if loss < bins[2]:
        return "medium"
    return "large"


def window_documents(bags: Sequence[Mapping[str, int]], window: int) -> list[dict[str, int]]:
    """Merge consecutive bags of one player into documents of `window` bags each (the fitting corpus).

    Bags are in game order. Windows do not overlap; a shorter final window is kept when it is non-empty.
    """
    if window < 1:
        raise ValueError("window must be >= 1")
    docs: list[dict[str, int]] = []
    for start in range(0, len(bags), window):
        doc: dict[str, int] = {}
        for bag in bags[start : start + window]:
            for token, count in bag.items():
                doc[token] = doc.get(token, 0) + int(count)
        if doc:
            docs.append(doc)
    return docs


class LdaMistakeTopics:
    """MistakeTopics (interfaces.MistakeTopics) on scikit-learn's LatentDirichletAllocation."""

    def __init__(self, n_topics: int, seed: int, max_iter: int = 20) -> None:
        if n_topics < 1:
            raise ValueError("n_topics must be >= 1")
        self.n_topics = int(n_topics)
        self.seed = int(seed)
        self.max_iter = int(max_iter)
        self._vectorizer: DictVectorizer | None = None
        self._lda: LatentDirichletAllocation | None = None

    @property
    def fitted(self) -> bool:
        return self._lda is not None

    @property
    def vocabulary(self) -> tuple[str, ...]:
        self._require_fitted()
        assert self._vectorizer is not None
        return tuple(self._vectorizer.get_feature_names_out())

    def _require_fitted(self) -> None:
        if self._lda is None or self._vectorizer is None:
            raise RuntimeError("LdaMistakeTopics.fit must run before assign or describe")

    def fit(self, bags: Sequence[Mapping[str, int]]) -> None:
        docs = [{str(t): float(c) for t, c in bag.items()} for bag in bags]
        if not docs or all(not d for d in docs):
            raise ValueError("fit needs at least one non-empty bag")
        vectorizer = DictVectorizer(sparse=True)
        x = vectorizer.fit_transform(docs)
        lda = LatentDirichletAllocation(
            n_components=self.n_topics,
            learning_method="batch",
            max_iter=self.max_iter,
            random_state=self.seed,
        )
        lda.fit(x)
        self._vectorizer = vectorizer
        self._lda = lda

    def assign(self, bag: Mapping[str, int]) -> list[float]:
        """Topic distribution of one bag, summing to 1. Unknown tokens are ignored; no known token -> uniform."""
        self._require_fitted()
        assert self._vectorizer is not None and self._lda is not None
        known = self._vectorizer.vocabulary_
        doc = {str(t): float(c) for t, c in bag.items() if str(t) in known and c > 0}
        if not doc:
            return [1.0 / self.n_topics] * self.n_topics
        theta = np.asarray(self._lda.transform(self._vectorizer.transform([doc]))[0], dtype=float)
        theta = np.clip(theta, 0.0, None)
        total = float(theta.sum())
        if total <= 0.0:
            return [1.0 / self.n_topics] * self.n_topics
        return [float(v) for v in theta / total]

    def describe(self, k: int, top_n: int = 8) -> list[tuple[str, float]]:
        """Top tokens of topic k with their within-topic weights (the topic's token distribution)."""
        self._require_fitted()
        assert self._vectorizer is not None and self._lda is not None
        if not 0 <= k < self.n_topics:
            raise IndexError(f"topic {k} out of range for {self.n_topics} topics")
        weights = np.asarray(self._lda.components_[k], dtype=float)
        weights = weights / weights.sum()
        names = self._vectorizer.get_feature_names_out()
        order = np.argsort(-weights, kind="stable")[: max(0, int(top_n))]
        return [(str(names[i]), float(weights[i])) for i in order]

    # ---- persistence (results/topics_v1.json, written by scripts/fit_topics.py) ------------------------------

    def to_json(self) -> dict[str, Any]:
        """The fitted model as plain JSON: vocabulary, topic-word pseudo-counts and the LDA settings."""
        self._require_fitted()
        assert self._vectorizer is not None and self._lda is not None
        return {
            "kind": "LdaMistakeTopics",
            "n_topics": self.n_topics,
            "seed": self.seed,
            "max_iter": self.max_iter,
            "vocabulary": list(self.vocabulary),
            "components": np.asarray(self._lda.components_, dtype=float).tolist(),
            "doc_topic_prior": float(self._lda.doc_topic_prior_),
            "topic_word_prior": float(self._lda.topic_word_prior_),
            "max_doc_update_iter": int(self._lda.max_doc_update_iter),
            "mean_change_tol": float(self._lda.mean_change_tol),
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> LdaMistakeTopics:
        """Rebuild a fitted model from `to_json` output; `assign` and `describe` give the same results."""
        if data.get("kind") != "LdaMistakeTopics":
            raise ValueError("not an LdaMistakeTopics model")
        model = cls(int(data["n_topics"]), int(data["seed"]), int(data["max_iter"]))
        vocab = [str(t) for t in data["vocabulary"]]
        components = np.asarray(data["components"], dtype=float)
        if components.shape != (model.n_topics, len(vocab)):
            raise ValueError(f"components shape {components.shape} != ({model.n_topics}, {len(vocab)})")
        vectorizer = DictVectorizer(sparse=True)
        vectorizer.feature_names_ = vocab
        vectorizer.vocabulary_ = {t: i for i, t in enumerate(vocab)}
        lda = LatentDirichletAllocation(
            n_components=model.n_topics,
            learning_method="batch",
            max_iter=model.max_iter,
            random_state=model.seed,
            max_doc_update_iter=int(data["max_doc_update_iter"]),
            mean_change_tol=float(data["mean_change_tol"]),
        )
        lda.components_ = components
        # E[log beta] under the Dirichlet posterior, as scikit-learn computes it after fitting.
        lda.exp_dirichlet_component_ = np.exp(psi(components) - psi(components.sum(axis=1))[:, np.newaxis])
        lda.doc_topic_prior_ = float(data["doc_topic_prior"])
        lda.topic_word_prior_ = float(data["topic_word_prior"])
        lda.n_features_in_ = len(vocab)
        lda.n_iter_ = 0
        lda.n_batch_iter_ = 0
        lda.bound_ = float("nan")
        model._vectorizer, model._lda = vectorizer, lda
        return model

    def save(self, path: str | Path, **metadata: Any) -> Path:
        """Write the model (plus any metadata, such as corpus statistics or names) as JSON."""
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({**metadata, **self.to_json()}, indent=1) + "\n", encoding="utf-8")
        return out

    @classmethod
    def load(cls, path: str | Path) -> LdaMistakeTopics:
        return cls.from_json(json.loads(Path(path).read_text(encoding="utf-8")))
