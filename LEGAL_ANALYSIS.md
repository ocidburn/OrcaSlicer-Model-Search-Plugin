# Legal Analysis — OrcaSlicer 3D Model Search Plugin

## Executive Summary

4 adapters all use **publicly accessible endpoints** (no auth, no access control bypass).
All platforms serve search results and license metadata from public URLs that the platform's
own web frontend calls. We access the same public data at reasonable rates with license
display before download and a first-run disclaimer. Risk: LOW.

Core safeguards implemented:
1. License metadata displayed BEFORE download (non-negotiable)
2. Public endpoints only — no auth bypass, no credential extraction
3. No caching, redistribution, or re-hosting of model files
4. First-run disclaimer requiring user acknowledgment
5. No data collection or telemetry

---

## Platform-by-Platform Analysis

### 1. MakerWorld (Bambu Lab) — `api.bambulab.com`

| Aspect | Detail |
|--------|--------|
| Endpoint | `GET api.bambulab.com/v1/search-service/select/design2?keyword=X&limit=30` |
| Auth | None required. Public endpoint. |
| License metadata | `license` field: BY, BY-SA, BY-NC, BY-ND, CC0, Standard Digital File License, Exclusive |
| Files | `designExtension.model_files[]` — model name, type, size |
| Access method | Same API called by makerworld.com frontend. Public CDN endpoint. |
| ToS risk | **LOW** — public endpoint, no auth bypass, no scraping. |

### 2. Nexprint (Elegoo) — `nexprint.com`

| Aspect | Detail |
|--------|--------|
| Endpoint | `GET nexprint.com/gateway/api/v1/model-library-server/model-base-info/search?keyword=X&pageNo=1&pageSize=30` |
| Auth | None required. Public gateway. |
| License metadata | `licenseType` integer: 0=CC0, 1=BY, 2=BY-SA, 3=BY-NC, 4=BY-NC-SA, 5=BY-ND, 6=BY-NC-ND, 7=ARR |
| Files | `model-base-info/get?id=X` — `modelFileInfoList[]` with fileUrl, fileName |
| Access method | Public REST gateway. Same API called by nexprint.com frontend. |
| ToS risk | **LOW** — public gateway endpoint, no auth bypass. |

### 3. Makeronline (Anycubic) — `makeronline.com`

| Aspect | Detail |
|--------|--------|
| Endpoint | `POST makeronline.com/api/search/model` with `{keyword, page, page_size, print_type:0, search:1}` |
| Auth | None required. Public endpoint. |
| License metadata | `license` integer. 1=BY, 2=BY-SA, 3=BY-NC, 4=BY-NC-SA, 5=BY-ND, 6=BY-NC-ND, 7=CC0, 8=Standard |
| Files | `api/mold/detail?id=X` — `files[]` with url, file_name |
| Access method | Same POST endpoint called by makeronline.com Nuxt frontend. |
| ToS risk | **LOW** — public endpoint, no auth bypass. |

### 4. Printables (Prusa Research) — `printables.com`

| Aspect | Detail |
|--------|--------|
| Endpoint | `GET printables.com/search/models?q=X` — HTML page with JSON-LD embedded data |
| Auth | None required. Public search page. |
| License metadata | `license` name (extracted from JSON-LD embedded in search page `<script type="application/ld+json">`) |
| Files | Search results link to model detail pages. Files hosted on media.printables.com. |
| Access method | HTML scraping of public search results page. Same page served to anonymous browsers. |
| ToS risk | **LOW** — accessing public search results page at reasonable rates. No authentication bypass. |
| Precedent | hiQ v. LinkedIn (9th Cir. 2022): accessing publicly available website data is not a CFAA violation. |

### 5. Thingiverse (UltiMaker)

Disabled. Thingiverse requires OAuth app registration but the developer portal was
removed in the 2025 site migration. API still functions but new app tokens cannot
be obtained. Re-evaluate when developer portal returns.

### 6. GrabCAD (Stratasys)

Disabled. Both v1 and v2 REST APIs return 404. Public API fully retired.

---

## Key Legal Framework

### No CFAA / Computer Misuse Violation

All 4 adapters access **publicly available** endpoints without:
- Circumventing any authentication or access control
- Extracting credentials or session tokens
- Password cracking or credential stuffing
- Exceeding authorized access

The hiQ v. LinkedIn precedent (9th Circuit, 2022) established that accessing publicly
available data on a website is not a violation of the Computer Fraud and Abuse Act (CFAA),
even when the website owner objects. All our endpoints are reachable without authentication.

### No Copyright Infringement

- We do NOT host, cache, mirror, or redistribute any 3D model files
- We display license metadata before download (legal requirement for CC licenses)
- Downloaded files are stored on the user's local machine
- The user is responsible for complying with each model's license

### GDPR / Data Privacy

- No user data is collected, stored, or transmitted
- Search queries go directly from the plugin to the platform's API
- No analytics, no tracking, no telemetry
- No cookies or persistent identifiers

---

## Implemented Safeguards

1. **First-run disclaimer** — modal dialog explaining license responsibilities
2. **License display** — every search result shows license name, summary, and link
3. **License acknowledgment** — download button disabled until user views license
4. **No caching** — downloads go to the plugin's local directory only
5. **No redistribution** — files are never uploaded or shared
6. **Rate limiting** — reasonable request intervals (30s timeout, sequential queries)

---

## Risk Matrix

| Risk | Severity | Mitigation | Status |
|------|----------|------------|--------|
| User downloads ARR model without attribution | HIGH | License displayed before download; first-run disclaimer | Implemented |
| Platform ToS violation (scraping) | MEDIUM | Public endpoints only; no auth bypass; reasonable rates | Implemented |
| Mass copyright infringement | MEDIUM | No bulk download; license gate; disclaimer | Implemented |
| Anti-bot blocking | LOW | Rate limiting; user-agent header | Implemented |
| GDPR violation | LOW | No data collection | Verified |
| OrcaSlicer ToS violation | LOW | Plugin is separate work (AGPL-compatible MIT license) | N/A |

---

## License

Plugin licensed under MIT. OrcaSlicer is AGPL-3.0. Python plugins loaded at runtime
via OrcaSlicer's plugin system are separate works — not derivative works. The plugin
imports `orca` as an API boundary (standard plugin model under copyright law).
