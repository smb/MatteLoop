"""Module entry point for ``python -m matteloop``."""

from __future__ import annotations

import sys

from matteloop.app import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
