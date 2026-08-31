"""Native bundle entry point kept outside the importable application package."""

from __future__ import annotations

import re
import sys

_RESOURCE_TRACKER_PAYLOAD = re.compile(
    r"from multiprocessing\.resource_tracker import main;main\([0-9]+\)\Z"
)


def _prepare_multiprocessing_payload(argv: list[str]) -> str | None:
    """Return a safe multiprocessing payload and remove its interpreter args."""
    try:
        code_index = argv.index("-c")
    except ValueError:
        return None

    if code_index + 1 >= len(argv):
        raise ValueError("matteloop: interpreter -c argument is missing its code")

    payload = argv[code_index + 1]
    if _RESOURCE_TRACKER_PAYLOAD.fullmatch(payload) is None:
        raise ValueError(
            "matteloop: refusing to execute unsupported interpreter payload; "
            "only the multiprocessing resource-tracker bootstrap is supported"
        )

    del argv[1 : code_index + 2]
    return payload


if __name__ == "__main__":
    try:
        interpreter_payload = _prepare_multiprocessing_payload(sys.argv)
    except ValueError as error:
        raise SystemExit(str(error)) from None
    if interpreter_payload is not None:
        exec(interpreter_payload, {"__name__": "__main__"})
    else:
        from matteloop.app import main

        raise SystemExit(main())
