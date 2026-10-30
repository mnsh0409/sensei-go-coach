"""Live Meta Model API checks. Run with  pytest -q -m network tests/test_meta_api.py"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

from tests.conftest import require_env

pytestmark = pytest.mark.network
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "meta_smoke_test.py"


def load_script():
    spec = importlib.util.spec_from_file_location("meta_smoke_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def client_and_mod():
    require_env("MODEL_API_KEY")
    from openai import OpenAI

    mod = load_script()
    return OpenAI(base_url=mod.BASE_URL, api_key=os.environ["MODEL_API_KEY"]), mod


def test_models(client_and_mod):
    client, mod = client_and_mod
    assert mod.step_models(client)


def test_tool_round_trip(client_and_mod):
    client, mod = client_and_mod
    assert mod.step_tools(client)
