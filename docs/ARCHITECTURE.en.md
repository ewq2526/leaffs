# LeafFS Technical Documentation

Author: ewq2526.

This document is for **people taking over or extending the LeafFS code base**: it explains what each
layer does, why the key mechanisms are designed the way they are, and where to touch things when you
change them. For usage and deployment see `../README.md`, for version history see `../CHANGELOG.md`, and for
Android build details see `../android/README.md`.

> Convention: this document describes the behaviour of the **current code**. Wherever a design reason
> cannot be read off the code, it is written down in the corresponding section — most of those reasons
> were paid for with a real bug. Read them before deleting anything.

---

## 1. Overview

### 1.1 What it is

A LAN file sharing / download server: deploy it on one machine in a local network, and other devices on
that network can browse, preview, upload and download files through a browser — **no client to install**.

- Backend: Python 3.12, standard library `http.server` (`ThreadingHTTPServer`) plus `websockets`,
  **no web framework**.
- Frontend: static HTML/CSS/JS, with separate Chinese and English page files, **no build step**.
- Desktop form: a pywebview embedded window (WebView2 on Windows).
- Android form: the whole Python server is packed into an APK by Chaquopy, with GeckoView as the container.

### 1.2 Three ways to run

One `leaffs/` package serves all three forms; **there is only one copy of the source**.

| Form | Entry | Shell | Data directory |
|---|---|---|---|
| From source (development) | `python -m leaffs` | Embedded window, falling back to the system browser | Project root |
| Packaged (distribution) | Executable | Embedded window, falling back to the system browser | Directory of the main program |
| Android | APK | GeckoView container | The app's private directory |

The differences are concentrated in **entry assembly** and **path resolution**; the business code is
shared entirely. Android does not reuse the desktop startup function (it has to avoid the embedded
window, the certificate notice page and other desktop-only steps) and ships its own orchestration entry.

### 1.3 Ports

| Port | Purpose | Protocol |
|---|---|---|
| 8080 | Main site (pages and API) | HTTP, or HTTPS when TLS is enabled |
| 8081 | WebSocket (file listings, download progress, push) | ws, or wss when TLS is enabled |
| 8082 | Certificate notice page | Plain HTTP, **only provided when TLS is enabled** |

All ports are configurable. **The certificate notice page must stay plain HTTP**: its entire reason to
exist is to be reachable when the certificate is not trusted yet.

### 1.4 Process and thread model

The server is **multi-threaded**, not an async framework:

- **HTTP**: one thread per connection. With HTTP/1.1 keep-alive, one connection serves several requests
  in sequence. There are admission limits (whole-server and per-IP); over the limit the connection is
  **rejected outright, never queued**.
- **WebSocket**: its own port and its own asyncio event loop, running in its own thread; push shares
  that loop.
- **Admin push**: a background thread emits data to subscribers on a fixed cadence, and **builds
  nothing when there are no subscribers**.
- **Watchdog**: a background thread that detects stalled requests and writes all thread stacks to a
  file for evidence.
- **Downloader**: `aria2c` is a **separate child process** driven over RPC, with a supervisor that
  restarts it when it dies.
- **Android**: adds a foreground service (to stay alive) and the GeckoView container; the Python side
  is the same stack as above.

### 1.5 Startup assembly order

`start_server()` in `leaffs/app.py` is the **only assembly point**, and the order matters:

1. Logging comes up → read config → read accounts → **read the session table back** → start session
   cleanup;
2. Clean up orphaned thumbnails + **clean up the upload temp directory**;
3. Create the shared directory and log one startup line (shared directory, ffmpeg, aria2c availability);
4. Generate the **local one-time login token** (overwriting the previous one);
5. If TLS is enabled: build the TLS context first, and **refuse to start if that fails**;
6. Start the certificate notice page (TLS only) → HTTP → WebSocket → admin push → watchdog;
7. Start `aria2c` and its supervisor in the background;
8. Print the startup banner (including the local token link);
9. Open the desktop embedded window; with no desktop, fall back to the system browser and keep the main
   thread alive.

A few things **must not be reordered**:

- **The session table must be read back before HTTP starts**, otherwise the first few requests see no
  existing sessions — that is exactly how "a restart signs everyone out" happened.
- **The TLS context must be built before HTTP starts, and failure must not silently fall back to plain
  HTTP.** This is a deliberate security policy: rather than start, it refuses to serve in the clear
  while the user believes HTTPS is on.
- **The upload temp directory is cleaned only at startup.** Cleaning it at runtime would delete files
  that are being uploaded right now, and at startup there are provably none in flight.

---

## 2. Module map

`leaffs/` is split by **domain**; one package does one thing. Dependencies generally flow one way:
`server` and `web` depend on the domain packages, domain packages avoid depending on each other, and
`paths` / `runtime_log` / `utils` sit at the bottom.

| Package / file | Responsibility | Key files |
|---|---|---|
| `app.py` | **Startup assembly**: read data, start the threads, open the window | — |
| `leaffs.py`, `__main__.py` | Entry shims pointing at `app.py` | — |
| `server/` | **Transport**: HTTP, WebSocket, TLS, push, certificate notice page, host utilities | `handler.py` (HTTP handler and the unified exit), `ws.py`, `tls.py`, `push.py`, `cert_remind.py`, `hosts.py` |
| `auth/` | **Accounts and sessions**: login, rate limiting, tokens, user management, self-service API | `core.py`, `login_api.py`, `local_token.py`, `users_api.py`, `self_api.py` |
| `files/` | **Files**: upload/download, listings, search, stats, thumbnails, paths and quota | `core.py`, `api.py` |
| `share/` | **Sharing**: virtual share directory mappings, access-code gate | `mappings.py`, `access.py` |
| `config/` | **Configuration**: read/write, validation, the three configuration tiers | `core.py`, `api.py` |
| `dl/` | **Downloader**: task model, aria2c RPC and supervision | `manager.py`, `dl_core.py`, `dl_api.py`, `dl_rpc.py`, … |
| `web/` | **Page rendering**: static assets, page selection, theme injection, content policy | `render.py` |
| `web_page/` | **Page files** (assets, not code): `*.html` and `*.en.html` | — |
| `utils/` | Shared utilities (including the project-wide cookie parsing convention) | `core.py`, `log.py` |
| Bottom-level modules | `paths.py` (path resolution), `runtime_log.py` (logging), `watchdog.py` (stall evidence), `ui_theme.py` (theme colours) | — |

### 2.1 Single-implementation choke points

When changing something, **prefer these places; do not write a second copy elsewhere**:

| Choke point | Location | What it governs |
|---|---|---|
| Unified response exit | `send_json` / `send_error` / `redirect` / `end_headers` in `server/handler.py` | Status codes, body delimitation, security headers, cache headers, page content policy |
| Directory listing | `build_listing()` in `files/api.py` | The **only** listing algorithm, shared by HTTP and WebSocket (it computes, it does not send) |
| Temp-content predicate | `is_upload_tmp_entry()` / `is_upload_tmp_relpath()` in `paths.py` | Upload temp content is invisible to every read path |
| Share unlock decision | `code_gate()` in `share/access.py` | "Who counts as unlocked" |
| Cookie parsing | `parse_cookies()` in `utils/core.py` | Cookie value convention across the site |
| Theme implementation | `web_page/common/theme.js` | Theme colours across the site |
| Embedded-window outbound guard | `_install_webview_guard()` in `app.py` | The desktop build must not reach outside addresses |

⚠️ These choke points were **paid for**, not designed out of tidiness:

- Directory listings used to be built **twice** (HTTP and WebSocket), which produced a real defect:
  the public shares folder vanished from the live channel;
- The temp-content check used to be **spread across seven read paths**; missing one exposed it;
- Response headers used to be emitted **by each handler**; some responses ended up with no cache header
  and others with no security headers.

**When you add an exit or a read path, call the existing functions. A second copy will miss something.**

---

## 3. HTTP request handling

### 3.1 Dispatch

The HTTP handler is a `BaseHTTPRequestHandler` subclass; `do_GET` / `do_POST` dispatch by path to a set
of handlers. Two conventions run through the whole project:

- **Page handlers enforce permissions themselves**, API handlers only forward. The two shapes within a
  domain are different — do not copy one onto the other.
- **Contract-level checks belong at the outermost layer, business validity further down.** A real
  counter-example: putting "missing parameter → 400" in the business layer meant the outer layer
  rejected the request first, and that carefully written 400 could never be reached.

### 3.2 The unified exit

Every HTTP response goes through the unified exit, which is responsible for four things:

| Exit | Responsibility |
|---|---|
| `send_json` | JSON responses (status code + body delimitation) |
| `send_error` | Error responses (uniform rejection convention) |
| `redirect` | Redirects (also needing body delimitation) |
| `end_headers` (overridden) | **Backstop**: body delimitation, security headers, cache headers, page content policy, `Connection` header |

A few rules:

- **Body delimitation**: under HTTP/1.1 every response must be self-delimiting (`Content-Length` or
  chunked), otherwise the connection cannot be safely reused. The unified exit checks and warns when it
  is missing — that check is what makes keep-alive safe to enable.
- **Caching is off by default**: a response that never declared a cache policy is backfilled with
  `no-store`. The rule is "**not cached by default; caching must be declared explicitly**", with exactly
  two exceptions: static assets and thumbnails.
- **Security headers** are added in one batch by the unified exit, with an idempotence marker so hand-written
  responses do not emit them twice.
- **Page content policy** is emitted only for HTML pages, not for static assets or JSON (see chapter 12).

⚠️ **Count how many exits there are** before changing "all responses". The main HTTP service goes through
the four functions above, but:

- **the WebSocket handshake does not** — it builds its HTTP response directly in `ws.py`;
- **the certificate notice page does not** — `cert_remind.py` has its own handler class.

Go through all three when you change shared response behaviour. Missing the WS handshake is a mistake
that has already been made once.

### 3.3 Connection reuse (keep-alive)

The server declares HTTP/1.1 and supports connection reuse, so **one connection serves several requests
in sequence**. That creates four things you must get right:

| Concern | What to do | What happens otherwise |
|---|---|---|
| **Idle reclamation** | While idle, use a short **keep-alive timeout** (default 15 s, configurable), not the read timeout | Idle connections held open by browsers keep occupying server threads |
| **Per-request budget** | Reset timing and timeout at the start of every request | The longer a connection lives, the less budget later requests get, and they fail mysteriously |
| **Draining** | Anything left unread from the previous request **must be drained** before the connection may be reused | Leftover bytes are parsed as the start of the next request — the two requests get mixed up |
| **Old clients** | Treat HTTP/1.0 clients as "use once and close" | Old clients wait forever for behaviour they expect |

In addition, **a chunked request body is rejected explicitly** rather than silently treated as "no body" —
silently dropping it makes uploaded content vanish.

### 3.4 Admission control

Connections have two limits: **whole-server** and **per-IP**. Both are decided when the connection is
established, and **over the limit it is rejected outright, never queued** — queueing would let an attacker
exhaust server resources with a small number of connections.

Reads and writes both have timeout backstops: a single read has a read timeout, and a connection that
makes no progress for a long time is closed.

---

## 4. WebSocket and push

### 4.1 What it carries

WebSocket runs on **its own port** with its own event loop, and carries three kinds of work: live file
listings and change notifications, download progress, and live data for the admin page.

⚠️ So **"read the file listing once" has two paths** (HTTP and WebSocket). That is exactly where real
defects have appeared — see 4.3.

### 4.2 Handshake and messages

- **Origin validation happens before the handshake**: with an illegitimate origin (Origin / Host), the
  connection **is never established**. Validating after the handshake means "accept first, judge later",
  and that window is long enough to do damage.
- **Messages have a shape contract**: the shape is validated (strings, lists of strings, …) before
  entering business branches. Without it the failure mode is not an error but **silent confusion** —
  for example a string arriving where a list of paths was expected gets iterated character by character.
- **Malformed messages get an explicit reply and the connection stays open**: frame-level or
  envelope-level problems get a format-error message, and the connection is **not** closed according to
  the WebSocket protocol. The trade-off is deliberate: a user seeing "the page suddenly can't connect"
  is far worse than seeing one error message.
- **Subscription messages not being acknowledged is by design**: subscribing only registers interest;
  a push follows when something happens. No acknowledgement does not mean timeout.

### 4.3 There is only one listing exit

The directory listing has two read paths (HTTP and WebSocket), but **only one construction algorithm**
(the choke point from chapter 2):

- It **computes, it does not send**, returning a structured result that each transport layer delivers
  in its own way;
- Its fixed order is: normalise path → reject temp content → check permission → scan → inject virtual
  share entries.

⚠️ This is a hard constraint, because two copies produced two real defects:

- The HTTP path merged the public shares folder and the WebSocket path did not — the symptom was
  **the public shares folder disappearing from the listing after any file operation**;
- The WebSocket path missed the temp-upload check — the symptom was **being able to list in-progress
  upload files by name**.

The first one took a long time to diagnose, because **the frontend prefers WebSocket** and only falls
back to HTTP after certain operations, so it looked like "sometimes it is right" and was easily
misdiagnosed as a frontend caching problem.

### 4.4 Push

Push shares the WebSocket event loop and is organised by topic: live admin data, download status changes,
QR sign-in events, and gallery change notifications.

One design constraint: **nothing is built when nobody is subscribed**. The admin push runs on a fixed
cadence, but most of the time nobody is looking at the admin page, and "compute it, then discover nobody
wants it" is pure waste.

---

## 5. Authentication and sessions

### 5.1 Roles

Three roles: **user / admin / super admin**. The role decides two things: which paths are reachable, and
which configuration may be changed.

Super admins have two special properties, both introduced after real bugs:

- **They may exist without a password**: that is the state on first start, and the only way in is the
  local one-time token;
- **They are identified by role, not by username**: after a super admin renames themselves, or `admin`
  is deleted and a new super admin is created, they must still be recognised as super admin. Code that
  checked "is the username `admin`" once made the default-password reminder **never appear again**.

### 5.2 Passwords

Stored as a slow hash (PBKDF2-HMAC-SHA256) in the format "iterations, salt, digest"; iterations and salt
length are configurable. Changing those two parameters **only affects newly set or changed passwords** —
existing ones do not break.

**The default-password reminder is decided by role**: if any super admin still has no password, the page
says so.

### 5.3 Sessions

- Sessions live **both in memory and on disk**: memory for fast lookup, disk so that sign-in state
  survives a restart. ⚠️ The session file is **equivalent to credentials** — deleting it signs everyone
  out, and it must be treated as sensitive when backing up.
- Expiry is **absolute; there is no idle timeout**. That is a deliberate trade-off: this is positioned as
  a self-hosted personal service, where frequent re-login costs more than it gains. To revoke access, use
  the mechanisms below.
- **Revocation must take effect immediately and must not come back after a restart.** Sign-out, kicking a
  device, changing a password and deleting an account all have to actually remove the session record, not
  just mark it in memory — this is the extra requirement that appears once sessions are persisted.
- Cookie attributes match the nature of the service: not readable from page scripts, and cross-site
  requests are restricted.

### 5.4 Login rate limiting

Login has several layers of rate limiting, and **each layer keys on something different** — that is the
point:

- Keying only on the source IP is bypassed by "change IP and the counter resets";
- Keying only on the account is bypassed by "change the name and the counter resets";
- So both are needed, plus a **cross-account global layer** to catch distributed spraying.

Thresholds and windows are constants, and changing them means checking the conventions pinned by tests.
When a limit trips, the response must **state the reason** rather than returning a generic "wrong username
or password" — the latter makes a legitimate locked-out user think they misremembered their password.

⚠️ Known trade-off: **while locked out, a correct password is rejected too.** That is the inherent cost
of brute-force protection, not a defect.

### 5.5 Local one-time token

Used for getting into the admin UI the first time, and for a forgotten password: the service generates a
new token at every start, surfaces it through the startup banner link, and it works **only from the
loopback address and only once**.

- The token **has no query interface** (by design: being able to look it up means being able to steal it);
- A candidate is **shape-checked before it reaches the comparison**, so malformed input is treated as "no
  match" and never enters the comparison logic;
- The token file is rewritten at every start and deleted once used.

### 5.6 QR sign-in

One side shows a QR code and another side confirms, completing sign-in. The QR session has a short
lifetime and enough randomness, and its state is only "waiting / confirmed / expired" — it **contains no
account information**.

The polling endpoint is unauthenticated, but its input space cannot be enumerated, so it is not a probe
for "does this session exist".

---

## 6. Permission model

### 6.1 One decision entry point

The path → permission decision lives in a single function (`check_path_permission_core` in `files/core.py`)
and is **shared by HTTP and WebSocket**. The rules in short:

| Role | May reach |
|---|---|
| Super admin / admin | Everything |
| User | Their own directory + the public directory |
| Guest | The public directory (and guest mode must be on) |

"The share area under the public directory" is the single exception; see 6.3.

Two accompanying conventions:

- **An exception in the decision means denial.** The implementation must log it before denying — an
  exception must be neither read as "has permission" nor silently treated as "no permission" (otherwise
  the problem can never be diagnosed).
- **There is a second line of defence**: every assembled absolute path is checked again for "actually
  inside the shared root". The first decision answers "should this person see it"; the second answers
  "can this path escape". Neither substitutes for the other.

### 6.2 Path handling

A relative path is normalised before the decision. Normalisation has three **musts**, each corresponding
to a real defect:

- **`..` must be checked before `normpath`** — `normpath` resolves it away first, so checking afterwards
  finds nothing;
- **Drive letters must be blocked** — `normpath` does not handle them, and `join(shared_root, 'C:/x')` on
  Windows **returns `C:/x` directly**, escaping the shared root;
- **An empty path must be rejected explicitly** — after normalisation an empty string is "valid", and
  `join(shared_root, '')` **is the shared root itself**.

That last one caused the most severe defect so far: when the delete endpoint received an empty path, the
target was "the path itself", so **the entire shared root was recursively deleted and success was
returned**. Endpoints of the delete family — those that act on the target path itself — must reject empty
values explicitly. Upload and mkdir treat an empty path differently (parent directory = shared root) and
have role restrictions, so their nature is different.

Single file and directory names have their own sanitisation rules (rejecting separators, control
characters, Windows reserved device names, over-length names, and so on). When uploading a folder the
browser sends a multi-segment relative path; every segment goes through the same rules and the total
number of segments is capped.

### 6.3 The public-share reserved area

`public/shares` is a **virtual area**: nothing physical is stored on disk, only mappings are registered.
It must be defended, because any role that can write to the public directory can create a **physical**
directory there, and since "listings prefer disk entries, downloads use disk paths", files inside that
physical directory **bypass the access code**.

Hence three lines of defence: the permission decision rejects the area for everyone except admins
(admins are let through deliberately, as a cleanup channel), listing merges strip same-named physical
entries, and registering a share forbids pointing its source at that area.

### 6.4 Rejection convention

Identity and permission rejections use **one status code**, without distinguishing "not signed in" from
"not permitted". The reason is direct: distinguishing them is a probe for "does this resource exist /
are you allowed".

Page-type requests have their own convention (not signed in → redirect to sign-in; not permitted →
reject).

⚠️ When changing the convention for "all responses", remember there are three response exits (section 3.2) —
do not change only the HTTP one.

---

## 7. Files and uploads

### 7.1 Read and write paths

**Reads**: listings, downloads (with resume support), raw content / preview, thumbnails, packaged
downloads, search, statistics.

**Writes**: upload, delete, create directory.

Both families must go through the permission decision, and both must stay inside the single listing exit
(section 4.3) and the single temp-content predicate (section 7.4).

### 7.2 Upload

Uploads are streamed; there is **no "receive into memory, then write to disk" step**:

1. Validate the request type and the **client-declared size** (pre-check only; **nothing is accounted for
   yet**);
2. Check role, path and write permission; do a fast quota pre-check using the declared value;
3. Parse multipart while streaming, writing into a **temporary file** as it goes;
4. Once complete, **atomically move it into place** (same-volume rename); the file only exists from the
   moment the rename succeeds.

Key points:

- **Temporary files live in a dedicated directory under the shared root**, named "thread id + nanosecond
  timestamp" so they cannot collide;
- **The interruption criterion is "did we read the terminating boundary"**, not "did the connection close
  cleanly" — a well-formed body always ends with the boundary. Choosing the wrong criterion means half a
  file is moved into place as if complete, and success is reported;
- **Any exception deletes the temporary file**; nothing half-written is left behind;
- **Name collisions are handled per role**: guests **never overwrite** (they get an automatically
  uniquified name), everyone else keeps the atomic-overwrite semantics.

### 7.3 Quota accounting

The quota is accounted by **bytes actually read**, not by the length the request declares. This was
changed after being exploited:

> It used to reserve based on the declared length, so "declare a large value, never send the body" could
> **virtually fill the quota** for as long as the request stayed alive, during which everyone else's
> uploads were rejected. Now, if the data never actually arrives, not a single byte is reserved.

Three accompanying rules:

- Accounting hangs off the **single data read point**, so "skipped or rejected parts" count too — they
  really were read;
- A reservation is meaningful **only for the duration of the request** (it guards against overselling
  during the window between the quota check and the write); it must be settled when the request ends;
- **The settlement order must not be reversed**: first invalidate the directory-size statistics, then
  settle the reservation. Reversed, the same bytes get counted twice.

⚠️ When reading configuration fails, it must **never degrade to "unlimited"**. Reading "configuration
error" as "the user asked for no limit" is the classic trap in this kind of code, and this project
explicitly chooses "let the exception abort this operation" at several limits.

### 7.4 Visibility of temporary content

In-progress upload content must be invisible to every read path. The predicate exists **only once**
(chapter 2's choke point), but there are **about ten call sites**: listings (the scan layer and the
name-based entry check are two separate places), search, download, raw content, thumbnails, and packaged
downloads (base directory, individual entry, directory recursion).

⚠️ Two easy mistakes:

- **"Not listing it" does not stop "walking into it"** — skipping it in the listing layer is not enough;
  the by-name access path must be blocked separately;
- **The block returns 404, not 403**. The meaning is "no such resource", unrelated to permission;
  returning 403 admits that something was there.

### 7.5 Packaged downloads

Two modes, with the trade-off written next to the configuration key:

| Mode | How | Cost |
|---|---|---|
| **Full streaming** (default) | Compress while sending; the output stream is **deliberately non-seekable**, which makes the zip library use its streaming form | **No total length**, so clients show no total size or progress |
| Package first, then send | Build the archive first into **memory or a temporary file** (small archives stay in memory, larger ones spill to disk), then send | Large archives consume temporary disk (and may fill it), and the first byte is later |

The streaming implementation has one **correctness** requirement: once the body is written it must be
explicitly finalised and flushed, otherwise the client keeps waiting for the next chunk. This is not a
performance optimisation.

Limits are enforced in several places: URL length, parameter length, entry count. **When nothing could be
packaged at all, the answer is 404**, not a 200 with an empty archive — that would be a false success;
and at that point "not permitted" and "does not exist" are **deliberately not distinguished**, because
distinguishing them is a permission probe.

---

## 8. Sharing

### 8.1 Model: registering references, not copying files

Sharing is **not copying**: once a file is shared, no copy and no link appears on disk — only a **mapping
is registered**, saying "this virtual path points at that source file". Three consequences follow:

- If the source file is deleted or moved, the share simply stops resolving; no extra cleanup is needed;
- Sharing takes no extra space and does not change the quota accounting;
- The mapping table is persisted, and has **both a total and a per-user cap** (to prevent runaway growth).

The virtual share directory is fixed at "the share area under the public directory"
(`public/shares/<username>/`), and it is **produced only by the mapping table** — nothing physical is
stored on disk. That is the premise for every defence in the next section.

Publishing performs several checks: the source path must be inside the shared root, must already be a
file, and must not be something inside the share area itself. Virtual file names **automatically avoid**
collisions with disk entries, so the "disk + virtual" two-layer view never shows duplicate names.

### 8.2 Merging into listings: one implementation, one unlock decision

The listing choke point (section 4.3) has a companion function that merges virtual entries into the
listing result. It has **exactly one implementation** and **exactly one caller** (inside `build_listing`),
so the HTTP and WebSocket paths see the same view.

It receives an "**is this unlocked**" callback:

- For a sharer whose callback says "not unlocked", **not a single virtual entry is merged** — not even
  the folder itself appears, and the statistics do not count it;
- The reason: **file names, sizes and modification times are information in themselves**; nobody should
  get a free look at the listing;
- At the third level, an unlocked check that fails yields an **empty listing rather than a rejection** —
  a rejection would admit that something was there.

That callback is **implemented only once**, as `code_gate()` in `share/access.py`; `ws.py` does not write
its own copy. ⚠️ This is a hard requirement: the WebSocket listing must also filter by access code, and
not filtering means handing a code-protected share **to anyone who can connect over WS**. Since the WS
side has no HTTP handler, the decision was deliberately built as a handler-free version — otherwise the
two copies would drift apart.

### 8.3 The public-share reserved area

The share area is virtual and nothing physical lives there. This must be defended, because any role able
to write to the public directory could create a **physical** directory there, and since "listings prefer
disk entries, downloads use disk paths", files inside it would bypass the access code.

Hence three lines of defence: the permission decision rejects the area for everyone except admins (admins
are let through deliberately, as a cleanup channel), listing merges strip same-named physical entries,
and registering a share forbids pointing its source at the area.

### 8.4 Access codes

The access-code gate must protect **both** "listing a directory" and "fetching content" — protecting only
the latter would leak file names, sizes and timestamps. The content exits that currently pass through the
gate are: download, raw content / preview, thumbnails, packaged downloads, and the public share page's
data endpoint.

Design points:

- **Hashing uses a slow hash** (the same parameters and format as login passwords), not a single round.
  Access codes have a minimum length of just six characters, so a single-round hash could be cracked
  offline the moment the mapping file leaks — and at that point **the online IP lock and global lock both
  become worthless**;
- Hashes in the old format must still verify, and are **upgraded automatically the first time the correct
  code is entered**. ⚠️ When touching this, **do not delete the old-format branch** — that would
  invalidate every existing access code at once;
- **Writing is only allowed on a successful verification** — a wrong code must never trigger any write,
  otherwise that becomes a write side effect usable for probing;
- The slow hash must not be computed while holding the lock (it is not reentrant). Always "**take the
  value under the lock, compute outside it**"; an upgrade write must re-acquire the lock and re-check that
  nothing changed concurrently.

**Authorisation tickets**: the credential issued after a successful check is an **opaque random value**
(about 192 bits), with the server recording "who" and "expires when".

⚠️ The ticket **must not be "the hash of the code"**: that value can be derived from the code — an
attacker could enumerate candidate codes offline, compute the hash, and write a matching cookie to
download directly, **never going through the code-entry endpoint at all, so the IP lock and global lock
catch nothing**. Likewise, expiry must be **decided by the server**; the browser-side expiry is only there
to clear the cookie conveniently.

### 8.5 Brute-force protection

Two locks with different scopes:

- **Per source IP**: once cumulative wrong codes for the day reach the threshold, that IP is locked;
- **Global**: trips when the number of wrong codes in a sliding window reaches the threshold **and they
  came from at least two different sources**.

⚠️ The "at least two sources" part of the second rule is not optional. Without it, **any anonymous
visitor could lock a share for everyone with five wrong codes**, and repeating that once per window would
extend the lock indefinitely — a ready-made denial of service.

Two cross-day pitfalls are recorded in the comments: **do not clear the global lock timestamp** (it is an
absolute timestamp with its own expiry judgement, and clearing it lets an attacker-triggered lock dissolve
at midnight), and **do not clear the whole user table** (that wipes the access codes themselves, which
presents as "a day later, the access code never works again").

Access codes themselves **are not per-day** and must be loaded back across days; only counters and locks
reset daily.

The mapping table also **reloads based on a file fingerprint** — the same data root may be modified by
something outside this process (another instance, an external backup restore, manual editing). The
"load once and use forever" approach keeps stale values and **never self-heals**, updating only on
restart. The fingerprint **deliberately avoids the inode**: this module writes by "write a temp file, then
atomically replace", so the inode changes on every write and using it would degrade to "reload every
time".

### 8.6 Cascade on user deletion

Only **two places** persist data keyed by username and need clearing: the "shared by" entries in the
mapping table, and all of that user's records in the access-code table (code hash, error counters, both
locks, tickets).

⚠️ Order convention: **archive the home directory first → then purge the share data → finally delete the
account and sessions**. The reasoning is that the purge is irreversible but low-harm (the user just sets a
new code), whereas "account deleted, code still there" makes **a recreated account with the same name
inherit the old code** — the share page still demands a code, and nobody knows it.

---

## 9. Configuration

### 9.1 Three tiers, corresponding to different permissions

| Tier | Page | Who may change it | What it covers |
|---|---|---|---|
| **Basic** | Admin page | Admin / super admin | Guest mode, quotas, rate limits, concurrency |
| **Advanced** | Advanced settings | Admin (a few fields super-admin only) | Ports, TLS, upload and preview limits, caches, packaging mode |
| **Deep** | Deep configuration | Admin (**some fields super-admin only**) | Password hashing parameters, session lifetime, connections and timeouts, thumbnails, log switches, access control |

The real purpose of the tiers is **permission tiering**: a few deep fields can weaken security (password
iterations, session lifetime, the access-log switch, certificate trust settings), so **only a super admin
may change them**, and a normal admin submitting those fields has the **whole request rejected**.
Otherwise "can change the log switch" equals "can erase their own traces".

A few **special constraints that must hold**:

- Disabling TLS requires an explicit confirmation parameter; without it the request is rejected — the step
  between plaintext and encryption should not be a single click;
- Ports must be distinct from each other and must not take the certificate notice page's port;
- The whole-server connection limit may not be set too small (see 9.4).

### 9.2 Files and sensitivity

| File | Contents | Sensitivity |
|---|---|---|
| `server_config.json` | All runtime parameters (plus a backup kept when values are clamped at load) | Normal |
| `users.json` | Accounts, password hashes, roles, quotas, UI language preference | **High** |
| `sessions.json` | The session table (**its contents are credentials**) | **High** |
| `share_access.json` | Access-code hashes, tickets, brute-force counters | **High** |
| `share_mappings.json` | Share mappings | Normal |
| `downloader_config.json` | Downloader-specific configuration | Normal |
| `local_token.txt` | The local one-time token (rewritten at every start, deleted once used) | **High** |
| `selfsigned.crt` / `.key` | The auto-generated server certificate | Private key is sensitive |

⚠️ The "high" entries live in the config directory and must be **treated as credentials when backed up or
copied**: deleting the session file signs everyone out, and leaking the access-code table means access
codes can be cracked offline.

A JSON file in the config directory that is not on this list is almost always a leftover from an older
version — start from that assumption rather than treating it as a new mystery.

### 9.3 Defaults live in exactly one place

Defaults are defined as **module-level constants in the configuration module**, and loading aligns with
them key by key via `get(key, default)`. Same-named constants elsewhere (for example the auth module's
iteration count and session lifetime) are **overwritten** by the configured values once loading finishes.

⇒ **Change a default in the configuration module**; changing the constant elsewhere and expecting it to
take effect does nothing.

### 9.4 Validation: reject, do not silently clamp

The rules at write time are strict, each corresponding to a real defect:

- **Unknown keys reject the whole request** — the old behaviour was "an unknown key enters no branch → nothing
  changes → yet success is returned", leaving the user believing the change was applied;
- **Out-of-range values are rejected, not clamped** — clamping presents as "the user submitted 0 and the
  system quietly stored something else", the same class of defect as the previous one;
- **Boolean keys accept only real booleans**; numeric keys reject booleans and non-finite floats outright;
- **Some keys have semantic floors**, not just ranges: for example, if the whole-server connection limit
  were allowed to be 1, the service would **deadlock itself** — not even the request that changes it back
  could get a connection slot, leaving only editing the file and restarting.

Writing must also be **read-modify-write**: read what is on disk, **overwrite only the keys this module
owns**, and keep everything else as-is. Rebuilding the whole file replaces the user's configuration and
wipes keys this code does not recognise.

The write itself is "temp file + atomic replace", and failure must be **reported as failure to the user** —
returning "success" for something that only took effect in memory is lying.

### 9.5 Clamping at load time must leave a trace

At load time, clearly invalid values are clamped to the boundaries, but **the act of clamping must itself be
recorded**:

- It shares the same "managed keys" list as writing;
- It compares the **raw values on disk**, not the normalised ones — otherwise values like `"false"` or
  `"8082"`, which are "semantically equivalent but wrongly typed", would never be corrected;
- On drift it backs up first, then writes back **only the clamped keys** — never the whole file;
- The whole file is written only when the file does not exist at all (first start).

### 9.6 When changes take effect

**Changing configuration is not the same as changing behaviour** — the most easily overlooked rule:

| Effect timing | Examples |
|---|---|
| **On save** | Password hashing parameters, session lifetime, caches and debounce, the connection limit, guest mode and quotas |
| **On restart** | Ports, the certificate notice page port, the TLS switch |
| **Read on use** | Upload/preview limits and other keys read while handling a request |

⚠️ TLS is "restart plus singleton cache" for two reasons at once: the TLS context is built **only once per
process**, so **replacing the certificate also requires a restart**.

⚠️ The UI text says "some parameters take full effect after a restart", but **which keys those are is not
listed anywhere**. When you touch configuration code, confirm this and update that text — right now it is
the user's only hint.

---

## 10. Downloader

### 10.1 Task types and dispatch

Four types are supported: **HTTP/HTTPS direct links, m3u8 streams, torrent files and magnet links**.
Type detection follows a fixed order (magnet → torrent → m3u8 → direct link), and anything unrecognised is
treated as a direct link; ed2k is explicitly unsupported and raises an error.

Each type has its own implementation, but **only direct links are downloaded by us** — magnets, torrents
and m3u8 are all handed to the external `aria2c` process and driven over RPC.

### 10.2 Data model and states

A task holds: an identifier, the source URL, the save directory, the type, the owning user, progress and
statistics fields (downloaded, total, speed, filename, connections, seeds, peers, upload speed), and
phase information (a magnet first goes through a "fetch metadata" phase before "fetch files").

State transitions: `waiting → downloading → completed / partial / error / paused / cancelled`.

- **"Partial" is terminal** (there is currently no follow-up merge or resume flow), so fragment debris
  must not be left in the temporary directory;
- What is sent to the frontend is a **whitelist of fields**: raw parameters and internal markers are not
  exported;
- Completing a task **also invalidates the directory cache** — otherwise the new file appears neither in
  the statistics nor in the push notification (the push hangs off the cache invalidation hook).

### 10.3 Starting and supervising aria2c

- The executable is taken from the bundled copy first, falling back to `PATH`;
- **A random RPC secret is generated at every start** — other processes on the machine cannot guess it, so
  they cannot drive the RPC either;
- Starting is **idempotent and double-checked** (the background start thread and the first download task may
  arrive at the same time);
- Readiness is decided by **polling a real handshake**, not a fixed sleep, so it does not slow down server
  startup;
- The supervisor checks on a fixed cadence: process exited → restart; process alive but unreachable for too
  long → restart as well;
- The RPC client **rebuilds itself** when it sees the secret change (the secret changes on every restart).

⚠️ Stopping it tries three methods in order (by process tree, by image name, then a direct kill), because
any single one can miss. There are also console-event and exit-hook backstops so that a forcibly killed
server does not leave an orphan process behind.

### 10.4 Restrictions on external targets

This is the part of the downloader that needs the most care — it is **the only feature where the server
itself initiates outbound requests**.

The decision method: **resolve all IPs, judge each one, and reject the whole thing if any fails**
(literal IPs take a fast path and do not touch the network). By default only public addresses are allowed;
**even with "allow private targets" switched on, loopback, link-local (including cloud metadata ranges),
reserved ranges and carrier-grade NAT ranges are still never allowed**.

⚠️ Validating only the first hop is not enough; **every redirect hop must be validated**, so:

| Path | Validation point |
|---|---|
| httpx | A **request hook** — every hop and every final request passes through it before being sent |
| urllib | A **safe redirect handler** instead |
| m3u8 | The entry URL + **the final URL after redirects** + **every segment** that gets assembled |
| Remote torrent | **Controlled prefetch** (with a byte cap, double-checked); the http link is never handed to aria2 to fetch itself |
| Local torrent | Only allowed from our own temporary directory (a whitelist, not a blacklist) |

Other restrictions: trackers are fetched only from a **whitelisted host over https**, with a line cap —
guarding against "configuration edited to point at an internal address and this module fetching it"; the
per-file size limit is checked in **three places** (before starting, on the response header, and on
**cumulative bytes actually streamed**), because trusting `Content-Length` alone is defeated by a server
that lies or omits it.

Both the target address and the paths recorded inside a torrent are validated: if a torrent's internal file
paths contain absolute paths, `..` or drive letters, it is **rejected outright**.

### 10.5 Access control and quotas

- **Only admins may read or write the configuration.** It is not just "concurrency / speed limit": it also
  contains **policy information** such as "may private targets be downloaded" and "is DHT exposed to the
  public". The frontend was fixed in the same place — a non-admin's downloader page **does not show the
  settings area and does not fetch the configuration**;
- **Task ownership**: admins see everything, everyone else sees only their own; guests are isolated **by
  source IP**, so guests behind different IPs cannot see each other's tasks;
- **Three caps**: pool total, tasks per user, and simultaneous downloads per user (0 or a negative value
  means that dimension is unlimited). The decision is **confirmed a second time under the lock when the
  task enters the pool**, for atomicity;
- Age-based cleanup deletes only tasks that have **reached a terminal state and exceeded the retention
  period**.

### 10.6 Deletion semantics

⚠️ **Never delete a directory recursively.** Deleting a task deletes only "the files that task actually
produced":

- **Direct links**: only the file name itself and its `.tmp` (defensively taking a single file name only);
- **Magnets / torrents**: nothing local is deleted — the files belong to aria2, and deletion goes through
  the RPC remove or is left to the user;
- **m3u8**: only the paths recorded in the result, and after normalisation they must fall under the save
  directory;
- Resume control files are deleted by **exact name concatenation**, **never with a directory-level wildcard**
  — that would delete other tasks' or other users' control files.

**Cancelling a task only stops it and deletes no files**: file semantics belong to the delete endpoint, and
the two are kept separate.

---

## 11. Page rendering and multilingual support

### 11.1 There is no build step

Pages are **read from disk and served at runtime**: the original HTML/CSS/JS under `web_page/` is returned
as-is — no bundling, no compilation, no template engine. Two consequences to remember:

- **Whatever is written in a served file is visible to the user** — comments in HTML/JS/CSS are sent to
  visitors verbatim. Internal paths, file names and server-mechanism notes **belong in Python docstrings**,
  not in page comments (a test guards this);
- **Placeholder replacement is plain string replacement, with no "unreplaced means error" mechanism** —
  miss one and the page shows `__XXX__` to the user. Hence two automated gates: a static one checking "is
  there replacement code for it", and a dynamic one checking "does any placeholder remain in the rendered
  page".

⚠️ Static assets are served as **original files with no substitution at all**. That is the precondition
that makes conditional requests (section 15.2) safe: the same URL is byte-identical for every user, so
"unchanged → 304" cannot cross-contaminate. **Session-injected pages are the other path**: they differ per
user and are always uncacheable — do not casually add validators to them.

### 11.2 Two languages: two files in the same directory

Multilingual support is done by **writing English page files directly** (same name plus `.en.html`), not by
a runtime dictionary: on each request the language decision decides whether to swap in the English file,
and **when the English file does not exist it falls back to Chinese**; a read failure also falls back
silently (one translation file must not break the page).

**Language decision order**: ① an explicit language cookie → ② the preference stored on the server for a
signed-in account → ③ fall back to Chinese.

⚠️ Rule ② exists for the **built-in window**: it is an incognito session where cookies do not persist, so
only the server-side account preference can restore the previous choice. Guests and anonymous visitors have
no account preference and take rule ③.

### 11.3 Theme colours

The theme is **one unit** (light/dark plus accent colour) and looks **only at client-side cookies**; the
server stores nothing and the account holds nothing. The server reads the cookie purely so the **first paint
already has the matching colours** — instead of rendering a default and then having JS change it.

⚠️ The theme button used to be copied into more than twenty static pages, with even the initial label
differing between them (an English page had a Chinese word). Now there is **one implementation and one
placeholder**.

### 11.4 Injected addresses and identity

The server injects several values, each with its own lesson:

| Injected value | Why it must be injected |
|---|---|
| **WS port** | The frontend must reach the live channel, and the port is configurable |
| **Server base URL** | A share page must hand out the **LAN address**, but the page itself was opened from a loopback address (localhost in the desktop window, 127.0.0.1 on Android) — a link built from `location.origin` **does not open for anyone else** |
| **"Is a LAN available"** | The frontend uses it to show "no LAN available" |
| **Current username and role** | The page shows and hides features accordingly rather than guessing |

⚠️ Two placeholder names **deliberately differ from the JS global property names** — otherwise a global
replacement would also rewrite the property assignment.

⚠️ The injected base URL comes from the `Host` request header and is **untrusted input**: it must be
validated as a legitimate host name before being placed into an HTML attribute. In a DNS rebinding scenario
`Host` is attacker-controlled, and inserting it verbatim allows closing the attribute and injecting markup.

⚠️ Injected values must be escaped for their **output context**: escaping for text and escaping for an
attribute are different. Using text escaping in an attribute context once caused a stored XSS (a regular
user could thereby reach the administrator). The rule now is "attribute contexts have their own escaping",
and a test pins it.

---

## 12. Page policy and outbound blocking

### 12.1 Why several layers

The user's requirement is "**the software must not reach outside**". That is not a switch but **several
independent layers**, and no single one is sufficient:

| Layer | What it covers | Executed by |
|---|---|---|
| **Content policy (CSP)** | Sub-resources, scripts and connections inside the page | The browser |
| **Navigation whitelist** | Page navigation and new windows | The client engine |
| **Network-layer interception** | Every non-local request | The client engine |
| **Inline rendering restriction** | HTML/SVG inside user files is not rendered as a page | The server |

The value of layering is **different coverage**: CSP is enforced by the browser, so it applies on desktop,
Android and any browser **at once**, independent of the client implementation; whereas the navigation
whitelist governs navigation only and cannot see sub-resources.

### 12.2 Page content policy

Page responses carry a CSP that locks sub-resources **to the same origin**: `default-src 'self'`;
`connect-src` carries `'self'` plus **a dynamic** entry for our own WS address; `img-src` adds `data:` and
`blob:` (thumbnails and QR codes may be inlined); `'unsafe-inline'` is kept (inline styles and inline
scripts are used extensively).

⚠️ **The WS entry must be dynamic, and both `ws` and `wss` are listed**: the frontend connects to a WS on
**our own other port**, and `'self'` does not cover it — omitting it **silently** breaks the downloader's
live progress (no error, it simply stops moving).

⚠️ Before the header is composed, **`Host` must be sanitised**: the CSP is assembled as a string, and a
newline smuggled into it is **header injection**. Sanitisation accepts only two shapes (bracketed IPv6, and
alphanumerics with dots and hyphens), and drops the port entirely — we know our own WS port, the client does
not have to tell us.

**Who emits it**: the unified exit (section 3.2) adds it automatically based on `Content-Type`, **not by hand
on each page**. There is more than one place that serves HTML, hand-editing always misses one, and the
missed one is precisely the gap. Responses that already carry an explicit CSP (static assets by mime, JSON,
file streams) are **not overwritten**.

The rules are based on **having checked how the pages are actually written**, not copied from a template:
there is no `<video>/<audio>/<iframe>` in the pages, so media-src and frame-src are unnecessary; and
precisely because it was checked, inline styles and inline scripts must be allowed.

### 12.3 Outbound blocking on desktop

Three layers are attached to the embedded window:

1. **Navigation layer**: navigation to a non-local address is cancelled — **the navigation never happens**;
2. **Network layer**: a non-local request is answered with a constructed empty response. Measurement shows
   this **really sends no packet**, rather than sending and discarding;
3. **New-window layer**: the shell framework's own handler is removed first (by default it **hands the link
   to the system browser**, which amounts to us deliberately releasing the outbound link), and replaced with
   "do nothing".

⚠️ All three are needed because their coverage differs: **the navigation layer cannot stop programmatic
loads**, and intercepting a navigation **itself produces a resource request**, which the network layer must
catch.

⚠️ The implementation depends on reaching the engine's native events, so **upgrading the shell framework
requires re-checking all three layers**. An earlier implementation polled the current URL — that is "navigate
away, then pull it back", leaving the external page at least a few hundred milliseconds of loading window;
now the window is **zero**.

⚠️ Do not retry the "rewrite DNS resolution" style of startup argument: it was measured to be **filtered by
the engine**, and external sites still open — it is **fake protection**. That kind of protection is worse
than none, because it makes people believe they are safe.

### 12.4 Blocking on Android

The Android side has two parts, one for **navigation** and one for **sub-resources**:

- **Navigation whitelist**: the container judges load requests and allows only local addresses;
- **Sub-resources**: intercepted at the network layer by a **built-in browser extension**, allowing only
  local addresses.

⚠️ Sub-resources do not go through the navigation callback, so "controlling navigation" is **not** the same
as "controlling sub-resources" — that is why the two are separate.

⚠️ The extension's decision criteria **must match the client's own whitelist**, otherwise "navigation allowed,
sub-resource blocked" appears — an extremely hard symptom to diagnose (a test pins this consistency). Two
accompanying rules: **a parse failure counts as untrusted** (better to block wrongly than to let through),
and **content scripts cannot be injected into `data:` pages**, so the permission and error pages must carry
their own styling rather than relying on injection.

### 12.5 The server-side complement

User-uploaded HTML/SVG and similar script-like files **are not executed inline**: previews return them as
plain text or as downloads. This blocks "put a malicious page into the shared directory, then lure someone
into opening it".

⚠️ It is only one layer of defence in depth; **do not rely on it as the only line** — the page itself says
"do not rely on this mechanism to store and share executable or script-bearing content".

---

## 13. Android

### 13.1 Form

The Android build packs **the entire server** into an APK: once the app is started, other devices on the same
Wi-Fi network or hotspot can use it through a browser at the phone's address — it is **the same server and
the same pages** as the desktop build.

- The Python runtime is provided by the packaging framework (Chaquopy). **The single source of truth for the
  source code is the repository's `leaffs/`**: a Gradle sync task copies it in at build time, so **desktop
  code needs zero changes and no second copy is maintained**;
- The UI container is **GeckoView**, not the system WebView — changing the container means **changing who
  controls the network stack**;
- 64-bit only; flavours are split by ABI (arm64 for devices, x86 for emulators), and **picking the wrong one
  fails the build**.

### 13.2 Entry point and differences from desktop

Android **does not reuse** the desktop startup function; it ships its own orchestration entry. The differences
are all in things the desktop has and Android does not:

| Item | Desktop | Android |
|---|---|---|
| Embedded window / browser fallback | Yes | No (it has its own container) |
| Downloader supervisor | Started | **Not started** (no aria2c; the code degrades on its own) |
| Main thread | Blocked by the window | Not blocked |
| Data root | Project root / executable directory | The app's private directory |

Before starting it also **pre-checks that the ports are free**. ⚠️ The pre-check must set `SO_REUSEADDR`,
otherwise TIME_WAIT is misread as "port in use", which presents as "closing the app and reopening it always
fails to start".

Paths are redirected to the app's private directory by injecting an environment variable, and that **must
happen before any business module is imported** — path constants are evaluated at module level, and doing it
later has no effect.

### 13.3 What is shared with desktop

HTTP, WebSocket, push, watchdog, TLS, page rendering and all business logic are **the same code**, so desktop
changes travel into the APK automatically — **provided it is rebuilt**.

⚠️ Data is **independent on each device and not synchronised**: what is shared is the software, not the data.

### 13.4 Container pitfalls (all of these were hit)

These only matter when changing the Android container, but each is very hard to infer from the symptom:

- **There is no "execute JS" API**: the only way to run a script is to load a `javascript:` address; the
  session object also has no "current URL" property, so you must remember it yourself.
- **Certificate errors have no server-side callback**: only a load-failure notification, and **refusing it
  changes nothing** — acceptance can only be done by **in-page JS inside the error page** adding the
  exception. ⚠️ Exceptions are recorded per `host:port`, so allowing the main port **does not** cover the WS
  port; both must be allowed once.
- **The error page must be a `data:` URL**: returning the container's built-in error page address **loads no
  error page at all**, only a blank screen — you must construct a `data:` page yourself.
- **Do not run the acceptance script on the "load failed" tick**: the document is still empty then, and the
  script fails **silently**.
- **Extensions use manifest v2**: v3 makes the extension fail to install (the callback never arrives).
- **The base library's default request header leaks the runtime and library versions**; override it when
  creating the server.
- **Hostname resolution is doomed to fail on Android** (the hostname is the device model name), so that logic
  must be skippable; network interface names are also unreliable (the hotspot interface is not necessarily
  `wlan0`) — **use the system API to distinguish Wi-Fi from hotspot**.
- **Restarting the app re-extracts the bundled assets** ⇒ pushing files into the extraction directory **does
  not survive the next start**, so it cannot be used for verification.

### 13.5 Network reachability on Android

One class of problem appears only on Android: **the address visitors use is one the server cannot recognise**.
Wi-Fi and hotspot are two different interfaces, and the "default route egress address" often points at the
cellular network while a hotspot is running — an address nobody on the LAN can open.

Therefore:

- A replaceable hook "**collect all local addresses**" is provided, with an Android implementation registered
  that uses the system API — the only way to obtain the hotspot interface's address;
- "**The single outward address**" (QR codes, share links, admin display) and "**all local addresses**"
  (whitelist, certificate SAN) are two different things and **must not be merged**. One wrong in the former
  means nobody can open the link; one missing in the latter rejects legitimate connections;
- Collection results have a **short TTL cache** — it is called on **every WS handshake**, and that runs in a
  synchronous callback (it cannot await, nor be moved into a thread pool), and on Android it also pays a DNS
  timeout. The cost is that a network change takes a few seconds at most to be reflected.

---

## 14. Data, logging and operation

### 14.1 Directory layout

Startup derives five directories, **all under the "data root"** (which depends on the run form; see 1.2):

| Directory / file | Contents |
|---|---|
| `config/` | All configuration and data files (the table in 9.2) |
| `shared_files/` | The shared root: the public directory and each account's directory |
| `shared_files/.uploads/` | **The upload temp directory** (invisible to users; see 7.4) |
| `.cache/` | Runtime cache: thumbnails, folder-size cache, downloader session files, … |
| Log file | The runtime log (rotating; see 14.3) |

**Bundled assets** (page files, bundled external programs) are separate from data: they live in the "asset
root" (the package directory when running from source, the extraction directory when packaged) and are
**read-only**; data lives in the data root and is **writable**. That distinction is what makes the packaged
build work at all.

⚠️ The upload temp directory **deliberately sits under the shared root** rather than the cache directory:
placing a file uses an "same-volume atomic replace", so it must be on the same volume as its target; and this
way its effect on the quota accounting is unchanged from before.

### 14.2 The cache directory's dual identity

⚠️ `.cache/` is simultaneously the **runtime cache directory** (the service is using it) and the place where
**development-time working files** are piled up. **Before cleaning it, tell the two apart**:

- Thumbnails and folder-size caches are **runtime data**; deleting them makes the service recompute, but is
  not fatal;
- The downloader's session file and peer cache are **runtime data**; deleting them loses resume state;
- Working files (documents, probe scripts, screenshots) are **gone for good** once deleted.

So cleanup must **not use wildcard batch deletion**. The historical approach was: draw the red lines first
(list what must never be touched), and run the full test suite immediately afterwards as acceptance — if
runtime data was deleted by mistake, the tests go red.

### 14.3 Logging

- **Sanitisation happens at the single entry point**, not by callers remembering to do it. There are many
  entry points for user-controlled text into the log (request lines, usernames, exception details, …), and
  "every caller remembers" cannot hold — it has already failed once. The entry point strips newlines and
  control characters (a newline can forge a log line; control characters scramble terminal display);
- **Short fields are truncated, long text is not**: truncating a traceback loses the evidence;
- ⚠️ **Exception details are never echoed to the client.** A Windows `OSError` message **contains a full
  absolute path**, and black-box penetration testing reconstructed the server's directory layout exactly that
  way. The approach is "a fixed message outward, details only into the log", rather than scrubbing paths out
  of the response;
- Sensitive parameters in the query string (session, token, password, access code) are **masked** in the
  access log;
- **Viewing and clearing the log require a role** (admin or above).

### 14.4 Watchdog

A background thread wakes once a second and decides whether things look stalled: **there is work in flight
(requests, or recent WS activity) and nothing has completed for a while**.

When it trips, it writes **all thread stacks** into a timestamped evidence file whose first line records the
scene (in-flight request count, quiet duration, time), so a human can tell a false positive from a real stall.

- The criterion includes a **cooldown**, so the same stall is not dumped over and over;
- A long streaming download with no other requests **may produce one false positive** — recording the scene
  in the file header is exactly what makes that judgeable;
- The cost is negligible (one thread waking once a second).

There is a second, **manual** channel: with an environment variable set, pressing `Ctrl+Break` at runtime
dumps all thread stacks to a file on demand. The two channels are independent.

### 14.5 Bundled external programs

A few external programs are shipped alongside (certificate generation, downloader, thumbnails). The lookup
order is: **package extraction root → directory of the main program → source asset directory → system PATH**,
returning "not available" when none is found, and the caller degrades — for example a failed thumbnail
generation logs an entry (with the return code and the tail of the error output) instead of silently
pretending it is "still generating".

⚠️ **Missing is not an error**: when running from source these programs are not committed by default, and
their absence disables only the corresponding feature while everything else keeps working. That is what the
README tells users, and it is how the code degrades.

---

## 15. Static assets and caching

### 15.1 Not cached by default; caching is the exception

Cache policy goes through the **unified exit**: a response that never declared a cache policy is backfilled
with `no-store`. The rule is "**not cached by default; caching must be declared explicitly**".

The rule came out of omission: responses used to be hand-written everywhere, and the paths that emitted **no
cache header at all** happened to include the ones that most needed it — an authorised success response, a
one-time QR code, user file content and packaged downloads, and the public share page. A blanket `no-store`
would not do either (static assets would be re-transferred in full every time), which is why the rule became
"off by default, declare it to turn it on".

Only **two places** currently declare caching explicitly: static assets and thumbnails.

⚠️ One companion rule: the "cache policy declared" marker in the response headers is **what the unified exit
uses to decide whether to backfill**. A 304 response **must therefore declare the policy again** — leaving it
out makes the backstop mark static assets as completely uncacheable (it only looks at "was a policy declared
for this response"). A regression test pins this.

### 15.2 Conditional requests

Static assets support conditional requests: the response carries `ETag` and `Last-Modified`, and when the
client comes back with a validator and **the asset is unchanged, only the headers are returned** (no body).

The precedence and the details (all per the HTTP specification):

- **When `If-None-Match` is present, `If-Modified-Since` is ignored** — otherwise a stale latter would
  overturn the former's decision;
- ETags use the **weak comparison**, with any `W/` prefix stripped before comparing;
- `*` is supported;
- Time comparisons are interpreted in **UTC**; a date that genuinely arrives without a time zone is also
  treated as UTC — falling back to the system's local time zone shifts every decision.

⚠️ When the file's state cannot be read, it is better to **emit no validator** (falling back to "full
transfer every time") than to pretend there is an ETag.

### 15.3 Why only static assets can do this

The meaning of `no-cache` is "**may** be stored, but must be revalidated before each use", and revalidation
requires a validator. Previously no validator was ever sent, so every request returned a full 200 with the
whole file — `no-cache` had effectively degraded into `no-store`, and **the intended bandwidth saving never
materialised**.

⚠️ **The security precondition**: static assets are served as **original files with no substitution** ⇒ the
same URL is byte-identical for every user, so "unchanged → 304" cannot cross-contaminate.

**Session-injected pages are the other path**: they differ per user and are always `no-store`; **do not
casually add validators to them** — that would hand one user's page to another. A test specifically pins
"anonymous and signed-in clients receive the same ETag".

---

## 16. Development conventions

### 16.1 Comments

Code comments are uniformly **bilingual** (one Chinese line plus an adjacent English line, or both in one
sentence), and only informative comments are kept; duplicated, outdated or meaningless comments are removed,
and the English part may be marked as AI-translated. The code base is being cleaned up in batches under this
convention.

⚠️ More important than "bilingual" is that **comments must explain "why"**. The most valuable comments in
this project are all of that kind: "this is written this way because writing it the other way once caused
this problem". **Read the comments before deleting code** — many constraints exist only there and are
invisible in the code itself.

### 16.2 Tests

- pytest, split by topic under `tests/`;
- Many tests **start a real service** (a child process, with its own data root and its own ports) rather than
  mocking — that tests real behaviour, at the cost of pre-allocating ports and running more slowly;
- ⚠️ **"The test process" and "the test's data root" are two different things**: when calling business
  functions directly inside the test process, module-level path constants still point at the **real project
  directory**. A white-box test that writes to disk must monkeypatch the file path to a temporary root,
  otherwise it pollutes real data;
- **Temporary data roots are "whoever creates it registers it"**, cleaned up when the module ends; long-lived
  leftovers are caught by an age-based sweep;
- ⚠️ **Write the test first and watch it fail on unfixed code.** More than once this project discovered that a
  test was not actually testing anything — **green does not mean it can detect the defect; confirm it goes red
  first**;
- A test that changes shared state **must restore it in `finally`**: otherwise one failed assertion leaves
  dirty state that turns a whole batch of later tests red, making real failures indistinguishable from
  knock-on ones;
- ⚠️ **Static-assertion tests must be updated together with refactors**: they assert "does this line exist in
  the code", so changing the implementation without changing them turns the full suite red — which is good,
  but you need to know why.

### 16.3 Commits

- **One thing per commit**: a commit does one thing;
- The commit message is written to a file and passed with `-F` (avoiding shell quoting problems). It states
  the **background / root cause / change / verification**, and also **what was deliberately left untouched**;
- Check `git diff --stat` before committing to confirm only the expected files changed;
- ⚠️ **Never commit a half-finished change** — an unfinished piece makes people believe the feature already
  works.

### 16.4 Documentation set

| File | Audience | Contents |
|---|---|---|
| `../README.md` | Users / extenders | Features, running, configuration, limitations |
| `../CHANGELOG.md` | Users | User-visible changes per release |
| **This file** | **People taking over the code** | Architecture, mechanisms, why it is designed this way |
| `../for-distribution/DISTRIBUTION_README.md` | End users | Distribution build usage notes |
| `../for-distribution/THIRD_PARTY_NOTICES.md` | Compliance | Third-party components and licences |
| `../android/README.md` | People building the Android package | Android structure and build |

> Layout convention: documents aimed at users and developers stay in the **repository root**
> (hosting platforms look for the root `README`); everything that must be **handed out with a
> distribution** (distribution notes, third-party notices, license texts) lives under
> `for-distribution/`; this file and the translation glossary live under `docs/`.

Convention: **user-facing documents are not mixed into this file** — users do not need implementation
details. Conversely, this file does not explain "how to use it"; that is the README's job.

### 16.5 A checklist before you start

It is worth asking yourself a few questions before changing this project:

1. **How many paths does this thing have?** The most common defect class here is "the same algorithm
   implemented twice" — directory listings, session revocation and the unlock decision all fell into it. Find
   the choke point first and **change the choke point rather than adding a copy elsewhere**;
2. **Who reads this value?** When adding an exit or a read path, first look for an existing predicate
   (section 2.1) and call it instead of writing another condition;
3. **What happens when it fails?** Silent failure is a primary cleanup target here: a configuration read
   error treated as "unlimited", a failed delete reported as success, a thumbnail failure indistinguishable
   from "still generating" — **prefer an error over pretending success**;
4. **Can this limit deadlock the service?** Some parameters, set too small, leave the service unable to
   repair itself (for example a connection limit too small for the request that would change it back);
5. **How will you verify it actually took effect?** And **first confirm that the verification method can
   detect the problem** — twice in this project a "verified" result came from a method that was itself invalid
   (a probe placed on the wrong page, mismatched schemes).

---

> This document describes the **current code**. When you change a mechanism, update it too — like the code,
> it is useless once it has drifted.


