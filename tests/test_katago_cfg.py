"""The shipped KataGo analysis config (configs/katago_analysis.cfg) has the settings CLAUDE.md asks for."""

from __future__ import annotations

from pathlib import Path

from eval.hardware import analysis_settings

CFG = Path(__file__).resolve().parents[1] / "configs" / "katago_analysis.cfg"


def test_shipped_analysis_config():
    s = analysis_settings(CFG)
    assert s["numAnalysisThreads"] == 8 and s["numSearchThreadsPerAnalysisThread"] == 2
    assert s["reportAnalysisWinratesAs"] == "BLACK"  # KataGo's default, overridden per query by the wrapper
    assert s["wideRootNoise"] is None  # KataGo's analysis default (eval/README.md, "Engine settings")
    text = CFG.read_text(encoding="utf-8")
    assert "logDir = logs/katago" in text and text.count("  # Sensei:") == 3  # the three changed lines
