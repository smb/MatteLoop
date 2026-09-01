"""Command-line entry points for MatteLoop."""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path

from matteloop import __version__


def _run_gui() -> int:
    """Lazily import Qt only for the normal graphical launch path."""
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    from matteloop.core.execution_providers import provider_options_from_runtime
    from matteloop.core.parameters import V1_MODEL_IDS
    from matteloop.core.state import AppState, ModelAvailabilityChanged
    from matteloop.logs import configure_logging
    from matteloop.ui.controller import SourceController
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
    install_theme(application)
    settings = QSettings()
    stored_model = settings.value("parameters/model_id")
    model_id = stored_model if stored_model in V1_MODEL_IDS else "birefnet-portrait"
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
    application.aboutToQuit.connect(controller.shutdown)
    window.show()
    return application.exec()


def main(argv: Sequence[str] | None = None) -> int:
    """Run MatteLoop, handling headless diagnostics before Qt is imported."""
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(prog="matteloop")
    parser.add_argument("--version", action="store_true", help="show the version")
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="verify the executable's headless startup surface",
    )
    args = parser.parse_args(argv)

    if args.version:
        print(f"MatteLoop {__version__}")
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
