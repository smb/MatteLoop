"""Capture the authentic application states used in the README.

The source frame is decoded with MatteLoop's PyAV path and the cutout is
produced by the pinned rembg model. The window receives those genuine images
through a small immutable state snapshot instead of starting the asynchronous
preview job; the model dialog and all painted widgets are real application
widgets. The Transform section is seeded with the demo clip's real timeline
facts because no finished cut workspace is needed for this developer tool. The
model dialog reads a temporary cache containing one sparse catalog-sized
placeholder; its displayed location is a fixed fictional current-product path,
so it never reads or creates the capturing machine's model cache.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from fractions import Fraction
from pathlib import Path

# Keep generated English UI text independent of the host account's locale.
os.environ["LC_ALL"] = "C"
os.environ["LANG"] = "C"

from PIL import Image
from PySide6.QtCore import QLocale, QSettings
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QWidget

from matteloop.core.parameters import ParameterState
from matteloop.core.state import (
    AppState,
    PreviewRequested,
    PreviewResult,
    PreviewSucceeded,
    SourceLoaded,
    SourceLoadRequested,
    reduce,
)
from matteloop.jobs.models.catalog import ModelCatalog
from matteloop.jobs.source import decode_frame, probe_source
from matteloop.paths import model_cache_root
from matteloop.ui.main_window import MainWindow
from matteloop.ui.model_manager import ModelManagerDialog
from matteloop.ui.theme import install_theme
from matteloop.ui.transform_group import CutFacts

_MODEL_ID = "birefnet-general-lite"
_DEMO_CACHE_DISPLAY_ROOT = Path("/Users/example/Library/Caches/matteloop/models")
_OUTPUT_DIRECTORY = Path("/Users/example/Movies/MatteLoop")
_WINDOW_SIZE = (1440, 900)
_SOURCE_ID = "screenshot-source"
_LOAD_REQUEST_ID = "screenshot-load"
_PREVIEW_REQUEST_ID = "screenshot-preview"
_JOB_ID = "screenshot-job"


class _Store:
    def __init__(self, state: AppState) -> None:
        self.state = state
        self._listeners: list[Callable[[AppState], None]] = []

    def dispatch(self, event: object) -> None:
        del event

    def subscribe(self, listener: Callable[[AppState], None]) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)


class _Services:
    def dispatch(self, command: object) -> None:
        del command


def _qimage(image: Image.Image) -> QImage:
    rgba = image.convert("RGBA")
    return QImage(
        rgba.tobytes(),
        rgba.width,
        rgba.height,
        rgba.width * 4,
        QImage.Format.Format_RGBA8888,
    ).copy()


def _demo_images(source_path: Path) -> tuple[object, QImage, QImage]:
    metadata = probe_source(source_path)
    decoded = decode_frame(
        source_path,
        Fraction(0),
        1,
        expected_revision=metadata.revision,
        validation_proof=metadata.validation_proof,
    )
    try:
        source_image = decoded.image.convert("RGB")
        try:
            os.environ["U2NET_HOME"] = str(
                model_cache_root() / "2.0.75" / _MODEL_ID
            )
            from rembg import new_session, remove

            model_home = Path(os.environ["U2NET_HOME"])
            weight = model_home / f"{_MODEL_ID}.onnx"
            if not weight.is_file():
                raise FileNotFoundError(f"missing local model weight: {weight}")
            segmenter = new_session(_MODEL_ID)
            cutout = remove(source_image, session=segmenter)
            if not isinstance(cutout, Image.Image):
                raise TypeError("rembg returned a non-image cutout")
            return metadata, _qimage(source_image), _qimage(cutout)
        finally:
            source_image.close()
    finally:
        decoded.image.close()


def _state(metadata: object, source_frame: QImage, result_frame: QImage) -> AppState:
    state = reduce(
        AppState(
            parameters=ParameterState(
                model_id=_MODEL_ID,
                output_directory=_OUTPUT_DIRECTORY,
            )
        ),
        SourceLoadRequested(_SOURCE_ID, _LOAD_REQUEST_ID),
    )
    state = reduce(
        state,
        SourceLoaded(_SOURCE_ID, _LOAD_REQUEST_ID, metadata, source_frame),
    )
    state = reduce(state, PreviewRequested(_JOB_ID, _PREVIEW_REQUEST_ID))
    return reduce(
        state,
        PreviewSucceeded(
            _JOB_ID,
            PreviewResult(_SOURCE_ID, _PREVIEW_REQUEST_ID, result_frame),
        ),
    )


def _capture(widget: QWidget, path: Path) -> None:
    if not widget.grab().save(str(path), "PNG"):
        raise RuntimeError(f"could not save screenshot: {path}")


def _transform_facts(metadata: object) -> CutFacts:
    frame_count = getattr(metadata, "frame_count", None)
    frame_rate = getattr(metadata, "average_rate", None)
    if type(frame_count) is not int or frame_count < 1:
        frame_count = max(
            1,
            int(getattr(metadata, "duration") * getattr(metadata, "peak_rate")),
        )
    if not isinstance(frame_rate, Fraction) or frame_rate <= 0:
        frame_rate = getattr(metadata, "peak_rate")
    fps = max(1, round(float(frame_rate)))
    return CutFacts(
        cache_key="screenshot",
        frame_count=frame_count,
        framed_size=(
            getattr(metadata, "width"),
            getattr(metadata, "height"),
        ),
        fps=fps,
        delays_ms=(max(1, round(1000 / fps)),) * frame_count,
    )


def _capture_main_states(
    application: QApplication,
    state: AppState,
    metadata: object,
    output: Path,
) -> None:
    settings = QSettings(
        QSettings.IniFormat,
        QSettings.UserScope,
        "MatteLoop",
        "issue-43-screenshots",
    )
    settings.clear()
    window = MainWindow(_Store(state), _Services(), settings)
    window.resize(*_WINDOW_SIZE)
    window.show()
    application.processEvents()
    _capture(window, output / "main-window.png")

    for key, (button, body) in window.inspector.disclosures.items():
        expanded = key == "transform"
        button.setChecked(expanded)
        body.setVisible(expanded)
    window.inspector.transform_group.set_cut(_transform_facts(metadata))
    application.processEvents()
    _capture(window, output / "transform-expanded.png")
    window.close()


def _capture_model_manager(
    application: QApplication, output: Path
) -> None:
    catalog = ModelCatalog.load_resource()
    spec = catalog.get(_MODEL_ID)
    artifact = spec.artifact
    if artifact is None:
        raise RuntimeError(f"V1 model {_MODEL_ID!r} has no artifact")

    real_cache_root = model_cache_root()
    with tempfile.TemporaryDirectory(prefix="matteloop-screenshot-cache-") as root:
        cache_root = Path(root)
        if cache_root == real_cache_root:
            raise RuntimeError("screenshot cache unexpectedly uses the real cache")
        artifact_path = (
            cache_root
            / catalog.rembg_version
            / _MODEL_ID
            / artifact.runtime_filename
        )
        artifact_path.parent.mkdir(parents=True)
        with artifact_path.open("wb") as placeholder:
            placeholder.truncate(artifact.size_bytes)
        if artifact_path.stat().st_size != artifact.size_bytes:
            raise RuntimeError("screenshot model placeholder has the wrong size")

        dialog = ModelManagerDialog(
            catalog,
            cache_root,
            active_model=lambda: _MODEL_ID,
        )
        dialog.refresh()
        if dialog.cache_root == real_cache_root:
            raise RuntimeError("model dialog unexpectedly uses the real cache")
        display_root = str(_DEMO_CACHE_DISPLAY_ROOT)
        dialog.set_message(f"{len(dialog.entries)} V1 model(s); cache: {display_root}")
        dialog.cache_location_label.setText(display_root)
        dialog.cache_location_label.setToolTip(display_root)
        dialog.cache_location_label.setAccessibleDescription(display_root)
        row_height = dialog.model_list.sizeHintForRow(0)
        dialog.model_list.setFixedHeight(
            row_height * dialog.model_list.count()
            + 2 * dialog.model_list.frameWidth()
        )
        dialog.adjustSize()
        dialog.show()
        application.processEvents()
        _capture(dialog, output / "model-manager.png")
        dialog.close()


def main() -> None:
    QLocale.setDefault(
        QLocale(QLocale.Language.English, QLocale.Country.UnitedStates)
    )
    repo_root = Path(__file__).resolve().parents[1]
    source_path = repo_root / "assets" / "demo" / "golden-retriever.mp4"
    output = repo_root / "assets" / "screenshots"
    output.mkdir(parents=True, exist_ok=True)
    metadata, source_frame, result_frame = _demo_images(source_path)
    state = _state(metadata, source_frame, result_frame)

    application = QApplication.instance() or QApplication([])
    install_theme(application)
    _capture_main_states(application, state, metadata, output)
    _capture_model_manager(application, output)


if __name__ == "__main__":
    main()
