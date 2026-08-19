# Changelog

## Unreleased

## 0.8.0

- Restored the non-excluded public NIH 3D, Smithsonian 3D, and NASA 3D
  Resources adapters listed in the platform landscape, including direct public
  ZIP/STL/3MF import where each source exposes it.
- Added Yeggi and STLFinder coverage. Meta-search results never masquerade as
  hosted files: STLFinder delegates import to the registered original portal,
  while Yeggi's interactive Turnstile flow stays in the browser.
- Added validated direct import for public Pinshape `/stl/` resources.
- Removed excluded CGTrader from the adapter registry, search UI, tests, and
  current documentation. Wikimedia Commons remains excluded.
- Extended challenge detection to Cloudflare Turnstile pages returned with
  HTTP 200 and retained the single standard-UA retry plus browser fallback.

## 0.7.2

- Added one HTML-only retry with standard browser navigation headers when a
  response is explicitly marked `cf-mitigated: challenge` or matches a
  Cloudflare challenge page.
- Added a per-source **Open in browser** action when interactive Cloudflare
  verification remains necessary after the retry.
- Kept the plugin-identifying User-Agent unchanged for JSON/API traffic and
  continued to leave CAPTCHA solving and other interactive checks to the user's
  browser.

## 0.7.1

- Fixed Thingiverse licenses remaining `Unknown` until the user opened each result card.
- Added automatic background hydration of missing Thingiverse licenses and complete metrics, limited to four concurrent official API requests and skipped when the search response already supplies a license.
- Kept background failures silent and retryable from the card while preventing stale results from an older search from overwriting the active result set.

## 0.7.0

- Added real server-side pagination for Printables, MakerWorld, MakerOnline, Nexprint, Thingiverse, and MyMiniFactory, using each service's page or offset parameter.
- Added a **Load next pages** action that fetches every eligible selected source, de-duplicates new models, reapplies global filters/sorting, and preserves the current display page.
- Added per-source result diagnostics with loaded and visible counts, exact totals when exposed, pagination/exhaustion state, first-page-only labels, and portal-specific errors.
- Kept lazy-loaded model details in the server-side result cache so loading more pages cannot revert updated license or metric metadata.

## 0.6.2

- Fixed MyMiniFactory search cards by reading official nested image variants such as `images[].thumbnail.url` and preferring the primary image.
- Fixed MyMiniFactory license metadata by reading the documented singular `license` field, normalizing its Digital File Store license, and using the `licenses[].type=store` flag only as a fallback.

## 0.6.1

- Added client-side pagination for the merged result set with numbered pages, previous/next controls, visible result ranges, and 12/24/48-card page sizes.
- Preserved full-result indexing and the active page when lazy model details are refreshed, while new searches reset to page one.

## 0.6.0

- Removed NIH 3D, Smithsonian 3D, NASA 3D Resources, and Wikimedia Commons from the adapters, platform registry, search UI, live smoke checks, tests, and current documentation.

## 0.5.9

- Added official per-file Thingiverse render thumbnails and file sizes to the multi-file import picker.
- Added enlarged mouse-hover and keyboard-focus previews for Thingiverse file images, with an official `default_image` fallback when a file thumbnail is absent.

## 0.5.8

- Completed MakerOnline's registered OAuth URL with its official redirect URI, `read` scope, state, and language parameters.
- Added explicit normalization for a copied MakerOnline `mo_access_token` cookie or Cookie header while keeping browser-profile access out of the plugin.
- Added per-file Printables render thumbnails, file sizes, and enlarged mouse-hover or keyboard-focus previews to the multi-file import picker.

## 0.5.7

- Updated MakerOnline's official-login button from the old generic user-center page to Anycubic's current `cas.anycubic.com` OAuth authorization endpoint and clarified that the plugin still needs a Slicer Next session import or access token.

## 0.5.6

- Fixed MakerWorld 3MF downloads whose print-profile names contain decimal dots, such as `0.2mm layer`, being saved without the required `.3mf` extension and therefore skipped by OrcaSlicer import.

## 0.5.5

- Added lazy search-result thumbnails with an `IntersectionObserver` fallback for embedded browsers without native lazy loading.
- Added background MakerWorld print-profile metadata and image preloading when a result card is selected; Import reuses the cached choices.
- Added enlarged mouse-hover and keyboard-focus previews for MakerWorld print-profile images.

## 0.5.4

- Added a hover/focus help tooltip to every portal authentication card with portal-specific login, token, cookie, or API-key instructions.
- Reused the same instruction registry inside the account dialog so the short tooltip and detailed authorization flow cannot drift apart.

## 0.5.3

- Fixed MakerWorld's complete-profile response normalization so profile covers, titles, authors, descriptions, ratings, and print settings are taken from the populated response fields instead of an empty nested placeholder.
- Merged the complete profile list with rich design metadata and added profile descriptions to the picker UI.

## 0.5.2

- Replaced Printables filename-based storage URL construction with its canonical `getDownloadLink` GraphQL mutation.
- Added an explicit MakerWorld print-profile picker with profile metadata instead of silently downloading the first profile.
- Added a MakerWorld format choice: direct signed 3MF import or the official signed-in browser flow for raw STL/CAD files.
- Added regression coverage for Printables mixed-case/spaced filenames and MakerWorld profile/format routing.
- Organized the repository into `src/`, `tests/`, `scripts/`, `docs/`, and `typings/` while keeping the plugin itself as one downloadable Python file.
- Centralized test-module loading and reusable JavaScript/Windows IPC validation helpers.

## 0.5.1

- Fixed Thingiverse results showing `Unknown` when the compact search response omits the model license.
- Added lazy official Thingiverse detail loading when a result card opens, avoiding an extra API request for every search hit.
- Canonicalized Thingiverse Creative Commons names such as `Creative Commons - Attribution` to `CC BY` with the official license URL and summary.
- Added Thingiverse download, view, and print counters from the detail response plus regression coverage for the real response shape.

## 0.5.0

- Added a common nullable metrics schema and merged-result sorting by relevance, normalized popularity, downloads, likes, rating, publication date, print count, name, and platform.
- Added explicit free-only and direct-import-only filters; unknown values are never guessed as zero or free.
- Added Smithsonian 3D, Wikimedia Commons, NASA 3D Resources, NIH 3D, YouMagine, Pinshape, and CGTrader/browser search support.
- Replaced Thingiverse's non-functional client-rendered HTML search with its authenticated official API.
- Replaced MyMiniFactory's obsolete HTML endpoints with its documented API v2 and explicit API-key authentication.
- Replaced Creality Cloud's removed search URL with its current model-tag pages.
- Converted Thangs and CGTrader to clearly labelled browser-search results because their interactive protection blocks anonymous programmatic search.
- Added source-native metrics for Printables, MakerWorld, Makeronline, Nexprint, Thingiverse, MyMiniFactory, and NIH 3D.
- Added an opt-in live public-catalog smoke test and expanded registry/UI/sorting regression coverage.

## 0.4.0

- Replaced five parallel platform maps with one `PlatformSpec` registry covering search, import, authentication hosts, cookie scope, login URL, and referer behavior.
- Removed the redundant always-true adapter `enabled()` hook and development-only autorun path.
- Rebuilt authorization headers on every redirect so MakerWorld and Anycubic credentials cannot follow a cross-host redirect to a CDN.
- Unified signed and ordinary downloads on the same bounded streaming path; removed the legacy `urllib`/Amazon S3 special case.
- Added a bounded Windows IPC timeout so an unresponsive OrcaSlicer window cannot hang the plugin worker indefinitely.
- Fixed Win32 error capture to read `GetLastError` after `EnumWindows`, and stopped advertising RAR/7z/G-code as directly importable formats.
- Consolidated size limits and hardened download cleanup and HTML-response rejection.
- Removed obsolete development probes, machine-specific deployment scripts, live-network pseudo-tests, handoff notes, plans, and superseded documentation.
- Consolidated versioned changelog fragments into this file and expanded registry/security regression coverage.
- Reduced complexity in authentication, resolver, download, and UI dispatch paths.

## 0.3.4

- Fixed Windows import by sending OrcaSlicer's native `WM_COPYDATA` payload directly to the current main window.
- Removed the unsupported `--single-instance` flag from all platforms.
- Closed the model detail panel when clicking elsewhere in the search window.

## 0.3.3

- Added per-portal search checkboxes, select-all/select-none controls, a selected-source counter, and persisted selection.

## 0.3.2

- Added current-project import and a checkbox picker for models with multiple downloadable files.

## 0.3.1

- Renamed the action to `Search 3D Models` and reused the existing non-modal search window.

## 0.3.0

- Expanded search/import support to Thingiverse, Cults3D, MyMiniFactory, Thangs, Creality Cloud, and GrabCAD.
- Added browser-session authentication, validated download probing, safe ZIP extraction, and browser fallbacks for gated content.

## 0.2.2

- Removed the legacy Anycubic password endpoint and added Anycubic Slicer Next token import.
