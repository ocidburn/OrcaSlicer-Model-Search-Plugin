# Source, Account, and Legal Notes

This document records how the plugin reaches each catalog and which boundaries
the implementation enforces. It is a technical project note, not legal advice.
Platform terms and model licenses can change; the linked model page remains the
authoritative source.

## Operating principles

- The project does not host, mirror, or redistribute model files.
- Search requests go from the user's OrcaSlicer installation to the selected
  catalog. There is no project-operated proxy, telemetry service, or analytics.
- Downloads use a catalog's own URL and, where required, the user's own account
  session or developer credential.
- A model's license is shown when the source exposes it. `Unknown` is preserved
  when it does not; the plugin does not invent a permissive license.
- Paid, checkout, membership, CAPTCHA, and other interactive flows remain in
  the official browser.
- Browser-only search cards are labelled as such and are not presented as
  downloadable models.

## Source map

| Source | Search mechanism | Credential | File behavior |
|---|---|---|---|
| Printables | Public GraphQL `searchPrints2` | None | Canonical per-file `getDownloadLink` URLs |
| MakerWorld | Bambu search service | None for search; user token for import | Selected signed 3MF profile; raw STL/CAD stays in browser |
| Makeronline | Public search endpoint | User Anycubic token for import | Account-authorized files |
| Nexprint | Public model-library gateway | User `auth_token` for import | Account-authorized files |
| Cults3D | Public HTML catalog | User browser cookies for files | Account/checkout rules preserved |
| GrabCAD | Member HTML catalog | User browser cookies | Member download rules preserved |
| Thingiverse | Official API | Personal developer access token | Official files API |
| MyMiniFactory | Documented API v2 | Personal API key | API-key metadata; OAuth/store downloads stay in browser |
| Yeggi | Pre-filled official browser search | Browser interaction | Meta-search; original portal owns the file flow |
| STLFinder | Public indexed model pages | Original portal credential, when required | Delegates only to a registered original portal |
| Creality Cloud | Current public model-tag pages | None for search | Direct public file when exposed; otherwise browser |
| Smithsonian 3D | Public Smithsonian file API | None | Public STL ZIP resource |
| NASA 3D Resources | Official NASA GitHub mirror | None | Canonical public STL/3MF raw files |
| NIH 3D | Public Discover application | None | Validated public file when exposed; otherwise browser |
| YouMagine | Current public HTML search | None | Validated public file when exposed; otherwise browser |
| Pinshape | Current public HTML search | None | Validated public STL when exposed; otherwise browser |
| Thangs | Pre-filled official browser search | Browser interaction | Browser only |

Undocumented public web endpoints can change without notice. Their use here is
limited to the same search metadata exposed to an ordinary visitor; the plugin
does not solve challenges, impersonate another account, or bypass access gates.

## Credentials and privacy

The credential store contains tokens or copied session-cookie values only.
Passwords are never persisted. MakerWorld may accept a password transiently to
obtain the user's token; the value is removed from the message immediately after
the login attempt.

Credentials are scoped by the central `PlatformSpec` registry:

- authorization headers are attached only to allow-listed platform hosts;
- headers are rebuilt after every redirect;
- query parameters are removed after the first redirect;
- copied cookies are placed in a domain-scoped cookie jar;
- signed CDN/storage downloads do not receive the platform bearer token.

The plugin does not collect search history, account identifiers, downloaded
filenames, or usage analytics. A selected platform still receives the query and
the ordinary request metadata needed to answer it under that platform's own
privacy policy.

## Download safeguards

- Only HTTP(S) URLs are accepted.
- Literal and DNS-resolved private/local targets are rejected.
- HTML/login responses are rejected as model files.
- Redirects and download sizes are bounded.
- Filenames are sanitized and collision-safe.
- ZIP extraction rejects path traversal and oversized archives.
- Unsupported archive types are not advertised as importable.
- A failed browser/file resolution is reported as a failure, not a successful
  import.

## User responsibility

The user is responsible for reviewing and complying with both the model license
and the hosting platform's terms. In particular, attribution, non-commercial,
no-derivatives, share-alike, editorial-use, trademark, publicity, and government
media restrictions may continue to apply after a technically successful
download.

License metadata reflects what the platform or uploader published. It is not a
guarantee that the uploader owns the design or selected the correct license.
Questions or takedown requests concerning a model should be directed to the
platform hosting that model; this repository stores no model content.

## Maintenance checklist

When adding or changing a source:

1. Prefer a documented first-party API.
2. Record authentication and download boundaries in this file and the README.
3. Keep unknown license, price, and free-status values nullable.
4. Add deterministic adapter tests and, for anonymous sources, a live smoke case.
5. Verify credential scoping, redirect behavior, SSRF checks, and browser fallback.
6. Re-check the source's current developer terms and robots/access controls.

The plugin is distributed under the MIT License and supplied without warranty,
as described in [LICENSE](../LICENSE).
