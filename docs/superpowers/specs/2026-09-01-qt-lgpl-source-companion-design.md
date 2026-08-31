# Qt/PySide LGPL Source Companion Design

**Date:** 2026-09-01  
**Status:** Approved for implementation

## Goal

Make the unsigned native MatteLoop build locally publication-ready for the
Qt/PySide LGPL boundary without relying on a written source offer. Every
successful native build must place the application, verified media source
archive and checksum, and a verified Qt source companion and checksum together
in `dist/`.

Windows remains unqualified. This work does not publish, upload, sign, or
notarize an artifact and does not claim that a replacement build has been
tested on an actual macOS 13 host.

## Exact corresponding sources

The checked-in strict manifest records exactly three official HTTPS archives:

| Component | Archive | SHA-256 |
|---|---|---|
| Qt Base 6.10.3 | `qtbase-everywhere-src-6.10.3.tar.xz` | `383dc907816338f0cba72088a524c07458dfc69ce684ca9132fcc4fe91c24b0b` |
| Qt Image Formats 6.10.3 | `qtimageformats-everywhere-src-6.10.3.tar.xz` | `84605dd91037482b5b7c7ecc5c27aee8acc1cd7f1fe77bc564777ddf365d7d28` |
| PySide Setup 6.10.3 | `pyside-setup-everywhere-src-6.10.3.tar.xz` | `2c7462fe0cecb5b8ac0a3d92014b8d0b88bd4d9f8646709dab5286d9416f45bc` |

The manifest rejects extra/missing keys, non-HTTPS URLs, floating versions,
wrong archive roots, wrong names, and malformed digests. Download and digest
promotion reuse `scripts.media_stack.sources.ensure_source`; no unpinned fetch,
lock, journal, or recovery state is introduced.

Only those three sources belong in this companion. They correspond to the
actual bundle's Qt Core/DBus/Gui/Network/Widgets libraries, platform and image
format plugins, PySide bindings, and Shiboken runtime. No unrelated Qt module
source is added.

## Companion artifact

A focused `scripts.qt_source` module owns typed manifest loading, cache
identity, deterministic archive construction, cache validation, and the frozen
result type. The default cache is
`.matteloop-build-cache/qt-sources/<identity>/`; verified original downloads
remain in its source cache and the finished companion is reused only when its
canonical checksum verifies.

The companion filename is
`MatteLoop-qt-sources-6.10.3-<identity>.tar.gz`. Its adjacent `.sha256` is
canonical `<digest><two spaces><filename>\n`. Identity covers raw manifest
bytes, an explicit recipe revision, exact four-distribution inventory, and the
names and bytes of every included project-side evidence file.

The gzip/tar output uses sorted names, gzip mtime zero, empty gzip filename,
tar mtime zero, mode `0644`, numeric uid/gid zero, and empty owner/group names.
It contains:

- the three original `.tar.xz` archives unchanged under `sources/`;
- the checked-in manifest and canonical source checksum/provenance inventory;
- exact installed distribution inventory for `PySide6`,
  `PySide6_Essentials`, `PySide6_Addons`, and `shiboken6`, all 6.10.3;
- a component-to-source inventory matching the modules/plugins selected by the
  packaging spec;
- the full GPL-3.0 and LGPL-3.0 texts and the prominent Qt/PySide notice;
- `RELINK.md` with practical macOS and Windows replacement steps;
- `patches/README.md`, explicitly recording that MatteLoop applies no Qt or
  PySide source patch; and
- the project-side packaging/build evidence required to reproduce and replace
  the dynamic libraries: `packaging/pysidedeploy.spec`, packaging entry/smoke
  scripts, `scripts/build.py`, `pyproject.toml`, and `uv.lock`.

The companion does not include a written offer, URLs in place of source,
compiled Qt binaries, the application, or unrelated source trees.

## Native build integration

Native prerequisites require exact installed versions for all four
distributions:

```text
PySide6==6.10.3
PySide6_Essentials==6.10.3
PySide6_Addons==6.10.3
shiboken6==6.10.3
```

`scripts/build.py` prepares or verifies the Qt source companion before starting
Nuitka, alongside the existing verified media stack. A missing source,
checksum mismatch, inventory mismatch, or malformed cache entry stops the
build. After Nuitka, the existing forbidden-component gate and packaged smoke
must pass before the media and Qt source pairs are copied to `dist/`.

Publication uses the existing bounded temp-file, fsync, and replace mechanics
for each archive/checksum pair. There is no cross-artifact lock, journal,
rollback framework, or recovery state. A build returns success only after the
application and both verified source/checksum pairs exist. GitHub Actions
already uploads all of `dist/`, so no new upload or publication step is added.

## In-bundle notice and installation information

The repository and native packaging spec include:

- `legal/GPL-3.0.txt` — exact complete GPL version 3 text;
- `legal/LGPL-3.0.txt` — exact complete LGPL version 3 text; and
- `legal/QT-PYSIDE-LGPL-NOTICE.md` — prominent component/source/replacement
  notice.

The notice and `RELINK.md` explain:

- where the companion and its checksum must accompany the app;
- how to verify and unpack it;
- how to build Qt Base, Qt Image Formats, Shiboken, and PySide 6.10.3 from the
  included source with the platform's supported toolchain;
- which dynamic library, binding, and plugin paths in the unsigned macOS app
  or Windows standalone directory are replaceable;
- that replacements must remain ABI-compatible with 6.10.3;
- how to run the packaged offline smoke after replacement; and
- that ad-hoc signing after macOS replacement is only a local unsigned-use
  qualification step, not project signing/notarization support or a
  distributable signature claim.

The project remains 0BSD. The GPL/LGPL texts describe third-party options and
obligations and do not relicense MatteLoop's original code.

## Sanitized media repair evidence

The media builder increments its explicit recipe revision so old compliance
archives cannot be accepted under the corrected recipe identity. The actual
macOS repair subprocess continues to inherit the process environment, prepend
staging `prefix/lib`, and preserve the inherited external DYLD tail.

Only this allowlisted sanitized evidence is recorded in `build/commands.txt`:

```text
env DYLD_LIBRARY_PATH=${STAGING}/prefix/lib MACOSX_DEPLOYMENT_TARGET=13.0 <pinned tool Python> <pinned delocate script> ...
```

No other environment key, secret, inherited value, or external DYLD tail is
serialized. Windows repair command evidence is unchanged.

## Documentation boundary

README, third-party notices, and build documentation must state that binary
distribution keeps these five deliverables together:

1. the app;
2. the media complete-source archive;
3. the media checksum;
4. the Qt source companion; and
5. the Qt companion checksum.

The local host is macOS 26 targeting a 13.0 minimum. The artifact has not been
launched on an actual macOS 13 host. Windows, Actions execution, signing,
notarization, upload, release creation, and publication remain unclaimed.

## Testing and qualification

TDD behavior coverage proves:

- strict manifest loading and identity invalidation;
- pinned HTTPS source fetch and digest failure behavior through the reused
  source primitive;
- deterministic normalized companion bytes and exact content inventory;
- exact four-distribution prerequisite enforcement;
- packaging spec declaration of all three legal files;
- build failure when the Qt companion is absent or invalid;
- both source pairs and checksums are required for successful evidence
  publication;
- repair evidence contains exactly the two sanitized environment assignments
  and excludes inherited/sentinel values; and
- Windows command construction and media gates remain unchanged.

Requalification runs targeted tests, ruff, mypy, guardrails/G6, the exact full
repository gate, a fresh forced media build, media cache hit, real app rebuild,
both checksum/content inspections, in-bundle legal/relink inspection, committed
bundle gate, packaged smoke, and direct Mach-O minimum-version inspection. Only
actual measured results update documentation and the Task 8 report.

