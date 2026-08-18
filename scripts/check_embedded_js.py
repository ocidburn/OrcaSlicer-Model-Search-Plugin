"""Extract the embedded WebView JavaScript for an external syntax check."""

from __future__ import annotations

import re
from pathlib import Path

PLUGIN_PATH = Path(__file__).resolve().parents[1] / "src" / "search_engine.py"


def main() -> None:
    source = PLUGIN_PATH.read_text(encoding="utf-8")
    page = re.search(r'PAGE\s*=\s*r?"""(.*?)"""', source, re.DOTALL)
    if page is None:
        raise RuntimeError("PAGE literal not found")
    script = re.search(r"<script>(.*?)</script>", page.group(1), re.DOTALL)
    if script is None:
        raise RuntimeError("Embedded script not found")
    print(script.group(1))


if __name__ == "__main__":
    main()
