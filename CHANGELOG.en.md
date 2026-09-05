# LeafFS Changelog

> English version. AI-translated from the Chinese original.

Author: ewq2526. Version format: major.minor.patch. There were no earlier versions or change records; this file records changes starting from 1.0.3.

## 1.0.4 - 2026-09-05

### Features

- Added multilingual support: the interface offers Chinese and English, and the language is chosen on the My Account page and saved per browser (cookie `leaf_lang`).
- The English version uses a direct-write English page file approach: same-name `.en.html` files, and the server returns the matching file by language, falling back to Chinese when no English file exists; English content is marked as AI-translated.
- Added a unified translation glossary `TRANSLATION_GLOSSARY.md` to keep translations consistent across all English pages.

### Documentation

- Since the CHANGELOG baseline starts at 1.0.3, this round of multilingual changes is recorded under 1.0.4.

## 1.0.3 - 2026-09-05

### Security

- HTTPS certificates: when no certificate is configured, a random self-signed server certificate is automatically generated at first startup and written into the config; no CA is generated and no trust store is installed; when a certificate is configured, the configured one is used.
- Fixed /browse/ always returning 403 when entering subfolders: the path is percent-decoded before permission checks.
- Fixed QR-code sign-in being blocked with 401: /api/qrlogin is added to the invalid-session whitelist, so a scanning device can complete sign-in even with an expired cookie.
- The certificate notice page (8082) now recognizes signed-in devices: sign-in sets a non-Secure sign-in marker, a signed-in device visiting 8082 is automatically redirected back to the HTTPS main site, and the marker is cleared on sign-out.
- The default-password change reminder is moved into a banner at the top of the page; the Secure attribute of the session and sign-in marker cookies stays consistent with the transport layer.

### Features

- Added a user info page /me (navigation "My Account") that shows the current account and role and provides sign-out; a consistent entry point is added to Browse, Gallery, Downloader, Admin and every admin subpage.
- QR code colors follow the light/dark theme (white dots on a black background in dark theme), and the QR code in the QR-sign-in dialog now has a border.
- Admin page: whole-server connection count, HTTP connections per device and WebSocket connections per device are consolidated into one "connection limit" card; saving takes effect immediately.
- Advanced Settings adds a ZIP packaging mode switch, defaulting to full streaming (compressing while sending), with the option to switch to package-first-then-send and a prominent warning about the disk usage risk.
- Deep Config removes run parameters duplicated with Advanced Settings; the whole-server and per-device connection limits are moved out.
- Server log preview now shows the newest first and fills the card with row-height auto sizing.
- Text across the whole site is selectable for copying.

### Fixes

- Fixed guests being denied access to subfolders inside the Public directory.
- The per-device WebSocket connection limit changed from a fixed value to a configurable item (config key `ws_max_conn_per_ip`, default 8, adjustable on the Admin page).

### Documentation

- README, distribution notes, third-party component and license notices, and installer license files updated to be consistent with 1.0.3; the AI-assisted development statement and third-party component obligations are noted.
