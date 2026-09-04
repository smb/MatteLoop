"""Optional hang diagnostics for the UI test suite.

Issue #39: tests/ui occasionally hangs -- no output, no crash, no progress
-- rather than failing outright, and nobody has ever captured a stack from
one. MATTELOOP_TEST_HANG_DUMP_SECONDS arms faulthandler.dump_traceback_later()
around every test item, the same technique matteloop.logs already uses for
native crashes in the shipped app (see _enable_fault_reports there). It
mirrors pytest's own built-in faulthandler_timeout plugin
(_pytest/faulthandler.py): open the target file once for the whole session
rather than per test -- closing it while a dump from the previous test's
timer could still be in flight reproducibly segfaulted the interpreter here
-- and switch it by an environment variable rather than an ini option so it
can be turned on for a single measurement run without touching
pyproject.toml. Writing to a plain file instead of the duped stderr fd
means the dump survives however a caller redirects the subprocess's own
stdout/stderr.

Off by default: the normal suite is unaffected by this file.
"""

from __future__ import annotations

import faulthandler
import os
from collections.abc import Generator
from pathlib import Path
from typing import TextIO

import pytest

_SECONDS_ENV = "MATTELOOP_TEST_HANG_DUMP_SECONDS"
_FILE_ENV = "MATTELOOP_TEST_HANG_DUMP_FILE"
_DEFAULT_FILE = "tests-ui-hang-dump.log"

_dump_file: TextIO | None = None


def _configured_seconds() -> float | None:
    raw = os.environ.get(_SECONDS_ENV, "").strip()
    return float(raw) if raw else None


def pytest_configure(config: pytest.Config) -> None:
    global _dump_file
    if _configured_seconds() is None:
        return
    dump_path = Path(os.environ.get(_FILE_ENV, _DEFAULT_FILE))
    _dump_file = dump_path.open("a", buffering=1, encoding="utf-8")


def pytest_unconfigure(config: pytest.Config) -> None:
    global _dump_file
    if _dump_file is not None:
        _dump_file.close()
        _dump_file = None


@pytest.hookimpl(wrapper=True, trylast=True)
def pytest_runtest_protocol(item: pytest.Item) -> Generator[None, object, object]:
    seconds = _configured_seconds()
    if seconds is None or _dump_file is None:
        return (yield)
    _dump_file.write(f"\n--- armed for {item.nodeid} ({seconds}s) ---\n")
    faulthandler.dump_traceback_later(seconds, file=_dump_file, exit=False)
    try:
        return (yield)
    finally:
        faulthandler.cancel_dump_traceback_later()
