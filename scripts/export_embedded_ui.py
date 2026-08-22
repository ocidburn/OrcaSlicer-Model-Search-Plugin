"""Print the embedded WebView document exactly as the plugin serves it.

`check_embedded_js.py` pulls the script out of the source literal for a syntax
check. The behavioural tests need the whole document *after* the substitutions
the module performs at import time, so this loads the module rather than
scraping it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PLUGIN_PATH = Path(__file__).resolve().parents[1] / "src" / "search_engine.py"


def load_page() -> str:
    spec = importlib.util.spec_from_file_location("search_engine_ui_export", PLUGIN_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load the plugin from {PLUGIN_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        return module.PAGE
    finally:
        sys.modules.pop(spec.name, None)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    print(load_page())


if __name__ == "__main__":
    main()
