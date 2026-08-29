"""Command-line entry points for rembgGUI."""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path

from rembggui import __version__


def main(argv: Sequence[str] | None = None) -> int:
    """Run rembgGUI, handling headless diagnostics before Qt is imported."""
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(prog="rembggui")
    parser.add_argument("--version", action="store_true", help="show the version")
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="verify the executable's headless startup surface",
    )
    args = parser.parse_args(argv)

    if args.version:
        print(f"rembgGUI {__version__}")
        return 0
    if args.smoke_test:
        from rembggui.smoke import run_smoke

        configured_work_dir = os.environ.get("REMBGGUI_SMOKE_WORK_DIR")
        try:
            if configured_work_dir:
                result = run_smoke(Path(configured_work_dir), use_fake_model=True)
            else:
                with tempfile.TemporaryDirectory(prefix="rembggui-smoke-cli-") as raw:
                    result = run_smoke(Path(raw), use_fake_model=True)
        except Exception as error:
            print(
                json.dumps(
                    {
                        "error": {
                            "message": str(error),
                            "type": type(error).__name__,
                        },
                        "ok": False,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            return 1
        print(
            json.dumps(
                {"ok": True, "result": result.to_primitives()},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0

    print("The graphical application is not available in this scaffold yet.")
    return 0
