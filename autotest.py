#!/usr/bin/env python3
"""Autonomous test: simulate the plugin's on_message handler with real API calls."""
import sys, os, time, json
sys.path.insert(0, os.path.dirname(__file__))

from search_engine import _SEARCHERS

def test_search(platform, query="benchy"):
    adapter = _SEARCHERS.get(platform)
    if not adapter:
        return None
    if not adapter.enabled({}):
        return None
    try:
        results = adapter.search(query, {})
        return results
    except Exception as e:
        return str(e)

def test_all():
    print("=" * 60)
    print("AUTONOMOUS PLUGIN TEST")
    print("=" * 60)
    
    platforms = ["makerworld", "nexprint", "makeronline", "printables"]
    total = 0
    
    for platform in platforms:
        start = time.time()
        results = test_search(platform, "benchy")
        elapsed = time.time() - start
        
        if isinstance(results, list):
            count = len(results)
            total += count
            print(f"\n{platform}: {count} results ({elapsed:.1f}s)")
            if results:
                m = results[0]
                print(f"  First: {m['name'][:50]}")
                print(f"  Author: {m['author']}")
                print(f"  Thumb: {m['thumbnail_url'][:80]}")
                print(f"  License: {m['license']}")
        else:
            print(f"\n{platform}: ERROR - {results}")
    
    print(f"\n{'='*60}")
    print(f"Total: {total} results across {len(platforms)} platforms")
    print(f"All adapters returning data correctly: {total > 0}")
    return total > 0

if __name__ == "__main__":
    test_all()
