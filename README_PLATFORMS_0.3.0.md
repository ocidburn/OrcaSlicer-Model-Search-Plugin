# OrcaSlicer Model Search Plugin — v0.3.0

This build expands search/import coverage while keeping authentication only where the platform actually gates downloads.

## Platforms

| Platform | Search | Direct import attempt | Plugin authentication |
|---|---:|---:|---|
| Thingiverse | Yes | Public ZIP / public files | No |
| Cults3D | Yes | Signed-in session only | Yes — browser Cookie header/session |
| MyMiniFactory | Yes | Public/free files; paid objects fall back to browser | No |
| Thangs | Yes | Public/free files; paid/member objects fall back to browser | No |
| Makeronline (Anycubic) | Yes | Authenticated model files | Yes — Anycubic Slicer Next token or pasted access token |
| Creality Cloud | Yes | Public 3MF/STL/CAD URLs when exposed | No |
| Nexprint (Elegoo) | Yes | Authenticated model files | Yes — `auth_token` browser session cookie |
| GrabCAD | Yes | Signed-in Community Library files | Yes — browser Cookie header/session |
| Printables | Yes | Public files | No |
| MakerWorld | Yes | Authenticated profile download | Yes — Bambu session |

## Download behavior

The plugin never treats a web page as a model file. Candidate download URLs are probed first and accepted only when the response looks like an actual model/archive attachment. If a platform keeps a paid, membership, checkout, or interactive download behind its web UI, the plugin opens the official model page instead of reporting a false import success.

ZIP downloads are expanded safely. Only supported model formats are extracted, archive paths are flattened/sanitized, and per-file/total extraction limits are enforced.

## Authentication safety

- Passwords are not persisted.
- Cults3D and GrabCAD copied browser cookies are stored as per-platform sessions and loaded into domain-scoped cookie jars.
- Session cookies are not added to explicit headers for external CDN hosts.
- Nexprint auth cookies are scoped to the Nexprint domain.
- MakerWorld/Anycubic authorization headers are only attached to allow-listed portal hosts.
- Downloads reject localhost/private literal IP targets and HTML/login responses.

## Validation

The release test suite covers authentication isolation, file resolvers, search parsing, paid/member fallbacks, stale sessions, cookie scoping, archive extraction, registry/UI coverage, and version metadata.

Two independent verification passes are performed before publishing: Python compilation, embedded JavaScript syntax validation, and the complete unit-test suite in both the source tree and a clean copied tree.

## Important platform constraints

Cults3D requires an account to download files and its documented API intentionally does not expose other users' 3D files. GrabCAD Community Library downloads require membership. For those two platforms the plugin uses the user's existing browser session rather than storing account passwords.

Public/free flows on Thingiverse, MyMiniFactory, Thangs, and Creality Cloud do not get an account UI in this plugin. If a particular model is paid/member-only or the site does not expose a direct public file URL, the plugin falls back to the official browser page.
