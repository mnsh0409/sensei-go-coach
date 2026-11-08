"""GTC poster pipeline (TASKS.md T10b): benchmark tokens, the figures, the renderer and the poster check."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from eval.hardware import CUDA_VERSION_RE
from eval.tokens import Resolver, TokenError, gpu_key

ROOT = Path(__file__).resolve().parents[1]


def _latency(gpu: str, backend: str, vps: tuple[float, float, float], *, dirty: bool = False, cuda: str | None = "12.8",
             finished: str = "2026-11-08T10:00:00+08:00") -> dict:
    levels = {}
    for c, v in zip(("1", "4", "8"), vps, strict=True):
        levels[c] = {
            "concurrency": int(c),
            "analyze": {"p50_ms": 800.0 / int(c) ** 0.2, "p95_ms": 1000.0, "mean_ms": 810.0},
            "human_policy": {"p50_ms": 70.0, "p95_ms": 90.0, "mean_ms": 72.4},
            "visits_per_s": v,
        }
    return {
        "kind": "engine_benchmark",
        "git_commit": "abc1234",
        "git_dirty": dirty,
        "hardware": {"gpu": gpu, "driver": "610.88", "cuda": cuda, "katago_backend": backend},
        "finished": finished,
        "levels": levels,
    }


def _c1(gpu: str = "NVIDIA GeForce RTX 3090", backend: str = "cuda") -> dict:
    return {
        "claim": "C1",
        "pilot": False,
        "git_commit": "abc1234",
        "git_dirty": False,
        "n": 400,
        "wall_clock_s": 4391.0,
        "hardware": {"gpu": gpu, "katago_backend": backend},
    }


def _write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def bench_dir(tmp_path: Path) -> Path:
    _write(tmp_path / "C1_20261031-000000.json", _c1())
    _write(tmp_path / "latency_pc_cuda.json", _latency("NVIDIA GeForce RTX 3090", "cuda", (250.0, 990.0, 1400.0)))
    _write(tmp_path / "latency_pc_trt.json", _latency("NVIDIA GeForce RTX 3090", "trt", (300.0, 1200.0, 1820.0)))
    _write(tmp_path / "latency_lx_cuda.json", _latency("NVIDIA GeForce RTX 4090", "cuda", (400.0, 1600.0, 2000.0)))
    _write(tmp_path / "latency_lx_trt.json", _latency("NVIDIA GeForce RTX 4090", "trt", (500.0, 2000.0, 3000.0)))
    _write(
        tmp_path / "latency_bw_cuda.json",
        _latency("NVIDIA RTX PRO 6000 Blackwell Server Edition", "cuda", (1.0, 2.0, 99999.0), dirty=True),
    )
    return tmp_path


def test_gpu_keys():
    assert gpu_key("NVIDIA GeForce RTX 3090") == "3090"
    assert gpu_key("NVIDIA GeForce RTX 4090") == "4090"
    assert gpu_key("NVIDIA RTX PRO 6000 Blackwell Workstation Edition") == "p6000"
    assert gpu_key(None) == ""


def test_cuda_version_from_nvidia_smi_header():
    header = "| NVIDIA-SMI 610.88    Driver Version: 610.88    CUDA Version: 13.1     |"
    assert CUDA_VERSION_RE.search(header).group(1) == "13.1"
    assert CUDA_VERSION_RE.search("CUDA Driver Version: 13.0").group(1) == "13.0"
    assert CUDA_VERSION_RE.search("no gpu here") is None


def test_hw_tokens_read_clean_benchmarks_only(bench_dir: Path):
    r = Resolver(bench_dir)
    assert r.render("{{hw.3090.cuda.vps}}") == "1,400"
    assert r.render("{{hw.4090.trt.vps}}") == "3,000"
    assert r.render("{{hw.3090.cuda.cu}}") == "610.88 / 12.8"
    assert r.render("{{hw.3090.cuda.p50}}") == "800"
    assert r.render("{{hw.hp_ms}}") == "72"  # human-policy mean at 1 query on the C1 GPU and backend
    assert r.render("{{hw.conc_gain}}") == "5.6"  # 1400 / 250
    assert r.render("{{hw.trt_gain}}") == "1.3× to 1.5×"  # 1820/1400 and 3000/2000
    with pytest.raises(TokenError, match="no clean"):
        r.render("{{hw.p6000.cuda.vps}}")  # only a dirty file exists
    assert Resolver(bench_dir, allow_dirty=True).render("{{hw.p6000.cuda.vps}}") == "99,999"
    with pytest.raises(TokenError, match="unknown benchmark metric"):
        r.render("{{hw.3090.cuda.nope}}")


def test_hw_cuda_missing_and_single_gpu_gain(tmp_path: Path):
    _write(tmp_path / "C1_20261031-000000.json", _c1())
    _write(tmp_path / "latency_a.json", _latency("NVIDIA GeForce RTX 3090", "cuda", (250.0, 990.0, 1400.0), cuda=None))
    r = Resolver(tmp_path)
    assert r.render("{{hw.3090.cuda.cu}}") == "610.88 / n/a"
    with pytest.raises(TokenError, match="no GPU has clean CUDA and TensorRT"):
        r.render("{{hw.trt_gain}}")
    _write(tmp_path / "latency_b.json", _latency("NVIDIA GeForce RTX 3090", "trt", (300.0, 1200.0, 1820.0)))
    assert Resolver(tmp_path).render("{{hw.trt_gain}}") == "1.3×"


def test_newest_clean_benchmark_wins(tmp_path: Path):
    _write(tmp_path / "latency_old.json", _latency("NVIDIA GeForce RTX 3090", "cuda", (1.0, 2.0, 100.0),
                                                   finished="2026-10-30T10:00:00+08:00"))
    _write(tmp_path / "latency_new.json", _latency("NVIDIA GeForce RTX 3090", "cuda", (1.0, 2.0, 200.0)))
    assert Resolver(tmp_path).render("{{hw.3090.cuda.vps}}") == "200"


def test_c1_games_per_hour(bench_dir: Path):
    assert Resolver(bench_dir).render("{{C1.gph}}") == "328"  # 400 games in 4391 s


def _poster_root(tmp_path: Path, bench_dir: Path) -> Path:
    """A repository copy with the committed results plus the synthetic clean benchmarks."""
    root = tmp_path / "repo"
    shutil.copytree(ROOT / "results", root / "results")
    for f in (root / "results").glob("latency_*.json"):
        f.unlink()
    for f in bench_dir.glob("latency_*.json"):
        shutil.copy(f, root / "results" / f.name)
    shutil.copytree(ROOT / "poster", root / "poster", ignore=shutil.ignore_patterns("figures", "*_filled.pptx"))
    (root / "poster" / "poster_meta.yaml").write_text(
        'author_1: "A. Author"\naffiliation_1: "Some University"\nauthor_2: ""\naffiliation_2: ""\n'
        'repo_url: "https://github.com/example/repo"\n',
        encoding="utf-8",
    )
    return root


def test_render_poster_end_to_end(tmp_path: Path, bench_dir: Path):
    pytest.importorskip("pptx")
    pytest.importorskip("matplotlib")
    pytest.importorskip("qrcode")
    from eval import poster_figures

    sys.path.insert(0, str(ROOT / "poster"))
    import render_poster

    root = _poster_root(tmp_path, bench_dir)
    try:
        Resolver(root / "results").result("C2base")
    except TokenError:
        pytest.skip("the committed results do not include a clean C2 and relevance_baselines.json")
    figures = root / "poster" / "figures"
    poster_figures.build_all(root, figures)
    assert json.loads((figures / "manifest.json").read_text(encoding="utf-8"))["allow_dirty"] is False

    out = root / "poster" / "sensei_gtc2027_poster_filled.pptx"
    path, problems, dropped = render_poster.render(
        template=root / "poster" / "sensei_gtc2027_poster.pptx",
        out=out,
        results=root / "results",
        meta=render_poster.load_meta(root / "poster" / "poster_meta.yaml"),
        figures=figures,
    )
    assert problems == [] and path == out and out.is_file()
    assert sorted(dropped) == ["RTX PRO 6000 CUDA", "RTX PRO 6000 TensorRT"]  # the Blackwell file is dirty
    texts = render_poster.poster_texts(out)
    joined = "\n".join(t for _, t in texts)
    assert "{{" not in joined
    assert "A. Author (Some University)" in joined and "1.3× to 1.5×" in joined
    names = {n for n, _ in texts}
    assert not any(n.startswith("Figure") and n.endswith("label") for n in names)

    sys.path.insert(0, str(ROOT / "scripts"))
    import check_numbers

    assert check_numbers.check_poster(root, root / "results") == []
    figures_manifest = figures / "manifest.json"
    figures_manifest.write_text(json.dumps({"allow_dirty": True}), encoding="utf-8")
    assert any("not built from clean results" in p for p in check_numbers.check_poster(root, root / "results"))


def test_render_poster_reports_missing_names(tmp_path: Path, bench_dir: Path):
    pytest.importorskip("pptx")
    sys.path.insert(0, str(ROOT / "poster"))
    import render_poster

    root = _poster_root(tmp_path, bench_dir)
    meta = {"author_1": "A. Author", "affiliation_1": "", "author_2": "", "affiliation_2": "", "repo_url": ""}
    _, problems, _ = render_poster.render(
        template=root / "poster" / "sensei_gtc2027_poster.pptx",
        out=tmp_path / "x.pptx",
        results=root / "results",
        meta=meta,
        figures=tmp_path / "nofigures",
    )
    assert any("affiliation_1" in p for p in problems)
    assert any("repo_url" in p for p in problems)
    assert not (tmp_path / "x.pptx").exists()


def test_benchmark_output_does_not_dirty_the_next_backend_run(tmp_path: Path):
    import subprocess

    from eval.run import git_info

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    try:
        git("init", "-q")
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git is not available")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / "results").mkdir()
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    git("add", "a.py")
    git("commit", "-q", "-m", "init")
    (tmp_path / "results" / "latency_3090_cuda.json").write_text("{}", encoding="utf-8")
    assert git_info(tmp_path)[1] is True  # the general rule is unchanged
    assert git_info(tmp_path, ignore_untracked=("results/latency_",))[1] is False
    (tmp_path / "b.py").write_text("y = 2\n", encoding="utf-8")
    assert git_info(tmp_path, ignore_untracked=("results/latency_",))[1] is True
