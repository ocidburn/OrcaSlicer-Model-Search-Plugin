# PLAN — OrcaSlicer 3D Model Search Engine Plugin

## Strategic Goal

A Python `Script` plugin for OrcaSlicer that searches Printables, Thingiverse, and GrabCAD
for 3D models, displays results with license metadata, and downloads selected models.

## Architecture

```
orca_plugins/search_engine/
  search_engine.py          # main plugin file (PEP 723 + @orca.plugin + capability)
  search_engine/
    __init__.py
    searchers/
      __init__.py
      base.py               # Abstract base: SearchProvider, ModelResult, LicenseInfo
      printables.py         # Printables GraphQL adapter
      thingiverse.py        # Thingiverse REST adapter
      grabcad.py            # GrabCAD REST adapter
    ui/
      __init__.py
      search_dialog.html    # HTML search UI
      result_card.html      # Jinja2 fragment for result card
    compliance.py           # License display, ToS checks, disclaimers
    downloader.py           # Async download to data_dir()
```

Actually, ponytail says: fewest files possible. Single `.py` entry file + maybe a second
for the HTML template. Everything else is premature.

## Simplified Architecture (Ponytail)

```
orca_plugins/search_engine/
  search_engine.py          # everything: API, UI, download, compliance
```

One file. PEP 723 metadata. `@orca.plugin` class. `Script` capability.

### Flow

1. User clicks "Run" on plugin → `execute()` called
2. Plugin shows `show_dialog()` with embedded HTML search UI
3. User types query, selects platforms
4. Plugin spawns threads to search each platform API
5. Results pushed back via `on_message` → displayed in HTML
6. User clicks a result → license shown in detail view
7. User clicks "Download" → file saved to disk
8. Plugin shows message box: "Downloaded to X. Open it from File menu."

## Implementation Phases

### Phase 0: Legal Foundation (DONE)
- [x] LEGAL_ANALYSIS.md written
- [x] Platform risk assessment complete
- [x] Scope decision: Printables + Thingiverse + GrabCAD

### Phase 1: Plugin Scaffold
- PEP 723 metadata block
- `@orca.plugin` + `orca.script.ScriptPluginCapabilityBase`
- Minimal `execute()` that opens HTML dialog with "Hello World"
- Test: plugin visible in Plugins dialog, runs without errors

### Phase 2: Search Backend
- `SearchProvider` abstract interface
- Printables search (GraphQL, needs OAuth token)
- Thingiverse search (REST, needs app token)
- GrabCAD search (REST, optional token)
- `requests` dependency in PEP 723
- Test: each provider returns `ModelResult` list

### Phase 3: HTML Search UI
- Search input + platform checkboxes
- Results grid with thumbnails
- License badge on each result (color-coded)
- Detail view with license description
- Download button
- Theme-aware (CSS variables from `orca.host.ui`)

### Phase 4: Compliance Layer
- License display before download (mandatory)
- First-run disclaimer dialog
- Platform ToS check before API call
- Configurable license filter

### Phase 5: Download
- Async download to `orca_plugins/search_engine/downloads/`
- Progress dialog
- Error handling (network, disk full, permission)
- Post-download message to user

### Phase 6: Polish & Test
- Error states and edge cases
- Rate limit handling
- Token configuration UI (custom HTML editor)
- Test with OrcaSlicer nightly build

## Key Constraints (from plugin system docs)

- `execute()` runs on UI thread → search MUST be async (`threading`)
- File writes only in `data_dir()` (audit hook)
- `requests` declared in `dependencies` → resolved by `uv`
- Plugin placed in `data_dir()/orca_plugins/search_engine/`
- No programmatic model import into plater (orca.host is read-only)
