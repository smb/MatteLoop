"""Command-line entry points for MatteLoop."""

from __future__ import annotations

import argparse
import json
import logging
import multiprocessing
import os
import platform
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from importlib import metadata
from pathlib import Path
from typing import Any

from matteloop import __version__

_LOGGER = logging.getLogger(__name__)
_ONNXRUNTIME_DISTRIBUTIONS = (
    "onnxruntime-directml",
    "onnxruntime-gpu",
    "onnxruntime",
)


def _run_gui() -> int:
    """Lazily import Qt only for the normal graphical launch path."""
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    from matteloop.core.execution_providers import provider_options_from_runtime
    from matteloop.core.parameters import V1_MODEL_IDS
    from matteloop.core.state import AppState, ModelAvailabilityChanged
    from matteloop.logs import configure_logging
    from matteloop.ui.controller import SourceController
    from matteloop.ui.i18n import (
        configure_locale,
        install_translators,
        selected_language,
    )
    from matteloop.ui.main_window import MainWindow
    from matteloop.ui.preferences import load_parameters
    from matteloop.ui.store import ReducerStore
    from matteloop.ui.theme import install_theme

    configure_logging()
    application = QApplication.instance()
    if not isinstance(application, QApplication):
        application = QApplication(["matteloop"])
    application.setApplicationName("MatteLoop")
    application.setApplicationDisplayName("MatteLoop")
    application.setOrganizationName("MatteLoop")
    application.setApplicationVersion(__version__)
    settings = QSettings()
    language = selected_language(settings)
    configure_locale(language)
    _translators = install_translators(application, language)
    install_theme(application)
    stored_model = settings.value("parameters/model_id")
    model_id = stored_model if stored_model in V1_MODEL_IDS else "birefnet-portrait"
    _log_runtime_diagnostics()
    provider_options = provider_options_from_runtime(model_id=model_id)
    store = ReducerStore(
        AppState(parameters=load_parameters(settings, provider_options))
    )
    controller = SourceController(store, settings=settings, parent=application)
    availability = dict(controller.model_options).get(
        store.state.parameters.model_id, False
    )
    store.dispatch(ModelAvailabilityChanged(availability))
    window = MainWindow(
        store,
        controller,
        settings,
        model_options=controller.model_options,
        provider_options=provider_options,
    )
    controller.set_dialog_parent(window)
    controller.attach_transform_stage(
        window.inspector.transform_group, window.result_canvas
    )
    application.aboutToQuit.connect(controller.shutdown)
    window.show()
    return application.exec()


def _load_onnxruntime() -> object:
    import onnxruntime  # type: ignore[import-untyped]

    return onnxruntime


def _onnxruntime_flavor(device: str) -> str:
    if "DML" in device:
        return "onnxruntime-directml"
    if "GPU" in device:
        return "onnxruntime-gpu"
    return "onnxruntime"


def _onnxruntime_distribution() -> tuple[str, str]:
    for distribution in _ONNXRUNTIME_DISTRIBUTIONS:
        try:
            return distribution, metadata.version(distribution)
        except metadata.PackageNotFoundError:
            continue
    # Frozen bundles (Nuitka standalone) carry no dist-info for onnxruntime,
    # so fall back to facts read straight off the loaded module.
    try:
        import onnxruntime

        version = str(onnxruntime.__version__)
        device = str(onnxruntime.get_device())
    except Exception:
        return "none", "none"
    return _onnxruntime_flavor(device), version


def _error_reason(error: BaseException) -> str:
    return str(error) or type(error).__name__


def _log_runtime_diagnostics() -> None:
    try:
        runtime = _load_onnxruntime()
        distribution, distribution_version = _onnxruntime_distribution()
        available_providers = tuple(runtime.get_available_providers())  # type: ignore[attr-defined]
        device = runtime.get_device()  # type: ignore[attr-defined]
    except Exception as error:
        _LOGGER.warning("Could not collect ONNX Runtime startup diagnostics: %s", error)
        return
    _LOGGER.info(
        "ONNX Runtime distribution=%s version=%s device=%s available_providers=%s",
        distribution,
        distribution_version,
        device,
        available_providers,
    )


def _runtime_diagnostic_lines(
    runtime: object | None, runtime_error: str | None
) -> tuple[str, ...]:
    from matteloop.core.execution_providers import (
        ProviderOption,
        provider_options_from_runtime,
    )

    if runtime is None:
        unavailable = runtime_error or "runtime unavailable"
        return (
            f"onnxruntime device: unavailable ({unavailable})",
            f"onnxruntime available providers: unavailable ({unavailable})",
            f"provider options: unavailable ({unavailable})",
        )

    try:
        device = str(getattr(runtime, "get_device")())
    except Exception as error:
        device = f"unavailable ({_error_reason(error)})"
    try:
        available_providers = tuple(
            str(provider) for provider in getattr(runtime, "get_available_providers")()
        )
        available_text = repr(list(available_providers))
    except Exception as error:
        available_text = f"unavailable ({_error_reason(error)})"
    options: tuple[ProviderOption, ...] = ()
    options_error: str | None = None
    try:
        options = provider_options_from_runtime(runtime)
    except Exception as error:
        options_error = _error_reason(error)
    lines = [
        f"onnxruntime device: {device}",
        f"onnxruntime available providers: {available_text}",
    ]
    if options_error is not None:
        lines.append(f"provider options: unavailable ({options_error})")
    elif options:
        lines.append("provider options:")
        lines.extend(
            f"  {option.provider}: {option.label}"
            + (" [recommended]" if option.recommended else "")
            for option in options
        )
    else:
        lines.append("provider options: none")
    return tuple(lines)


def _collect_provider_diagnostics(runtime: object | None = None) -> tuple[str, ...]:
    """Collect headless runtime facts without creating an ONNX session."""
    try:
        distribution, distribution_version = _onnxruntime_distribution()
        distribution_text = (
            "none"
            if distribution == "none"
            else f"{distribution} {distribution_version}"
        )
    except Exception as error:
        distribution_text = f"unavailable ({_error_reason(error)})"
    lines = [
        f"MatteLoop version: {__version__}",
        f"Python/platform: {platform.python_version()} / {platform.platform()}",
        f"ONNX Runtime distribution: {distribution_text}",
    ]
    runtime_error: str | None = None
    if runtime is None:
        try:
            runtime = _load_onnxruntime()
        except Exception as error:
            runtime_error = _error_reason(error)
            lines.append(f"onnxruntime: unavailable ({runtime_error})")
    lines.extend(_runtime_diagnostic_lines(runtime, runtime_error))
    if sys.platform == "win32":
        lines.extend(_video_adapter_diagnostics())
    return tuple(lines)


def _video_adapter_diagnostics() -> tuple[str, ...]:
    powershell = os.path.join(
        os.environ.get("SystemRoot", r"C:\Windows"),
        "System32",
        "WindowsPowerShell",
        "v1.0",
        "powershell.exe",
    )
    command = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
        "$ErrorActionPreference='Stop'; "
        "Get-CimInstance Win32_VideoController | Select-Object Name,DriverVersion"
    )
    run_kwargs: dict[str, Any] = {
        "capture_output": True,
        "check": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": 10,
    }
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", None)
    if creationflags is not None:
        run_kwargs["creationflags"] = creationflags
    try:
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
            **run_kwargs,
        )
        output = " ".join(result.stdout.split())
        return (f"video adapters: {output or 'none'}",)
    except Exception as error:
        return (f"video adapters: unavailable ({_error_reason(error)})",)


def main(argv: Sequence[str] | None = None) -> int:
    """Run MatteLoop, handling headless diagnostics before Qt is imported."""
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(prog="matteloop")
    parser.add_argument("--version", action="store_true", help="show the version")
    parser.add_argument(
        "--providers",
        action="store_true",
        help="show the installed ONNX Runtime providers",
    )
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="verify the executable's headless startup surface",
    )
    args = parser.parse_args(argv)

    if args.version:
        print(f"MatteLoop {__version__}")
        return 0
    if args.providers:
        for line in _collect_provider_diagnostics():
            print(line)
        return 0
    if args.smoke_test:
        from matteloop.smoke import run_smoke

        configured_work_dir = os.environ.get("MATTELOOP_SMOKE_WORK_DIR")
        try:
            if configured_work_dir:
                result = run_smoke(Path(configured_work_dir), use_fake_model=True)
            else:
                with tempfile.TemporaryDirectory(prefix="matteloop-smoke-cli-") as raw:
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

    return _run_gui()
