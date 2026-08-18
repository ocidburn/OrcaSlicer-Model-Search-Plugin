# Changelog — v0.3.2

- Fixed `Import into OrcaSlicer` on Windows and macOS. The plugin now uses OrcaSlicer's own cross-platform single-instance IPC to hand downloaded model files to the already-running plater.
- Imports are added to the current OrcaSlicer project instead of being left only in the download directory.
- When a model exposes more than one downloadable file, the plugin now shows a file-selection dialog with checkboxes before downloading.
- Added Select all / Select none controls and imports only the checked files.
- Single-file models continue directly without an extra confirmation dialog.
- Added regression tests for single-instance handoff, missing-file handling, multi-file selection, single-file fast path, and UI checkbox coverage.
