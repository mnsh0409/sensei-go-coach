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
