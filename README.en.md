# Camphor (English)

> English version. AI-translated from the Chinese original.

A minimal Linux system. Kernel and userland come from Debian 13, trimmed down to what one
full-screen file service needs; it ships `leaffs` (browsing, sharing, downloads, device panel)
and serves Web, SMB and SSH.

It runs on any machine that can boot an x86_64 kernel, mount a disk and drive a display.

## Install

Write the release image to a USB stick, boot from it, and follow the three steps on screen:

1. **Partition**: pick a disk, adjust partitions, or let it use the remaining space.
   An existing EFI system partition is reused.
2. **Account**: user name and password.
3. **Install**: unpack the system, write the bootloader; progress and log stay on the page.

Reboot from the disk. The first boot expands the application layer; after that it is a
normal Camphor machine.

## Use

Boots straight into the full-screen interface: browse, upload, download, share. Other devices
on the same LAN open the service address in a browser — no client to install.

The device panel is `/system` (super admin): CPU, memory, temperature, battery, disk capacity
and throughput, service states, Wi-Fi.

## Repository

```
app/                application layer: units, configuration, leaffs code
etc/ usr/           system layer, at real paths
mkimage/            build: package lists, image scripts, boot artwork, build docs
docs/               application (LEAFFS.md), internals (ARCHITECTURE.md), glossary
for-distribution/   distribution and third-party licences
tests/              tests
```

The medium carries three layers: system skeleton, application image, and installer
dependencies. See `mkimage/BUILD.md` for the layering and the two-stage install.

## Documentation

- How to build the image, step by step, and how to verify it: `mkimage/BUILD.md`
- Installer: boot chain, web backend, the two stages, troubleshooting: `mkimage/INSTALLER.md`
- Pitfalls already hit: `mkimage/PITFALLS.md`
- What the application does: `docs/LEAFFS.md`
- How the server works: `docs/ARCHITECTURE.md`
- Changes: `CHANGELOG.md`

## Licence

See `for-distribution/THIRD_PARTY_NOTICES.en.md`. Fonts and ffmpeg are GPL/OFL components —
ship their licence texts.

This project is heavily AI-assisted (primary model: DeepSeek V4 Flash).
