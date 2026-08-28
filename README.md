# rembgGUI

rembgGUI is a cross-platform desktop application for previewing and removing
video backgrounds, then exporting lossless transparent animated WebP files.

## Development

This project requires CPython 3.13. Install its locked dependencies with:

```sh
uv sync --all-groups
```

The two commands below are headless: they do not initialize Qt or download a
model, so they are safe for CI and smoke checks.

```sh
uv run rembggui --version
uv run rembggui --smoke-test
```

Run the checks with:

```sh
uv run ruff check .
uv run mypy src
QT_QPA_PLATFORM=offscreen uv run pytest -q
```

Model weights and generated workspaces are intentionally local and are never
committed.

### Synthetic fixture rotation

`tests.fixtures.media_factory.make_video(..., rotation=...)` writes a sorted,
versioned adjacent sidecar named `<video>.rembggui.json`. It records the
counter-clockwise presentation rotation as `rotation_ccw`. The locked PyAV 16
wheel cannot author portable MP4 display-matrix metadata, so future synthetic
source-decoder tests must consume this explicit fixture contract instead of
expecting a display matrix in the video container.
