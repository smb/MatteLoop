from __future__ import annotations

from matteloop.ui.preview_canvas import PreviewCanvas


def test_checkerboard_predicate_follows_the_property_alone(qtbot) -> None:
    """A plain PreviewCanvas has no frame session to hold, so its checkerboard
    predicate must keep following the ``checkerboard`` property exactly as it
    did before ``ResultPlayerCanvas`` gained an overridable reason of its
    own (see test_result_player.py's player-holding-frames case)."""
    canvas = PreviewCanvas("Original", "original_canvas")
    qtbot.addWidget(canvas)

    canvas.setProperty("checkerboard", False)
    assert canvas._should_paint_checkerboard() is False  # noqa: SLF001

    canvas.setProperty("checkerboard", True)
    assert canvas._should_paint_checkerboard() is True  # noqa: SLF001
