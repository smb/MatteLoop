from __future__ import annotations

import logging
from pathlib import Path

import pytest

from matteloop.logs import configure_logging, log_file


@pytest.fixture(autouse=True)
def _detach_handlers() -> None:
    root = logging.getLogger()
    before = list(root.handlers)
    level = root.level
    yield
    for handler in list(root.handlers):
        if handler not in before:
            handler.close()
            root.removeHandler(handler)
    root.setLevel(level)


def test_configured_logging_records_a_failure_a_frozen_build_cannot_print(
    tmp_path: Path,
) -> None:
    target = tmp_path / "logs" / "matteloop.log"

    assert configure_logging(target) == target
    logging.getLogger("matteloop.test").error("preview failed: %s", "tls handshake")

    assert "preview failed: tls handshake" in target.read_text(encoding="utf-8")


def test_logging_can_be_switched_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MATTELOOP_LOG_LEVEL", "off")
    target = tmp_path / "matteloop.log"

    assert configure_logging(target) is None
    assert not target.exists()


def test_an_unwritable_destination_never_stops_the_application(
    tmp_path: Path,
) -> None:
    blocked = tmp_path / "file"
    blocked.write_text("not a directory", encoding="utf-8")

    assert configure_logging(blocked / "nested" / "matteloop.log") is None


def test_log_file_lives_beside_the_products_other_user_data() -> None:
    assert log_file().name == "matteloop.log"
    assert "matteloop" in str(log_file()).casefold()
