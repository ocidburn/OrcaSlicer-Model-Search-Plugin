# Changelog — v0.3.0

- Added Thingiverse public search and public ZIP/file import.
- Added Cults3D search and authenticated browser-session download handling.
- Added MyMiniFactory public/free search and import with paid-store browser fallback.
- Added Thangs public/free search and import with membership/marketplace browser fallback.
- Retained and hardened Makeronline (Anycubic) authenticated import.
- Added Creality Cloud public search and direct 3MF/STL/CAD import attempts.
- Retained and hardened Nexprint (Elegoo) authenticated import.
- Added GrabCAD Community Library search/download using a domain-scoped browser session.
- Added generic HTML/SSR model discovery and validated download-candidate probing.
- Added Range-probe fallback for CDNs that reject partial requests.
- Added stale-login-page detection for Cults3D and GrabCAD.
- Added safe ZIP extraction for Thingiverse and other archive downloads.
- Added browser fallback for paid/member/checkout-only flows instead of false success.
- Expanded UI platform filters and account controls only for gated resources.
- Added catalog adapter and security regression tests.
