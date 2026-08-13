"""Exercise the real PrintablesSearcher from search_engine.py: search, then
resolve a result to a file URL and confirm the bytes are actually served."""
import importlib.util
import os
import sys
import urllib.request

spec = importlib.util.spec_from_file_location(
    "se", os.path.join(os.path.dirname(os.path.abspath(__file__)), "search_engine.py"))
se = importlib.util.module_from_spec(spec)
sys.modules["se"] = se
spec.loader.exec_module(se)

results = se.PrintablesSearcher.search("benchy", {})
assert results, "search returned nothing"
print("results:", len(results))
for r in results[:3]:
    print("  -", r["name"], "|", r["author"], "|", r["license"], "|", r["url"])

first = results[0]
assert first["platform"] == "Printables"
assert first["url"].startswith("https://www.printables.com/model/")
assert first["license"], "license missing"
assert first["thumbnail_url"].startswith("https://media.printables.com/"), first["thumbnail_url"]

resolver = se._FILE_RESOLVERS["Printables"]
files = resolver(first["url"])
assert files, "no files resolved"
print("files:", [(f["name"], f["url"][:70] + "...") for f in files[:2]])

req = urllib.request.Request(files[0]["url"], headers={"User-Agent": se._BROWSER_UA})
with urllib.request.urlopen(req, timeout=30) as resp:
    head = resp.read(64)
    print("file HTTP", resp.status, resp.headers.get("content-type"),
          "bytes", resp.headers.get("content-length"))
assert resp.status == 200 and head, "file did not download"
print("\nALL CHECKS PASSED")
