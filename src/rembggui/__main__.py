"""Module entry point for ``python -m rembggui``."""

from __future__ import annotations

import sys

from rembggui.app import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
