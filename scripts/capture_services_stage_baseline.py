"""Capture structural baseline for services/* pipeline-stage migration.

Same pattern as capture_agent_proxy_baseline.py but extended to cover
proxy + services + compression + cache + routing + telemetry public
symbol surfaces, plus pytest collection + tip-conformance verdict +
version.

Phase D of each stage migration (S-PS-01..S-PS-05) diffs against this
baseline to detect public-API drift.
"""

from __future__ import annotations

import importlib
import json
import pkgutil
import subprocess
import sys
import warnings
from pathlib import Path

BASELINE_DIR = Path("tests/baselines/services-stage-logic-2026-04-20")

PACKAGES = [
    "tokenpak.proxy",
    "tokenpak.services",
    "tokenpak.compression",
    "tokenpak.cache",
    "tokenpak.routing",
    "tokenpak.telemetry",
]


def public_names(module_name: str) -> list[str]:
    import types

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        mod = importlib.import_module(module_name)
    all_ = getattr(mod, "__all__", None)
    if all_ is not None:
        return sorted(all_)
    skip = {"annotations", "warnings"}
    return sorted(
        n
        for n in dir(mod)
        if not n.startswith("_")
        and n not in skip
        and not isinstance(getattr(mod, n, None), types.ModuleType)
    )


def walk_package(package_name: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        try:
            root = importlib.import_module(package_name)
        except Exception as e:
            return {package_name: [f"<IMPORT-ERROR: {type(e).__name__}: {e}>"]}
    out[package_name] = public_names(package_name)
    for info in pkgutil.walk_packages(root.__path__, prefix=package_name + "."):
        try:
            out[info.name] = public_names(info.name)
        except Exception as e:
            out[info.name] = [f"<IMPORT-ERROR: {type(e).__name__}: {e}>"]
    return out


def capture() -> None:
    BASELINE_DIR.mkdir(parents=True, exist_ok=True)

    full: dict[str, dict[str, list[str]]] = {}
    for pkg in PACKAGES:
        full[pkg] = walk_package(pkg)
    (BASELINE_DIR / "public_symbols.json").write_text(
        json.dumps(full, indent=2, sort_keys=True) + "\n"
    )

    version = subprocess.run(
        [sys.executable, "-m", "tokenpak", "--version"],
        capture_output=True,
        text=True,
    )
    (BASELINE_DIR / "tokenpak_version.txt").write_text(
        (version.stdout or "").strip() + "\n" + (version.stderr or "").strip() + "\n"
    )

    collect = subprocess.run(
        ["pytest", "-q", "--tb=no", "--co"],
        capture_output=True,
        text=True,
    )
    (BASELINE_DIR / "pytest_collect_stdout.txt").write_text(collect.stdout)
    (BASELINE_DIR / "pytest_collect_returncode.txt").write_text(f"{collect.returncode}\n")

    tip = subprocess.run(
        [sys.executable, "scripts/tip_conformance_check.py"],
        capture_output=True,
        text=True,
    )
    (BASELINE_DIR / "tip_conformance_stdout.txt").write_text(tip.stdout)
    (BASELINE_DIR / "tip_conformance_returncode.txt").write_text(f"{tip.returncode}\n")

    total_modules = sum(len(v) for v in full.values())
    print(f"baseline captured -> {BASELINE_DIR}")
    print(f"  packages: {len(PACKAGES)}  modules total: {total_modules}")
    print(f"  pytest --co exit: {collect.returncode}")
    print(f"  tip-check exit: {tip.returncode}")


if __name__ == "__main__":
    capture()
