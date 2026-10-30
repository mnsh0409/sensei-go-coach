"""Offline tests for sensei.metaapi with a fake response object and a temporary log path."""

from __future__ import annotations

import json
import logging
import math
import time
from types import SimpleNamespace

import pytest

metaapi = pytest.importorskip("sensei.metaapi")

CFG = {
    "api": {"price_input": 1.25, "price_cached_input": 0.15, "price_output": 4.25, "budget_warn_usd": 100},
    "explainer": {"max_sentences": 4},
}


def fake_response(prompt_tokens=1000, completion_tokens=200, cached_tokens=None):
    details = None if cached_tokens is None else SimpleNamespace(cached_tokens=cached_tokens)
    usage = SimpleNamespace(
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, prompt_tokens_details=details
    )
    message = SimpleNamespace(content="ok", tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


def test_log_call_writes_one_json_line_with_cost(tmp_path):
    log = tmp_path / "logs" / "api_calls.jsonl"
    t0 = time.time() - 0.5
    rec = metaapi.log_call("explain", fake_response(1000, 200, cached_tokens=400), t0, "muse-spark-1.3", log, CFG)

    expected = ((1000 - 400) * 1.25 + 400 * 0.15 + 200 * 4.25) / 1e6
    assert math.isclose(rec["cost_usd"], expected, rel_tol=1e-9)
    assert rec["step"] == "explain"
    assert rec["model"] == "muse-spark-1.3"
    assert rec["prompt_tokens"] == 1000 and rec["completion_tokens"] == 200 and rec["cached_tokens"] == 400
    assert rec["latency_s"] >= 0.5
    assert isinstance(rec["ts"], float)

    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == rec


def test_log_call_without_cached_tokens(tmp_path):
    log = tmp_path / "api.jsonl"
    rec = metaapi.log_call("ask", fake_response(300, 50), time.time(), "m", log, CFG)
    assert rec["cached_tokens"] is None
    assert math.isclose(rec["cost_usd"], (300 * 1.25 + 50 * 4.25) / 1e6, rel_tol=1e-9)


def test_log_call_without_usage_records_none(tmp_path):
    log = tmp_path / "api.jsonl"
    resp = SimpleNamespace(choices=[], usage=None)
    rec = metaapi.log_call("models", resp, time.time(), "m", log, CFG)
    assert rec["prompt_tokens"] is None and rec["completion_tokens"] is None and rec["cost_usd"] is None
    assert metaapi.total_cost_usd(log) == 0.0


def test_log_call_reads_prices_from_default_config(tmp_path):
    """Without cfg, the prices come from configs/default.yaml."""
    log = tmp_path / "api.jsonl"
    rec = metaapi.log_call("explain", fake_response(1_000_000, 0), time.time(), "m", log)
    cfg = metaapi.load_config()
    assert math.isclose(rec["cost_usd"], cfg["api"]["price_input"], rel_tol=1e-9)


def test_total_cost_sums_the_log_and_skips_bad_lines(tmp_path):
    log = tmp_path / "api.jsonl"
    r1 = metaapi.log_call("a", fake_response(1000, 100), time.time(), "m", log, CFG)
    r2 = metaapi.log_call("b", fake_response(2000, 300, cached_tokens=1000), time.time(), "m", log, CFG)
    with log.open("a", encoding="utf-8") as f:
        f.write("not json\n\n")
        f.write(json.dumps({"step": "no cost"}) + "\n")
    assert math.isclose(metaapi.total_cost_usd(log), r1["cost_usd"] + r2["cost_usd"], rel_tol=1e-9)
    assert metaapi.total_cost_usd(tmp_path / "missing.jsonl") == 0.0


def test_budget_warning_only_past_threshold(tmp_path, caplog):
    log = tmp_path / "api.jsonl"
    cfg = {"api": {**CFG["api"], "budget_warn_usd": 0.01}}
    assert metaapi.budget_warning(log, cfg) is None  # no log yet
    metaapi.log_call("a", fake_response(1000, 100), time.time(), "m", log, cfg)  # about 0.0017 USD
    assert metaapi.budget_warning(log, cfg) is None
    with caplog.at_level(logging.WARNING, logger="sensei.metaapi"):
        for _ in range(6):  # pushes the total past 0.01 USD
            metaapi.log_call("a", fake_response(1000, 100), time.time(), "m", log, cfg)
    warning = metaapi.budget_warning(log, cfg)
    assert isinstance(warning, str) and "0.01" in warning
    # The warning is logged once, not on every later call.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "budget" in r.getMessage()]
    assert len(warnings) == 1
    assert metaapi.budget_warning(log, {"api": {}}) is None


def test_make_client_reads_environment_and_never_logs_the_key(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "sk-test-secret-key")
    monkeypatch.setenv("META_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("META_MODEL", "muse-test")
    client, model = metaapi.make_client(env_file=None)
    assert model == "muse-test"
    assert str(client.base_url).startswith("https://example.invalid/v1")
    assert type(client).__name__ == "OpenAI"

    log = tmp_path / "api.jsonl"
    metaapi.log_call("models", fake_response(1, 1), time.time(), model, log, CFG)
    assert "sk-test-secret-key" not in log.read_text(encoding="utf-8")
    assert "sk-test-secret-key" not in repr(client)


def test_make_client_loads_env_file_without_overriding_environment(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("MODEL_API_KEY=from-file\nMETA_MODEL=file-model\n", encoding="utf-8")
    # setenv first so monkeypatch records the original state and removes the value load_dotenv writes.
    monkeypatch.setenv("MODEL_API_KEY", "placeholder")
    monkeypatch.delenv("MODEL_API_KEY")
    monkeypatch.delenv("META_BASE_URL", raising=False)
    monkeypatch.setenv("META_MODEL", "env-model")
    _client, model = metaapi.make_client(env_file=env)
    assert model == "env-model"
    assert str(_client.base_url).startswith(metaapi.DEFAULT_BASE_URL)


def test_make_client_fails_without_key(monkeypatch):
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="MODEL_API_KEY"):
        metaapi.make_client(env_file=None)


def test_usage_counts_accepts_dicts():
    resp = {"usage": {"prompt_tokens": 10, "completion_tokens": 5, "cached_tokens": 4}}
    assert metaapi.usage_counts(resp) == (10, 5, 4)
    assert metaapi.usage_counts(SimpleNamespace()) == (None, None, None)
