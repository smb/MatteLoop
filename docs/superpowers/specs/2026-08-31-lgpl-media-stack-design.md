# MatteLoop LGPL Media Stack Design

Status: approved in chat on 2026-08-31

## Context

MatteLoop uses PyAV for video input and animated WebP output. PyAV is the
Python binding; its bundled FFmpeg and libwebp libraries perform the native
decode, pixel conversion, container, and WebP encoding work.

The stock PyAV 16.1.0 wheels bundle a broad FFmpeg build whose native
dependency graph includes `libx264` and `libx265`. MatteLoop's source can stay
under 0BSD, but a native artifact intended for publication must not inherit
those GPL components. The release build therefore needs a reproducible,
LGPL-compatible PyAV wheel built against a deliberately limited FFmpeg stack.

The existing packager already provides the integration seam: it excludes
`av` from Nuitka's import following and copies the selected PyAV package tree
into the standalone application. This design changes which package tree is
copied; it does not change MatteLoop's runtime media pipeline.

## Goals

- Build the native media stack from pinned source on macOS arm64 and Windows
  x64, the two V1 artifact targets.
- Preserve H.264 and H.265 input decoding, MP4/MOV input, and lossless animated
  alpha WebP output.
- Keep FFmpeg and libwebp dynamically linked and independently identifiable in
  the application bundle.
- Make local `scripts/build.py` and the manually dispatched GitHub Actions
  workflow use the same builder and verification code.
- Cache the finished media wheel without trusting the cache: every reuse is
  re-verified.
- Produce the source, configuration, checksums, changes, notices, and licence
  material needed to accompany the native artifact.
- Fail before packaging if the media stack cannot be proven free of GPL and
  non-free components.

## Non-goals

- Linux artifacts, macOS x64, universal binaries, signing, notarization, and
  Windows signing remain outside V1.
- The normal source-development environment may continue using the locked
  upstream PyAV wheel. Only a distributable native artifact is required to use
  the custom wheel.
- The build does not add H.264 or H.265 encoding to MatteLoop.
- The build does not replace PyAV with AVFoundation, Media Foundation, a media
  CLI, or a second runtime code path.
- Publication and permanent hosting of a GitHub Release remain a separate,
  explicitly authorized action. This work produces qualified uploadable
  artifacts only.

## Source and binary contract

One checked-in manifest records exact source URLs and SHA-256 digests. The
initial stack is:

| Component | Initial version | Role |
|---|---:|---|
| PyAV | 16.1.0 | Python bindings |
| FFmpeg | 8.0.1 | demux, decode, conversion, WebP mux/encode |
| libwebp | 1.6.0 | lossless animated WebP implementation |

Version changes require an explicit manifest and checksum update. There are
no floating `latest` downloads.

FFmpeg is built with shared libraries, programs and documentation disabled,
external-library autodetection disabled, and libwebp explicitly enabled. The
configuration must not enable GPL, non-free, x264, x265, or OpenH264
components. Disabling autodetection prevents libraries installed on a build
runner from silently changing the resulting dependency graph.

PyAV requires the shared FFmpeg libraries `libavcodec`, `libavdevice`,
`libavfilter`, `libavformat`, `libavutil`, `libswresample`, and `libswscale`.
They remain separate `.dylib` or `.dll` files in the repaired wheel and final
application. libwebp remains a separate dynamically linked library as well.

## Build components

### Manifest

`packaging/media-stack/manifest.toml` is the single source of truth for:

- source versions, URLs, and SHA-256 digests;
- required FFmpeg configuration tokens and forbidden tokens;
- required PyAV codecs and formats;
- Python ABI and supported platform/architecture pairs;
- pinned wheel-build and wheel-repair tool versions.

The manifest is data only. It cannot inject arbitrary shell commands.

### Builder

`scripts/build_media_stack.py` exposes one cross-platform command. It:

1. validates the host, Python ABI, manifest, TLS, and native toolchain;
2. downloads missing source archives into the local source cache;
3. verifies every archive before extracting it;
4. builds and installs shared libwebp into an isolated staging prefix;
5. builds and installs shared FFmpeg against only that prefix;
6. builds the pinned PyAV source distribution against the staged FFmpeg;
7. repairs the wheel with `delocate` on macOS or `delvewheel` on Windows;
8. invokes the verifier and only then places the wheel in the finished cache;
9. creates a compliance archive from the exact inputs and recorded build
   metadata.

Temporary and partially built outputs never become cache hits. A failed build
leaves diagnostics in its explicitly named work directory but cannot replace a
previous verified wheel.

On Windows, the workflow uses MSYS2 and the Visual Studio 2022 toolchain. This
follows FFmpeg's supported MSVC build path and produces DLLs compatible with
the CPython/PyAV extension build. On macOS, the deployment target is macOS 13
arm64 and the Xcode command-line toolchain is used.

### Verifier

`scripts/verify_media_stack.py` verifies a wheel in an isolated temporary
environment. It does not inspect the `av` package installed in the project's
development environment.

The verifier checks:

- wheel name, PyAV version, CPython ABI, operating system, and architecture;
- source-manifest fingerprint embedded beside the wheel;
- FFmpeg's runtime `library_meta` configuration and licence strings;
- absence of `--enable-gpl`, `--enable-nonfree`, x264, x265, and OpenH264;
- absence of forbidden library filenames and native dependency edges;
- presence of the H.264 and HEVC decoders;
- presence of the MOV/MP4 and WebP formats and the `libwebp_anim` encoder;
- decoding of committed tiny H.264 and H.265 fixtures through MatteLoop's
  production source path;
- encoding and reopening a two-frame lossless alpha WebP through MatteLoop's
  production WebP path.

macOS dependency inspection uses `delocate`/`otool`; Windows uses
`delvewheel` and the Visual Studio binary inspection tools. The check examines
the transitive dependency graph, not only filenames.

## Local native build integration

`scripts/build.py` gains a media-stack preparation phase before invoking
`pyside6-deploy`:

1. ask `build_media_stack.py` to ensure the wheel identified by the current
   manifest exists;
2. verify that wheel even on a cache hit;
3. extract it into the existing temporary build directory;
4. pass that extracted `av` directory to `prepare_temporary_spec`;
5. build the Nuitka standalone bundle as it does today;
6. inspect the finished bundle again for forbidden native libraries;
7. run the packaged offline smoke test;
8. copy the compliance archive and its checksum beside the native artifact.

The stock PyAV package in `.venv` is never an eligible fallback for a native
release build. A missing compiler, failed download, invalid checksum, or failed
licence check stops the build with an actionable message rather than silently
producing a differently licensed artifact.

The untracked local cache lives below `.matteloop-build-cache/media-stack/`.
Its identity includes the manifest contents, builder version, operating
system, architecture, CPython ABI, and deployment target. A command-line
override may force a rebuild or select a wheel for diagnosis, but cannot skip
verification.

## GitHub Actions integration

The existing manually dispatched release matrix remains the only native
workflow. Each macOS arm64 and Windows x64 job performs:

1. checkout and pinned Python/uv setup;
2. target-specific native toolchain setup, including MSYS2 on Windows;
3. restore the source and verified-wheel cache keyed by the full media-stack
   identity;
4. run the media-stack ensure-and-verify command;
5. run the normal MatteLoop native build;
6. run the final bundle scan and packaged smoke test;
7. upload the unsigned application and matching compliance archive.

The workflow never downloads a MatteLoop-maintained binary wheel from a
separate release. The source build and application build therefore share one
run, one manifest, and one evidence trail. A cache hit saves compilation time
but executes all verification gates again.

GitHub Actions references remain pinned to immutable commit hashes. Ordinary
pull-request CI does not compile FFmpeg; unit tests cover the manifest,
command construction, cache identity, wheel selection, and failure behavior.

## Smoke and codec fixtures

The packaged smoke test currently creates its source using `libx264`. That
encoder is intentionally absent from the custom stack. The smoke generator is
changed to FFmpeg's built-in LGPL MPEG-4 encoder so it can continue proving the
packaged demux/decode boundary without a GPL encoder.

Two tiny deterministic source fixtures separately prove the advertised codec
contract:

- MP4 with 8-bit SDR H.264 video;
- MP4 with 8-bit SDR H.265 video.

Their authored frames, generation recipe, licence, and SHA-256 digests are
recorded beside the fixtures. The fixture files remain test inputs and are
excluded from the application bundle. This keeps the offline packaged smoke
self-contained while ensuring the custom wheel is tested against both V1
source codecs during the native build.

## Compliance output

Every successful target build produces a matching target-specific compliance
archive containing:

- the exact PyAV, FFmpeg, and libwebp source archives;
- the manifest and all source checksums;
- the effective FFmpeg configure line and compiler/toolchain versions;
- `changes.diff` for FFmpeg and any patches applied to other components;
- PyAV, FFmpeg LGPL, libwebp, and wheel-repair-tool licence texts;
- the generated native dependency inventory;
- a short rebuild instruction for both target platforms.

The application bundle continues to contain MatteLoop's `LICENSE` and
`THIRD_PARTY_NOTICES.md`. Before an artifact is published, its download surface
must name FFmpeg and link the matching source compliance archive. An expiring
GitHub Actions artifact alone is not permanent source hosting; a later
authorized GitHub Release must upload the application and matching compliance
archive together.

MatteLoop's own project licence remains 0BSD. LGPL obligations apply to the
dynamically linked FFmpeg libraries and their distribution material, not to a
relicensing of MatteLoop's Python source.

Codec patent questions are independent of the FFmpeg copyright licence. This
design removes GPL components but does not make a legal conclusion about
H.264 or H.265 patent requirements in a particular jurisdiction or commercial
distribution model.

## TLS and network behavior

Downloads use normal certificate verification and honor `SSL_CERT_FILE` and
the host trust configuration. They never disable TLS verification. On the
`/opt/homebrew/etc/ca-certificates/cert.pem`, which contains

Checksums remain mandatory even when TLS succeeds. A download or checksum
failure reports the component and expected digest and stops before extraction.

## Testing

Ordinary CI covers:

- strict manifest parsing and rejected unknown platforms;
- deterministic cache fingerprints;
- pinned URL and checksum handling;
- platform command construction;
- custom-wheel selection with no stock-wheel fallback;
- required/forbidden configuration evaluation;
- final-bundle forbidden-name scanning;
- smoke generation without x264/x265;
- existing native build and workflow structure contracts.

The manually dispatched native workflow additionally covers real source
compilation, repaired-wheel import, transitive native dependencies, H.264 and
H.265 decode, animated alpha WebP encode/reopen, Nuitka packaging, and the
frozen smoke test on both target operating systems.

Repository changes are verified with the required project command:

```sh
uv run ruff check . && uv run mypy src && QT_QPA_PLATFORM=offscreen uv run pytest -q
```

## Acceptance criteria

- `scripts/build.py` produces no native artifact from the stock PyAV wheel.
- A second local build reuses the cached custom wheel and re-runs verification.
- The macOS arm64 and Windows x64 workflow jobs build from pinned sources and
  upload an application plus the matching compliance archive.
- FFmpeg runtime metadata and the transitive binary graph contain no GPL,
  non-free, x264, x265, or OpenH264 component.
- The custom wheel decodes the committed H.264 and H.265 fixtures.
- MatteLoop still creates and validates a lossless animated transparent WebP.
- The packaged offline smoke test passes without any external media CLI,
  model download, or GPL encoder.
- The complete existing lint, type, and test suite passes.
