"""Exercise the bounded Windows IPC path without a running OrcaSlicer window."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PLUGIN_PATH = Path(__file__).resolve().parents[1] / "src" / "search_engine.py"


def main() -> None:
    if sys.platform != "win32":
        raise RuntimeError("Windows IPC smoke test must run on Windows")
    spec = importlib.util.spec_from_file_location(
        "search_engine_ipc_smoke", PLUGIN_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load plugin module from {PLUGIN_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    ok, detail = module._send_windows_instance_message(sys.executable, [])
    if ok or "could not find" not in detail:
        raise RuntimeError(f"Unexpected Windows IPC result: {(ok, detail)!r}")
    print(f"Windows IPC smoke OK: {detail}")


if __name__ == "__main__":
    main()
