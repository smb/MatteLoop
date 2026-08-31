# Third-party notices

MatteLoop's original source code, documentation, and visual assets are
available under the [Zero-Clause BSD license](LICENSE), to the extent that
copyright or related rights exist. That license does not replace the licenses
of the third-party material described below.

This file documents both the source distribution and the delivered native
build contract. Each native build produces a target-specific complete source
archive containing the exact media sources, build evidence, license texts, and
notices for the verified platform artifact.

## Material stored in this repository

### IBM Plex fonts

The IBM Plex Sans and IBM Plex Mono font files under `resources/fonts/` are
licensed under the SIL Open Font License 1.1. The complete license accompanies
the fonts in [`resources/fonts/OFL.txt`](resources/fonts/OFL.txt).

Source: <https://github.com/IBM/plex>

### MatteLoop visual assets

The status icons under `resources/icons/` were created for MatteLoop and are
covered by 0BSD.

The MatteLoop application icon and UI mark under `assets/branding/matteloop/`
were generated with Grok through MuAPI and then selected and finished for this
project. They are covered by 0BSD to the extent copyright or related rights
exist. Generation details, prompts, transformations, and checksums are retained
in [`assets/branding/matteloop/README.md`](assets/branding/matteloop/README.md).

MatteLoop contributors do not reserve separate project-controlled trademark
restrictions for the MatteLoop name or logo. To the extent such rights exist
and can be licensed, permission is granted to use them for any purpose without
fee.

## Runtime dependencies

The Python source distribution declares dependencies rather than copying their
source into MatteLoop. Native application bundles may contain their binaries
and must preserve all applicable notices.

| Component | License used by MatteLoop | Source |
|---|---|---|
| PySide6 / Qt for Python | LGPL-3.0-only community option; other upstream options are GPL or commercial | <https://doc.qt.io/qtforpython-6/licenses.html> |
| PyAV | BSD-3-Clause; native artifacts use the verified source build described below | <https://github.com/PyAV-Org/PyAV> |
| FFmpeg | LGPL-2.1-or-later build; GPL and non-free components are disabled | <https://ffmpeg.org/legal.html> |
| libwebp | BSD-3-Clause | <https://chromium.googlesource.com/webm/libwebp> |
| Pillow | MIT-CMU | <https://github.com/python-pillow/Pillow> |
| NumPy | BSD-3-Clause, with separately licensed bundled components | <https://numpy.org/doc/stable/license.html> |
| rembg | MIT | <https://github.com/danielgatis/rembg> |
| ONNX Runtime | MIT | <https://github.com/microsoft/onnxruntime> |
| platformdirs | MIT | <https://github.com/tox-dev/platformdirs> |

The locked dependency graph is recorded in `uv.lock`. It includes transitive
packages beyond this summary. Release notices must be generated from the actual
artifact rather than inferred only from this table.

### Delivered LGPL media build

The stock PyAV 16.1.0 wheel currently selected by `uv.lock` bundles FFmpeg with
`libx264` and `libx265`; that stock wheel is not publishable in a MatteLoop
native artifact. The delivered packager instead compiles PyAV 16.1.0 against
shared FFmpeg 8.0.1 and libwebp 1.6.0 with GPL, non-free, and autodetected
components disabled. It verifies the FFmpeg configuration, LGPL library
licenses, `h264`, `hevc`, and `libwebp_anim` codecs, `mov` and `webp` formats,
native dependency inventory, and absence of forbidden components before the
wheel can be packaged.

Every successful target build creates
`MatteLoop-media-sources-<target>-<identity>.tar.gz`. It includes the exact
digest-verified FFmpeg, libwebp, PyAV, and Cython 3.3.0 source archives;
manifest and checksums; effective commands; compiler and tool versions;
licences; dependency inventory; provenance and verifier reports; any source
changes; and rebuild instructions. Its adjacent `.sha256` is published beside
the application artifact, while an artifact-set JSON in the build cache binds
all media evidence by digest and identity.

The manual GitHub Actions workflow only creates a temporary unsigned build
artifact. It does not publish, sign, notarize, create a release, or provide a
permanent source host. Any later authorized native publication must place the
matching complete source archive and checksum on a durable endpoint beside
each application artifact. This boundary does not prevent publishing
MatteLoop's own source under 0BSD.

The unsigned macOS arm64 artifact completed the repository, media verifier,
codec-fixture, bundle, checksum, and packaged smoke gates on 2026-09-01.
Windows x64 remains unqualified until its separately authorized native or
manual Actions run passes the same gates; the macOS result is not evidence for
the Windows artifact.

H.264 and H.265 patent or royalty obligations are separate from LGPL source
compliance and require their own distribution-specific assessment.

## Model weights downloaded at runtime

Model weights are not stored in this repository or bundled in native
artifacts. MatteLoop downloads a selected weight on first use from the pinned
URLs in `resources/model-manifest.json`. Those weights are not covered by
MatteLoop's 0BSD license.

The V1 catalog uses these upstream model families:

| MatteLoop model IDs | Upstream project |
|---|---|
| `u2net`, `u2netp`, `u2net_human_seg` | <https://github.com/xuebinqin/U-2-Net> |
| `silueta` | Source link recorded by the rembg model catalog: <https://github.com/danielgatis/rembg> |
| `isnet-general-use` | <https://github.com/xuebinqin/DIS> |
| `isnet-anime` | <https://github.com/SkyTNT/anime-segmentation> |
| `birefnet-general`, `birefnet-general-lite`, `birefnet-portrait`, `birefnet-dis`, `birefnet-hrsod`, `birefnet-cod`, `birefnet-massive` | <https://github.com/ZhengPeng7/BiRefNet> |

A source-code repository license does not by itself prove the license or
provenance of a separately published converted weight. Users should review the
terms linked by the upstream model publisher before use. Redistribution of the
downloaded weights is outside MatteLoop's source license.

`bria-rmbg` is excluded from the V1 application because its model-specific
terms require a separate commercial agreement for commercial use.
`u2net_cloth_seg` is also excluded from V1 and is not offered by the UI.

## No endorsement

The third-party names above identify upstream components and do not imply that
their authors or publishers endorse MatteLoop.
