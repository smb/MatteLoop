# MatteLoop

MatteLoop cuts the background out of a video on your own machine and gives you
back a transparent animated WebP that loops.

![The MatteLoop main window: a source video on the left with a crop rectangle,
the cut-out result on the right over a transparency checkerboard, and the
inspector on the far right](assets/screenshots/main-window.png)

Nothing leaves your computer. The video, the model weights, every intermediate
frame and the finished loop stay on your disk — there is no account, no upload,
and no network call except the one that fetches a model weight the first time
you use it.

## What it is for

You have a clip of a subject — a person, a pet, a product — and you want it
without its background, as a loop you can drop onto any page or into any
composite. MatteLoop is the whole path from that clip to that loop:

- **Open a local video** and scrub to the part you want.
- **Pick the frames** with IN and OUT points on the timeline.
- **Preview a single frame** to judge the cutout before committing to a render.
- **Trim, crop and resize** the finished cut, and watch it loop on screen until
  it is right.
- **Render** a lossless transparent animated WebP.
- **Open Preferences** from the gear button to choose or clear the output
  directory.

The result is lossless and genuinely transparent — an alpha channel, not a
matte painted onto a colour.

### Judge the loop before you render it

The transform stage works on the cut you already have. Trimming, cropping and
resizing never touch the stored frames; they are a specification applied when
the file is encoded, so you can change your mind as often as you like without
segmenting anything again.

![The inspector's Transform section expanded, showing trim first and last
frame, crop position and size, and resize width, height and
percent](assets/screenshots/transform-expanded.png)

### Choose the model that suits the shot

Different subjects want different weights. MatteLoop ships a catalog of them
and downloads only what you ask for, so a first run costs one model rather than
the whole set. The manager shows what is on disk, what a weight costs, and
which weights are left over from an earlier version of the segmentation
runtime.

![The model manager listing the model catalog with size, cache status and which
model is active, and buttons to download, remove or re-download outdated
weights](assets/screenshots/model-manager.png)

Weights are not bundled with the application. They download on first use and
are cached locally; `birefnet-portrait` alone is about 928 MiB and the complete
catalog is about 6.35 GiB, which is exactly why you fetch them one at a time.

## Built on rembg

The segmentation in MatteLoop is [rembg](https://github.com/danielgatis/rembg)
by Daniel Gatis. The model catalog, the session classes that run each
architecture, and the whole approach to background removal come from that
project, and the weights MatteLoop downloads are its release artifacts, listed
with their checksums in `resources/model-manifest.json`. MatteLoop is the
desktop application around it: the timeline, the preview, the transform stage
and the WebP encoder.

The authors of the individual model architectures behind those weights are
credited in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md), along with every
other component and its licence.

## Getting it

Native builds for macOS 15+ arm64 and Windows x64 are published on the
[releases page](https://github.com/smb-org/MatteLoop/releases). They are unsigned:
Windows warns through SmartScreen, and macOS needs a right-click Open the first
time. Linux artifacts are deferred, and the current native qualification covers
macOS 15+ arm64 only.

## Run from source

Use CPython 3.13 and install the locked environment with:

```sh
uv sync --frozen --all-groups
uv run matteloop
```

For quick diagnostics, `uv run matteloop --version` avoids Qt and model access;
`uv run matteloop --smoke-test` exercises the local runtime without downloading
weights; and `uv run matteloop --providers` reports which ONNX Runtime
execution providers the installed runtime actually offers.

Run the checks with:

```sh
uv run ruff check .
uv run mypy src
QT_QPA_PLATFORM=offscreen uv run pytest -q
```

Model weights and generated workspaces are intentionally local and are never
committed.

The screenshots above are generated rather than collected, so a layout change
never leaves them quietly out of date. Regenerate them with:

```sh
QT_QPA_PLATFORM=offscreen uv run python scripts/screenshots.py
```

It decodes `assets/demo/golden-retriever.mp4`, segments it with
`birefnet-general-lite` — which has to be in the local model cache already —
and rewrites `assets/screenshots/`.

Native builds automatically compile or reuse a source-pinned, verified LGPL
FFmpeg/libwebp/PyAV wheel; the stock PyAV wheel is not an eligible packaging
fallback. They also create a checksum-verified Qt/PySide 6.10.3 source
companion containing the exact official Qt Base, Qt Image Formats, and PySide
Setup archives, package inventory, complete GPL/LGPL texts, and replacement
instructions. See [docs/building.md](docs/building.md) for local and manual
Actions commands, the five inseparable distribution outputs, unsigned-artifact
launch warnings, and current platform qualification status.

Notes for people writing decoder tests live in
[docs/testing-fixtures.md](docs/testing-fixtures.md).

## License

MatteLoop's original source code, documentation, and visual assets are licensed
under the [Zero-Clause BSD license](LICENSE), to the extent copyright or
related rights exist. You may use, copy, modify, and distribute them for any
purpose, with or without fee or attribution.

MatteLoop does not reserve separate project-controlled trademark restrictions
for its name or logo; they may be reused with the same freedom.

Third-party libraries, fonts, and model weights keep their own licenses. Model
weights download on first use and are not part of the MatteLoop distribution.
See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for the component and
native-release details. The full third-party [GPL version 3](legal/GPL-3.0.txt)
and [LGPL version 3](legal/LGPL-3.0.txt) texts, prominent
[Qt/PySide notice](legal/QT-PYSIDE-LGPL-NOTICE.md), and practical
[replacement instructions](legal/RELINK.md) are kept in the repository and
inside every native app; they do not change MatteLoop's 0BSD license.
