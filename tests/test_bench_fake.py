"""scripts/bench_engine.py against the fake engine (offline)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "bench_engine.py"

pytest.importorskip("sensei.engine_katago")
pytest.importorskip("sensei.game_go")


def load_bench():
    spec = importlib.util.spec_from_file_location("bench_engine", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_fixed_positions_are_deterministic():
    bench = load_bench()
    a = bench.fixed_positions(5, seed=7)
    b = bench.fixed_positions(5, seed=7)
    assert [p.moves for p in a] == [p.moves for p in b]
    assert len(a[0].moves) == 10 and len(a[-1].moves) == 160
    assert [p.moves for p in bench.fixed_positions(5, seed=8)] != [p.moves for p in a]


def test_bench_writes_the_contract(tmp_path):
    bench = load_bench()
    rc = bench.main(
        [
            "--fake",
            "--positions",
            "4",
            "--visits",
            "20",
            "--concurrency",
            "1",
            "2",
            "--host",
            "test host",
            "--repeats",
            "2",
            "--out",
            str(tmp_path),
        ]
    )
    assert rc == 0
    path = tmp_path / "latency_test-host_fake.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["kind"] == "engine_benchmark" and data["katago_version"].endswith("fake")
    assert set(data["hardware"]) >= {"host", "gpu", "driver", "cuda", "katago_backend", "cpu", "ram_gb"}
    assert data["hardware"]["katago_backend"] == "fake"
    assert set(data["levels"]) == {"1", "2"}
    for row in data["levels"].values():
        for key in ("analyze", "human_policy"):
            assert row[key]["p50_ms"] > 0 and row[key]["p95_ms"] >= row[key]["p50_ms"]
        assert row["visits_per_s"] > 0 and row["queries_per_s"] > 0
        assert row["repeats"] == 2 and len(row["visits_per_s_runs"]) == 2
        lo, hi = row["visits_per_s_ci95"]
        assert lo <= row["visits_per_s"] <= hi
        assert len(row["analyze"]["p50_ms_runs"]) == 2
    assert data["repeats"] == 2 and data["katago_build"] is None  # fake engine: no binary to fingerprint


def test_t_interval_and_build_fingerprint(tmp_path):
    bench = load_bench()
    assert bench.t_interval([10.0]) is None
    lo, hi = bench.t_interval([10.0, 12.0, 11.0, 13.0, 9.0])  # mean 11, sd 1.58, t(0.975, 4) = 2.776
    assert abs(lo - 9.0) < 0.05 and abs(hi - 13.0) < 0.05
    appdir = tmp_path / "squashfs-root"
    (appdir / "usr" / "bin").mkdir(parents=True)
    (appdir / "usr" / "bin" / "katago").write_bytes(b"binary")
    app_run = appdir / "AppRun"
    app_run.write_text("not runnable", encoding="utf-8")
    info = bench.katago_build(str(app_run), "cuda12.8-cudnn9.8")
    assert info["label"] == "cuda12.8-cudnn9.8" and info["binary"] == "katago"
    assert info["sha256"] == "9a3a45d01531a20e89ac6ae10b0b0beb0492acd7216a368aa062d1a5fecaf9cd"  # sha256(b"binary")
