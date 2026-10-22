# LeafOS (English)

> English version. AI-translated from the Chinese original.

A minimal Linux system. Kernel and userland come from Debian 13, trimmed down to what one full-screen file service needs; it ships `leaffs` (browsing, sharing, downloads, device panel) and serves Web, SMB and SSH.

It runs on any machine that can boot an x86_64 kernel, mount a disk and drive a display.

## Install

1. Install a minimal Debian 13, ext4 root, 512M ESP.
2. Copy `etc/`, `usr/` and `boot/` from this repository to the same paths on root and the ESP (`boot/grub.cfg` → `EFI/BOOT/grub.cfg`).
3. Install `fonts-noto-cjk`, `ffmpeg`, `firmware-{iwlwifi,realtek,atheros,brcm80211,misc-nonfree}`.
4. Add the kernel modules you need (storage, input, i915, rtw88, zram, netfilter, …); if the system disk is USB, `uas` must be in the initramfs.
5. Enable the units listed in `etc/systemd/system/leafos-units.list`.

## Use

Boots straight into the full-screen interface: browse, upload, download, share. Other devices on the same LAN open the service address in a browser — no client to install.

The device panel is `/system` (super admin): CPU, memory, temperature, battery, disk capacity and throughput, service states, Wi-Fi.

## Repository

```
etc/ usr/ boot/     system configuration, at its real paths
leaffs/             the shipped application: file service, web pages, device panel
docs/               application (LEAFFS.en.md), internals (ARCHITECTURE.en.md), glossary
for-distribution/   distribution and third-party licences
tests/              tests
```

## Documentation

- What the application does: `docs/LEAFFS.en.md`
- How the server works: `docs/ARCHITECTURE.en.md`
- Changes: `CHANGELOG.en.md`

## Licence

See `for-distribution/THIRD_PARTY_NOTICES.en.md`. Fonts and ffmpeg are GPL/OFL components — ship their licence texts.

This project is heavily AI-assisted (primary model: DeepSeek V4 Flash).
