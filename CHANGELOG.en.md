# LeafFS Changelog

> English version. AI-translated from the Chinese original.

Author: ewq2526. Version format: major.minor.patch. There were no earlier versions or change records; this file records changes starting from 1.0.3.

## Unreleased

> Fixes and configuration added after 1.0.6; not yet released.

### Fixes

- **Names like `NUL` and `CON` are no longer treated as files**: Windows resolves these reserved names to devices, so `os.path.exists` returns true for them and they slipped past the "does this file exist" check, only failing further down in the thumbnail pipeline — the same input gave an administrator a 500 and a normal user a 404 (no disclosure and no privilege bypass, but the status code should not fork like that). Such names are now rejected at the path entry point; normal names such as `NULL.txt`, `COM10` and `console` are unaffected.
- **Concurrent login attempts can no longer break past the failure lockout**: the lockout check and the failure counter each took the lock separately, before and after the password hash computation, leaving that stretch unguarded — so concurrent attempts all saw "not at the limit yet" and were let through together, raising the attempts allowed in one burst from 6 to 20. The check and the count now happen inside the same lock, and a successful sign-in still resets the counter. (Reproduced by an external tester: 8 concurrent requests, all 8 let through.)
- **A WebSocket delete request that deletes nothing now reports failure**: this message reads the field `paths`, and when it was missing, empty, or written as `files` (the name HTTP's `/api/delete` uses), the old code always answered "success, 0 deleted" — leaving the caller to believe the files it asked about were gone. That case now returns an explicit failure with a reason. (The branch itself works: with the right field name and valid paths it does delete the files.)
- **A wrong parameter name for quota or speed limit now returns an error instead of silently meaning "unlimited"**: `/api/users/quota` and `/api/users/speed` read the fields `quota_mb` and `speed_kb`; when the name did not match or the field was missing, the old code **silently used 0** — and 0 is a legal value here (= no quota / no limit), so the response said "success" while the setting was actually changed to unlimited. Missing or invalid parameters are now rejected with a clear error, and no setting is modified.
- **A folder's "N files / X bytes" no longer gets stuck**: the directory aggregate cache wrote entries under one key shape and invalidated them under another (writes used mixed separators like `users/zzw`, invalidation normalized to `users\zzw`), and the two never matched — so **invalidation had never actually worked**. The visible effect: newly uploaded files did not change the count (measured over 240 seconds with three uploads, all stuck at the old value), and after an admin deleted an account and recreated it under the same name, the new account saw the **previous** owner's file count and used space. Cache keys are now normalized so invalidation takes effect; on startup, entries written under the old key shape are discarded and recomputed.
- **Thumbnail cache keys no longer collide (one account could see another account's image)**: a thumbnail's on-disk name used to be "the relative path with separators replaced by underscores", and that transform is **lossy** — `users/zzv/p_s.png` and `users/zzv_p/s.png` collapsed into the same file. Reproduced by an external tester: an account requested **its own** file and received another account's thumbnail, while reading the other account's original file directly still returned 404 — the permission check itself was fine; the step that picked the cached file was not. On-disk names now use a path hash, so they cannot collide; thumbnails written under the old naming are cleaned up at startup and regenerated on demand.
- **An unrecognized WebSocket message type is no longer silently dropped**: on authenticated connections (guest, admin) such messages used to get **no reply at all**, leaving the client waiting indefinitely — anonymous connections did get an answer, because they are rejected by the auth check before reaching message dispatch. A clear error is now returned and the connection stays usable. Reported by an external tester.
- **aria2c cleanup no longer kills by image name**: it used to run `taskkill /f /im aria2c.exe`, taking down **every** aria2c on the machine — including another LeafFS instance's downloader (a second installation, or one running from source). That instance would immediately start a new one, leaving the two fighting each other. Cleanup now matches this instance's RPC port and clears only its own process.

### Configuration

- New configuration key `guest_login_max_per_min`: the guest sign-in rate limit (per IP, per minute; default 10). Adjustable under "Deep Config" on the admin page, or directly in `config/server_config.json`.

### Documentation

- The Android APK now ships the third-party notices and license texts (`assets/THIRD_PARTY_NOTICES.md` and `assets/licenses/` inside the APK, 23 files in total); previously the APK contained **no** third-party notice at all.

## 1.0.6 - 2026-09-19

> This release adds an Android version; the rest is security hardening and stability fixes.

### Features

- Added an Android version: run the LeafFS server on an Android phone. Once the app is started, other devices on the same Wi-Fi network or hotspot can browse, preview, upload and download files through a browser by visiting the phone's address, with nothing to install on those devices.
- The Android version shares the same server and web pages as the desktop version: the interface, account system, configuration format and operation are identical. The two keep their data separately, and nothing is synchronized between them.
- On the phone, data (accounts, shared files, configuration) is stored in the app's private directory, and uninstalling the app removes it; back up anything important beforehand.
- Added file sharing: share files and folders from your own directory; shared items appear as "Public shares" in the browsing page's shares area, and others can also reach them directly through the share page `/p/<owner>`. A share is a reference, not a copy — the files stay where they are.
- A share can be protected with an access code: once set, anyone who has not passed the check can see neither the listing nor the contents of that share; the owner, and shares without a code, are unaffected.

### Security

- This release includes a round of security hardening and permission corrections covering access control, uploads, sessions and credentials, page content policy, and outbound access blocking in the built-in window and on mobile; for security reasons the individual items are not listed here.

### Fixes

- Search for guests was always denied; it now returns the results the visitor is entitled to see, by role.
- The upload quota used to fill up permanently: after a certain amount every upload failed, deleting already-uploaded files did not help, and only a restart recovered it; the quota is now returned when an upload ends.
- Partially uploaded files used to appear with odd names in the file list, search results and folder-size totals; they are now completely invisible.
- When an upload was interrupted midway, the partial data used to be saved as a complete file; it is no longer saved, and a clear error is returned instead.
- Refreshing or leaving the page during an upload used to silently stall the queue, so later files were never sent; the page now explains what happened and the queue continues, and desktop browsers ask for confirmation before refreshing while files are uploading.
- Deleting a file used to report success regardless of the outcome; the result is now reported honestly, "file in use" and "permission denied" are distinguished, and the list refreshes immediately on partial success.
- A failed packaged download used to return an empty archive and still report success; it now returns a clear error.
- The public shares folder used to disappear from the list after any file operation until the page was reopened; it now stays in place.
- After a service restart (including the mobile app being killed and restarted by the system) everyone used to be signed out; sign-in state is now preserved, while sign-out, kicking a device and deleting an account still take effect immediately and do not come back after a restart.
- Rejected requests often showed only a "network error" on the client instead of the reason returned by the server; the explicit message is now delivered.
- The file selection outline and the check mark in the corner often disagreed; they are now always in sync.
- Deleting an account used to leave that account's home directory in place; it is now archived, and recreating an account with the same name gets an empty directory.
- Entering an invalid value or an unknown key in Deep Config used to report success without taking effect; it is now rejected with an explanation.
- Non-admins used to see a downloader settings section that did nothing, and could read global policy through it; it is no longer shown or sent to them.
- Temporary upload files left behind by a crash or force-stop used to occupy disk space indefinitely; they are now cleaned up when the service starts.
- The certificate notice page (8082) no longer redirects you to the main site on its own: it used to do so merely because "this browser has signed in before", even when the session had long expired, which dropped you into the browse page and bounced you back to sign-in — as if the button had done nothing. The page now only explains the situation, and the button takes you to the main site.

### Architecture

- The main service changed from one connection per request to connection reuse, with idle connections actively reclaimed; Advanced Settings gained an "idle connection timeout" (default 15 seconds, adjustable 1–300) — the batch of static resources loaded when a page opens no longer re-handshakes one by one, and idle connections held by browsers no longer fill up the server.
- Supporting connection reuse: the time budget is recalculated for every request, and anything left unread from the previous request must be drained before the connection may be reused, so requests no longer slow each other down or mix up their content.
- Static resources now use conditional requests: when a resource is unchanged only the response headers are returned, so reopening a page no longer re-transfers the whole static bundle, while updated pages still take effect immediately instead of showing a stale one.
- The desktop built-in browser engine was upgraded from 4.2.2 to 6.2.1, and outbound blocking changed from polling the current address to hooking the engine's navigation, network and new-window events, removing the previous window of up to 0.4 seconds.
- The two channels that build the directory listing (page requests and the live channel) were merged into a single implementation, so the two no longer disagree.
- Cache policy now goes through a single exit point: caching is denied by default, and only what genuinely should be cached (static resources, thumbnails) declares it explicitly.
- Identity and permission rejections now uniformly return 404 (previously split across 401 / 403 / 404).

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
