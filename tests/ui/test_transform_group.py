from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
from pathlib import Path

from PySide6.QtWidgets import QLabel

from matteloop.core.crop import centered_crop_for_aspect
from matteloop.core.parameters import TransformChanged
from matteloop.core.specs import (
    PLATFORM_SIZE_PRESETS,
    MismatchMode,
    ResizeSpec,
    SizePreset,
    TransformSpec,
)
from matteloop.core.state import AppState, ArtifactResult
from matteloop.ui.parameter_presentation import (
    ParameterPresentation,
    present_parameters,
)
from matteloop.ui.transform_group import CutFacts, TransformGroup


def _presentation(
    transform: TransformSpec = TransformSpec(),
    artifact: ArtifactResult | None = None,
) -> ParameterPresentation:
    base = present_parameters(AppState())
    return replace(base, transform=transform, artifact=artifact)


def _facts(
    frame_count: int = 10,
    framed_size: tuple[int, int] = (256, 128),
    fps: int = 15,
    delay: int = 67,
) -> CutFacts:
    return CutFacts(
        cache_key="a" * 64,
        frame_count=frame_count,
        framed_size=framed_size,
        fps=fps,
        delays_ms=tuple(delay for _ in range(frame_count)),
    )


def _group(qtbot, presets: tuple[SizePreset, ...] | None = None):
    emitted: list[object] = []
    kwargs = {} if presets is None else {"presets": presets}
    group = TransformGroup(emitted.append, **kwargs)
    qtbot.addWidget(group)
    return group, emitted


def _index_with_data(combo, data: object) -> int:
    """Locate a combo row by value equality.

    QComboBox.findData compares opaque Python objects (Fraction, SizePreset)
    by identity, not value, so a freshly built lookup key never matches the
    object actually stored on the row.
    """
    for index in range(combo.count()):
        if combo.itemData(index) == data:
            return index
    raise AssertionError(f"no combo item with data {data!r}")


def test_choosing_a_size_preset_fills_dimensions_and_leaves_mismatch(qtbot) -> None:
    group, emitted = _group(qtbot)
    group.apply(_presentation(), editable=True)
    group.set_cut(_facts())
    group.mismatch_combo.setCurrentIndex(
        group.mismatch_combo.findData(MismatchMode.STRETCH)
    )
    emitted.clear()
    preset = next(p for p in PLATFORM_SIZE_PRESETS if p.label == "256x128")

    group.size_preset_combo.setCurrentIndex(group.size_preset_combo.findData(preset))

    assert group.width_spinbox.value() == 256
    assert group.height_spinbox.value() == 128
    assert group.mismatch_combo.currentData() == MismatchMode.STRETCH
    assert emitted
    last = emitted[-1]
    assert isinstance(last, TransformChanged)
    assert last.transform.resize == ResizeSpec(256, 128, MismatchMode.STRETCH)


def test_a_fake_preset_row_appears_under_its_own_platform_header(qtbot) -> None:
    fake = SizePreset("Acme", "999x999", 999, 999)
    group, _emitted = _group(qtbot, presets=(*PLATFORM_SIZE_PRESETS, fake))
    group.apply(_presentation(), editable=True)
    group.set_cut(_facts())

    model = group.size_preset_combo.model()
    labels = [model.item(row).text() for row in range(model.rowCount())]

    assert "Acme" in labels
    header_row = labels.index("Acme")
    assert labels[header_row + 1] == "999x999"
    assert group.size_preset_combo.findData(fake) >= 0


def test_editing_first_frame_emits_a_valid_transform_change(qtbot) -> None:
    group, emitted = _group(qtbot)
    group.apply(_presentation(), editable=True)
    group.set_cut(_facts(frame_count=10))
    emitted.clear()

    group.first_frame_spinbox.setValue(2)

    assert emitted == [TransformChanged(TransformSpec(first_frame=2))]


def test_clearing_both_resize_axes_to_auto_shows_an_error_and_emits_nothing(
    qtbot,
) -> None:
    group, emitted = _group(qtbot)
    group.apply(
        _presentation(transform=TransformSpec(resize=ResizeSpec(width=256))),
        editable=True,
    )
    group.set_cut(_facts())
    assert group.width_spinbox.value() == 256
    assert group.height_spinbox.value() == group.height_spinbox.minimum()
    emitted.clear()

    group.width_spinbox.setValue(group.width_spinbox.minimum())

    assert emitted == []
    assert group.width_spinbox.property("invalid") is True
    assert group.height_spinbox.property("invalid") is True


def test_percentage_fills_both_dimensions_and_clears_on_manual_edit(qtbot) -> None:
    group, emitted = _group(qtbot)
    group.apply(_presentation(), editable=True)
    group.set_cut(_facts(framed_size=(640, 360)))
    emitted.clear()

    group.percent_spinbox.setValue(50)

    assert group.width_spinbox.value() == 320
    assert group.height_spinbox.value() == 180
    last = emitted[-1]
    assert isinstance(last, TransformChanged)
    assert last.transform.resize == ResizeSpec(320, 180, MismatchMode.KEEP)

    group.width_spinbox.setValue(300)

    assert group.percent_spinbox.value() == 0


def test_percent_rounds_from_the_displayed_decimal_not_the_binary_float(
    qtbot,
) -> None:
    """12.85% of 1000 px is a half-pixel case: Fraction(12.85) preserves the
    binary float's approximation (128.49999...) and rounds down to 128,
    while Fraction("12.85") preserves the decimal the spin box actually
    shows and rounds half-up to 129, matching what the user typed."""
    group, emitted = _group(qtbot)
    group.apply(_presentation(), editable=True)
    group.set_cut(_facts(framed_size=(1000, 1000)))
    emitted.clear()

    group.percent_spinbox.setValue(12.85)

    assert group.width_spinbox.value() == 129
    assert group.height_spinbox.value() == 129
    last = emitted[-1]
    assert isinstance(last, TransformChanged)
    assert last.transform.resize == ResizeSpec(129, 129, MismatchMode.KEEP)


def test_percent_rounding_unaffected_by_the_decimal_fix_stays_the_same(
    qtbot,
) -> None:
    """2.35% of 10000 px is not a half-pixel case (235 exactly, whether the
    fraction comes from the binary float or the decimal text), so the fix
    must not perturb it. (10000 px, rather than the defect report's 1000 px,
    keeps the result above the width spinbox's 128 px "Auto" floor.)"""
    group, emitted = _group(qtbot)
    group.apply(_presentation(), editable=True)
    group.set_cut(_facts(framed_size=(10000, 10000)))
    emitted.clear()

    group.percent_spinbox.setValue(2.35)

    assert group.width_spinbox.value() == 235
    assert group.height_spinbox.value() == 235


def test_choosing_an_aspect_preset_emits_a_centred_crop_and_locks_it(qtbot) -> None:
    group, emitted = _group(qtbot)
    locks: list[object] = []
    group.aspect_lock_changed.connect(locks.append)
    group.apply(_presentation(), editable=True)
    group.set_cut(_facts(framed_size=(200, 100)))
    emitted.clear()

    group.aspect_combo.setCurrentIndex(
        _index_with_data(group.aspect_combo, Fraction(1, 1))
    )

    expected = centered_crop_for_aspect(
        Fraction(1, 1), source_width=200, source_height=100
    )
    last = emitted[-1]
    assert isinstance(last, TransformChanged)
    assert last.transform.crop == expected
    assert locks == [Fraction(1, 1)]
    assert group.crop_width_spinbox.value() == expected.width
    assert group.crop_height_spinbox.value() == expected.height


def test_aspect_lock_survives_an_apply_round_trip_and_free_actually_unlocks(
    qtbot,
) -> None:
    group, emitted = _group(qtbot)
    locks: list[object] = []
    group.aspect_lock_changed.connect(locks.append)
    group.apply(_presentation(), editable=True)
    group.set_cut(_facts(framed_size=(1920, 1080)))
    emitted.clear()

    group.aspect_combo.setCurrentIndex(
        _index_with_data(group.aspect_combo, Fraction(16, 9))
    )
    last = emitted[-1]
    assert isinstance(last, TransformChanged)

    # A dispatched TransformChanged always comes back through apply() once the
    # reducer accepts it; the aspect choice must not be reset by that round trip.
    group.apply(_presentation(transform=last.transform), editable=True)

    assert group.aspect_combo.currentData() == Fraction(16, 9)

    locks.clear()
    group.aspect_combo.setCurrentIndex(_index_with_data(group.aspect_combo, None))

    assert locks == [None]


def test_raising_first_frame_above_last_adjusts_last_instead_of_raising(
    qtbot,
) -> None:
    group, emitted = _group(qtbot)
    group.apply(
        _presentation(transform=TransformSpec(first_frame=0, last_frame=3)),
        editable=True,
    )
    group.set_cut(_facts(frame_count=10))
    emitted.clear()

    group.first_frame_spinbox.setValue(4)

    last = emitted[-1]
    assert isinstance(last, TransformChanged)
    assert last.transform.first_frame == 4
    assert last.transform.last_frame is None or last.transform.last_frame >= 4


def test_lowering_last_frame_below_first_adjusts_first_instead_of_raising(
    qtbot,
) -> None:
    group, emitted = _group(qtbot)
    group.apply(
        _presentation(transform=TransformSpec(first_frame=5)), editable=True
    )
    group.set_cut(_facts(frame_count=10))
    emitted.clear()

    group.last_frame_spinbox.setValue(2)

    last = emitted[-1]
    assert isinstance(last, TransformChanged)
    assert last.transform.last_frame == 2
    assert last.transform.first_frame <= 2


def test_apply_with_editable_false_disables_every_control(qtbot) -> None:
    group, _emitted = _group(qtbot)
    group.set_cut(_facts())

    group.apply(_presentation(), editable=False)

    for widget in group.tab_widgets():
        assert not widget.isEnabled()


def test_set_cut_none_disables_every_control(qtbot) -> None:
    group, _emitted = _group(qtbot)
    group.apply(_presentation(), editable=True)
    group.set_cut(_facts())
    assert group.first_frame_spinbox.isEnabled()

    group.set_cut(None)

    for widget in group.tab_widgets():
        assert not widget.isEnabled()


def test_readout_shows_frame_count_duration_and_size(qtbot) -> None:
    group, _emitted = _group(qtbot)
    group.apply(
        _presentation(TransformSpec(first_frame=1, last_frame=3)), editable=True
    )
    group.set_cut(_facts(frame_count=10, framed_size=(256, 128), delay=100))

    text = group.readout_label.text()

    assert "3 of 10 frames" in text
    assert "0.300 s" in text
    assert "256" in text and "128" in text
    assert "rendered" not in text


def test_readout_appends_the_rendered_artifact_size(qtbot) -> None:
    group, _emitted = _group(qtbot)
    artifact = ArtifactResult(
        source_id="s",
        request_id="r",
        output_path=Path("out.webp"),
        width=200,
        height=100,
        file_size=2048,
    )
    group.apply(_presentation(artifact=artifact), editable=True)
    group.set_cut(_facts())

    text = group.readout_label.text()

    assert "rendered" in text
    assert "200" in text and "100" in text


def test_tab_widgets_returns_the_focus_order(qtbot) -> None:
    group, _emitted = _group(qtbot)

    widgets = group.tab_widgets()

    assert widgets[0] is group.first_frame_spinbox
    assert group.size_preset_combo in widgets
    assert len(widgets) == len(set(widgets))


def test_crop_and_resize_sections_have_distinguishing_headings(qtbot) -> None:
    """Without a heading, crop's and resize's identical Width/Height field
    pairs are indistinguishable (only the trim rows read unambiguously)."""
    group, _emitted = _group(qtbot)

    trim_heading = group.findChild(QLabel, "transform_trim_heading")
    crop_heading = group.findChild(QLabel, "transform_crop_heading")
    resize_heading = group.findChild(QLabel, "transform_resize_heading")

    assert trim_heading is not None and trim_heading.text() == "Trim"
    assert crop_heading is not None and crop_heading.text() == "Crop"
    assert resize_heading is not None and resize_heading.text() == "Resize"
    # Each heading sits in its own section, ahead of that section's Width
    # field, not attached to a sibling section.
    assert crop_heading.parentWidget() is group.crop_width_spinbox.parentWidget()
    assert resize_heading.parentWidget() is group.width_spinbox.parentWidget()
    assert crop_heading.parentWidget() is not resize_heading.parentWidget()
    # A plain heading label is not a tab stop and must not shift focus order.
    assert trim_heading not in group.tab_widgets()
    assert crop_heading not in group.tab_widgets()
    assert resize_heading not in group.tab_widgets()
    assert group.tab_widgets()[0] is group.first_frame_spinbox
