"""C3, curriculum in simulation (SCOPE.md "C3 Curriculum in simulation"; parameters in configs/prereg.yaml, C3).

This is a closed-loop SIMULATION with assumed learning dynamics. It shows that the curriculum finds and targets
weaknesses under those dynamics, not that humans learn faster. No engine and no model API are involved.

Per learner: `n_topics` topics, `weak_topics_per_learner` of them weak (mistake probability `p_weak`, `p_base`
elsewhere). Each session the policy picks a target topic from what it has observed so far; the learner then
plays `moves_per_session` moves spread over the topics uniformly at random, each a mistake or not at the topic's
current rate; every policy's curriculum sees the per-topic (successes, failures) of the session once; finally
the lesson takes effect: the target's rate is multiplied by (1 - eta_target), every other rate by
(1 - eta_transfer).

Metric: the mean mistake-rate curve, rate_s = mean over topics of the true mistake probability during session s
(the expected per-move mistake rate, since moves are spread uniformly), and its area AUC = sum over the sessions.
Per-learner AUCs are averaged, so the AUC of the mean curve equals the mean per-learner AUC and the bootstrap
over learners is paired. Headline: gap closure = (AUC_rr - AUC_th) / (AUC_rr - AUC_oracle). Success:
AUC(thompson) < AUC(round_robin) with the paired CI over learners excluding 0.

Seeds: the confirmatory seed is `seeds.global`; per-learner streams come from numpy's SeedSequence (one child per
learner, one grandchild per policy), and one extra child seeds the bootstraps.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np

from eval.stats import paired_bootstrap_ci, ratio_of_means_ci
from sensei.curriculum import ThompsonCurriculum

POLICIES = ("thompson", "greedy_mean", "round_robin", "uniform_random", "oracle")
HEADLINE_POLICIES = ("thompson", "round_robin", "oracle")

METRIC = "AUC(round_robin) - AUC(thompson) of the mean mistake-rate curve over sessions (simulation)"


@dataclass(frozen=True)
class LearnerRun:
    """One policy on one simulated learner."""

    n_topics: int
    weak: tuple[int, ...]
    curve: tuple[float, ...]
    """Mean true mistake rate during each session (before that session's lesson takes effect)."""
    selections: tuple[int, ...]
    """Target topic of each session."""

    @property
    def auc(self) -> float:
        return float(sum(self.curve))


def select_target(
    policy: str,
    session: int,
    curriculum: ThompsonCurriculum,
    rates: np.ndarray,
    rng: np.random.Generator,
) -> int:
    """Target topic index for this session under `policy`."""
    n_topics = len(curriculum.arms)
    if policy == "thompson":
        return curriculum.arms.index(curriculum.select(rng))
    if policy == "greedy_mean":
        means = np.array([curriculum.posterior_mean(a) for a in curriculum.arms], dtype=float)
        best = np.flatnonzero(means == means.max())
        return int(best[0]) if len(best) == 1 else int(best[rng.integers(len(best))])
    if policy == "round_robin":
        return session % n_topics
    if policy == "uniform_random":
        return int(rng.integers(n_topics))
    if policy == "oracle":
        return int(np.argmax(rates))  # the highest TRUE mistake rate; ties go to the lowest index
    raise ValueError(f"unknown policy {policy!r}; known: {POLICIES}")


def simulate_learner(c3: Mapping[str, Any], weak: np.ndarray, policy: str, rng: np.random.Generator) -> LearnerRun:
    """Run one policy on one learner whose weak topics are `weak`; `rng` drives selection and observations."""
    n_topics = int(c3["n_topics"])
    sessions = int(c3["sessions"])
    moves = int(c3["moves_per_session"])
    eta_target = float(c3["eta_target"])
    eta_transfer = float(c3["eta_transfer"])
    rates = np.full(n_topics, float(c3["p_base"]))
    rates[np.asarray(weak)] = float(c3["p_weak"])
    arms = [f"topic_{k}" for k in range(n_topics)]
    curriculum = ThompsonCurriculum(arms, discount=float(c3["discount"]), prior=tuple(c3["prior"]))

    curve: list[float] = []
    selections: list[int] = []
    for session in range(sessions):
        target = select_target(policy, session, curriculum, rates, rng)
        curve.append(float(rates.mean()))
        topics = rng.integers(0, n_topics, size=moves)
        mistakes = rng.random(moves) < rates[topics]
        successes = np.bincount(topics[mistakes], minlength=n_topics)
        failures = np.bincount(topics[~mistakes], minlength=n_topics)
        curriculum.update({arms[k]: (int(successes[k]), int(failures[k])) for k in range(n_topics)})
        transfer = np.full(n_topics, 1.0 - eta_transfer)
        transfer[target] = 1.0 - eta_target
        rates *= transfer
        selections.append(target)
    return LearnerRun(
        n_topics=n_topics, weak=tuple(int(k) for k in weak), curve=tuple(curve), selections=tuple(selections)
    )


def identified(run: LearnerRun, by_session: int) -> bool:
    """True when the weak topics are strictly the most-selected arms over sessions 1..by_session."""
    counts = np.bincount(np.asarray(run.selections[:by_session], dtype=int), minlength=run.n_topics)
    weak = np.asarray(run.weak, dtype=int)
    others = np.setdiff1d(np.arange(run.n_topics), weak)
    if len(others) == 0:
        return True
    return bool(counts[weak].min() > counts[others].max())


def validate_c3_config(c3: Mapping[str, Any]) -> list[str]:
    policies = list(c3["policies"])
    unknown = [p for p in policies if p not in POLICIES]
    if unknown:
        raise ValueError(f"unknown C3 policies {unknown!r}; known: {POLICIES}")
    missing = [p for p in HEADLINE_POLICIES if p not in policies]
    if missing:
        raise ValueError(f"C3 needs the policies {HEADLINE_POLICIES} for its headline; missing {missing!r}")
    if len(set(policies)) != len(policies):
        raise ValueError("duplicate C3 policies")
    n_topics, n_weak = int(c3["n_topics"]), int(c3["weak_topics_per_learner"])
    if not 1 <= n_weak < n_topics:
        raise ValueError("weak_topics_per_learner must be between 1 and n_topics - 1")
    if int(c3["identification_session"]) < 1 or int(c3["sessions"]) < 1:
        raise ValueError("sessions and identification_session must be >= 1")
    return policies


def run_c3(cfg: Mapping[str, Any], seed: int) -> dict[str, Any]:
    """Run the simulation for every policy and return the claim's part of the result file (eval/README.md)."""
    c3 = cfg["C3"]
    policies = validate_c3_config(c3)
    n_learners = int(c3["n_learners"])
    n_topics = int(c3["n_topics"])
    n_weak = int(c3["weak_topics_per_learner"])
    n_resamples = int(c3["bootstrap_resamples"])
    ci = float(c3["ci"])
    # Reduced configs (tests) may run fewer sessions than identification_session; the share is then taken at the
    # last session and the effective session is recorded.
    ident_session = min(int(c3["identification_session"]), int(c3["sessions"]))

    root = np.random.SeedSequence(int(seed))
    children = root.spawn(n_learners + 1)
    bootstrap_seed = int(children[-1].generate_state(1, dtype=np.uint32)[0])

    runs: dict[str, list[LearnerRun]] = {p: [] for p in policies}
    for learner_seq in children[:n_learners]:
        weak = np.sort(np.random.default_rng(learner_seq).choice(n_topics, size=n_weak, replace=False))
        for policy, policy_seq in zip(policies, learner_seq.spawn(len(policies)), strict=True):
            runs[policy].append(simulate_learner(c3, weak, policy, np.random.default_rng(policy_seq)))

    auc = {p: np.array([r.auc for r in runs[p]], dtype=float) for p in policies}
    mean_curve = {p: np.mean([r.curve for r in runs[p]], axis=0).tolist() for p in policies}
    ident_share = {p: float(np.mean([identified(r, ident_session) for r in runs[p]])) for p in policies}

    diff_rr_th = auc["round_robin"] - auc["thompson"]
    estimate, lo, hi = paired_bootstrap_ci(diff_rr_th, n_resamples, ci, bootstrap_seed)
    success = bool(estimate > 0.0 and lo > 0.0)

    gap_num = diff_rr_th
    gap_den = auc["round_robin"] - auc["oracle"]
    gap, gap_lo, gap_hi = ratio_of_means_ci(gap_num, gap_den, n_resamples, ci, bootstrap_seed)

    vs_thompson = {}
    for p in policies:
        if p == "thompson":
            continue
        est_p, lo_p, hi_p = paired_bootstrap_ci(auc[p] - auc["thompson"], n_resamples, ci, bootstrap_seed)
        vs_thompson[p] = {"estimate": est_p, "ci": [lo_p, hi_p]}

    details: dict[str, Any] = {
        "simulation": True,
        "note": (
            "Closed-loop simulation with assumed learning dynamics (SCOPE.md C3): it checks that the curriculum "
            "finds and targets weaknesses under those dynamics, not that humans learn faster."
        ),
        "metric": METRIC,
        "auc_definition": "sum over sessions of the mean true mistake rate during the session, per learner",
        "headline": {"name": "gap_closure", "estimate": gap, "ci": [gap_lo, gap_hi]},
        "auc": {p: float(auc[p].mean()) for p in policies},
        "auc_sd": {p: float(auc[p].std(ddof=1)) if n_learners > 1 else 0.0 for p in policies},
        "mean_curve": mean_curve,
        "gap_closure": {"estimate": gap, "ci": [gap_lo, gap_hi]},
        "auc_vs_thompson": vs_thompson,
        "greedy_vs_thompson": vs_thompson.get("greedy_mean"),
        "identification_session": ident_session,
        "identification_rule": (
            "weak topics are strictly the most-selected arms over sessions 1..identification_session "
            "(min count over weak topics > max count over the others)"
        ),
        "identification_share": ident_share,
        "policies": policies,
        "bootstrap_seed": bootstrap_seed,
        "success_rule": "AUC(thompson) < AUC(round_robin) with the paired CI over learners excluding 0",
    }
    return {
        "n": n_learners,
        "estimate": estimate,
        "ci": [lo, hi],
        "success": success,
        "details": details,
        "engine_queries": 0,
        "epsilon": None,
        "concurrency": None,
    }


def run_claim(
    cfg: Mapping[str, Any],
    *,
    seed: int,
    pilot: bool,
    concurrency: int | None,
    results_dir: Any = None,
) -> dict[str, Any]:
    """Entry point used by eval.run; C3 ignores `pilot`, `concurrency` and `results_dir` (no engine)."""
    return run_c3(cfg, seed)
