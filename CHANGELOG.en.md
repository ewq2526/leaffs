# LeafFS Changelog

> English version. AI-translated from the Chinese original.

Author: ewq2526. Version format: major.minor.patch. There were no earlier versions or change records; this file records changes starting from 1.0.3.

## 1.0.5 - 2026-09-07

### Features

- Reworked the My Account page: change your own password (the old password must be verified; after a change, other devices of the account are signed out), list this account's signed-in devices and sign out all other devices with one click, show current-device info and storage usage (admins: server total; regular users: own folder; guests: public folder), and show the LAN server address with one-click copy.
- Interface language and theme color (blue/green/purple/orange/red) are chosen on the My Account page and also saved to the signed-in account on the server; after the built-in window (an incognito session) restarts and signs in again, the account's choices are restored. Browsers with a language cookie keep using their own choice.
- The whole UI supports swappable theme colors, each with matching background colors for light and dark mode.
- In English mode, dynamic data texts on admin pages (stats, connection list, status indicators, QR descriptions, etc.) now follow the interface language.

### Fixes

- Fixed occasional startup failure with HTTPS enabled and no visible cause: TLS context build errors are now logged.
- Fixed server-wide and parent-directory storage stats not refreshing after changes in subdirectories: folder-size cache invalidation now also invalidates ancestor aggregates.
- The server address on the My Account page no longer shows localhost; it shows the LAN address instead.

### Architecture

- Code structure refactor: HTTP, WebSocket, push notifications, TLS, and runtime path resolution were split into separate layered modules, and controllers were moved into their domain packages; external interfaces, configuration, and data files are unchanged.
- Added automated regression tests (tests/): they start the real service and cover sign-in, permissions, upload/download, and account APIs on key paths.

## 1.0.4 - 2026-09-05

### Features

- Added multilingual support: the interface offers Chinese and English, and the language is chosen on the My Account page and saved per browser (cookie `leaf_lang`).
- The English version uses a direct-write English page file approach: same-name `.en.html` files, and the server returns the matching file by language, falling back to Chinese when no English file exists; English content is marked as AI-translated.
- Added a unified translation glossary `TRANSLATION_GLOSSARY.md` to keep translations consistent across all English pages.

### Fixes

- The upload target directory is created automatically when missing; if no file is saved at all, the request now honestly returns failure with reasons instead of falsely reporting success.
- Folder deletion and creation over WebSocket now run in background threads, so deleting a large directory no longer blocks live page communication.
- aria2c (the process used for magnet/torrent downloads) is restarted automatically when it exits unexpectedly or becomes unreachable, and the page status refreshes accordingly.
- When the service appears frozen, evidence is collected automatically: a watchdog detects stalled requests and writes all thread stacks to config/freeze_dump_*.txt for troubleshooting.

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
