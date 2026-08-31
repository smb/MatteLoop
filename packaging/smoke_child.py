"""Locate a native bundle and require its offline smoke JSON to pass."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from matteloop.smoke_child import spawn_smoke_target

__all__ = ["find_frozen_executable", "main", "spawn_smoke_target"]


def find_frozen_executable(dist_dir: Path) -> Path:
    """Return the single expected executable from a pyside6-deploy output."""
    dist_dir = dist_dir.resolve()
    if sys.platform == "darwin":
        candidates = tuple(
            path
            for path in (dist_dir / "MatteLoop.app" / "Contents" / "MacOS").glob("*")
            if path.is_file() and os.access(path, os.X_OK)
        )
    else:
        suffix = ".exe" if os.name == "nt" else ""
        bundle = dist_dir / "MatteLoop.dist"
        preferred = bundle / f"matteloop{suffix}"
        fallback = bundle / f"__main__{suffix}"
        candidates = tuple(path for path in (preferred, fallback) if path.is_file())
    if len(candidates) != 1:
        rendered = ", ".join(str(path) for path in candidates) or "none"
        raise RuntimeError(f"expected one frozen executable, found: {rendered}")
    return candidates[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dist_dir", type=Path)
    args = parser.parse_args(argv)
    executable = find_frozen_executable(args.dist_dir)
    environment = os.environ | {"QT_QPA_PLATFORM": "offscreen"}
    completed = subprocess.run(
        [str(executable), "--smoke-test"],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )
    try:
        payload = json.loads(completed.stdout.strip())
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"frozen smoke did not emit JSON; stderr={completed.stderr!r}"
        ) from error
    if completed.returncode != 0 or payload.get("ok") is not True:
        raise RuntimeError(f"frozen smoke failed: {payload!r}")
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
