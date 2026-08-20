# 3D Model Search Engine for OrcaSlicer

Search and import 3D models from multiple model portals without leaving OrcaSlicer.

**Current plugin version: v0.8.8**

The plugin opens a non-modal `Search 3D Models` window, lets you choose which portals participate in each search, shows model/license metadata, resolves downloadable files, and imports supported geometry into the **currently open OrcaSlicer project**.

## Quick start

1. Download [`src/search_engine.py`](src/search_engine.py) from this repository. It remains a standalone deployment file; the `src/` directory only keeps the repository organized.
2. Install/activate it with OrcaSlicer's **Plugins** dialog. For a manual side-load, place it at:
   - Windows: `%APPDATA%\OrcaSlicer\orca_plugins\search_engine\search_engine.py`
   - Linux: `~/.config/OrcaSlicer/orca_plugins/search_engine/search_engine.py`
   - macOS: `~/Library/Application Support/OrcaSlicer/orca_plugins/search_engine/search_engine.py`
3. Restart OrcaSlicer if the plugin was added while OrcaSlicer was running.
4. Open **Prepare**, press **Space**, choose **Search 3D Models**, and press Enter. In builds without Actions Speed Dial, launch the script from the Plugins UI.
5. Tick the portals you want to search, enter a query, and press **Search**.
6. Open a result and press **Import into OrcaSlicer**.
7. If the model contains multiple files, select the files to import. For MakerWorld and Creality Cloud, select a print profile and download format first.

The selected search portals are remembered by the embedded UI and restored the next time the search window is opened.

## Main features

- Search across 14 model catalogs and browser fallbacks from one OrcaSlicer window, every one of them selected by default.
- Load additional API result pages without discarding or duplicating models already shown.
- Per-source progress shows loaded/visible counts, known catalog totals, more-page availability, first-page-only sources, and individual portal errors.
- Sort merged results by relevance, normalized popularity, downloads, likes, rating, date, print count, name, or platform.
- Filter to models explicitly marked free or to sources with a direct-import path.
- A dedicated checkbox for every registered search adapter.
- **Select all / Select none** controls for search sources.
- Selected-source counter and protection against starting a search with no portal selected.
- Search-source selection is persisted between launches.
- Search results include model name, author, platform, thumbnail and license information when the platform exposes it.
- Search-result thumbnails load lazily as cards approach the visible area.
- One-click **Open in browser** fallback for downloads that require a portal checkout, membership flow, CAPTCHA, or other interactive browser step.
- Separate per-portal authentication sessions where authentication is actually required.
- Every authentication card includes a keyboard-accessible help tooltip with portal-specific connection instructions.
- Passwords are never persisted.
- Single-file downloads import immediately.
- Multi-file models show a checkbox list before downloading. Printables and Thingiverse files include their individual rendered thumbnail and an enlarged mouse-hover or keyboard-focus preview when the platform provides one.
- Selecting a MakerWorld or Creality Cloud result preloads its print-profile metadata and images in the background; the importer reuses that cache.
- Print-profile thumbnails support an enlarged mouse-hover and keyboard-focus preview.
- MakerWorld and Creality Cloud models show their available print profiles and let you choose direct 3MF import or the official browser flow for STL/CAD files.
- ZIP downloads are safely expanded and supported model files are imported.
- Downloaded models are handed to the already-running OrcaSlicer instance and added to the current project.
- Re-running `Search 3D Models` reuses the existing search window instead of opening duplicates.
- The search field receives focus when the action is invoked again.
- The model detail/import panel closes when you click elsewhere in the search window.

## Supported portals

| Portal | Search | Direct import | Authentication |
|---|---:|---:|---|
| **Printables** | Yes | Yes, for public files | None |
| **Thingiverse** | Yes, official API | Yes | Personal Thingiverse API token |
| **MyMiniFactory** | Yes, official API | OAuth archives only; otherwise browser | Personal MyMiniFactory API key |
| **Yeggi** | Browser meta-search | Original portal | Interactive Turnstile check |
| **Thangs** | Yes, JSON search | Yes, signed ZIP when `downloadUrl` is available | Bearer access token for import |
| **STLFinder** | Yes, when Cloudflare permits | Delegated to the original registered portal | Original portal credentials when required |
| **Creality Cloud** | Yes, JSON API | Selected signed 3MF profile; STL/CAD in browser | `model_token` from the user's official session for 3MF |
| **MakerWorld / Bambu Lab** | Yes | Yes | Bambu/MakerWorld account session |
| **Nexprint / Elegoo** | Yes | Yes | `auth_token` session cookie |
| **Makeronline / Anycubic** | Yes | Yes | Anycubic access token / Anycubic Slicer Next session |
| **Cults3D** | Yes | Yes, for files available to the signed-in account | Cults3D browser session cookies |
| **GrabCAD** | Yes | Yes, for files available to the signed-in account | GrabCAD browser session cookies |
| **YouMagine** | Yes | Public file when exposed; otherwise browser | None |
| **Pinshape** | Yes | Yes, for public STL files; otherwise browser | None |

A successful search result does not guarantee that the portal allows a direct programmatic download. Paid models, member-only models, CAPTCHA-protected downloads, checkout flows, or pages that do not expose a direct file URL are intentionally sent to **Open in browser** instead of being reported as a successful import.

See [`docs/PLATFORMS.md`](docs/PLATFORMS.md) for the wider platform survey: which catalogs are worth searching for 3D printing, how popular each one is, and why some well-known model sites are deliberately left out.

## Portal details

### Printables

- Public search through Printables.
- No login is required for public model files.
- The plugin requests every file's canonical temporary URL through Printables' `getDownloadLink` mutation. It never guesses a storage URL from filename capitalization, spaces, or hyphens.
- Available STL files can be selected and imported into the active OrcaSlicer project. The picker uses Printables' per-file `filePreviewPath` renders and shows a larger preview on mouse hover or keyboard focus.

### Thangs

- Search uses Thangs' official production JSON API instead of its Cloudflare-protected web proxy and preserves the official `downloadUrl` resolver.
- Sign in on the official Thangs page, copy the access token from an authenticated `Authorization: Bearer` request, and connect it through the Thangs account card.
- Import requests a short-lived `signedUrl` from Thangs, preserves the API filename, restores the ZIP extension when necessary, and then downloads the archive from the signed storage URL.
- The Bearer token is allow-listed only to `production-api.thangs.com` and is never attached to the public website, Google Storage, or another signed download host. Results without `downloadUrl` remain in the official browser flow.

### Thingiverse

- Uses Thingiverse's official API instead of its client-rendered search page.
- Create/open a Thingiverse developer app, paste its personal access token in the plugin, and enable the Thingiverse source.
- Search cards whose compact API result omits the license automatically load their official license and complete download/view/print counters in a bounded background queue. Opening each card is no longer required.
- Downloadable files, their sizes, and their individual render thumbnails come from the same authenticated API. The file picker shows a larger preview on mouse hover or keyboard focus.

### MyMiniFactory

- Uses the documented MyMiniFactory API v2 and requires a personal API key.
- API-key search provides model metadata, likes, views, and dates.
- Search cards use the official primary image's nested thumbnail URL and show the API's `license` value. Store-only responses fall back to the documented MyMiniFactory Digital File Store License flag instead of displaying `Unknown`.
- The documented API exposes archive downloads only to an OAuth-connected user; API-key-only and storefront downloads open the official model page.

### Yeggi and STLFinder

- Both are meta-search engines and do not host the model files themselves.
- Yeggi currently requires an interactive Turnstile check, so its query opens in the browser.
- STLFinder model results resolve the original supported portal. Import is then delegated to that portal's existing adapter and credential rules instead of scraping or mirroring a file through STLFinder.
- If Cloudflare requires interactive verification, the source status provides **Open in browser** after the standard-UA compatibility retry.

### Creality Cloud

- Public search uses Creality Cloud's current JSON model-search service with 30-result server pages, platform-native sorting, metrics, license values, and catalog totals.
- Card thumbnails come from the API's normal model covers. The 10-by-10-pixel lazy-load blur placeholder is never selected as the result image.
- Selecting a card loads every public Print Setting and its full profile thumbnail. Import then asks for a profile and either **3MF print profile** or **STL/CAD files**.
- Direct 3MF import asks Creality Cloud for the official signed profile URL. Sign in on the official page and connect the `model_token` cookie value (or a Cookie header containing `model_token` and `model_user_id`); the password and browser profile are never read.
- Original STL/CAD files, paid models, and other interactive flows stay on the official model page.

### MakerWorld / Bambu Lab

Search is public, but model import requires a MakerWorld/Bambu account session.

The account panel supports:

- Bambu email + password login.
- Verification-code/MFA continuation when required.
- Pasting an existing Bambu Cloud access token.

Only the resulting session token is saved. The password is not stored.

Selecting a MakerWorld card starts a public background request for its print-profile metadata and preloads the profile images. Import reuses the cached response, and hovering over or focusing a profile thumbnail shows a larger preview. MakerWorld import lists the available print profiles with their title, creator, printer, layer settings, plate count, estimated print time, and rating when supplied by MakerWorld. The user explicitly selects a profile and then chooses:

- **3MF print profile** — asks MakerWorld for the authenticated signed profile URL and imports it directly.
- **STL/CAD files** — opens the selected profile on MakerWorld for its signed-in browser download flow. Bambu's token API does not expose raw files to this plugin, so this option is never presented as a successful direct import.

### Nexprint / Elegoo

Search is public. Import requires an authenticated Nexprint session.

1. Click **Account** for Nexprint.
2. Use **Open official login** and sign in on Nexprint.
3. Copy the `auth_token` cookie value from the authenticated browser session.
4. Paste it into the plugin and connect.

The plugin does not ask for or store the Nexprint password.

Selecting a Nexprint card loads its public print-profile list in the background
and preloads every profile cover. Import displays the available 3MF profiles
with their preview, material, layer height, wall and infill settings, print
time, plate count, rating, download count, and file size. Hovering over or
focusing a profile image opens the enlarged preview. After the user selects a
profile, the plugin requests its official account-authorized signed URL using
Nexprint's profile file ID; no download URL is guessed or constructed.

### Makeronline / Anycubic

Search is public. Import requires an Anycubic/Makeronline access token.

Supported connection methods:

- **Open official login** — opens Anycubic's registered MakerOnline OAuth flow with its required `redirect_uri`, `scope=read`, and `state=ac_maker_online` parameters. After the browser returns to MakerOnline, copy the `mo_access_token` cookie value (or its Cookie header), paste it into the plugin, and connect.
- **Import from Anycubic Slicer Next** — the plugin checks known local Slicer Next configuration locations for an `access_token`.
- Paste an existing access token from an authenticated Anycubic/Makeronline session. The field accepts a raw token, `mo_access_token=...`, a copied Cookie header, `XX-Token: ...`, or `Authorization: Bearer ...`.

The old direct `api.cloud.anycubic.com` email/password flow is intentionally not used.

MakerOnline completes its OAuth code exchange on the MakerOnline origin and stores the result in `mo_access_token`. OrcaSlicer's plugin UI does not expose the system browser's cookie store, so the plugin never reads browser profiles; the session is handed over deliberately instead. Use **Sign in in browser** to do that without leaving the browser (see [Browser sign-in](#browser-sign-in)), or paste the token directly. If Slicer Next stores the session in an encrypted/unsupported location, the same hand-over applies.

### Cults3D

Search is public, but Cults3D requires a signed-in account even for many free-file downloads.

1. Open the official Cults3D login from the account panel.
2. Sign in in your browser.
3. Paste the authenticated Cookie header/session cookies into the plugin.

The plugin sends those cookies only to allowed Cults3D hosts. If the saved session expires, the plugin asks you to refresh it.

Cults3D paid/checkout flows remain in the browser.

If Cloudflare challenges a catalog request, the plugin makes one compatibility
retry with standard browser navigation headers. If Cloudflare still requires
interactive verification, the Cults3D source status shows **Open in browser**;
the plugin does not attempt to solve or bypass the challenge.

### GrabCAD

GrabCAD Community Library search/download requires a member session.

1. Open the official GrabCAD login from the account panel.
2. Sign in in your browser.
3. Paste the Cookie header/session cookie string into the plugin.

Search and file resolution then use that saved GrabCAD browser session. Expired sessions are detected and rejected instead of returning a fake successful import.

### YouMagine and Pinshape

- YouMagine and Pinshape use their current public HTML search pages.
- Both use validated public file resolution. Pinshape's public `/stl/` resources can be imported directly even though its account download buttons remain on the official site.

## Sorting and filters

Every result is normalized to the same nullable metric fields: downloads, likes, rating, rating count, views, print/make count, publication date, price, and free status. A missing counter stays unknown and is sorted after known values; it is never converted to zero.

Raw counters from different portals are not directly comparable. **Popularity (normalized)** computes a platform-relative score from available counters and compares each result's percentile within its own platform. Exact **Downloads**, **Likes**, and **Rating** sorts remain available when raw values are what you want.

**Free only** includes only results whose source explicitly marks them free. **Direct import only** removes browser-only search cards and sources without a direct file path.

The merged, sorted result set is paginated in the search window. The default is 100 cards per page, with 100, 150, 200, 250, and 300-card options, numbered page navigation, previous/next controls, and a visible result range. A new search returns to page one.

This display pagination is separate from portal pagination. **Load next pages** fetches the next source page from every selected portal that reports more results, merges models without duplicates, and reapplies the active filters and global sort. Printables, MakerWorld, MakerOnline, Nexprint, Thingiverse, MyMiniFactory, Thangs, and STLFinder support this flow. Other HTML catalogs and browser fallbacks are explicitly labelled **first page only** because they do not expose a stable paginated interface.

## Search-source selection

The **Search portals** section contains one checkbox for every registered adapter:

- Thingiverse
- Cults3D
- Yeggi
- MyMiniFactory
- Thangs
- STLFinder
- Makeronline
- Creality Cloud
- Nexprint
- GrabCAD
- Printables
- MakerWorld
- YouMagine
- Pinshape

Every portal starts selected, so a first search covers all of them. Use **Select all** or **Select none** for quick changes. The selected list is stored in the embedded UI's local storage and restored on the next launch.

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

The auth store writes tokens/session values only. Password-like fields are stripped before persistence. Cloudflare clearances are kept in the same file, keyed by host, alongside the User-Agent they are bound to.

## Browser sign-in

Connecting a portal used to mean opening its login in your browser, digging a
value out of developer tools, switching back to OrcaSlicer, and pasting it into
a small field. **Sign in in browser** in the account panel removes the switch
back.

Pressing it opens two tabs: the portal's own login page, and a short finish
page served by the plugin on `http://127.0.0.1:<port>` &mdash; a listener bound
to loopback, valid for one credential and five minutes. Sign in to the portal
normally, then hand the session over on the finish page. OrcaSlicer picks it up
immediately; the account panel updates on its own.

**What this is not.** The plugin cannot read your browser's cookies. Orca's
plugin UI can only render HTML the plugin supplies: it has no API to load a
portal's login page in-process and no access to any cookie jar, and the system
browser's cookie store is not exposed to it either. That is a boundary worth
keeping, so for most portals you still copy one value &mdash; you just no longer
carry it between windows by hand.

A portal *can* complete with no copying at all when its sign-in can be pointed
back at the loopback origin, which the receiver accepts on `/callback` as
`access_token`, `auth_token`, `token`, or `code`. That applies to an OAuth app
you register yourself. Portals whose OAuth clients are registered against their
own domains (MakerOnline, Creality Cloud) cannot redirect to your machine, so
those keep the hand-over page.

MakerWorld does not need any of this: it still signs in directly from
OrcaSlicer with your Bambu account, including the verification code.

How the endpoint is kept to itself:

- It binds `127.0.0.1` only, never a routable interface.
- Every request must carry a random single-use state value, compared in
  constant time. Without it the answer is `403`.
- Requests are refused unless the `Host` header is loopback, and a page on any
  other site is refused by its `Origin`/`Referer`.
- It serves nothing but the finish page, the callback, and a silent favicon;
  answers `no-store` and `Referrer-Policy: same-origin`, which keeps the state
  off third-party sites while leaving the browser's own same-origin
  relationship intact; and never writes the credential to a log.
- A submission is judged by `Sec-Fetch-Site` when the browser reports it, and
  by `Origin` otherwise. A browser may serialise the origin of its own form
  post as `null`, so a null origin is not treated as hostile by itself.
- It stops the moment a credential is accepted, when the account panel is
  closed, or after the timeout &mdash; whichever comes first.

If the socket cannot be opened at all &mdash; a sandbox may refuse it &mdash;
the plugin says so and the existing paste field keeps working.

## Cloudflare verification

Some catalogs put a Cloudflare browser check in front of their pages. **The
plugin does not solve, answer, or work around that check.** It hands the check
back to you and can then reuse the result.

When a search or import is blocked, the plugin names the host that is asking
and offers **Add Cloudflare verification**. The flow is:

1. Open the page in your own browser and pass the check yourself.
2. In that same tab, copy the `cf_clearance` cookie value (a full `Cookie`
   header containing it is also accepted).
3. Copy that browser's `User-Agent` string.
4. Paste the host, the cookie, and the User-Agent into the panel and save.

Both halves are required. Cloudflare binds a clearance to the exact User-Agent
that earned it, so the cookie on its own is refused - that mismatch is the
usual reason a pasted clearance appears to do nothing. The panel deliberately
does **not** prefill the User-Agent from the plugin window, because the
embedded webview is not the browser that passed the check.

If you reach a portal through **Sign in in browser**, the clearance usually
arrives on its own. A `Cookie` header copied from a browser that has just
passed a check already contains `cf_clearance`, and the hand-over page runs in
that same browser, so it reports the matching `User-Agent` itself. Both halves
are present, and the plugin keeps them without asking you to repeat the
exercise here. The panel above stays for the cases where they are not — a
clearance obtained separately, or one that has expired.

The plugin's own panel deliberately does *not* prefill the User-Agent, because
the OrcaSlicer window is an embedded webview and its agent is not the one that
earned the clearance. The hand-over page is the opposite case, which is why the
capture happens there.

What to expect:

- A clearance is tied to your current IP address and expires on Cloudflare's
  own schedule. Changing network, or simply waiting long enough, invalidates it.
- When a stored clearance stops being accepted, the plugin discards it and says
  so instead of retrying a dead session.
- A clearance saved for a domain also covers its subdomains, matching how the
  browser scopes the cookie. This matters for catalogs whose API lives
  elsewhere: Thangs serves search from `production-api.thangs.com`, so a
  clearance saved for `thangs.com` covers it.
- The cookie is attached only to the host it was saved for. Redirects to a CDN
  or any other host never carry it.

Stored verifications live in the same `sessions.json` as portal sessions and
can be removed from the panel with **Forget host**.

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
- Thangs signed download URLs are downloaded without leaking the Thangs bearer token to the signed storage host.

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

The plugin keeps one deployment file, `src/search_engine.py`, but platform behavior is no longer spread across parallel dictionaries. Each portal has one `PlatformSpec` entry containing its display name, adapter, authentication hosts, cookie mode/scope, login URL, referer policy, session-recheck policy and profile-picker behavior. Search, import, authentication status and UI routing all consume that registry, including the generated display-name-to-key map used by the embedded UI.

Independent portal searches run concurrently with a maximum of eight workers. Results are still merged in the portal-selection order, so relevance ordering is deterministic rather than dependent on network completion order.

Anonymous and authenticated HTML catalog requests share the same redirect limit and per-hop public-address validation. Temporary HTTP sessions and responses are closed after catalog probes and profile/file resolution.

Repository layout:

```text
src/       standalone plugin source
tests/     unit and regression tests
scripts/   repeatable validation helpers
docs/      release history, platform survey, and legal notes
typings/   OrcaSlicer API type stubs
.github/   cross-platform CI configuration
```

The regression suite covers authentication, redirect credential isolation, DNS/private-address rejection, catalog adapters, import flow, multi-file selection, registry/UI consistency and Speed Dial window reuse.

Typical checks:

```bash
python -c "import pathlib, py_compile; [py_compile.compile(str(path), doraise=True) for directory in ('src', 'scripts', 'tests') for path in pathlib.Path(directory).glob('*.py')]"
python -m unittest discover -s tests -t . -v
pyright --project pyrightconfig.json
ruff check .
vulture src/search_engine.py --min-confidence 80
radon cc src/search_engine.py -s -a
bandit -q -r src/search_engine.py
python scripts/live_smoke.py
```

The embedded JavaScript can also be extracted and validated with:

```bash
python scripts/check_embedded_js.py > embedded-ui.js
node --check embedded-ui.js
```

The v0.8.8 validation gates are:

- Python compile check
- embedded JavaScript syntax check
- all unit/regression tests, repeated twice
- Pyright: **0 errors, 0 warnings**
- Ruff and Vulture: no findings
- Bandit: no unreviewed findings
- Radon complexity report and Windows IPC smoke test
- opt-in live search smoke across public programmatic catalogs
- live first/second-page verification for public paginated APIs, including duplicate detection

## Current limitations

- Portal websites and private web APIs can change without notice; an adapter may need updating when a site changes its frontend/API.
- Yeggi provides a browser-search link. Thangs uses its official production JSON API; the browser fallback remains available if that API itself ever requires interaction.
- Thingiverse and MyMiniFactory require user-supplied developer credentials for their documented APIs.
- MakerWorld download endpoints are not a public stable third-party API contract.
- Browser-session integrations depend on the user's own valid portal session.
- Paid, checkout, member-only, CAPTCHA and interactive flows intentionally stay in the browser.
- License metadata is informational. The model page is authoritative when platform metadata is missing or inconsistent.
- Real-account end-to-end behavior can only be validated with a valid account/session for the relevant portal.

## Legal

This project does not host, mirror or redistribute model files. Downloads use the model portal's own URLs and the user's own account/session when required.

The plugin does not grant rights to any model. You are responsible for complying with the model's license and the portal's terms.

See [`docs/LEGAL_ANALYSIS.md`](docs/LEGAL_ANALYSIS.md) for additional project notes. Nothing in this repository is legal advice.

## Changelog

See [`docs/CHANGELOG.md`](docs/CHANGELOG.md) for the consolidated release history.

## License

MIT, see [LICENSE](LICENSE).

OrcaSlicer is a separate project licensed under its own terms.
