# 3D Model Search Engine for OrcaSlicer

Search and import 3D models from multiple model portals without leaving OrcaSlicer.

**Current plugin version: v0.4.0**

The plugin opens a non-modal **Search 3D Models** window, lets you choose which portals participate in each search, displays available model/license metadata, resolves downloadable files, and hands supported geometry to the currently running OrcaSlicer instance.

## Install

1. Download `search_engine.py` from this repository.
2. Install/activate it through OrcaSlicer's **Plugins** dialog, or side-load it as:
   - Windows: `%APPDATA%\OrcaSlicer\orca_plugins\search_engine\search_engine.py`
   - Linux: `~/.config/OrcaSlicer/orca_plugins/search_engine/search_engine.py`
   - macOS: `~/Library/Application Support/OrcaSlicer/orca_plugins/search_engine/search_engine.py`
3. Fully restart OrcaSlicer after replacing the plugin file.
4. In **Prepare**, press **Space** and run **Search 3D Models**. Builds without Actions Speed Dial can launch it from the Plugins UI.

## Supported portals

| Portal | Search | Direct import | Plugin authentication |
|---|---:|---:|---|
| **Printables** | Yes | Public files | None |
| **Thingiverse** | Yes | Public files/ZIP when exposed | None |
| **MyMiniFactory** | Yes | Public/free direct files | None |
| **Thangs** | Yes | Public/free direct files | None |
| **Creality Cloud** | Yes | Public STL/3MF/CAD when exposed | None |
| **MakerWorld / Bambu Lab** | Yes | Yes | Bambu/MakerWorld session |
| **Nexprint / Elegoo** | Yes | Yes | `auth_token` cookie |
| **Makeronline / Anycubic** | Yes | Yes | Access token / Anycubic Slicer Next session |
| **Cults3D** | Yes | Files available to the signed-in account | Browser session cookies |
| **GrabCAD** | Yes | Files available to the signed-in account | Browser session cookies |

A search result does not guarantee a programmatic download. Paid, member-only, checkout, CAPTCHA, or otherwise interactive flows intentionally fall back to **Open in browser** instead of reporting a false successful import.

## Authentication

Authentication controls are shown only for portals that need them.

### MakerWorld

MakerWorld import supports Bambu email/password login, verification-code continuation, or an existing Bambu Cloud access token. Only resulting tokens/session metadata are persisted; passwords are never written to disk.

### Nexprint

Sign in on the official Nexprint site and paste the `auth_token` cookie value. The plugin does not collect the Nexprint password.

### Makeronline / Anycubic

Use **Import from Anycubic Slicer Next** when a readable local session token is available, or paste an existing access token. The removed legacy `api.cloud.anycubic.com` direct email/password flow is not used.

### Cults3D and GrabCAD

Sign in on the official portal and paste the authenticated browser `Cookie` header/session cookie string. Cookies are parsed into domain-scoped cookie jars and are not manually attached to external CDN requests.

## Search and import behavior

- Every registered adapter has a dedicated source checkbox.
- **Select all / Select none** and a selected-source counter are available.
- Selected source portals are persisted by the embedded UI.
- Single-file results import immediately.
- Multi-file results show a checkbox picker before any file is downloaded.
- ZIP archives are safely expanded and only supported model files are handed to OrcaSlicer.
- If a direct download is unavailable, the official model page is opened instead.
- Re-running **Search 3D Models** reuses the existing search window.

### Current-project handoff

On **Windows**, the plugin sends OrcaSlicer's native `WM_COPYDATA` single-instance payload to the existing OrcaSlicer main window. It does not launch a second Orca process and does not use the invalid `--single-instance` CLI option.

On **macOS/Linux**, downloaded file paths are passed to the OrcaSlicer executable and OrcaSlicer's normal single-instance handling forwards them to the active plater.

## Supported local formats

The import/archive layer recognizes:

- `.3mf`
- `.stl`
- `.obj`
- `.step` / `.stp`
- `.iges` / `.igs`
- `.amf`
- `.ply`
- `.scad`
- `.fcstd`
- `.f3d`
- `.zip` as a supported archive container

RAR, 7z, and G-code are no longer advertised as directly importable by the plugin because the runtime does not safely extract/import them.

## Downloads and saved sessions

Downloaded models are stored below:

```text
<OrcaSlicer data dir>/model_downloads/
```

Authentication state is stored below:

```text
<OrcaSlicer data dir>/model_search_auth/sessions.json
```

Typical OrcaSlicer data roots are `%APPDATA%\OrcaSlicer` on Windows, `~/.config/OrcaSlicer` on Linux, and `~/Library/Application Support/OrcaSlicer` on macOS.

## Security properties

The v0.4.0 network/import layer includes the following safeguards:

- portal credentials are scoped to allow-listed portal hosts;
- authenticated HTTP redirects are followed manually and auth headers are recomputed for every hop;
- Makeronline `XX-Token` / Bearer credentials are therefore removed before a cross-host CDN redirect;
- browser cookies for Cults3D, GrabCAD, and Nexprint are domain-scoped;
- only HTTP(S) download URLs are accepted;
- localhost/private literal-IP targets are rejected;
- HTML/login pages are rejected as model files;
- individual downloads are limited to 500 MB;
- filenames are sanitized and collision-safe;
- ZIP extraction blocks traversal and applies extraction-size limits;
- expired/rejected authenticated sessions are surfaced as authentication errors.

## v0.4.0 refactor

The deep refactor removed production and repository legacy rather than keeping compatibility shims:

- removed the adapter `enabled()` hook that always returned `True`;
- removed the development-only `SEARCH_ENGINE_AUTORUN` path;
- removed the duplicate `_FILE_RESOLVERS` registry and made `_SEARCHERS` the adapter source of truth;
- removed the special MakerWorld `urllib`/AWS download branch;
- consolidated AuthStore atomic writes and Makeronline/Nexprint file parsing;
- split MakerWorld design/profile/download resolution into separate stages;
- split public-page collection, download probing, and candidate validation into smaller helpers;
- removed obsolete one-off tests, deployment probes, scratchpad code, staging scripts/reports, and old version-specific documentation files.

See [CHANGELOG.md](CHANGELOG.md) for release history.

## Development and validation

Permanent CI runs on **Ubuntu, Windows, and macOS**. Each job performs:

```bash
python -m py_compile search_engine.py
python -m unittest -v test_authenticated_import.py test_catalog_adapters.py test_import_flow.py test_speed_dial_action.py
pyright --project pyrightconfig.json
ruff check search_engine.py test_authenticated_import.py test_catalog_adapters.py test_import_flow.py test_speed_dial_action.py --select F,B,SIM,UP,PIE,RUF
```

The embedded JavaScript inside `PAGE` is extracted and checked with `node --check` as part of CI.

The test suite covers authentication isolation, cross-host redirect credential stripping, catalog adapters, public/gated download resolution, safe ZIP extraction, current-project handoff, multi-file selection, source-checkbox coverage, and Speed Dial window reuse.

## Troubleshooting

If the search UI is visible but buttons do not respond, fully restart OrcaSlicer after replacing `search_engine.py` and inspect:

```text
<datadir>/log/python_*.log
```

If a portal asks for login again, forget the saved session and reconnect using a fresh token/cookie from the official portal.

If **Import from Anycubic Slicer Next** cannot find a token, the installed Slicer Next build may store it in an encrypted or unsupported location; paste an existing access token manually instead.

## Limitations

- Portal websites and non-public web APIs can change without notice.
- MakerWorld download endpoints are not a stable public third-party API contract.
- Browser-session integrations depend on the user's own valid session.
- Paid/checkout/member/CAPTCHA flows intentionally remain in the browser.
- Model license metadata is informational; the portal model page is authoritative.
- Real-account end-to-end behavior for gated portals requires valid user credentials/session state and cannot be fully simulated by unit tests.

## Legal

This project does not host or mirror model files. Downloads use portal-provided URLs and the user's own account/session where required. The plugin does not grant rights to any model; comply with the model license and portal terms.

See [LEGAL_ANALYSIS.md](LEGAL_ANALYSIS.md) for additional project notes. Nothing in this repository is legal advice.

## License

MIT, see [LICENSE](LICENSE). OrcaSlicer is a separate project licensed under its own terms.
