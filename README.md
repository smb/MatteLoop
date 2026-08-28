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
