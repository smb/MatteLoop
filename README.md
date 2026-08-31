# MatteLoop

MatteLoop is a desktop app for previewing local videos, removing their
backgrounds, and exporting lossless transparent animated WebP files. V1
targets macOS 13+ arm64 and produces an unsigned Windows x64 artifact; Linux
artifacts remain deferred.

## Run from source

Use CPython 3.13 and install the locked environment with:

```sh
uv sync --frozen --all-groups
uv run matteloop
```

The 13 V1 models come from the upstream rembg release artifacts listed in
`resources/model-manifest.json`. They download on first use, are cached
locally, and are not bundled: `birefnet-portrait` is about 928 MiB and the
complete V1 catalog is about 6.35 GiB.

For quick diagnostics, `uv run matteloop --version` avoids Qt and model access;
`uv run matteloop --smoke-test` exercises the local runtime without downloading
weights.

Run the checks with:

```sh
uv run ruff check .
uv run mypy src
QT_QPA_PLATFORM=offscreen uv run pytest -q
```

Model weights and generated workspaces are intentionally local and are never
committed.

New installs use the MatteLoop cache and workspace names. Existing model
weights, compiled provider caches and thumbnails under
`~/Library/Caches/rembggui/` and promoted cuts in an output directory's
`.rembggui-work/` remain discoverable; when both old and new locations exist,
the new MatteLoop location is preferred.

See [docs/building.md](docs/building.md) for native macOS and Windows builds,
unsigned-artifact launch warnings, and packaging verification status.

### Synthetic fixture rotation

`tests.fixtures.media_factory.make_video(..., rotation=...)` writes a sorted,
versioned adjacent sidecar named `<video>.matteloop.json`. It records the
counter-clockwise presentation rotation as `rotation_ccw`. The locked PyAV 16
wheel cannot author portable MP4 display-matrix metadata, so future synthetic
source-decoder tests must consume this explicit fixture contract instead of
expecting a display matrix in the video container.
