#!/usr/bin/env python3
"""Standalone test for search adapters. No OrcaSlicer needed."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from search_engine import PrintablesSearcher, ThingiverseSearcher, MakerWorldSearcher, GrabcadSearcher, MakeronlineSearcher

TOKENS_FILE = os.path.join(os.path.dirname(__file__), "test_tokens.json")

def load_tokens():
    if not os.path.exists(TOKENS_FILE):
        template = {
            "printables_token": "",
            "thingiverse_token": "",
            "makerworld_token": "",
            "grabcad_token": "",
        }
        with open(TOKENS_FILE, "w") as f:
            json.dump(template, f, indent=2)
        print(f"Created {TOKENS_FILE} — fill in your tokens and re-run")
        return template
    with open(TOKENS_FILE) as f:
        return json.load(f)

def test_search(name, searcher, tokens, query="calibration cube"):
    enabled = searcher.enabled(tokens)
    print(f"\n{'='*60}")
    print(f"{name}")
    print(f"  enabled: {enabled}")
    if not enabled:
        print(f"  SKIP — no token provided")
        return
    try:
        results = searcher.search(query, tokens)
        print(f"  results: {len(results)}")
        for i, r in enumerate(results[:3]):
            print(f"  [{i+1}] {r['name'][:60]}")
            print(f"       by {r['author']} | {r['license']} | {r['download_url'][:80]}")
    except Exception as e:
        print(f"  ERROR: {e}")


if __name__ == "__main__":
    tokens = load_tokens()
    query = sys.argv[1] if len(sys.argv) > 1 else "calibration cube"

    print(f"Query: '{query}'")
    test_search("Printables", PrintablesSearcher(), tokens, query)
    test_search("Makeronline", MakeronlineSearcher(), tokens, query)
    test_search("Thingiverse", ThingiverseSearcher(), tokens, query)
    test_search("MakerWorld", MakerWorldSearcher(), tokens, query)
    test_search("GrabCAD", GrabcadSearcher(), tokens, query)
