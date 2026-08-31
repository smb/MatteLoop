from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


def _entrypoint_module() -> ModuleType:
    path = Path(__file__).parents[1] / "packaging" / "entrypoint.py"
    spec = importlib.util.spec_from_file_location("test_packaged_entrypoint", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_resource_tracker_interpreter_arguments_are_consumed() -> None:
    module = _entrypoint_module()
    argv = [
        "matteloop",
        "-S",
        "-s",
        "-c",
        "from multiprocessing.resource_tracker import main;main(7)",
    ]

    payload = module._prepare_multiprocessing_payload(argv)

    assert payload == "from multiprocessing.resource_tracker import main;main(7)"
    assert argv == ["matteloop"]


def test_non_multiprocessing_interpreter_payload_is_rejected() -> None:
    module = _entrypoint_module()
    argv = ["matteloop", "-c", "print('arbitrary code')"]

    with pytest.raises(ValueError, match="refusing to execute"):
        module._prepare_multiprocessing_payload(argv)

    assert argv == ["matteloop", "-c", "print('arbitrary code')"]


def test_application_arguments_without_interpreter_code_are_preserved() -> None:
    module = _entrypoint_module()
    argv = ["matteloop", "--version"]

    payload = module._prepare_multiprocessing_payload(argv)

    assert payload is None
    assert argv == ["matteloop", "--version"]
