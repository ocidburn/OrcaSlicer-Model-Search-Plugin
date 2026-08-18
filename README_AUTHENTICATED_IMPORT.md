# OrcaSlicer Model Search authenticated import — v0.2.2

## Makeronline / Anycubic authentication fix

v0.2.2 removes the legacy direct Anycubic username/password endpoint (`api.cloud.anycubic.com/api/user/public/login`) from the login path.

Use one of these supported session methods instead:

1. Sign in to Anycubic Slicer Next, then in the plugin choose Makeronline > Account > Import from Anycubic Slicer Next.
2. Paste an existing Makeronline/Anycubic access token into the Makeronline Account dialog.

The plugin does not persist passwords. Tokens are stored per portal in the plugin auth store with restrictive local file permissions where supported.

## Validation

- Python compile check: passed
- Embedded JavaScript syntax check: passed (`node --check`)
- Auth/import unit tests: 10/10 passed
