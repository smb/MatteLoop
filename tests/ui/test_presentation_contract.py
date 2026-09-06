from __future__ import annotations

import ast
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QSpinBox, QStyle, QStyleOptionSpinBox

from matteloop.core.crop_state import CropChanged, CropToggleChanged, ResetCrop
from matteloop.core.errors import AppError, ErrorCode
from matteloop.core.specs import CropSpec
from matteloop.core.state import (
    AppState,
    ArtifactResult,
    CancelRequested,
    EditedCutsChanged,
    EditedCutsScanRequested,
    ModelAvailabilityChanged,
    PreviewFailed,
    PreviewInvalidated,
    PreviewInvalidationReason,
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
from matteloop.ui.crop_presentation import CropPresentation
from matteloop.ui.inspector import Inspector
from matteloop.ui.presenter import present
from matteloop.ui.theme import install_theme, load_packaged_fonts

_SOURCE_ERROR_CASES = [
    (ErrorCode.SOURCE_NOT_LOCAL, "Open a video stored on this Mac."),
    (ErrorCode.SOURCE_UNREADABLE, "Open a video file that can be opened and read."),
    (ErrorCode.SOURCE_NO_VIDEO, "Open a file that contains a video track."),
    (ErrorCode.SOURCE_CORRUPT, "Open another video file; this one appears damaged."),
    (ErrorCode.SOURCE_ZERO_DURATION, "Open a video with a positive duration."),
    (ErrorCode.SOURCE_HDR_UNSUPPORTED, "Convert to 8-bit SDR and try again."),
    (ErrorCode.SOURCE_DIMENSIONS_UNSUPPORTED, "Resize to between 8×8 and 3840×2160."),
    (ErrorCode.SOURCE_FPS_UNSUPPORTED, "Convert the video to 60 fps or less."),
    (ErrorCode.SOURCE_DURATION_UNSUPPORTED, "Open a video under 10 minutes."),
    (ErrorCode.SOURCE_FORMAT_UNSUPPORTED, "Open an MP4, MOV, WebM, or MKV video."),
]


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


def _source_error(code: ErrorCode) -> AppState:
    loading = reduce(AppState(), SourceLoadRequested("source", "load"))
    return reduce(
        loading,
        SourceLoadFailed(
            "source",
            "load",
            AppError(code, "source", "source.test", "technical detail", "retry"),
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
        reduce(
            _current(), PreviewInvalidated(PreviewInvalidationReason.CROP_CLEANUP)
        ),
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
    path = Path("src/matteloop/ui/presenter.py")
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
    assert not any(name.startswith(("PySide6", "matteloop.jobs")) for name in imports)


@pytest.mark.parametrize(
    ("code", "expected"),
    _SOURCE_ERROR_CASES,
)
def test_each_source_error_code_selects_actionable_copy(
    code: ErrorCode, expected: str
) -> None:
    model = present(_source_error(code))

    assert model.source_error_message == expected
    assert model.source_error_detail == "technical detail"


def test_mapped_source_error_copies_are_distinct() -> None:
    messages = [
        present(_source_error(code)).source_error_message
        for code, _ in _SOURCE_ERROR_CASES
    ]

    assert len(messages) == len(set(messages))


def test_unmapped_source_error_uses_generic_copy() -> None:
    model = present(_source_error(ErrorCode.INVALID_ERROR))

    assert model.source_error_message == (
        "This video could not be read. Open another video."
    )
    assert model.source_error_detail == "technical detail"


def test_missing_packaged_fonts_fall_back_honestly(tmp_path: Path) -> None:
    (tmp_path / "resources").mkdir()
    (tmp_path / "resources" / "model-manifest.json").write_text("{}", encoding="utf-8")
    assert not load_packaged_fonts(runtime_root=tmp_path)


def test_spinbox_arrow_buttons_meet_minimum_target_size(qtbot) -> None:
    application = QApplication.instance()
    assert application is not None
    original_style_sheet = application.styleSheet()
    original_font = application.font()
    try:
        install_theme(application)
        spinbox = QSpinBox()
        qtbot.addWidget(spinbox)
        spinbox.resize(280, 32)
        spinbox.show()

        option = QStyleOptionSpinBox()
        spinbox.initStyleOption(option)
        style = spinbox.style()
        up_rect = style.subControlRect(
            QStyle.ComplexControl.CC_SpinBox,
            option,
            QStyle.SubControl.SC_SpinBoxUp,
            spinbox,
        )
        down_rect = style.subControlRect(
            QStyle.ComplexControl.CC_SpinBox,
            option,
            QStyle.SubControl.SC_SpinBoxDown,
            spinbox,
        )

        # WCAG's minimum target size is 24x24; the arrows are a stacked pair
        # splitting the control's height, so 24px wide is the achievable bar.
        assert up_rect.width() >= 24
        assert down_rect.width() >= 24
        assert up_rect.height() + down_rect.height() >= 24
        assert up_rect.x() + up_rect.width() <= spinbox.width()
        assert down_rect.x() + down_rect.width() <= spinbox.width()
    finally:
        application.setStyleSheet(original_style_sheet)
        application.setFont(original_font)


def test_inspector_uses_defaults_for_malformed_disclosure_settings(qtbot) -> None:
    settings = QSettings(
        QSettings.IniFormat,
        QSettings.UserScope,
        "matteloop-test",
        "malformed-settings",
    )
    settings.clear()
    settings.setValue("inspector/segmentation", "true")
    settings.setValue("inspector/crop_cleanup", 1)
    inspector = Inspector(settings)
    qtbot.addWidget(inspector)
    assert inspector.disclosures["segmentation"][0].isChecked()
    assert not inspector.disclosures["crop_cleanup"][0].isChecked()


def test_inspector_crop_fields_mirror_values_and_emit_reducer_commands(qtbot) -> None:
    settings = QSettings(
        QSettings.IniFormat, QSettings.UserScope, "matteloop-test", "crop-fields"
    )
    settings.clear()
    inspector = Inspector(settings)
    qtbot.addWidget(inspector)
    presentation = CropPresentation(
        "source", 100, 50, 100, 50, 0, 1.0, CropSpec(10, 8, 40, 20)
    )
    inspector.apply_crop(presentation, enabled=True, editable=True)
    commands: list[object] = []
    inspector.command_requested.connect(commands.append)

    inspector.crop_x_spinbox.setValue(12)
    inspector.crop_toggle.setChecked(False)
    inspector.crop_reset_button.click()

    assert commands[0] == CropChanged(CropSpec(12, 8, 40, 20))
    assert commands[1] == CropToggleChanged(False)
    assert commands[2] == ResetCrop()
    assert inspector.crop_width_spinbox.maximum() == 100
    assert inspector.crop_height_spinbox.maximum() == 50
