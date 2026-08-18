# Changelog — v0.3.4

- Fixed Windows import failure caused by `Invalid option --single-instance`.
- Windows now sends OrcaSlicer's native `WM_COPYDATA` single-instance payload directly to the current OrcaSlicer main window, so no second Orca process or CLI flag is required.
- macOS/Linux handoff no longer passes `--single-instance`; only downloaded file paths are supplied.
- The model detail/import panel now closes when the user clicks anywhere else in the search window.
- Clicking another result first closes the previous detail panel and then opens the newly selected model.
- Added regression tests for native Windows routing, absence of the invalid CLI flag, Orca argv serialization, and click-outside panel closing.
