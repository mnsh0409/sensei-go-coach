"""T10: result tokens, scripts/render_readme.py and scripts/check_numbers.py on a synthetic repository."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

tokens = pytest.importorskip("eval.tokens")

ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def result(claim: str, stamp: str, *, dirty: bool = False, commit: str = "abc1234", pilot: bool = False, **extra):
    data = {
        "claim": claim,
        "pilot": pilot,
        "git_commit": commit,
        "git_dirty": dirty,
        "config": {"C1": {"ci": 0.95, "success_max_cost_elo_upper": 150}, "C2": {"ci": 0.95}, "C3": {"sessions": 20}},
        "hardware": {"gpu": "NVIDIA GeForce RTX 4090"},
        "wall_clock_s": 9000.0,
        "n": 400,
        "estimate": 61.2345,
        "ci": [12.5, 109.75],
        "epsilon": 0.01,
        "engine_queries": 123456,
        "concurrency": 8,
        "details": {"summary": {"score": 0.4123, "mean_winrate_loss_per_move": 0.0042, "left_engine_top_rate": 0.31}},
    }
    data.update(extra)
    return f"{'pilot_' if pilot else ''}{claim}_{stamp}.json", data


def write_results(directory: Path, *items) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name, data in items:
        (directory / name).write_text(json.dumps(data), encoding="utf-8")
    return directory


def test_resolver_uses_the_latest_clean_result_and_formats(tmp_path):
    res = write_results(
        tmp_path / "results",
        result("C1", "20261101-100000"),
        result("C1", "20261102-100000", dirty=True, estimate=999.0),  # newer but dirty: ignored
        result("C1", "20261103-100000", commit="no-git", estimate=888.0),  # no commit: ignored
        result("C2", "20261102-100000", estimate=0.0412, ci=[0.011, 0.07]),
    )
    r = tokens.Resolver(res)
    assert r.render("{{C1.elo}} [{{C1.lo}}, {{C1.hi}}] Elo, n={{C1.n}}") == "61 [12, 110] Elo, n=400"
    assert (
        r.render("{{C1.estimate:.1f}} {{C1.queries}} {{C1.hours}} {{C1.gpu}}")
        == "61.2 123,456 2.5 NVIDIA GeForce RTX 4090"
    )
    assert r.render("{{C1.details.summary.score:.2f}} {{C1.ci.1}} {{C1.score_pct}}%") == "0.41 109.8 41.2%"
    assert r.render("U = {{C2.U_pct}} pp ({{C2.lo_pct}} to {{C2.hi_pct}})") == "U = 4.1 pp (1.1 to 7.0)"
    assert r.files == {"C1": "C1_20261101-100000.json", "C2": "C2_20261102-100000.json"}
    with pytest.raises(tokens.TokenError, match="no clean C3"):
        r.render("{{C3.gap}}")
    with pytest.raises(tokens.TokenError, match="not in"):
        r.render("{{C1.details.nothing}}")
    with pytest.raises(tokens.TokenError, match="unknown token source"):
        r.render("{{C9.estimate}}")


def test_blocks_and_literal_numbers(tmp_path):
    res = write_results(tmp_path / "results", result("C1", "20261101-100000"))
    r = tokens.Resolver(res)
    text = "Intro 2026.\n<!-- results -->\nCost {{C1.elo}} Elo (C1, U_k).\n<!-- /results -->\nOutro.\n"
    assert tokens.render_blocks(text, r) == text.replace("{{C1.elo}}", "61")
    with pytest.raises(tokens.TokenError, match="outside"):
        tokens.render_blocks("{{C1.elo}}\n<!-- results --><!-- /results -->", r)
    assert tokens.render_blocks("<!-- results --><!-- /results -->", r) == "<!-- results --><!-- /results -->"
    assert tokens.literal_numbers("Cost {{C1.elo:.1f}} Elo at 95% over 1,000, C1, x2, 3.5x") == ["95", "1,000", "3.5"]


def test_render_and_check_numbers(tmp_path, capsys):
    render = load_script("render_readme")
    check = load_script("check_numbers")
    res = write_results(tmp_path / "results", result("C1", "20261101-100000"))
    template = (
        "# Sensei\n\n<!-- results -->\n"
        "Strength cost {{C1.elo}} Elo ({{C1.ci_pct}}% CI {{C1.lo}} to {{C1.hi}}).\n<!-- /results -->\n"
    )
    (tmp_path / "README.tmpl.md").write_text(template, encoding="utf-8")
    args = ["--root", str(tmp_path), "--results", str(res)]
    assert check.main(args) == 1  # README.md not rendered yet
    assert render.main(args) == 0
    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert "Strength cost 61 Elo (95% CI 12 to 110)." in readme and "C1=C1_20261101-100000.json" in readme
    assert check.main(args) == 0

    (tmp_path / "README.md").write_text(readme.replace("61 Elo", "45 Elo"), encoding="utf-8")  # hand edit
    assert check.main(args) == 1
    assert render.main(args) == 0 and check.main(args) == 0

    write_results(res, result("C1", "20261105-100000", estimate=70.0))  # a newer clean result: README is stale
    assert check.main(args) == 1

    (tmp_path / "README.tmpl.md").write_text(template.replace("{{C1.elo}}", "61"), encoding="utf-8")
    assert render.main(args) == 0 and check.main(args) == 1  # a typed number inside a block
    assert "typed by hand: 61" in capsys.readouterr().out


def test_dirty_results_never_render(tmp_path, capsys):
    render = load_script("render_readme")
    res = write_results(tmp_path / "results", result("C1", "20261101-100000", dirty=True))
    (tmp_path / "README.tmpl.md").write_text("<!-- results -->{{C1.elo}}<!-- /results -->", encoding="utf-8")
    assert render.main(["--root", str(tmp_path), "--results", str(res)]) == 1
    assert not (tmp_path / "README.md").exists()
    assert "no clean C1" in capsys.readouterr().err


def test_repo_templates_type_no_numbers_in_results_blocks():
    for name in ("README.tmpl.md", "video_script.tmpl.md"):
        path = ROOT / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        blocks = tokens.BLOCK_RE.findall(text)
        assert blocks, f"{name} has no results block"
        for block in blocks:
            assert tokens.literal_numbers(block) == [], f"{name}: numbers typed by hand in a results block"
        for source, path_, _ in tokens.TOKEN_RE.findall(text):
            assert source in tokens.SOURCES or source in tokens.DERIVED, f"{name}: unknown token source {source}"
            if "." not in path_:
                assert path_ in tokens.ALIASES.get(source, {}) or path_ in ("estimate", "n", "ci", "epsilon")


def baselines_file(c2_path: Path, *, dirty: bool = False, sha: str | None = None, file: str | None = None) -> dict:
    """A minimal results/relevance_baselines.json pointing at a C2 result file."""
    import hashlib

    def stat(est, lo=None, hi=None):
        return {"estimate": est, "ci": None if lo is None else [lo, hi]}

    return {
        "kind": "relevance_baselines",
        "descriptive": True,
        "git_commit": "def5678",
        "git_dirty": dirty,
        "source": {
            "file": file or c2_path.name,
            "sha256": sha or hashlib.sha256(c2_path.read_bytes()).hexdigest(),
        },
        "bootstrap": {"ci": 0.95},
        "bands": ["15k-10k", "9k-5k", "4k-1k", "1d-3d"],
        "rates": {
            "lesson": [[stat(0.034), stat(0.046), stat(0.05), stat(0.061)], [None, stat(0.042), None, None],
                       [None, None, stat(0.05), None], [None, None, None, stat(0.038)]],
            "best": [stat(0.118), stat(0.15), stat(0.174), stat(0.236)],
            "rand": [stat(0.084), stat(0.122), stat(0.12), stat(0.172)],
        },
        "not_best_share": 0.712,
        "adjacent": {
            "lesson_ratio_gmean": stat(1.224),
            "best_ratio_gmean": stat(1.26),
            "rand_ratio_gmean": stat(1.267),
            "lesson_over_rand_gmean": stat(0.967, 0.71, 1.31),
            "lesson_diff_mean": stat(0.01),
            "best_diff_mean": stat(0.039),
            "rand_diff_mean": stat(0.029),
            "lesson_not_best_diff_mean": stat(0.0043),
        },
    }  # fmt: skip


def test_derived_c2base_source(tmp_path):
    res = write_results(tmp_path / "results", result("C2", "20261031-134717", estimate=0.01, ci=[0.0003, 0.0195]))
    c2_path = res / "C2_20261031-134717.json"
    (res / "relevance_baselines.json").write_text(json.dumps(baselines_file(c2_path)), encoding="utf-8")
    r = tokens.Resolver(res)
    text = (
        "{{C2base.best_x}} {{C2base.rand_x}} {{C2base.lesson_x}}, {{C2base.lesson_vs_rand}} random "
        "({{C2base.lesson_over_rand}}, {{C2base.ci_pct}}% CI {{C2base.lesson_over_rand_lo}} to "
        "{{C2base.lesson_over_rand_hi}}); {{C2base.weakest_band}} {{C2base.weakest_lesson_own_pct}}% to "
        "{{C2base.strongest_band}} {{C2base.weakest_lesson_strongest_pct}}%; best {{C2base.best_weakest_pct}}% to "
        "{{C2base.best_strongest_pct}}%; {{C2base.not_best_share_pct}}% {{C2base.best_gain_pct}}"
    )
    assert r.render(text) == (
        "1.26 1.27 1.22, at a rate not distinguishable from random (0.97, 95% CI 0.71 to 1.31); 15k-10k 3.4% to "
        "1d-3d 6.1%; best 11.8% to 23.6%; 71% 3.9"
    )
    assert r.files == {"C2": "C2_20261031-134717.json", "C2base": "relevance_baselines.json"}

    def fresh(data: dict) -> tokens.Resolver:
        (res / "relevance_baselines.json").write_text(json.dumps(data), encoding="utf-8")
        return tokens.Resolver(res)

    faster = baselines_file(c2_path)
    faster["adjacent"]["lesson_over_rand_gmean"] = {"estimate": 1.6, "ci": [1.2, 2.1]}
    assert fresh(faster).render("{{C2base.lesson_vs_rand}}") == "faster than"
    with pytest.raises(tokens.TokenError, match="not from a clean"):
        fresh(baselines_file(c2_path, dirty=True)).render("{{C2base.best_x}}")
    with pytest.raises(tokens.TokenError, match="rerun"):
        fresh(baselines_file(c2_path, sha="0" * 64)).render("{{C2base.best_x}}")  # the C2 file changed since
    stale = baselines_file(c2_path)
    write_results(res, result("C2", "20261101-090000", estimate=0.02, ci=[0.001, 0.03]))  # a newer clean C2
    with pytest.raises(tokens.TokenError, match="not from the latest clean C2"):
        fresh(stale).render("{{C2base.best_x}}")
    (res / "relevance_baselines.json").unlink()
    with pytest.raises(tokens.TokenError, match="cannot read"):
        tokens.Resolver(res).render("{{C2base.best_x}}")
