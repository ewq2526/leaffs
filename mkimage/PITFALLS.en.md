# Pitfalls

Every mistake made so far is listed here. One row per pitfall: **Symptom** is what you see on
site, **Cause** is why it happens, **Caught by** says whether a script can stop it before it
reaches the image, and **Fix** is what was done.

The "Caught by" column is the one that matters. Rows marked `preflight` / `postbuild` are checked
automatically by `mkimage/build.sh`; rows marked `manual` rely on this page.

```sh
sh mkimage/preflight.sh       <repo> <builddir>   # before starting a build
sh mkimage/postbuild-check.sh <ISO>               # after the ISO exists
```

The flow itself is in `BUILD.md`.

## 1. Build flow (chroot / binary)

| Symptom | Cause | Caught by | Fix |
|---|---|---|---|
| the image still has the old file after a rebuild, yet the exit code is 0 | `config/includes.chroot` is only copied during the chroot stage; `lb binary_chroot` copies the outer `chroot/` | manual | copy repository content into the outer `chroot/` as well (first step of `build.sh`) |
| after one run the build directory no longer has a usable build environment | when `cache/bootstrap` exists, `binary_chroot` replaces the outer `chroot` with a clean bootstrap and moves the real tree to `chroot/chroot`, which `binary_rootfs` deletes afterwards | preflight warning | either `rm -rf cache/bootstrap` first, or accept that the environment must be restored from a backup after this round |
| the ISO grows from 1.7 GB to 2.6 GB | the `mount --bind` of the local package repository is left mounted, so `cp -a chroot` copies the whole repository into the image | preflight | mount it to install, unmount before the chroot is copied |
| squashfs is 4 KB and the ISO 657 MB, exit code 0 | `binary_rootfs` removes `chroot/chroot` at the end; dropping its stage stamp makes mksquashfs compress an empty directory | postbuild (size floor) | dropping the `binary_rootfs` stamp requires re-running `binary_chroot`, squashing alone is not enough |
| `dpkg-divert: error: rename involves overwriting … not allowed` | the stand-in file is still there, so `--rename --remove` would overwrite it | preflight reports the count, re-checked before squashing | delete the stand-in first, then remove the diversion; the criteria are `start-stop-daemon` being an ELF and zero local diversions |
| `E: Unable to locate package squashfs-tools` | `mksquashfs` runs inside the chroot; having the package on the host does not count | manual | install the binary/installer stage dependencies into the chroot; from a local repository when offline |
| a resumed `binary` run fails with `File not found - …/localrepo/pool/…` | only `binary_rootfs` mounts the local repository; with its stage stamp present the resume skips the mount | manual | `mount --bind` it to `chroot/home/<user>/localrepo` by hand and unmount afterwards |
| `binary_loadlin` fails with `Unable to locate package loadlin` | `LB_LOADLIN` defaults to true and Debian 13 has no such package | already off in `auto/config` | `--loadlin false` |
| a resumed run fails to remove some package | `chroot.installed_tmp_pkgs` still lists the package from the failed round | manual | delete that line |
| `binary_bootloader_splash` needs `librsvg2-bin` | drawing the splash screen needs `rsvg-convert` | manual | add it to the local repository |
| the build hangs on `security.debian.org` | `lb config` does not overwrite old values, and security updates are a separate path on a mirror | preflight | re-run with both mirror variables after `lb clean --config` |
| `lb config` silently ignores the whole configuration and everything is default | the hooks or `auto/config` lack the executable bit | preflight | `chmod +x` |
| the boot artwork is corrupted after a commit (31023 → 31021 bytes) | `.gitattributes` `text eol=lf` treats the png as text and converts its line endings | preflight | add `*.png binary` and similar rules |
| `auto/config: bad interpreter`, package names with `\r`, apt cannot find packages | autocrlf makes the scripts and lists check out as CRLF | preflight (LF rules) | cover the build tree and `*.txt` in `.gitattributes` |

## 2. Image content (factory state)

| Symptom | Cause | Caught by | Fix |
|---|---|---|---|
| the Live entry reports `file /live/vmlinuz-… not found`, the screen shows only "you need to load the kernel first" | `--linux-packages none` makes `binary_linux-image` exit immediately, so `/live` has no kernel or initrd while the menu already names them by version | preflight + postbuild | a binary hook extracts kernel and initrd from `filesystem.squashfs` into `binary/live` |
| the Live entry drops into busybox, the screen is garbled | passing `--bootappend-live` explicitly replaces the whole default string, so `boot=live` is lost; without it live-boot does not take over the root mount | preflight | put `boot=live components` back into the string |
| the hostname is `localhost.localdomain` | `/etc/hostname` inside the image comes from the build environment | postbuild | ship `etc/hostname` from the repository and write it explicitly during the build |
| `/etc/hosts` carries the build host's name | `chroot_hacks` deletes the image's hosts file and the build environment's one gets squashed in | manual | ship a machine-independent `etc/hosts` from the repository |
| build paths appear in the image (`/home/<user>/localrepo`, `var/lib/apt/lists/_home_*`) | the whole tree is copied together with the chroot | postbuild | delete them in the cleanup step, keeping only `lists/partial` |
| the factory state already carries an identity: SSH host keys, a non-empty `machine-id` | the build environment's identity is squashed into the image | postbuild | delete the keys and truncate `machine-id` in the hook; systemd generates it on first boot |
| the image keeps the pip cache, stale bytecode, apt and dpkg build logs | build-time leftovers in the chroot | postbuild | clean them in the hook; apt logs live in a subdirectory, deleting only the top level misses them |
| `/usr/sbin/start-stop-daemon` is a `/bin/true` stand-in | the build-time dpkg diversion was not undone | postbuild | see the diversion row in section 1 |
| installing grub raises a conffile prompt and an unattended install stops | the image ships `/etc/default/grub`, which is a conffile | preflight | do not ship it; merge the keys from the finishing script instead |
| the installed system has no apt sources | d-i's `apt-setup` unconditionally overwrites them with the empty `sources.list.new` it generated | manual | ship `/camphor/sources.list` on the ISO and copy it back during finishing |
| the installed system's menu does not list other systems on the disk | `os-prober` is not installed in the image, and recent grub only runs it with `GRUB_DISABLE_OS_PROBER=false` | preflight + postbuild | add `os-prober` to the package list, merge that key and re-run `update-grub` while finishing |
| the installed system does not boot: the kernel starts but stops at `Running /scripts/local-block` and drops to a shell | `initramfs.conf` says `MODULES=list`, so only the SATA/USB set is included and NVMe / eMMC / virtio disks are not recognised | manual | `MODULES=most` (initrd grows from 46 MB to the hundred-MB range) |
| after install every service fails with `in-target is already running` | the build-time dpkg diversion was squashed into the image, so d-i's own diversion clashes and the lock is never released | manual | recopy the tree and treat it as a clean build: undo diversions, remove `00secure`/`00recommends`/`policy-rc.d`/`debian_chroot` |

## 3. Installer (preseed / late_command)

| Symptom | Cause | Caught by | Fix |
|---|---|---|---|
| language and keyboard are never asked | `auto-install/enable=true` keeps localechooser and console-setup from loading at all | manual | drop `auto-install/enable`, `debian-installer/locale`, `keyboard-configuration/xkb-keymap` |
| the partition overview is dismissed as soon as it appears, followed by `No root file system is defined` | `partman/choose_partition select finish` preselects the "finish" item of the overview screen | manual | preseed no `partman` keys at all; leave partitioning to the installer |
| `late_command` does not run, the installer returns to the main menu and nothing on screen says which step broke | dozens of lines of shell inside the preseed get truncated at a continuation line, `sh: syntax error: unterminated quoted string`, `07preseed` returns 2 | preflight + postbuild | move the work into `/camphor/postinstall.sh` and leave `late_command` as `sh /cdrom/camphor/postinstall.sh` |
| the installed `/etc/fstab` has a single line and the ESP and swap are gone | writing `/target/etc/fstab` with `printf >` overwrites the complete file partman wrote | manual | do not write `fstab` at all |
| the installer stops on a "missing firmware" prompt | the installer environment has no firmware packages | manual | `hw-detect/load_firmware false` (a workaround: `/firmware/*.deb` in the image are symlinks and why they are not taken has not been investigated) |
| the hostname field has no pre-filled value | `preseed.sh` reads `preseed/interactive` before loading each file, so a switch set later only affects later includes | postbuild (checks the chain) | split into three files: open interactive → set the hostname default and close it → all remaining silent answers |

## 4. Pitfalls of the verification itself

| Symptom | Cause | Fix |
|---|---|---|
| typing looks frozen, one passphrase takes a minute and a half | `sendkey` produces no output, and the screenshot helper waits for a timeout on every read | send keys without reading (`read=False`) |
| typing grub parameters by hand and comparing screenshots, and still getting it wrong | parameters are easy to insert on the wrong line, and the screen may not render the log at all | boot with `-kernel` / `-initrd` / `-append` and read the serial port |
| assuming success once the ISO boots | three things need separate verification: the ISO boots, the install works, the installed system boots | give each its own criterion |
| the install stalls and the screen only shows the main menu | d-i's log is on the fourth virtual terminal | `ctrl-alt-f4` to see `finish-install` / `preseed_command` / `log-output` |

## 5. Two things no script can catch

**One: success that exits 0 but is not.** Several rows above belong here: the 4 KB squashfs, the
`includes.chroot` change that never applied, the truncated `late_command`. In all of them the
build never reported an error and the ISO timestamp was fresh. So the criterion cannot be "the
build finished", only the facts `postbuild-check.sh` lists plus a real run on a machine.

**Two: stage stamps and resumed runs.** One build directory is at the same time the build
environment and the source of the live system, so every manual patch must be preceded by one
question: **will the next stage replace, delete or overwrite this change?**
`binary_rootfs` deletes `chroot/chroot`, `binary_chroot` replaces the outer `chroot`, and
`binary_grub_cfg` overwrites `binary/boot/grub`. When a stage is held back by hand, that question
is more reliable than memory.
