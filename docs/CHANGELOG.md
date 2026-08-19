# Changelog

## Unreleased

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
