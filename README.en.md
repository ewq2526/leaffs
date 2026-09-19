# LeafFS README (English)

> English version. AI-translated from the Chinese original.

LeafFS is a LAN file sharing/download server implemented in Python 3.12. It runs on one machine within a LAN; other devices on the same network can open the service address in a browser to browse, preview (Gallery), upload and download files. It provides account and guest access control, HTTPS/TLS, URL download tasks and more. No client installation is required. This document is for users and secondary developers.

## 1. Features

- File services: a Public directory and a private directory per account; browsing, upload, download, folder archive download, and online preview (Gallery).
- File sharing: share files and folders from your own directory; shared items appear as "Public shares" on the browse page, and others can also reach them through the share page `/p/<owner>`. A share can be protected with an access code. A share is a reference, not a copy — the files stay where they are.
- Access control: three roles, User, Admin and Super Admin; optional guest mode; quotas and speed limits.
- Download tasks: HTTP/HTTPS direct links, magnet links, torrents and m3u8, downloaded by the server and recorded in the library. By default only public-network targets are allowed, and Guests cannot use it by default; aria2c (the process used for magnet/torrent downloads) is restarted automatically when it exits unexpectedly.
- Others: QR-code sign-in, local passwordless token, automatic generation of self-signed HTTPS certificates, adjustable connection and concurrency limits, sign-in rate limiting and account lockout.
- Multilingual: the UI provides Chinese and English. The language is chosen on the My Account page and saved to the signed-in account on the server; when the browser has a language cookie, the cookie wins. The English versions are hand-written page files (`*.en.html`); the server returns them accordingly and falls back to Chinese when no English file exists. The English content is AI-translated (differences may exist); unified terminology is described in `TRANSLATION_GLOSSARY.md`.
- Look & feel: the theme color is swappable (blue/green/purple/orange/red), each with matching background colors for light and dark mode; it is chosen on the My Account page and saved to the account.
- Android build: the same server and web pages also run on an Android phone (once the app is started, devices on the same Wi-Fi network or hotspot use a browser to open the phone's address). See `android/README.md` for the build and directory structure.

## 2. Running

- Environment: Python 3.12.
- Startup: run `python -m leaffs` in the project root directory.
- Bundled binaries: the `openssl.exe`, `aria2c.exe`, `ffmpeg.exe` inside `leaffs/` and the OpenSSL runtime libraries (`libssl-3-x64.dll`, `libcrypto-3-x64.dll`, `zlib1.dll`) are **not uploaded with the source code** (see `.gitignore`). When running from source needs these features, place the corresponding files on the machine or install system versions; when they are missing, the related features are unavailable (automatic certificate generation, downloader, thumbnails) while the rest are unaffected.
- Server-side local sign-in: the one-time local token link shown at startup (containing `?leaf=`) signs in automatically as the default Super Admin. The token is one-time and local-only; use it to enter the admin interface for the first time and to change the default password. Do not send that link to anyone else.
- Default ports: HTTP 8080, WebSocket 8081, certificate notice page 8082 (started only when HTTPS is enabled).
- The first sign-in uses the default Admin account `admin`; change the password as soon as possible. While the password is still the default one, the admin interface reminds you to change it.
- Runtime data directory: the project root when running from source, or the main program directory when running the packaged build. Runtime outputs include config, shared_files and .cache.

## 3. Configuration

- `config/server_config.json`: main runtime configuration.
- `config/users.json`: accounts and password hashes (PBKDF2-HMAC-SHA256); do not edit by hand.
- `config/downloader_config.json`: independent downloader configuration.
- Admin page entries:
  - Admin page `/admin`: guest mode, quotas, speed limits, connection limits (whole machine, per device, WebSocket), connected users, log preview.
  - Advanced Settings `/admin/advanced`: ports, TLS, cache, upload and preview size limits, ZIP packaging mode and limit.
  - Deep Config `/admin/deep`: password hash iterations and salt length, thumbnail parameters, session lifetime, guest write and downloader switches, access log, connection idle timeout, certificate notice page bind address, compatibility archives.
  - User info `/me`: current account and role, storage usage, current device and signed-in device management (sign out other devices), change your own password, language and theme-color settings, and server address copy.
- ZIP packaging is fully streamed by default (compressed and sent on the fly, no temporary disk usage); it can be switched in Advanced Settings to pack-then-send mode (includes the total size, but large files occupy temporary disk space).

## 4. HTTPS and Certificates

- This program does not install any CA or root certificate into the system trust store, and provides no one-click trust mechanism.
- Server certificate sources, in priority order:
  1. `tls_cert` / `tls_key` explicitly configured in `config/server_config.json`;
  2. existing `config/selfsigned.crt` and `selfsigned.key`;
  3. when neither exists and TLS is enabled, a random self-signed server certificate is generated automatically on first startup and written into config; it is reused afterwards. This certificate generates no CA and is not installed into any trust store.
- The program refuses to start in plaintext mode only when no explicit certificate is available and automatic generation also fails.
- Browsers show an insecure warning for self-signed certificates, which is normal. The certificate notice page on port 8082 explains this in plain language; a logged-in browser visiting that page is redirected back to the HTTPS main site.

## 5. Known Limitations and Notes

1. This software is designed for trusted LANs; direct exposure to the public Internet is not recommended. For public use you must configure a proper certificate and domain yourself, turn off guest mode and strengthen password management; the program provides no WAF, CAPTCHA or 2FA.
2. Sign-in state is preserved, so a server restart no longer signs everyone out; signing out, kicking a device, changing a password and deleting an account all take effect immediately. Sign-in is rate-limited and lockable: at most 30 attempts per minute per egress IP; about 6 consecutive failures for the same account lock it for 10 minutes; 12 failures per 15 minutes at the account level cause a global lock; there is also a site-wide sign-in rate limit (**local access is exempt from it**) that protects the service when a flood of sign-in attempts arrives. Devices behind the same NAT or proxy share one egress IP and therefore share the rate-limit counters.
3. When guest mode is on, the Public directory is readable by anyone on the network; Guests can upload new files but cannot overwrite, delete or create directories. Do not put private files in the Public directory.
4. Uploads whose filenames contain path separators, control characters or Windows reserved names are rejected. Script-type files such as HTML and SVG are returned as plain text or downloads when previewed, never executed inline; do not rely on this mechanism to host and share executables or script-bearing content.
5. Concurrency and resources: by default the whole machine allows up to 256 simultaneous connections, at most 20 HTTP connections per device and at most 8 WebSocket connections per device; excess connections are rejected immediately without queueing. With connection reuse in effect, a connection that is **idle while waiting for the next request** is dropped after the "idle connection timeout" (15 seconds by default); while **within a single request**, a stall longer than the "connection idle timeout" (120 seconds by default) also drops it. All of the above can be adjusted on the Admin page and take effect immediately after saving.
6. The downloader allows only public-network targets by default; loopback, intranet and cloud metadata addresses are blocked.
7. The minimum password length of 8 applies only to newly set and changed passwords; existing weak passwords are not force-upgraded.
8. Logs are basic runtime logs (can be disabled) and do not provide full auditing; passwords and session tokens are never logged.
9. Data is stored as files and JSON without a database. Concurrent writes are protected by atomic writes; regular backups of config and shared_files are still recommended for extreme scenarios.

## 6. Forgot the Admin Password

- Regular change: after signing in, go to User Management from the Admin page, choose the account and change its password (after the change, the account's old sessions are signed out).
- Forgot the password: **rely on the server-side local token sign-in**. Restart the service on the local machine and open the one-time local token link shown in the startup message; you are signed in as the default Super Admin and can then change the password in User Management. No files need to be deleted. The token is regenerated on every startup and is local-only.

## 7. Development Notes

- **Architecture and mechanism notes are in `ARCHITECTURE.md`** (English: `ARCHITECTURE.en.md`): layering and dependency direction, request flow, and the key mechanisms of the auth / permission / files / sharing / config / downloader domains, including why they are designed that way.
- Main entry `leaffs/leaffs.py` (a compatibility shim pointing to the startup assembly in `leaffs/app.py`); server code is organized by domain: `auth` (accounts & sessions), `files`, `share` (sharing), `config`, `dl` (downloader), `web` (page rendering), `server` (HTTP/WebSocket/push/TLS/host utilities), `utils` (common utilities).
- Data directory rule: equals the project root when running from source, and the main program directory when running the packaged build.
- Main config keys: `http_port`, `ws_port`, `tls_trust_port`, `tls_enabled`, `tls_cert`, `tls_key`, `trust_bind_host`, `guest_mode`, `guest_public_write`, `downloader_guest_allowed`, `max_total_conns` (default 256), `max_conn_per_ip` (default 20), `ws_max_conn_per_ip` (default 8), `io_idle_timeout_secs` (default 120), `keepalive_timeout` (default 15), `zip_max_files`, `zip_streaming` (default true), `pbkdf2_iterations`, `salt_length`, `access_log`.
- Account password hash format: iterations, Base64 salt and Base64 digest, separated by `$`.
- Bundled external programs: `openssl.exe`, `aria2c.exe`, `ffmpeg.exe`; their purpose and licenses are described in Section 8 and the third-party notices file.
- API requests carrying an invalid session cookie uniformly return 401 before routing; anonymous paths such as `/api/qrlogin` are on the exemption list.
- All page text can be selected and copied; the QR code color changes with the light/dark theme.
- Multilingual implementation: the Chinese and English versions of each page live in the same directory, and English files end with `.en.html`. Language is resolved in this order: the request `leaf_lang` cookie wins; without a cookie, the language preference saved on the server for the signed-in account (in users.json) is used, so incognito sessions such as the built-in window restore their language after a restart. The language selector is on the My Account page; switching writes the cookie, saves the account preference, and refreshes the page. English is AI-translated, and terminology consistency is ensured by `TRANSLATION_GLOSSARY.md`.
- Comment conventions: code comments are uniformly **bilingual Chinese and English** (one Chinese line plus an adjacent English line, or both languages in a single line); only informative comments are kept, and duplicated, outdated or meaningless comments are removed; English parts may be marked as AI-translated. The whole repository is being cleaned up in batches under this convention.
- AI-assisted development: this project is heavily AI-assisted, mainly using DeepSeek V4 Flash; see `THIRD_PARTY_NOTICES.md`.

## 8. Third-Party Components and Licenses

The third-party components shipped with or depended on by this project, together with their purpose, licenses, upstream sources and redistribution practices, are described in the separate file `THIRD_PARTY_NOTICES.md`; distribute it along with any release artifacts. All listed components are used as-is and unmodified. If you find a third-party component missing from the notices or have any licensing questions, contact the author (ewq2526@163.com).

## 9. Disclaimer

This software is a heavily AI-assisted development project (main model DeepSeek V4 Flash). The author makes no warranty regarding the correctness, security or suitability of the software in any deployment environment, and accepts no liability for direct or indirect loss caused by its use. Code generated or co-written by AI may contain errors or security flaws; review it yourself and verify it in a real environment before use. For production environments or important data, back up and evaluate first. Where not covered here, actual runtime behavior prevails.
