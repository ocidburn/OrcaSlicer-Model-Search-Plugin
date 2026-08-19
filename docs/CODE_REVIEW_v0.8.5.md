# Code Review — v0.8.5

Review of `src/search_engine.py` and its test suite as of commit `03cb6d7`
(v0.8.5). Eleven findings, each reproduced against the running module before
being written down.

**Status: applied.** The changes were landed in `c091aa0` and released as
v0.8.7, re-derived against the then-current v0.8.6 code rather than by applying
the original patch verbatim. This document is kept as the record of *why* those
changes were made — the measurements below are not recoverable from the diff —
and of what was deliberately left alone.

## Method

The whole Python surface was read: helpers, `AuthManager`, the seventeen
catalog adapters, the Orca import/IPC layer, and the `SearchEngineScript`
coordinator. Findings were then confirmed empirically — by loading the module
with a stubbed `orca`, instrumenting the relevant call, and measuring — rather
than by reading alone. Every number quoted below comes from such a run.

The proposal was validated against the gates CI enforces, plus a behavioural
parity harness that ran the original and reworked coordinators side by side
over identical fixtures and compared merged result ordering, per-source
statistics, and popularity scores.

## Findings

Ordered by impact. F1–F3 were the ones worth acting on first.

### F1. Every selected portal was searched sequentially — high, performance

`SearchEngineScript._do_search` looped over the chosen portals and awaited each
in turn. Portal calls are independent network requests with 30-second timeouts,
so a search cost the *sum* of every portal's latency. With all seventeen
portals ticked, one slow catalog stalled the entire result set and a few
timeouts pushed the search into minutes.

Measured with eight stub portals whose sleeps totalled 1.62 s:

| | wall clock |
|---|---|
| before | 1.62 s (the sum) |
| after | 0.40 s (the slowest single portal) |

Fanned out across a `ThreadPoolExecutor` bounded by `_SEARCH_WORKERS = 8`,
reused for the `search_more` paging path. Per-portal work moved into
`_fetch_search_page`, which never raises and returns `(payload, stats, stop)`,
keeping error classification exactly where it was.

Merge order was preserved deliberately: results are combined in the order the
user selected the portals, not in completion order, so `source_rank` — and
therefore relevance sorting — does not depend on which worker finished first.
The parity harness confirmed merged ordering and per-source statistics were
byte-identical across the success, exhausted, and error branches.

### F2. The anonymous HTML fetch bypassed the SSRF and redirect policy — high, security

`_fetch_html` had two paths. The authenticated one went through
`AuthManager.request`, which re-derives scoped headers after every hop, caps
redirects at `_MAX_REDIRECTS`, and calls `_reject_obvious_local_target` on each
URL. The anonymous one called `Session.get(..., allow_redirects=True)`
directly, so requests followed redirects itself with none of those checks.

Reproduced against v0.8.5:

```
_fetch_html("http://127.0.0.1:8080/admin")
  -> request issued to 127.0.0.1                     (no guard)
_reject_obvious_local_target("http://127.0.0.1:8080/admin")
  -> ValueError: Refusing a private/local download URL
```

The initial URLs are hardcoded catalog templates, so this was not directly
attacker-controlled; the exposure was a catalog redirecting the anonymous fetch
at an internal address, with no hop limit either. Both paths now go through
`AuthManager.request`; the same call raises `ValueError` with zero requests
issued.

Trade-off, stated plainly: the anonymous path now performs a DNS resolution per
hop, which it previously skipped. The authenticated path already paid that
cost.

### F3. HTTP sessions were created and never closed — medium, resource leak

The module created `requests.Session` objects in eight places and closed one
(`_download_stream`). Each session owns a `urllib3` connection pool. The worst
case was `_validated_candidates`, which probes up to thirty download candidates
and opened a session per probe:

| call | opened | closed | leaked |
|---|---|---|---|
| `_probe_download` × 30 | 30 | 0 | **30** |
| `_fetch_html` × 10 | 10 | 0 | **10** |

Both counters are now zero. The review scoped its patch to `_fetch_html`,
`_probe_download`, and `Nih3DSearcher.search`, listing the four adapter-level
sites as a follow-up; `c091aa0` closed those too, so `session.close()` now
appears at nine sites.

### F4. Popularity scoring re-sorted once per result — medium, performance

`_add_popularity_scores` computed each item's percentile with
`sorted(...).index(raw)` *inside* the per-item loop, sorting the platform's
score list once per row. On 300 rows from one platform: `sorted()` called
**300** times, now **1**.

Positions are precomputed per platform in `_popularity_ranks`. Scores are
identical — `setdefault` preserves the first-occurrence semantics `list.index`
had for ties.

### F5. The credential store was re-read once per result row — medium, performance

`_load_search_page` called `self.auth.authenticated(spec.key)` inside its
per-row loop, and that chain reaches `AuthStore.load`, which opens and parses
`sessions.json` every time. Measured on a single 30-row page: **30** reads,
now **1**. Across seventeen portals at thirty rows each that was roughly 510
file opens per search.

`authenticated` and `importable` are per-adapter facts and were hoisted out of
the loop.

### F6. Per-source counters rescanned the merged list — low, performance

Each source's `loaded` count was `sum(item.get("_platform_key") == key for item
in results)` *inside* the portal loop — O(portals × results). Replaced by a
single `_apply_loaded_counts` pass, which also removed two near-identical
nine-key stat dictionaries per branch in favour of a `_source_stats` factory.

### F7. Platform behaviour was encoded as hardcoded key tuples — medium, maintainability

`PlatformSpec` is described in the README as the single source of truth, but
five call sites bypassed it and tested membership in literal tuples:

- `("grabcad", "cults3d")` in `_fetch_html`, `_probe_result`, and
  `_collect_page_candidates` — "a 401/403 here means the pasted browser session
  died".
- `("makerworld", "crealitycloud", "nexprint")` in `_resolve_import` and
  `_profile_choices` — "import goes through a print-profile picker".

Adding a portal in either category meant finding all five sites by hand. Now
two declarative flags on `PlatformSpec` — `session_recheck` and
`profile_picker` — plus a `_session_recheck(key)` helper.

### F8. The UI duplicated the display-to-key map — medium, latent maintainability

The embedded JavaScript carried a hand-written `platformKey()` object mapping
each display name to its registry key, with `String(display).toLowerCase()` as
a fallback.

It was **correct at the time** — all seventeen entries matched the registry,
checked programmatically. But nothing guarded it, and four display names do not
lowercase to their key (`Creality Cloud`, `NASA 3D Resources`, `NIH 3D`,
`Smithsonian 3D`), so a portal added to the registry and forgotten in the
JavaScript would have fallen through to a wrong key, breaking `isAuthed` and
`modelIdentity` in the UI rather than failing loudly. The object is now
generated from `_PLATFORM_SPECS` and pinned by a test.

### F9. An empty file selection discarded the pending import — low, correctness

`_import_selected` cleared `_pending_import_model` and `_pending_import_files`
*before* validating the selection. Submitting with nothing ticked reported
"Select at least one file to import." while the file list it refers to had
already been thrown away, forcing a restart of the whole import. The pending
state is now cleared only once a non-empty selection is confirmed.

### F10. Redundant double-assignment in Anycubic token normalization — low, clarity

```python
m = re.search(r"...mo_access_token=([^;\s]+)", token)
if m:
    token = m.group(1).strip()
else:
    m = re.match(r"...^(?:XX-Token|Authorization)...", token)
if m:                          # re-runs for the first branch too
    token = m.group(1).strip()
```

The second `if m:` re-applied `group(1)` to the match the first branch had
already consumed. Idempotent, so not a bug, but it reads as one. Replaced by a
single `re.search(...) or re.match(...)`; all four accepted token shapes
normalize identically before and after.

### F11. The two model-extension tuples were maintained separately — low, clarity

`_MODEL_FILE_EXTS` and `_LOADABLE_MODEL_EXTS` listed the same twelve extensions
3400 lines apart, differing only by `.zip`. Now derived:
`_MODEL_FILE_EXTS = _LOADABLE_MODEL_EXTS + (".zip",)`.

## Still open

Considered during the review and deliberately not changed. Re-checked against
the current tree; all four still stand.

| Item | Why it was left alone |
|---|---|
| `_do_search_more` complexity, C(13) | Splitting it further would touch the paging semantics the parity harness pins down. Worth a separate, separately-verified change. |
| `direct_import` defaults to `True` | `_normalize_result` marks any result without an explicit flag as directly importable, so HTML-catalog rows pass the "direct import only" filter and can still fail later with `BrowserRequired`. Optimistic by design — narrowing it would hide genuinely importable models. A behaviour question for the maintainer, not a defect. |
| Partial downloads left on disk | If file 3 of 5 fails, the first two stay in the download directory with no cleanup. Arguably correct for a downloads folder. |
| Adapter shape duplication | The four profile-picker adapters share a lot of structure (`_profile_record`, cover resolution, signed-URL extraction). A common base would help, but it is a large change with real regression risk across four live APIs, and is better done per-adapter with fixtures. |

## Verification performed

Every gate below is the one CI runs, executed against the reworked tree at
review time:

| Gate | Result |
|---|---|
| `py_compile` over `src`, `scripts`, `tests` | pass |
| `node --check` on the extracted embedded JS | pass |
| `python -m unittest discover -s tests -t .` | 113 passed (111 before; 2 added) |
| `ruff check .` | clean |
| `vulture --min-confidence 80` | clean |
| `bandit -q -r src/search_engine.py` | clean |
| `radon cc -n D` (must be empty) | empty |

Pyright was not run locally — it was not installed in the review environment.
The changes introduced no new imports or annotations; CI is the authority on
that gate.

Behavioural parity, before versus after, over identical fixtures:

| Property | Result |
|---|---|
| Merged result ordering, page 1 | identical |
| Merged result ordering after `search_more` | identical |
| Per-source statistics (`loaded`, `page`, `has_more`) | identical |
| Popularity scores over 400 mixed rows | identical |

The paging fixture deliberately exercised all three branches: portals that keep
paging, one that reports itself exhausted, and one that raises on page 2.

Two regression tests were added and are present in the current suite:

- `test_anonymous_html_fetch_rejects_private_redirect_targets` — pins F2 so the
  anonymous path cannot silently lose the guard again.
- `test_ui_platform_key_map_is_generated_from_the_registry` — pins F8.

---

Reviewed 19 August 2026 against commit `03cb6d7` (v0.8.5). Applied in
`c091aa0` (v0.8.7).
