# Building native artifacts

MatteLoop uses `pyside6-deploy` with Nuitka in standalone mode. Builds are
platform-specific: build the macOS artifact on an arm64 Mac and the Windows
artifact on an x64 Windows machine. Linux artifacts remain deferred.

The release artifacts are deliberately unsigned. macOS Gatekeeper and Windows
SmartScreen will warn on first launch; the steps below explain how to continue
after checking that the artifact came from the expected build.

The app icon is shipped as committed derived assets:
`assets/branding/matteloop/derived/matteloop.icns` for macOS and
`assets/branding/matteloop/derived/matteloop.ico` for Windows. The build helper
verifies both formats and selects the platform-appropriate asset; the corrected
1024 px masters remain alongside the original design masters.

## Prerequisites

You need:

- CPython 3.13.x. The project accepts `>=3.13,<3.14`.
- `uv` 0.11.32. The workflow pins this version and `uv.lock` pins the Python
  dependencies.
- macOS 13 or later on arm64, plus the Xcode Command Line Tools. Install the
  tools with `xcode-select --install` if `xcode-select -p` reports that they
  are missing. A first media-stack build also needs CMake, NASM, and
  `pkg-config`; install them with `brew install cmake nasm pkg-config`.
- Windows x64 with the Visual Studio 2022 Build Tools, including the Desktop
  development with C++ workload, MSVC v143, and a Windows SDK. The hosted
  `windows-2022` runner already provides these tools. A first media-stack build
  also needs CMake and MSYS2 with `base-devel`, `make`, `diffutils`, `nasm`,
  and `pkgconf`, with the MSVC amd64 environment active.

The native prerequisite gate requires PySide6, PySide6_Essentials,
PySide6_Addons, and shiboken6 to be installed at exactly 6.10.3. The locked
build also includes Nuitka 2.8.10, PyAV 16.1.0, Pillow 12.3.0, NumPy 2.5.2,
`rembg` 2.0.72, and CPU `onnxruntime` 1.29.0. Do not install a second Python
environment for the build; run the commands below from the repository root.

## Licensing gate

The repository's original code, documentation, and visual assets use 0BSD.
The native bundle includes the project's `LICENSE`, `THIRD_PARTY_NOTICES.md`,
the complete `GPL-3.0.txt` and `LGPL-3.0.txt` texts, the prominent
`QT-PYSIDE-LGPL-NOTICE.md`, and practical `RELINK.md`. Those installed files
are necessary but are not by themselves sufficient to qualify a binary
distribution: both corresponding-source archive/checksum pairs described
below must remain beside the app.

The stock PyAV 16.1.0 wheel currently contains FFmpeg libraries linked to
`libx264` and `libx265`. It is never a publishable input for a MatteLoop native
artifact. `scripts/build.py` instead builds or reuses the custom verified media
wheel described below and has no fallback to the installed stock wheel. See
[THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) for the component inventory
and model boundary.

This LGPL configuration does not answer codec-patent questions. H.264 and
H.265 decoding can be subject to separate patent or royalty rules depending on
where and how an artifact is distributed; those questions must be assessed
separately from source availability and LGPL compliance.

## Reproducible LGPL media stack

The checked-in `packaging/media-stack/manifest.toml` pins every downloaded
source by version, HTTPS URL, and SHA-256:

| Source | Version | SHA-256 | URL |
|---|---:|---|---|
| FFmpeg | 8.0.1 | `05ee0b03119b45c0bdb4df654b96802e909e0a752f72e4fe3794f487229e5a41` | <https://ffmpeg.org/releases/ffmpeg-8.0.1.tar.xz> |
| libwebp | 1.6.0 | `e4ab7009bf0629fd11982d4c2aa83964cf244cffba7347ecd39019a9e38c4564` | <https://storage.googleapis.com/downloads.webmproject.org/releases/webp/libwebp-1.6.0.tar.gz> |
| PyAV | 16.1.0 | `a094b4fd87a3721dacf02794d3d2c82b8d712c85b9534437e82a8a978c175ffd` | <https://files.pythonhosted.org/packages/78/cd/3a83ffbc3cc25b39721d174487fb0d51a76582f4a1703f98e46170ce83d4/av-16.1.0.tar.gz> |
| Cython source distribution | 3.3.0 | `eed0d93fbca7087f143b42c34b05a825849bdf17f101572c2105acfa49aa88b8` | <https://files.pythonhosted.org/packages/a9/d8/4981ef716ad0e3ff0d3ef383aefc6b03c4a88dee33b272bf8e0d833001ca/cython-3.3.0.tar.gz> |

The builder compiles shared libwebp, configures shared FFmpeg with GPL,
non-free, and autodetected components disabled, builds PyAV against those
libraries, repairs the wheel with `delocate` on macOS or `delvewheel` on
Windows, and verifies the finished wheel before it can enter the cache. The
build interpreter/provenance identity is CPython `cp313`; upstream PyAV 16.1.0
uses the stable-ABI wheel tags `cp311-abi3`. The verifier requires that exact
ABI pair plus `macosx_13_0_arm64` or `win_amd64`, then proves the candidate can
be imported and exercised by the current CPython 3.13 interpreter.

To build only the stack and print every output path as JSON:

```sh
uv run --frozen --no-sync python scripts/build_media_stack.py --json
```

The default cache is `.matteloop-build-cache/media-stack`. Its 24-character
identity covers the exact manifest bytes, builder-recipe revision, operating
system, normalized machine architecture, CPython ABI tag, and deployment
target. Changing any input selects a new
`<identity>/finished/` directory. A successful directory contains:

- the repaired `av-*.whl` and adjacent `*.provenance.json`;
- `verification-report.json`, containing runtime and native-dependency
  evidence;
- `MatteLoop-media-sources-<target>-<identity>.tar.gz`, containing the exact
  FFmpeg, libwebp, PyAV, and digest-bound Cython source archives, build commands,
  compiler and tool versions, licences, dependency inventory, verifier report,
  checksums, and rebuild instructions; and
- `*.artifact-set.json`, cryptographically binding the wheel, provenance,
  report, compliance archive, target, ABI, manifest, and identity.

A cache hit skips source compilation but validates the artifact-set binding and
runs the complete wheel verifier again. To deliberately compile from scratch,
run `scripts/build_media_stack.py --force` or pass
`--rebuild-media-stack` to `scripts/build.py`. `scripts/build.py --media-wheel
PATH` accepts an explicit candidate only when its adjacent provenance, report,
compliance archive, and artifact-set binding exist and all verification passes.

download, and native build. Do not disable TLS or substitute `/etc/ssl/cert.pem`:

```sh
SSL_CERT_FILE=/opt/homebrew/etc/ca-certificates/cert.pem \
  uv run --frozen --no-sync python scripts/build_media_stack.py --force --json
```

## Qt/PySide corresponding-source companion

`packaging/qt-source/manifest.toml` pins the corresponding source for the
actual PySide6 6.10.3 bundle. Every `scripts/build.py` run verifies or fetches

| Source | Version | SHA-256 | URL |
|---|---:|---|---|
| Qt Base | 6.10.3 | `383dc907816338f0cba72088a524c07458dfc69ce684ca9132fcc4fe91c24b0b` | <https://download.qt.io/official_releases/qt/6.10/6.10.3/submodules/qtbase-everywhere-src-6.10.3.tar.xz> |
| Qt Image Formats | 6.10.3 | `84605dd91037482b5b7c7ecc5c27aee8acc1cd7f1fe77bc564777ddf365d7d28` | <https://download.qt.io/official_releases/qt/6.10/6.10.3/submodules/qtimageformats-everywhere-src-6.10.3.tar.xz> |
| PySide Setup | 6.10.3 | `2c7462fe0cecb5b8ac0a3d92014b8d0b88bd4d9f8646709dab5286d9416f45bc` | <https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.10.3-src/pyside-setup-everywhere-src-6.10.3.tar.xz> |

The cache is `.matteloop-build-cache/qt-sources/<identity>/`. Its 24-character
identity covers the raw Qt manifest, explicit recipe revision, exact installed
versions and full wheel/package-file inventories for PySide6,
PySide6_Essentials, PySide6_Addons, and shiboken6, plus the names and bytes of
every bundled legal and project-side build evidence file. A cache hit is used
only when the adjacent checksum is canonical and matches the archive.

The deterministic output is
`MatteLoop-qt-sources-6.10.3-<identity>.tar.gz`. It contains the three original
source archives byte-for-byte, their checksums/URLs/archive roots, component
and installed-package inventories, full GPL-3.0/LGPL-3.0 texts, the notice and
`RELINK.md`, an explicit no-patch inventory, and the relevant packaging,
build, publication-helper, smoke, lock, and project metadata. It does not use
a written offer or substitute download links for source.

`RELINK.md` documents rebuilding Qt Base, Qt Image Formats, Shiboken, and
PySide, then replacing the bundle's dynamic libraries, bindings, and plugins
with ABI-compatible 6.10.3 outputs on unsigned macOS or Windows. A local ad-hoc
macOS re-sign after replacement is only an unsigned-use test step; it is not a
signing or notarization claim.

## macOS

Confirm the machine and install the locked environment:

```sh
uname -m
xcode-select -p
clang --version
cmake --version
nasm -v
pkg-config --version
uv --version
SSL_CERT_FILE=/opt/homebrew/etc/ca-certificates/cert.pem \
  uv sync --frozen --all-groups
```

`uname -m` must print `arm64`. Then build:

```sh
SSL_CERT_FILE=/opt/homebrew/etc/ca-certificates/cert.pem \
  uv run --frozen --no-sync python scripts/build.py
```

The unsigned app bundle is written to `dist/MatteLoop.app`. The helper checks
the pinned build tools and every data file named by the spec, automatically
builds or reuses and re-verifies the custom media wheel, invokes
`pyside6-deploy`, scans the final bundle for forbidden media components, and
runs the packaged offline smoke test. Only after all gates pass does it require
these five deliverables together in `dist/`:

1. `MatteLoop.app`;
2. `MatteLoop-media-sources-macos-arm64-<identity>.tar.gz`;
3. the media archive's adjacent `.sha256`;
4. `MatteLoop-qt-sources-6.10.3-<identity>.tar.gz`; and
5. the Qt companion's adjacent `.sha256`.

The verifier report and artifact-set binding remain in the media cache path
printed by `scripts/build_media_stack.py --json`. The application was built on
macOS 26 with a 13.0 deployment target; it has not been launched on an actual
macOS 13 host.

## Windows

Open PowerShell in the repository root. Confirm the interpreter and toolchain,
then install the locked environment:

```powershell
py -3.13 --version
uv --version
cmake --version
nasm -v
pkg-config --version
uv sync --frozen --all-groups
```

Build the x64 bundle:

```powershell
uv run --frozen --no-sync python scripts/build.py
```

The standalone bundle is written to `dist\MatteLoop.dist`, with the executable
inside that directory. A successful binary distribution must keep that app
directory plus its media source/checksum pair and Qt source/checksum pair
together. Windows x64 remains unqualified: no authorized local or manual
Actions build has yet completed the wheel verifier, fixture checks, Nuitka
bundle, forbidden-component scan, packaged smoke, and source-companion gates.
Do not infer Windows status from the committed workflow or macOS results.

## Manual GitHub Actions build

`.github/workflows/release.yml` is a manually dispatched, read-only,
two-target build. It prepares the target toolchain, restores only the exact
media-stack cache key, installs the frozen environment, and runs the same
`scripts/build.py` gate. The key includes the runner OS, matrix target, and a
hash of the media manifest, all `scripts/media_stack/**/*.py` files,
`scripts/build_media_stack.py`, and `scripts/verify_media_stack.py`; it has no
broad restore key.

The workflow uploads all of `dist/` as one temporary unsigned Actions artifact,
so the app and both source/checksum pairs follow the existing upload path. It
does not create a release, publish, sign, notarize, or permanently host the
corresponding sources. A later authorized publication must keep all five
deliverables together on a durable endpoint; an expiring Actions artifact is
not that endpoint.

## Models and first launch

Model weights are not bundled into either artifact. On the first Preview or
Render using a model, MatteLoop downloads that model from the upstream rembg
release location recorded in `resources/model-manifest.json` and caches it
locally. Later runs reuse the cache; the first use therefore needs network
access and enough disk space.

The default model cache is `~/Library/Caches/matteloop/models` on macOS.
Existing `~/Library/Caches/rembggui/` subdirectories — weights, compiled
provider caches and thumbnails — are still used when the new location does not
exist; if both exist, the MatteLoop location is preferred.

The 13 V1 models range from about 4.4 MiB (`u2netp`) to 927.6 MiB for the
largest BiRefNet models. The default `birefnet-portrait` is about 927.6 MiB;
downloading the complete V1 catalog would be about 6.35 GiB. These weights
are separate from the native bundle size.

## Running unsigned artifacts

On macOS, double-click `MatteLoop.app`. If Gatekeeper says the developer cannot
be verified, Control-click the app in Finder, choose **Open**, then confirm
**Open**. If that action is not offered, open **System Settings → Privacy &
Security**, find the blocked-app notice, choose **Open Anyway**, authenticate,
and confirm. Only do this after verifying the artifact and its build source.

On Windows, open `dist\MatteLoop.dist` and double-click the executable. When
SmartScreen displays **Windows protected your PC**, choose **More info**, then
**Run anyway**. If Windows still blocks it, open the file’s **Properties**,
select **Unblock**, apply the change, and run it again after verifying the
artifact.

## Verification status

macOS arm64 qualified on 2026-09-01 on macOS 26.6.2 (build 25G83), using
CPython 3.13.14, Apple clang 21.0.0, CMake 4.4.3, NASM 3.02,
`pkg-config` 3.0.6, and local `uv` 0.12.7. The workflow remains pinned to the
host, not estimates:

| Gate | Measured result |
|---|---|
| Exact repository gate | ruff passed; mypy passed 95 source files; pytest passed 1,479 tests with 15 warnings in 54.95 seconds |
| Forced media build | 342.81 seconds; identity `824842398768745fa5e6e346`; wheel `av-16.1.0-cp311-abi3-macosx_13_0_arm64.whl` |
| Cache hit | 9.21 seconds; returned the same five output paths, skipped compilation, validated the artifact set, and reran the verifier |
| Application build | 259.29 seconds; bundle scan and packaged offline smoke passed |

The committed verifier loaded the cached wheel with CPython 3.13, decoded the
committed H.264 and H.265 fixtures, and exercised production animated-WebP
encode/validation. Its report records FFmpeg 8.0.1; `h264`, `hevc`, and
`libwebp_anim`; `mov` and `webp`; LGPL-2.1-or-later library metadata; and no
forbidden dependency. Direct Mach-O inspection found a macOS 13.0 minimum on
all ten bundled dylibs. The committed final-bundle gate returned no findings.
The packaged smoke passed video decode, two-frame alpha WebP, Qt WebP support,
spawn-mode shared memory creation/unlink, and all 13 rembg session-class
resolutions.

Measured output sizes were:

| Output | Location | Size |
|---|---|---:|
| Unsigned app | `dist/MatteLoop.app` | 320,755,322 bytes (305.9 MiB) |
| Complete source archive | `dist/MatteLoop-media-sources-macos-arm64-824842398768745fa5e6e346.tar.gz` | 23,715,255 bytes |
| Source checksum | the adjacent `.sha256` | 134 bytes |
| Verified media wheel | the identity cache's `finished/` directory | 11,917,635 bytes |
| Provenance | adjacent `*.provenance.json` | 316 bytes |
| Verifier report | `finished/verification-report.json` | 14,284 bytes |
| Artifact-set binding | adjacent `*.artifact-set.json` | 890 bytes |

The archive checksum passed `shasum -a 256 -c`. This qualification covers the
unsigned macOS artifact only. Windows x64, workflow execution, signing,
notarization, upload, release creation, publication, permanent source hosting,
Gatekeeper distribution behavior, and SmartScreen behavior remain unclaimed
or unexercised.
