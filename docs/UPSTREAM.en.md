# Relationship to upstream leaffs

The Camphor repository root is a branch of [`leaffs`](https://gitee.com/ewq2526/leaffs.git). The application is a native component of the system, not an external dependency: it carries the changes the system needs, and it carries the system layer as well. Upstream `origin/main` is the application alone; this branch, `camphor`, is upstream plus the system layer and those changes.

## Baseline

Upstream commit `cb1aacb` (2026-10-22). Upstream commits after that point are not here yet. The upstream release tags `v1.0.4`, `v1.0.5` and `v1.0.6` all predate the baseline.

## What this branch changes against upstream

### Application (`leaffs/`)

| Location | Change |
|---|---|
| `system/` (4 new files) | Device panel collection and Wi-Fi control: `host.py` reads `/proc`, `/sys` and `systemctl`; `wifi.py` drives `wpa_cli` |
| `web_page/system/` (2 new files) | The panel page, one per language |
| `server/handler.py` | Registers `/system`, `/api/system`, `/api/wifi/status`, `/api/wifi/scan` and `/api/wifi/connect`, and adds those paths to `_SUPER_ADMIN_ONLY_GET` / `_SUPER_ADMIN_ONLY_POST` |
| `app.py` | Kiosk form (`LEAFFS_KIOSK=1` gives a fullscreen frameless window), first-frame background, whole-page zoom (`LEAFFS_ZOOM`, default 1.25) |
| `web_page/common/app.js` | Hides the System entry for anyone who is not `super_admin` |
| `web_page/common/style.css` | `color-scheme` declaration, page entry animation removed, System entry pushed to the right of the nav |
| 20 page html files | Each nav bar gains one line: `<a href="/system">System</a>` |

### System layer

Upstream has no `etc/`, `usr/` or `boot/`. Upstream `docs/` holds two application documents (`ARCHITECTURE`, `TRANSLATION_GLOSSARY`); here it also holds the application manual `LEAFFS` and this file. Upstream `for-distribution/` carries the Windows package and Android licence material; here it is the system's third-party licence register. The root README and CHANGELOG are rewritten for the system as well.

### Removed relative to upstream

57 files under `android/` (the Android client, of no use to the system) and 24 files under upstream `for-distribution/` (the Windows and Android distribution material, replaced by this repository's set). Upstream keeps changing both, which is why a whole-tree merge is far more painful than a path-scoped patch.

## Pulling upstream updates

```powershell
git fetch origin
git diff cb1aacb origin/main -- leaffs requirements.txt | git apply -3
```

That brings in upstream's changes to the application directory and the dependency list only; `android/` and the system layer stay out of it. Hunks that collide with local changes are left with conflict markers for review.

Afterwards:

1. If `git apply` is clean, run the tests: `python -m pytest`
2. Replace the `cb1aacb` baseline above with whatever `origin/main` points at
3. If `requirements.txt` moved, rebuild `/opt/leaffs/venv` and reinstall the dependencies

Two things not to do:

- **Do not `git merge origin/main` over the whole tree**: the 57 deletions under `android/` turn into modify/delete conflicts on every merge.
- **Do not `git checkout origin/main -- leaffs`**: that silently overwrites the changes above with upstream's versions, and reports nothing.

When upstream adds a page, the `/system` nav entry does not come along; add the line by hand.
