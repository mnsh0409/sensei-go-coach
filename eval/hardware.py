"""The hardware block written into every result file, latency file and the app's per-turn log, and the engine
metadata (KataGo version, net and analysis-config files with SHA-256, thread settings) of every engine result.

Pure standard library plus `nvidia-smi` when present; never raises on a machine without a GPU.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any


def _nvidia_smi(args: list[str]) -> str | None:
    exe = shutil.which("nvidia-smi")
    if exe is None:
        return None
    try:
        out = subprocess.run(
            [exe, *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10, check=False
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else None


CUDA_VERSION_RE = re.compile(r"CUDA(?:\s+(?:Driver|UMD|Runtime))?\s+Version\s*:\s*([0-9]+(?:\.[0-9]+)*)")
"""The CUDA version in nvidia-smi's header ("CUDA Version: 12.8"; some drivers print "CUDA Driver Version")."""


def _cuda_version() -> str | None:
    text = _nvidia_smi([])
    if not text:
        return None
    m = CUDA_VERSION_RE.search(text)
    return m.group(1) if m else None


def _ram_gb() -> float | None:
    try:
        if hasattr(os, "sysconf"):
            pages = os.sysconf("SC_PHYS_PAGES")
            page_size = os.sysconf("SC_PAGE_SIZE")
            return round(pages * page_size / 1024**3, 1)
    except (ValueError, OSError, AttributeError):
        pass
    try:  # Windows
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.dwLength = ctypes.sizeof(MemoryStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
            return round(status.ullTotalPhys / 1024**3, 1)
    except (AttributeError, OSError):
        pass
    return None


def hardware_block(katago_backend: str | None = None) -> dict[str, Any]:
    """Return the `hardware` block defined in eval/README.md. Missing values are None, never absent."""
    gpu = _nvidia_smi(["--query-gpu=name,driver_version", "--format=csv,noheader"])
    gpu_name, driver = (None, None)
    if gpu:
        first = gpu.splitlines()[0]
        parts = [p.strip() for p in first.split(",")]
        gpu_name = parts[0] if parts else None
        driver = parts[1] if len(parts) > 1 else None
    return {
        "host": platform.node(),
        "os": f"{platform.system()} {platform.release()}",
        "gpu": gpu_name,
        "driver": driver,
        "cuda": _cuda_version(),
        "katago_backend": katago_backend,
        "cpu": platform.processor() or platform.machine(),
        "ram_gb": _ram_gb(),
        "python": platform.python_version(),
    }


def sha256_file(path: str | os.PathLike[str] | None) -> str | None:
    """SHA-256 of a file, or None when the path is empty or not a file (the fake engine's placeholder names)."""
    if not path or not Path(path).is_file():
        return None
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


SETTINGS_RECORDED = (
    "numAnalysisThreads",
    "numSearchThreadsPerAnalysisThread",
    "wideRootNoise",
    "ignorePreRootHistory",
    "rootNumSymmetriesToSample",
    "reportAnalysisWinratesAs",
    "nnCacheSizePowerOfTwo",
)
"""Analysis-config keys copied into result files (None when the config leaves them at KataGo's default)."""


def analysis_settings(path: str | os.PathLike[str] | None) -> dict[str, Any]:
    """The SETTINGS_RECORDED values set in a KataGo analysis config (ints and floats parsed, the rest as text)."""
    out: dict[str, Any] = {key: None for key in SETTINGS_RECORDED}
    if not path or not Path(path).is_file():
        return out
    pattern = re.compile(r"\s*(" + "|".join(SETTINGS_RECORDED) + r")\s*=\s*([^#\s]+)")
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        m = pattern.match(line)
        if not m:
            continue
        raw = m.group(2)
        try:
            value: Any = int(raw)
        except ValueError:
            try:
                value = float(raw)
            except ValueError:
                value = raw
        out[m.group(1)] = value
    return out


def analysis_threads(path: str | os.PathLike[str] | None) -> dict[str, int | None]:
    """numAnalysisThreads and numSearchThreadsPerAnalysisThread from a KataGo analysis config (None if absent)."""
    settings = analysis_settings(path)
    return {k: settings[k] for k in ("numAnalysisThreads", "numSearchThreadsPerAnalysisThread")}


def engine_metadata(engine: Any) -> dict[str, Any]:
    """`katago_version`, `nets` and `analysis_config` for a result file (eval/README.md), from a KataGoEngine.

    Works on any object with `version()`, `model`, `human_model` and `config`; missing pieces become None.
    """
    version = None
    if hasattr(engine, "version"):
        try:
            version = engine.version()
        except Exception:  # noqa: BLE001 - metadata must never sink a run
            version = None
    model = getattr(engine, "model", None)
    human = getattr(engine, "human_model", None)
    config = getattr(engine, "config", None)
    nets = None
    if model:
        nets = {
            "main": {"file": Path(model).name, "sha256": sha256_file(model)},
            "human": {"file": Path(human).name if human else None, "sha256": sha256_file(human)},
        }
    analysis_config = None
    if config:
        overrides = getattr(engine, "QUERY_OVERRIDES", None)
        analysis_config = {
            "file": Path(config).name,
            "sha256": sha256_file(config),
            **analysis_settings(config),
            "query_overrides": None if overrides is None else {k: dict(v) for k, v in overrides.items()},
        }
    return {"katago_version": version, "nets": nets, "analysis_config": analysis_config}


if __name__ == "__main__":
    import json

    print(json.dumps(hardware_block(), indent=2))
