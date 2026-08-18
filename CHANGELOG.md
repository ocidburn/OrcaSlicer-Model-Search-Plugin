# Changelog

## v0.4.0

- Deep code review and structural refactor of authentication, catalog resolution, download probing, import dispatch, and adapter registration.
- Fixed cross-host redirect handling so Makeronline `XX-Token` / Bearer credentials are rebuilt per redirect hop and cannot be forwarded to an untrusted CDN host.
- Removed the legacy `enabled()` adapter hook, development `SEARCH_ENGINE_AUTORUN`, duplicate resolver registry, unused embedded-UI state, legacy AWS `urllib` download path, and duplicated AuthStore write logic.
- Unified platform/resolver lookup around `_SEARCHERS` and derived platform metadata.
- Split MakerWorld design/profile/download resolution into explicit stages; reduced `MakerWorldSearcher.get_files` cyclomatic complexity from E(36) to A(5).
- Split public catalog probing/resolution into smaller helpers; reduced `_probe_download` from D(25) to B(10) and `_public_page_files` from D(26) to B(10).
- Consolidated Makeronline/Nexprint API file parsing.
- Restricted downloadable archive discovery to formats the plugin actually handles; RAR/7z/G-code are no longer falsely advertised as importable.
- Fixed Windows `GetLastError` usage around `EnumWindows`.
- Added a regression test proving Makeronline credentials are dropped after a cross-host redirect.
- Updated import-flow tests to the unified adapter registry.
- Removed one-off legacy tests, scratchpad probes, deployment scripts, obsolete planning/handoff docs, version-specific README fragments, review transformation scripts, reports, and staging workflows.
- Added permanent multi-OS CI for Linux, Windows, and macOS.

## v0.3.4

- Fixed Windows import failure caused by `Invalid option --single-instance`.
- Windows sends OrcaSlicer's native `WM_COPYDATA` single-instance payload directly to the current OrcaSlicer main window.
- macOS/Linux handoff no longer passes `--single-instance`; only downloaded file paths are supplied.
- The model detail/import panel closes when the user clicks elsewhere in the search window.
- Added regression tests for native Windows routing, Orca argv serialization, and click-outside closing.

## v0.3.3

- Added a dedicated search checkbox for every supported portal.
- Added Select all / Select none controls and a selected-source counter.
- Search is blocked with a clear message when no portal is selected.
- Portal selection is persisted in the embedded UI.
- Added a regression test requiring UI portals to match `_SEARCHERS`.

## v0.3.2

- Fixed model handoff to the already-running OrcaSlicer instance.
- Added multi-file selection with checkboxes, Select all / Select none, and single-file fast path.
- Added regression tests for handoff, missing files, and multi-file selection.

## v0.3.1

- Renamed the action to `Search 3D Models`.
- Reuses an existing non-modal search window and restores focus to the search field.
- Added Speed Dial/window lifecycle regression tests.

## v0.3.0

- Added Thingiverse, Cults3D, MyMiniFactory, Thangs, Creality Cloud, and GrabCAD support.
- Hardened Makeronline and Nexprint authenticated import.
- Added generic HTML/SSR discovery, download probing, stale-session detection, safe ZIP extraction, and browser fallback for gated flows.
- Expanded per-platform UI/auth behavior and catalog/security regression tests.
