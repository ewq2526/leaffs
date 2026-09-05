# LeafFS Distribution README (English)

> English version. AI-translated from the Chinese original.

LeafFS is a file sharing/download server that runs on one computer within a LAN. Phones and computers on the same network can open the server address in a browser to browse, preview (Gallery), upload and download files, without installing a client.

This guide is for end users of the distribution package (executable file or packaged directory). Development and licensing details are in `README.md` and `THIRD_PARTY_NOTICES.md` in the project.

## 1. Quick Start

1. Start the program (double-click the launcher, or run `python -m leaffs` as instructed).
2. Find the local access address in the program startup message and open it in a browser.
3. First sign-in on the server: open the one-time local token link in the startup message; it signs you in automatically as the default Super Admin (the token is one-time and local-only). Use it to enter the admin interface and change the default password promptly.
4. The default account for the first sign-in is `admin` with password `admin`. After signing in, change the password immediately where the page prompts you.
5. Other devices on the same network can use the service by opening `http(s)://<LAN-IP>:<port>` in a browser.

## 2. Default Ports

- Main site: 8080. When HTTPS is enabled, access it over https.
- Certificate notice page: 8082 (provided only when HTTPS is enabled).

Ports can be changed in Advanced Settings on the Admin page.

## 3. Common Entries

- Browse: view and operate on files.
- Gallery: browse images, videos and more by category.
- Downloader: submit download tasks to the server (HTTP, magnet, torrent, m3u8).
- My Account: current account information and Sign Out; the interface language (Chinese / English) is chosen here. The language is remembered by the browser; the English interface is AI-translated and may differ from the Chinese.
- Admin: guest mode, users and quotas, connection limits, logs and other settings.

## 4. Guest Mode

- Off by default. When enabled, anyone can access the Public directory without signing in.
- Guests can download and upload new files, but cannot overwrite or delete files or create directories.
- Do not put private files in the Public directory.

## 5. Mobile Access and Certificate Warnings

- When HTTPS is enabled with the automatically generated self-signed certificate, mobile browsers show an insecure warning, which is normal.
- After confirming that the address is your local server, choose to continue as prompted.
- The certificate notice page on port 8082 explains this phenomenon; devices that are signed in are redirected back to the main site when visiting that page.

## 6. FAQ

- Port already in use: change the port in Advanced Settings on the Admin page, then restart.
- Forgot the Admin password: restart the service on this machine and use the one-time local token link in the startup message to sign in as Super Admin (a new local token is generated on every startup), then change the password in User Management on the Admin page; no files need to be deleted. If already signed in, you can also change it directly in User Management. Password changes are done inside the web interface; no standalone script is provided.
- Data backup: back up the `config` and `shared_files` directories.
- All page text can be selected and copied; the interface has light and dark themes.

## 7. Security Notes

- This software is meant for trusted LANs; do not expose it directly to the public Internet.
- Change the default Admin password as soon as possible.
- Connection and concurrency limits can be adjusted on the Admin page; overly large values may slow down this machine.

## 8. Notices

This project is heavily AI-assisted (mainly using DeepSeek V4 Flash). Third-party components, licenses and redistribution notes are in `THIRD_PARTY_NOTICES.md`. The author makes no warranty regarding the correctness, security or suitability of the software and accepts no liability for losses caused by its use; evaluate it yourself within a trusted network before using it.

Author: ewq2526 (contact email ewq2526@163.com). Copyright (c) 2026 ewq2526, licensed under `LICENSE.txt`.
