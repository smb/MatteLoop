from __future__ import annotations

import ast
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings

from rembggui.core.state import (
    AppState,
    ArtifactResult,
    CancelRequested,
    EditedCutsChanged,
    EditedCutsScanRequested,
    ModelAvailabilityChanged,
    PreviewFailed,
    PreviewInvalidated,
    PreviewRequested,
    PreviewResult,
    PreviewSucceeded,
    RenderPreflightRequested,
    RenderRequested,
    RenderSucceeded,
    SourceLoaded,
    SourceLoadFailed,
    SourceLoadRequested,
    reduce,
)
from rembggui.ui.inspector import Inspector
from rembggui.ui.presenter import present
from rembggui.ui.theme import load_packaged_fonts


def _ready() -> AppState:
    return reduce(
        reduce(AppState(), SourceLoadRequested("source", "load")),
        SourceLoaded("source", "load", "metadata"),
    )


def _current() -> AppState:
    running = reduce(_ready(), PreviewRequested("preview", "preview-request"))
    return reduce(
        running,
        PreviewSucceeded(
            "preview", PreviewResult("source", "preview-request", "result")
        ),
    )


def _artifact(state: AppState) -> AppState:
    running = reduce(state, RenderRequested("render", "render-request"))
    return reduce(
        running,
        RenderSucceeded(
            "render", ArtifactResult("source", "render-request", "output.webp")
        ),
    )


@pytest.mark.parametrize(
    "state",
    [
        AppState(),
        reduce(AppState(), SourceLoadRequested("source", "load")),
        reduce(
            reduce(AppState(), SourceLoadRequested("source", "load")),
            SourceLoadFailed("source", "load", "bad codec"),
        ),
        _ready(),
        reduce(_ready(), ModelAvailabilityChanged(False)),
        reduce(_ready(), PreviewRequested("preview", "preview-request")),
        _current(),
        reduce(_current(), PreviewInvalidated("Crop & cleanup")),
        reduce(
            reduce(_current(), PreviewRequested("retry", "retry-request")),
            PreviewFailed("retry", "source", "retry-request", "retry failed"),
        ),
        reduce(_ready(), RenderPreflightRequested()),
        reduce(_ready(), RenderRequested("render", "render-request")),
        reduce(
            reduce(_ready(), RenderRequested("render", "render-request")),
            CancelRequested("render"),
        ),
        _artifact(_ready()),
    ],
)
def test_present_accepts_every_reducer_built_visible_state(state: AppState) -> None:
    model = present(state)
    assert model.focus_target.value


def test_artifact_without_current_preview_does_not_promote_render() -> None:
    assert present(_artifact(_ready())).primary_action == "preview"


def test_edited_cuts_recovery_state_remains_truthful() -> None:
    artifact = _artifact(_current())
    scanning = reduce(
        artifact,
        EditedCutsScanRequested("source", "render-request", "edited-cuts"),
    )
    edited = reduce(
        scanning,
        EditedCutsChanged("source", "render-request", "edited-cuts", True),
    )
    assert present(edited).show_rebuild
    assert present(edited).primary_action == "preview"


def test_presenter_has_no_qt_or_job_imports() -> None:
    path = Path("src/rembggui/ui/presenter.py")
    module = ast.parse(path.read_text(encoding="utf-8"))
    imports = [
        alias.name
        for node in ast.walk(module)
        if isinstance(node, ast.Import)
        for alias in node.names
    ] + [
        node.module or ""
        for node in ast.walk(module)
        if isinstance(node, ast.ImportFrom)
    ]
    assert not any(name.startswith(("PySide6", "rembggui.jobs")) for name in imports)


def test_missing_packaged_fonts_fall_back_honestly(tmp_path: Path) -> None:
    (tmp_path / "resources").mkdir()
    (tmp_path / "resources" / "model-manifest.json").write_text("{}", encoding="utf-8")
    assert not load_packaged_fonts(runtime_root=tmp_path)


def test_inspector_uses_defaults_for_malformed_disclosure_settings(qtbot) -> None:
    settings = QSettings(
        QSettings.IniFormat,
        QSettings.UserScope,
        "rembggui-test",
        "malformed-settings",
    )
    settings.clear()
    settings.setValue("inspector/segmentation", "true")
    settings.setValue("inspector/crop_cleanup", 1)
    inspector = Inspector(settings)
    qtbot.addWidget(inspector)
    assert inspector.disclosures["segmentation"][0].isChecked()
    assert not inspector.disclosures["crop_cleanup"][0].isChecked()
