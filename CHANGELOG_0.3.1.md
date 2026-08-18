# Changelog — v0.3.1

- Renamed the OrcaSlicer script action to `Search 3D Models` for clearer discovery in Actions Speed Dial.
- Reuses the existing non-modal search window when the action is invoked again instead of closing and recreating it.
- Sends an `activate_search` message to the existing window and focuses/selects the search field inside the page.
- Focuses the search field on first window load.
- Added regression tests for the Speed Dial action name, single-window reuse, and recreation after close.
- Validation: Python compile, embedded JavaScript syntax, 34/34 unit tests, and Pyright with 0 errors / 0 warnings.

Note: the current public OrcaSlicer `UiWindow` API exposes `post()`, `is_open()`, and `close()`, but no native OS-level `focus()`/`raise()` method. The plugin therefore reuses the existing window without duplicating it and restores keyboard focus inside its search page.
