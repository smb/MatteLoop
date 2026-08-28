"""Command-line entry points for rembgGUI."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from rembggui import __version__


def main(argv: Sequence[str] | None = None) -> int:
    """Run rembgGUI, handling headless diagnostics before Qt is imported."""
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
        print("smoke: ok")
        return 0

    print("The graphical application is not available in this scaffold yet.")
    return 0
