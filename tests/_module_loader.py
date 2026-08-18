"""Shared loader for the standalone OrcaSlicer plugin module."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

PLUGIN_PATH = Path(__file__).resolve().parents[1] / "src" / "search_engine.py"


def load_plugin(module_name: str) -> ModuleType:
    """Load the deployment module under an isolated name for a test case."""
    spec = importlib.util.spec_from_file_location(module_name, PLUGIN_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load plugin module from {PLUGIN_PATH}")

    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous
    return module
