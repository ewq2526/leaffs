# LeafFS (English)

> English version. AI-translated from the Chinese original.

A LAN file server written in Python — usable from any browser, no client install. Beyond browsing, previewing, uploading and downloading files it provides account and guest access control, sharing, URL download jobs, HTTPS, and a read-only device panel (status and Wi-Fi).

For users and for people working on the code. Architecture and internals: `ARCHITECTURE.en.md`.

## Features

- **Files**: shared public folder plus a private folder per account; browse, upload, download, zip a folder, preview images/text/video/audio/PDF in place.
- **Sharing**: publish a file or folder as a public share, also reachable via `/p/<owner>`; optional access code; references, never copies.
- **Access control**: user / admin / super admin; optional guest mode; quotas and rate limits; login throttling and account lockout.
- **Download jobs**: HTTP/HTTPS URLs, magnet links, torrents, m3u8 — the server fetches and files them; public targets only.
- **System page** `/system`: host, CPU, memory, temperature, battery, per-disk capacity plus read/write and busy time, service states, Wi-Fi scanning and connecting. **Super admin only** — plain admins manage accounts and shares, not device configuration.
- **UI**: Chinese and English; light/dark themes with six accent colours, saved per account; all text selectable.
- **Kiosk mode**: with `LEAFFS_KIOSK=1` the window is fullscreen and frameless and the page is scaled to 125% (`LEAFFS_ZOOM` overrides), for a device wired to a display.

## Running

- Requires Python 3.13.
- Start with `python -m leaffs` from the project root. When run from source the data directory is the project root; in a packaged build it is the program directory (`config/`, `shared_files/`, `.cache/`).
- Ports: HTTP 8080, WebSocket 8081, certificate notice page 8082 (HTTPS only).
- First entry: the **one-time local token** link printed at startup (`?leaf=`) signs you in as super admin, local machine only, single use. The super admin starts with **no password** — set one immediately, and never share that link.
- External programs: on Linux the system `ffmpeg`, `aria2c` and `openssl` from `PATH`; the Windows build ships `*.exe` files that are not in the source tree. Whatever is missing only disables the matching feature (certificate generation / downloader / thumbnails).
- The system page needs root to run `systemctl` and `wpa_cli`.

## Configuration

| File | Contents |
|---|---|
| `config/server_config.json` | main runtime configuration |
| `config/users.json` | accounts and password hashes (PBKDF2-HMAC-SHA256) — do not edit by hand |
| `config/downloader_config.json` | downloader settings |

Admin entry points: `/admin` (guest mode, quotas, limits, connection caps, logs), `/admin/advanced` (ports, TLS, cache, upload and preview limits, ZIP mode), `/admin/deep` (hash parameters, thumbnails, session lifetime, access log), `/me` (own info, devices, password, language, theme), `/system` (device panel, super admin).

## HTTPS

No root certificate is installed into the system trust store. Certificate sources in priority order: explicit `tls_cert`/`tls_key` → an existing `config/selfsigned.crt` with `selfsigned.key` → generated self-signed on first TLS start. Browsers will warn; that is expected. Port 8082 explains it in plain words and never redirects on its own.

## Known limitations

1. Built for a **trusted LAN**. Exposing it to the internet needs a real certificate, guest mode off and strong passwords; there is no WAF, captcha or 2FA.
2. Defaults: 256 connections overall, 20 HTTP and 8 WebSocket per device; excess is refused, never queued. Idle connections drop after 15 s, a stalled request after 120 s. All adjustable in the admin page.
3. In guest mode the public folder is readable by anyone on the network; guests may upload but not overwrite, delete or create folders.
4. File names containing path separators, control characters or Windows reserved names are rejected; HTML/SVG is served as text or as a download, never inlined.
5. Data lives in files and JSON, no database. Writes are atomic per file, but back up `config/` and `shared_files/` regularly anyway.
6. Changing Wi-Fi briefly drops the current network; if the new one fails to connect, the previous network is restored.
7. Logging is basic runtime logging, not auditing; passwords and session tokens are never logged.

## Forgot the admin password

Change it in the admin page while signed in. If it is lost, restart the service on the machine itself and use the one-time local token link to enter as super admin, then set a new one — no files need deleting.

## Development

- Entry point `leaffs/app.py` (`leaffs/leaffs.py` is a compatibility shim). Code is split by domain: `auth`, `files`, `share`, `config`, `dl`, `web`, `server`, `system`, `utils`.
- `system/` is read-only collection plus Wi-Fi control: `host.py` reads `/proc`, `/sys` and `systemctl`, `wifi.py` drives `wpa_cli`, `api.py` assembles responses. Authorisation is decided once in the dispatch layer of `server/handler.py`, not re-checked in every handler.
- Every page exists twice, the English file ending in `.en.html`; language resolution is cookie `leaf_lang` then the account preference. English is AI-translated; terminology in `TRANSLATION_GLOSSARY.md`.
- Comments are bilingual (Chinese + English) and kept only where they carry information.
- This project is heavily AI-assisted (primary model: DeepSeek V4 Flash).

## Third-party components and disclaimer

Bundled and required third-party components, their licences and redistribution requirements are listed in `../for-distribution/THIRD_PARTY_NOTICES.en.md`; ship that file with any build. All components are used unmodified.

The software is provided as is. The author makes no warranty about its correctness, security or fitness in any particular deployment and accepts no liability for direct or indirect damage from its use. AI-assisted code may contain defects — back up important data and verify in your own environment.
