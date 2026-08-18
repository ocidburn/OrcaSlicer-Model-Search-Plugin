# Changelog

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
