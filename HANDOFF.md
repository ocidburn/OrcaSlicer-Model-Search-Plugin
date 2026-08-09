# OrcaSlicer Model Search Plugin — Status

**Updated**: 2026-08-09
**Project**: `/home/tommaso/projects/Orca_plugin_Search_Engine/`
**OrcaBelt instance**: behemoth, `--datadir /home/tommaso/.config/OrcaBelt2608-test`
**Plugin path on behemoth**: `~/.config/OrcaBelt2608-test/orca_plugins/search_engine/search_engine.py`
**Orca source** (behemoth): `~/projects/orca/orcaslicer-pr/src-belt-combined/src/`

---

## What works (verified in the running app, 2026-08-09)

- **Search**: 4 adapters, ~126 results for "benchy". All public APIs, no auth.
- **Thumbnails**: load fine (cross-origin images from a `file://` origin are not blocked).
- **Card clicks**: delegated listener on `#results`, detail panel opens as a fixed overlay.
- **Import into OrcaSlicer**: Printables model → STL downloaded → **lands in Prepare**.
  Verified: `3dbenchy.stl`, 60.001 × 31.004 × 48 mm, 225154 triangles, on the plate.
- **Open in browser**: hands the model page to the system browser via `xdg-open`.

| Platform | Results | Search API | File download |
|----------|---------|-----------|---------------|
| MakerWorld (Bambu) | 30 | `api.bambulab.com/v1/search-service/select/design2` | ❌ `{"error":"Please log in to download models."}` |
| Nexprint (Elegoo) | 30 | `nexprint.com/gateway/api/v1/model-library-server/model-base-info/search` | ❌ 401 账号未登录 on every `model-file/*` endpoint |
| Makeronline (Anycubic) | 30 | `POST makeronline.com/api/search/model` | ❌ `files[].url` is 403 (private S3) |
| **Printables (Prusa)** | 36 | HTML scrape via JSON-LD | ✅ **public, no auth** |

Disabled: Thingiverse (`/download:ID` → 403 robots), GrabCAD (API retired).

### How the Printables download works
`api.printables.com/graphql/` is open (introspection off, queries fine). The file
URL is not in the schema, but `stls { name filePreviewPath }` is — and the preview
image sits in the same folder as the STL:

```
filePreviewPath: media/prints/3161/stls/123914_<uuid>/3dbenchy_preview.png
→ https://files.printables.com/media/prints/3161/stls/123914_<uuid>/3dbenchy.stl
   HTTP 200, application/sla, 11285384 bytes, magic "solid Shape0"
```

### How the file reaches Prepare
The plugin host API is **read-only** — `orca.host.model/mesh/presets/slicing` only
inspect; there is no import/add-object binding. But every running instance listens on
the session bus for the message a second launch would send
(`slic3r/GUI/InstanceCheck.cpp`):

- name/interface: `com.orcaslicer.OrcaSlicer.InstanceCheck.Object<instance_hash>`
- object: `/com/orcaslicer/OrcaSlicer/InstanceCheck/Object<instance_hash>`
- method: `AnotherInstance(string)`

The string is an argv list in `unescape_strings_cstyle` format — **semicolon-separated,
quoted** (`"orca-slicer";"/path/file.stl"`), *not* space-separated. `argv[0]` is skipped
as the executable path. File paths there reach `EVT_LOAD_MODEL_OTHER_INSTANCE`, i.e. the
plater, and Orca switches to Prepare on its own. `_load_in_orca()` discovers the instance
hash from `ListNames`, so it needs no configuration.

---

## Corrections to the previous handoff

The old "OrcaSlicer webview constraints" list was **wrong on nearly every point**. It was
built by inference during debugging, never verified. Reproduced against the same
`libwebkit2gtk-4.1` (2.52.3) Orca links, with the same launcher env, and confirmed inside
the running app:

| Old claim | Reality |
|-----------|---------|
| Dynamic HTML with inline `onclick` is stripped | False — fires normally |
| `addEventListener` on dynamic elements doesn't work | False — works |
| Webview blocks cross-origin images | False — thumbnails load |
| `webbrowser.open()` / no external browser | False — `xdg-open` works, lands in the user's Chrome session |
| "Card clicks do nothing" | The handler *did* fire; the detail panel opened ~2300 px below the fold, under 10 rows of cards. Nobody scrolled. Now a fixed overlay. |
| Only fix is 40 static template cards | That hack also silently capped results at 40 of 126. Removed. |

`ORCABELT_DISABLE_WEBVIEW=1` in the launcher is genuinely inert (not referenced anywhere
in the source).

**Lesson**: the webview had no error channel, so every diagnosis was a guess. It now pipes
`window.onerror` and `jlog()` through `orca.postMessage` → Python `sys.stderr`, which the
host tees to `<datadir>/log/python_*.log`. Read that file before theorising.

---

## Launching the plugin

The Home page side panel now has a **Plugins** entry (between Recent and OrcaCloud) that
opens the same dialog as Tools ▸ Plugins. Three files in the fork, no new assets:

- `resources/web/homepage/index.html` — the `BtnItem` + inline puzzle SVG (`currentColor`, so it follows the theme)
- `resources/web/homepage/js/home.js` — `OnClickPlugins()` → `SendWXMessage({command:"homepage_plugins"})`
- `src/slic3r/GUI/GUI_App.cpp` — `homepage_plugins` → `CallAfter([this]{ open_plugins_dialog(); })`

The label is plain text, not `trans`/`tid`, because a new tid would need a localization entry.

## Testing

**Standalone webview harness** (`scratchpad/wkprobe.py`) — reproduces the plugin webview
outside Orca: same WebKit, same env vars, same `load_html` + `file://` base URI, on Xvfb
so `xdotool` clicks are reliable and the desktop is untouched. This is the fast loop; use
it before touching the real app.

**In-app autorun** — `SEARCH_ENGINE_AUTORUN=1` makes the plugin open its own window,
search "benchy" and click the first importable card, so the probes run with no mouse:

```bash
ssh behemoth 'pkill -f "[O]rcaBelt2608"'   # note the [O] — a plain pattern kills your own ssh
ssh behemoth 'export DISPLAY=:0 XAUTHORITY=/run/user/1000/.mutter-Xwaylandauth.G7VCT3 SEARCH_ENGINE_AUTORUN=1;
              setsid nohup /home/tommaso/bin/orca-belt-2026-08 </dev/null >/tmp/orca_test.log 2>&1 &'
ssh behemoth 'tail -20 "$(ls -t ~/.config/OrcaBelt2608-test/log/python_*.log|head -1)"'
```

Screenshots: `scrot` returns black under Xwayland; capture the window instead —
`import -window $(xdotool search --name "3D Model Search"|head -1) /tmp/x.png`.

---

## Next

1. **More importable platforms** — all three others gate files behind a login. Options:
   reuse the browser session's cookies, or add per-platform auth. Nothing else is scrapeable.
2. **Multi-file prints** — every STL of a print is downloaded and loaded. A print with
   many parts will drop them all on the plate; a file picker may be wanted.
3. **`orcaslicer://` deep links** — `GUI_App::start_download()` exists and the
   `AnotherInstance` payload accepts `orcaslicer://open?file=<url>`, letting Orca do the
   download itself. Unused; the plugin fetches with `requests` for control over headers.
