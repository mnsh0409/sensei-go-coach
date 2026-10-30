"""Meta Model API access: client factory, per-call logging with estimated cost, and the budget warning.

Every call to the API goes through `log_call`, which appends one JSON line to logs/api_calls.jsonl with the token
counts and the cost estimated from the prices in configs/default.yaml (`api` section). The API key is read from
MODEL_API_KEY (.env) and is never printed or logged.
"""

from __future__ import annotations

import copy
import functools
import json
import logging
import math
import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from openai import OpenAI

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.meta.ai/v1"
DEFAULT_MODEL = "muse-spark-1.3"


def _repo_root() -> Path:
    """The repository root for an editable install; the working directory otherwise."""
    here = Path(__file__).resolve().parents[2]
    if (here / "configs" / "default.yaml").is_file():
        return here
    return Path.cwd()


ROOT = _repo_root()
DEFAULT_ENV_FILE = ROOT / ".env"
DEFAULT_CONFIG_PATH = ROOT / "configs" / "default.yaml"
DEFAULT_LOG_PATH = ROOT / "logs" / "api_calls.jsonl"

_warned_paths: set[str] = set()


# ---- configuration -------------------------------------------------------------------------------------------------


@functools.lru_cache(maxsize=8)
def _load_config_cached(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """The runtime config (configs/default.yaml by default), as a fresh copy."""
    target = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    return copy.deepcopy(_load_config_cached(str(target.resolve())))


# ---- client ------------------------------------------------------------------------------------------------------


def make_client(env_file: str | Path | None = DEFAULT_ENV_FILE) -> tuple[OpenAI, str]:
    """An `openai.OpenAI` client for the Meta Model API and the model name.

    Reads MODEL_API_KEY, META_BASE_URL and META_MODEL from the environment after loading env_file (the repo .env by
    default; pass None to skip it). Variables already in the environment take precedence over the file. Raises
    RuntimeError when the key is missing; the key itself never appears in a message or a log.
    """
    if env_file is not None and Path(env_file).is_file():
        load_dotenv(env_file)
    key = os.environ.get("MODEL_API_KEY", "").strip()
    if not key:
        raise RuntimeError("MODEL_API_KEY is not set (put it in .env)")
    base_url = os.environ.get("META_BASE_URL", "").strip() or DEFAULT_BASE_URL
    model = os.environ.get("META_MODEL", "").strip() or DEFAULT_MODEL
    return OpenAI(base_url=base_url, api_key=key), model


# ---- usage and cost ------------------------------------------------------------------------------------------------


def _field(obj: Any, name: str) -> Any:
    """Attribute or mapping key, None when absent (works for SDK objects, dicts and test fakes)."""
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def usage_counts(response: Any) -> tuple[int | None, int | None, int | None]:
    """(prompt_tokens, completion_tokens, cached_tokens) from a chat completion; None where not exposed.

    cached_tokens comes from usage.prompt_tokens_details.cached_tokens (OpenAI layout) or usage.cached_tokens.
    """
    usage = _field(response, "usage")
    if usage is None:
        return None, None, None
    prompt = _as_int(_field(usage, "prompt_tokens"))
    completion = _as_int(_field(usage, "completion_tokens"))
    details = _field(usage, "prompt_tokens_details")
    cached = _as_int(_field(details, "cached_tokens")) if details is not None else None
    if cached is None:
        cached = _as_int(_field(usage, "cached_tokens"))
    return prompt, completion, cached


def estimate_cost_usd(
    prompt_tokens: int | None,
    completion_tokens: int | None,
    cached_tokens: int | None,
    prices: Mapping[str, Any],
) -> float | None:
    """Cost from per-million-token prices (price_input, price_cached_input, price_output).

    prompt_tokens includes the cached tokens, as in the OpenAI usage object. None when no count is available.
    """
    if prompt_tokens is None and completion_tokens is None:
        return None
    prompt = max(prompt_tokens or 0, 0)
    completion = max(completion_tokens or 0, 0)
    cached = min(max(cached_tokens or 0, 0), prompt)
    price_in = float(prices.get("price_input", 0.0))
    price_cached = float(prices.get("price_cached_input", price_in))
    price_out = float(prices.get("price_output", 0.0))
    return ((prompt - cached) * price_in + cached * price_cached + completion * price_out) / 1e6


# ---- the call log ----------------------------------------------------------------------------------------------------


def log_call(
    step: str,
    response: Any,
    t0: float,
    model: str,
    log_path: str | Path | None = None,
    cfg: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one JSON line for a completed API call and return the record.

    The record holds ts, step, model, latency_s (seconds since t0), prompt_tokens, completion_tokens, cached_tokens
    (None when the usage object does not expose them) and cost_usd from the `api` prices of cfg (configs/default.yaml
    when cfg is None). Logs a warning once per process when the logged total passes api.budget_warn_usd.
    """
    config = cfg if cfg is not None else load_config()
    prices = config.get("api", {}) if isinstance(config, Mapping) else {}
    path = Path(log_path) if log_path is not None else DEFAULT_LOG_PATH
    now = time.time()
    prompt, completion, cached = usage_counts(response)
    cost = estimate_cost_usd(prompt, completion, cached, prices)
    record = {
        "ts": round(now, 3),
        "step": step,
        "model": model,
        "latency_s": round(max(now - t0, 0.0), 3),
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cached_tokens": cached,
        "cost_usd": None if cost is None else round(cost, 8),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
    _warn_once(path, config)
    return record


def iter_log(log_path: str | Path | None = None):
    """The records of the call log, skipping blank or malformed lines. Yields nothing for a missing file."""
    path = Path(log_path) if log_path is not None else DEFAULT_LOG_PATH
    if not path.is_file():
        return
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, Mapping):
                yield record


def total_cost_usd(log_path: str | Path | None = None) -> float:
    """Sum of cost_usd over the log; 0.0 for a missing file."""
    total = 0.0
    for record in iter_log(log_path):
        cost = record.get("cost_usd")
        if isinstance(cost, int | float) and not isinstance(cost, bool) and math.isfinite(cost):
            total += float(cost)
    return total


def budget_warning(log_path: str | Path | None, cfg: Mapping[str, Any]) -> str | None:
    """A warning string once the logged total reaches api.budget_warn_usd, else None."""
    api = cfg.get("api", {}) if isinstance(cfg, Mapping) else {}
    limit = api.get("budget_warn_usd") if isinstance(api, Mapping) else None
    if limit is None:
        return None
    path = Path(log_path) if log_path is not None else DEFAULT_LOG_PATH
    total = total_cost_usd(path)
    if total < float(limit):
        return None
    return (
        f"Meta Model API spend logged in {path} is ${total:.2f}, past the warning threshold of "
        f"${float(limit):.2f} (api.budget_warn_usd)."
    )


def _warn_once(path: Path, cfg: Mapping[str, Any]) -> None:
    key = str(path)
    if key in _warned_paths:
        return
    message = budget_warning(path, cfg)
    if message:
        _warned_paths.add(key)
        logger.warning(message)
