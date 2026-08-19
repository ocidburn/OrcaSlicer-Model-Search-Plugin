"""Opt-in live search smoke test for public model catalogs.

External services change independently of this repository, so this script is
kept separate from deterministic unit tests. It performs search requests only;
it never downloads model files or uses saved account credentials.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PLUGIN_PATH = Path(__file__).resolve().parents[1] / "src" / "search_engine.py"
SPEC = importlib.util.spec_from_file_location("search_engine_live", PLUGIN_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Could not load plugin module")
PLUGIN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PLUGIN)

SEARCHES = (
    ("printables", "benchy"),
    ("makerworld", "benchy"),
    ("makeronline", "benchy"),
    ("nexprint", "benchy"),
    ("cults3d", "benchy"),
    ("yeggi", "benchy"),
    ("stlfinder", "benchy"),
    ("thangs", "benchy"),
    ("crealitycloud", "benchy"),
    ("smithsonian", "apollo"),
    ("nasa", "apollo"),
    ("nih3d", "heart"),
    ("youmagine", "cube"),
    ("pinshape", "benchy"),
)


def main() -> int:
    failures = []
    for key, query in SEARCHES:
        spec = PLUGIN._platform(key)
        try:
            rows = spec.adapter.search(query, None, {"sort": "relevance"})
        except PLUGIN.BrowserRequired as exc:
            if exc.url:
                print(f"OPEN {key}: interactive browser fallback ({exc.url})")
                continue
            failures.append(f"{key}: {exc}")
            print(f"FAIL {key}: {exc}")
            continue
        except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
            failures.append(f"{key}: {exc}")
            print(f"FAIL {key}: {exc}")
            continue
        count = len(rows)
        print(f"OK   {key}: {count} result(s)")
        if not count:
            failures.append(f"{key}: no results")
    if failures:
        print("\nLive smoke failures:")
        for failure in failures:
            print(f"- {failure}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
