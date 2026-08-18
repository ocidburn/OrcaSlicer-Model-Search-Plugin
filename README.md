# 3D Model Search Engine for OrcaSlicer

Search and import 3D models from multiple model portals without leaving OrcaSlicer.

**Current plugin version: v0.4.0**

The plugin opens a non-modal `Search 3D Models` window, lets you choose which portals participate in each search, shows model/license metadata, resolves downloadable files, and imports supported geometry into the **currently open OrcaSlicer project**.

## Quick start

1. Download `search_engine.py` from this repository.
2. Install/activate it with OrcaSlicer's **Plugins** dialog. For a manual side-load, place it at:
   - Windows: `%APPDATA%\OrcaSlicer\orca_plugins\search_engine\search_engine.py`
   - Linux: `~/.config/OrcaSlicer/orca_plugins/search_engine/search_engine.py`
   - macOS: `~/Library/Application Support/OrcaSlicer/orca_plugins/search_engine/search_engine.py`
3. Restart OrcaSlicer if the plugin was added while OrcaSlicer was running.
4. Open **Prepare**, press **Space**, choose **Search 3D Models**, and press Enter. In builds without Actions Speed Dial, launch the script from the Plugins UI.
5. Tick the portals you want to search, enter a query, and press **Search**.
6. Open a result and press **Import into OrcaSlicer**.
7. If the model contains multiple files, tick only the files you want and press **Import selected**.

The selected search portals are remembered by the embedded UI and restored the next time the search window is opened.

## Main features

- Search across 10 supported model portals from one OrcaSlicer window.
- A dedicated checkbox for every registered search adapter.
- **Select all / Select none** controls for search sources.
- Selected-source counter and protection against starting a search with no portal selected.
- Search-source selection is persisted between launches.
- Search results include model name, author, platform, thumbnail and license information when the platform exposes it.
- One-click **Open in browser** fallback for downloads that require a portal checkout, membership flow, CAPTCHA, or other interactive browser step.
- Separate per-portal authentication sessions where authentication is actually required.
- Passwords are never persisted.
- Single-file downloads import immediately.
- Multi-file models show a checkbox list before downloading.
- ZIP downloads are safely expanded and supported model files are imported.
- Downloaded models are handed to the already-running OrcaSlicer instance and added to the current project.
- Re-running `Search 3D Models` reuses the existing search window instead of opening duplicates.
- The search field receives focus when the action is invoked again.
- The model detail/import panel closes when you click elsewhere in the search window.

## Supported portals

| Portal | Search | Direct import | Authentication |
|---|---:|---:|---|
| **Printables** | Yes | Yes, for public files | None |
| **Thingiverse** | Yes | Yes, when a public download is exposed | None |
| **MyMiniFactory** | Yes | Yes, for public/free direct files | None |
| **Thangs** | Yes | Yes, for public/free direct files | None |
| **Creality Cloud** | Yes | Yes, when a public STL/3MF/CAD URL is exposed | None |
| **MakerWorld / Bambu Lab** | Yes | Yes | Bambu/MakerWorld account session |
| **Nexprint / Elegoo** | Yes | Yes | `auth_token` session cookie |
| **Makeronline / Anycubic** | Yes | Yes | Anycubic access token / Anycubic Slicer Next session |
| **Cults3D** | Yes | Yes, for files available to the signed-in account | Cults3D browser session cookies |
| **GrabCAD** | Yes | Yes, for files available to the signed-in account | GrabCAD browser session cookies |

A successful search result does not guarantee that the portal allows a direct programmatic download. Paid models, member-only models, CAPTCHA-protected downloads, checkout flows, or pages that do not expose a direct file URL are intentionally sent to **Open in browser** instead of being reported as a successful import.

## Portal details

### Printables

- Public search through Printables.
- No login is required for public model files.
- The plugin resolves available STL files and imports them into the active OrcaSlicer project.

### Thingiverse

- Public web search.
- No plugin authentication is used.
- The plugin first tries Thingiverse's public download-all ZIP route and then falls back to public model/file links.
- If the site does not expose a usable direct download, the model page is opened in the browser.

### MyMiniFactory

- Public search.
- Public/free direct files are imported without authentication.
- Paid/cart-only objects use the browser flow instead of attempting to bypass the storefront.

### Thangs

- Public search.
- Public/free direct files are imported without authentication.
- Member-only, paid, cart, or interactive-download models fall back to the browser.

### Creality Cloud

- Public search.
- Public STL/3MF/CAD downloads are imported without a plugin login when a direct file URL is available.
- Paid/subscription/browser-only downloads are opened on the official model page.

### MakerWorld / Bambu Lab

Search is public, but model import requires a MakerWorld/Bambu account session.

The account panel supports:

- Bambu email + password login.
- Verification-code/MFA continuation when required.
- Pasting an existing Bambu Cloud access token.

Only the resulting session token is saved. The password is not stored.

MakerWorld import resolves the design, chooses the requested/first printable profile, asks MakerWorld for the authenticated profile download, and downloads the returned signed 3MF URL.

### Nexprint / Elegoo

Search is public. Import requires an authenticated Nexprint session.

1. Click **Account** for Nexprint.
2. Use **Open official login** and sign in on Nexprint.
3. Copy the `auth_token` cookie value from the authenticated browser session.
4. Paste it into the plugin and connect.

The plugin does not ask for or store the Nexprint password.

### Makeronline / Anycubic

Search is public. Import requires an Anycubic/Makeronline access token.

Supported connection methods:

- **Import from Anycubic Slicer Next** — the plugin checks known local Slicer Next configuration locations for an `access_token`.
- Paste an existing access token from an authenticated Anycubic/Makeronline session.

The old direct `api.cloud.anycubic.com` email/password flow is intentionally not used.

If Slicer Next stores the session in an encrypted/unsupported location, paste the token manually.

### Cults3D

Search is public, but Cults3D requires a signed-in account even for many free-file downloads.

1. Open the official Cults3D login from the account panel.
2. Sign in in your browser.
3. Paste the authenticated Cookie header/session cookies into the plugin.

The plugin sends those cookies only to allowed Cults3D hosts. If the saved session expires, the plugin asks you to refresh it.

Cults3D paid/checkout flows remain in the browser.

### GrabCAD

GrabCAD Community Library search/download requires a member session.

1. Open the official GrabCAD login from the account panel.
2. Sign in in your browser.
3. Paste the Cookie header/session cookie string into the plugin.

Search and file resolution then use that saved GrabCAD browser session. Expired sessions are detected and rejected instead of returning a fake successful import.

## Search-source selection

The **Search portals** section contains one checkbox for every registered adapter:

- Thingiverse
- Cults3D
- MyMiniFactory
- Thangs
- Makeronline
- Creality Cloud
- Nexprint
- GrabCAD
- Printables
- MakerWorld

Use **Select all** or **Select none** for quick changes. The selected list is stored in the embedded UI's local storage.

A regression test checks that the UI checkbox set exactly matches the unified `_PLATFORMS` registry, so adding a future search adapter without exposing it in the UI causes the test suite to fail.

## Importing models into the current project

The public Python host API is primarily read-only for model mutation, so the plugin does not try to edit the plater through an undocumented Python object.

After downloading, the plugin hands the local files to OrcaSlicer's normal model-loading path.

On **Windows**, the plugin sends OrcaSlicer's native `WM_COPYDATA` single-instance message directly to the current OrcaSlicer main window. It does **not** launch a second OrcaSlicer process and does not depend on the `--single-instance` command-line option.

On **macOS/Linux**, the plugin starts the OrcaSlicer executable with only the downloaded file paths; OrcaSlicer's configured single-instance handling forwards them to the already-running plater.

The goal on every platform is the same: add the downloaded geometry to the **currently open project** rather than merely leaving it in the download directory.

### Single file

If a portal returns one file, the plugin downloads it and immediately hands it to OrcaSlicer.

### Multiple files

If a portal returns more than one file, the plugin shows a file picker containing:

- one checkbox per file;
- all files selected by default;
- **Select all**;
- **Select none**;
- selected-file counter;
- **Import selected**.

Only checked files are downloaded and imported.

### ZIP archives

ZIP downloads are extracted with path-safety checks. Supported geometry found inside the archive is handed to OrcaSlicer. If an archive contains no directly loadable model file, the plugin reports the download directory instead of claiming that import succeeded.

## Supported local model formats

The plugin recognizes common OrcaSlicer-loadable geometry/file extensions including:

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

Actual support after handoff still depends on the OrcaSlicer build and the file contents.

## Downloads and saved sessions

Downloaded files are stored under:

```text
<OrcaSlicer data dir>/model_downloads/
```

Authentication state is stored under:

```text
<OrcaSlicer data dir>/model_search_auth/sessions.json
```

Typical OrcaSlicer data directories:

- Windows: `%APPDATA%\OrcaSlicer`
- Linux: `~/.config/OrcaSlicer`
- macOS: `~/Library/Application Support/OrcaSlicer`

The auth store writes tokens/session values only. Password-like fields are stripped before persistence.

## Security behavior

The download/auth layer includes several defensive checks:

- Portal credentials are attached only to allow-listed hosts for that portal.
- Authentication headers are rebuilt after every redirect and are never attached outside the destination platform's host allowlist.
- Session cookies are domain-scoped and are not blindly forwarded to arbitrary CDN hosts.
- Localhost, private literal IPs and hostnames resolving to non-public addresses are rejected on download paths.
- Only HTTP(S) URLs are accepted.
- HTML/login pages are rejected as model files.
- Downloads have a 500 MB safety limit.
- Filenames are sanitized and collision-safe.
- ZIP extraction blocks path traversal.
- Expired/rejected sessions return an authentication error.
- MakerWorld signed download URLs are downloaded without leaking the portal bearer token to the signed storage host.

## Actions Speed Dial

The script capability name is:

```text
Search 3D Models
```

On OrcaSlicer builds with Actions Speed Dial support:

1. Open the **Prepare** page.
2. Press **Space**.
3. Search for `Search 3D Models`.
4. Press Enter or activate it from the list.
5. Optionally mark the action as a favorite for faster access.

Calling the action while its window is already open does not create a second copy. The existing search window is reused and the search input is activated.

## Troubleshooting

### Import downloads a file but nothing appears in OrcaSlicer

Check the status line in the search window. On Windows, the plugin sends the file list directly to the current OrcaSlicer main window through native `WM_COPYDATA` IPC. If the main window cannot be found or the handoff fails, the downloaded files remain in `<datadir>/model_downloads/` and the plugin reports the failure instead of claiming success.

### A portal asks for login again

The saved session probably expired or was rejected. Open that portal's **Account** card, use **Forget session**, sign in on the official site again, and reconnect with a fresh token/cookie.

### Makeronline cannot find the Anycubic Slicer Next token

Some Slicer Next builds may move or encrypt the token. Use the account dialog and paste an access token from an authenticated session manually.

### Paid/member-only model does not import

This is expected. The plugin does not attempt to bypass checkout, subscription, membership, CAPTCHA, or interactive download restrictions. Use **Open in browser**.

### Search window/buttons do not respond

Fully restart OrcaSlicer after replacing `search_engine.py`. Python/plugin errors are written to OrcaSlicer's log directory; check:

```text
<datadir>/log/python_*.log
```

## Development and validation

The plugin keeps one deployment file, `search_engine.py`, but platform behavior is no longer spread across parallel dictionaries. Each portal has one `PlatformSpec` entry containing its display name, adapter, authentication hosts, cookie mode/scope, login URL and referer policy. Search, import, authentication status and UI routing all consume that registry.

The regression suite covers authentication, redirect credential isolation, DNS/private-address rejection, catalog adapters, import flow, multi-file selection, registry/UI consistency and Speed Dial window reuse.

Typical checks:

```bash
python -m py_compile search_engine.py
python -m unittest discover -v
pyright --project pyrightconfig.json
ruff check .
vulture search_engine.py --min-confidence 80
radon cc search_engine.py -s -a
bandit -q -r search_engine.py
```

The embedded JavaScript can also be extracted from `PAGE` and validated with:

```bash
node --check <extracted-script.js>
```

The v0.4.0 validation gates are:

- Python compile check
- embedded JavaScript syntax check
- **51/51 tests**, repeated twice
- Pyright: **0 errors, 0 warnings**
- Ruff and Vulture: no findings
- Bandit: no unreviewed findings
- Radon complexity report and Windows IPC smoke test

## Current limitations

- Portal websites and private web APIs can change without notice; an adapter may need updating when a site changes its frontend/API.
- MakerWorld download endpoints are not a public stable third-party API contract.
- Browser-session integrations depend on the user's own valid portal session.
- Paid, checkout, member-only, CAPTCHA and interactive flows intentionally stay in the browser.
- License metadata is informational. The model page is authoritative when platform metadata is missing or inconsistent.
- Real-account end-to-end behavior can only be validated with a valid account/session for the relevant portal.

## Legal

This project does not host, mirror or redistribute model files. Downloads use the model portal's own URLs and the user's own account/session when required.

The plugin does not grant rights to any model. You are responsible for complying with the model's license and the portal's terms.

See [`LEGAL_ANALYSIS.md`](LEGAL_ANALYSIS.md) for additional project notes. Nothing in this repository is legal advice.

## Changelog

See [`CHANGELOG.md`](CHANGELOG.md) for the consolidated release history.

## License

MIT, see [LICENSE](LICENSE).

OrcaSlicer is a separate project licensed under its own terms.
