"""Main timeline-first application shell driven exclusively by immutable state."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QSettings, QTimer
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMainWindow, QVBoxLayout, QWidget

from rembggui.core.state import AppState, FocusTarget, SourceState
from rembggui.ui.action_shelf import ActionShelf
from rembggui.ui.inspector import Inspector
from rembggui.ui.ports import (
    ChooseVideoRequested,
    ManageModelsRequested,
    ManageWorkspacesRequested,
    OpenOutputFolderRequested,
    OpenOutputRequested,
    PreviewFrameRequested,
    RebuildEditedCutsRequested,
    RenderVideoRequested,
    StateStore,
    WindowServices,
)
from rembggui.ui.presenter import PresentationModel, present
from rembggui.ui.preview_canvas import PreviewStage, TimelinePlaceholder
from rembggui.ui.source_strip import SourceDropSurface, SourceStrip


class MainWindow(QMainWindow):
    """A passive view: widgets send UI commands and render reducer state."""

    def __init__(
        self,
        store: StateStore,
        services: WindowServices,
        settings: QSettings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("main_window")
        self.setAccessibleName("rembgGUI")
        self.setMinimumSize(1100, 720)
        self.resize(1440, 900)
        self._store = store
        self._services = services
        self._settings = settings
        self._unsubscribe: Callable[[], None] | None = None
        self._last_focus: FocusTarget | None = None
        self._build()
        self._restore_geometry()
        self.render_state(store.state)
        self._unsubscribe = store.subscribe(self.render_state)

    def _build(self) -> None:
        central = QWidget()
        central.setObjectName("central_content")
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.left_workspace = QWidget()
        self.left_workspace.setObjectName("left_workspace")
        left_layout = QVBoxLayout(self.left_workspace)
        left_layout.setContentsMargins(16, 16, 16, 0)
        left_layout.setSpacing(12)
        self.source_drop_surface = SourceDropSurface()
        self.choose_video_button = self.source_drop_surface.button
        self.source_strip = SourceStrip()
        self.source_filename = self.source_strip.filename
        self.source_dimensions = self.source_strip.dimensions
        self.source_duration = self.source_strip.duration
        self.source_frame_rate = self.source_strip.frame_rate
        self.source_file_size = self.source_strip.file_size
        self.replace_video_button = self.source_strip.replace_button
        self.preview_stage = PreviewStage()
        self.original_canvas = self.preview_stage.original_canvas
        self.result_canvas = self.preview_stage.result_canvas
        self.timeline_placeholder = TimelinePlaceholder()
        self.source_error_heading = QLabel("Couldn’t read this video")
        self.source_error_heading.setObjectName("source_error_heading")
        self.source_error_heading.setAccessibleName("Video load error")
        self.source_error_heading.hide()
        left_layout.addWidget(self.source_drop_surface, 1)
        left_layout.addWidget(self.source_strip)
        left_layout.addWidget(self.source_error_heading)
        left_layout.addWidget(self.preview_stage, 1)
        left_layout.addWidget(self.timeline_placeholder)
        root.addWidget(self.left_workspace, 1)
        inspector_column = QWidget()
        inspector_layout = QVBoxLayout(inspector_column)
        inspector_layout.setContentsMargins(0, 0, 0, 0)
        inspector_layout.setSpacing(0)
        self.inspector = Inspector(self._settings)
        self.inspector_scroll = self.inspector.scroll_area
        self.inspector_content = self.inspector.scroll_area.widget()
        self.action_shelf = ActionShelf()
        self.preview_button = self.action_shelf.preview_button
        self.render_button = self.action_shelf.render_button
        self.rebuild_button = self.action_shelf.rebuild_button
        self.open_output_button = self.action_shelf.open_output_button
        self.open_folder_button = self.action_shelf.open_folder_button
        self.success_banner = self.action_shelf.success_banner
        self.manage_models_button = self.inspector.manage_models
        self.manage_workspaces_button = self.inspector.manage_workspaces
        self.edited_cut_recovery = self.inspector.edited_cut_recovery
        inspector_layout.addWidget(self.inspector, 1)
        inspector_layout.addWidget(self.action_shelf)
        root.addWidget(inspector_column)
        self._resize_inspector()
        self.choose_video_button.clicked.connect(
            lambda: self._services.dispatch(ChooseVideoRequested())
        )
        self.replace_video_button.clicked.connect(
            lambda: self._services.dispatch(ChooseVideoRequested())
        )
        self.preview_button.clicked.connect(
            lambda: self._services.dispatch(PreviewFrameRequested())
        )
        self.render_button.clicked.connect(
            lambda: self._services.dispatch(RenderVideoRequested())
        )
        self.rebuild_button.clicked.connect(
            lambda: self._services.dispatch(RebuildEditedCutsRequested())
        )
        self.open_output_button.clicked.connect(
            lambda: self._services.dispatch(OpenOutputRequested())
        )
        self.open_folder_button.clicked.connect(
            lambda: self._services.dispatch(OpenOutputFolderRequested())
        )
        self.manage_models_button.clicked.connect(
            lambda: self._services.dispatch(ManageModelsRequested())
        )
        self.manage_workspaces_button.clicked.connect(
            lambda: self._services.dispatch(ManageWorkspacesRequested())
        )

    def resizeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().resizeEvent(event)
        if hasattr(self, "inspector"):
            self._resize_inspector()

    def _resize_inspector(self) -> None:
        width = min(400, max(340, 340 + max(0, self.width() - 1100)))
        self.inspector.parentWidget().setFixedWidth(width)  # type: ignore[union-attr]

    def render_state(self, state: AppState) -> None:
        """Render a complete immutable snapshot; never starts work itself."""
        model = present(state)
        self._render(model, state)

    def _render(self, model: PresentationModel, state: AppState) -> None:
        ready = state.source is SourceState.READY
        self.source_drop_surface.setVisible(
            state.source in {SourceState.EMPTY, SourceState.ERROR}
        )
        self.source_strip.setVisible(
            state.source in {SourceState.LOADING, SourceState.READY}
        )
        self.preview_stage.setVisible(model.show_stage)
        self.timeline_placeholder.setVisible(model.show_timeline)
        self.source_error_heading.setVisible(state.source is SourceState.ERROR)
        self.source_error_heading.setText("Couldn’t read this video")
        self.source_error_heading.setToolTip(model.source_error_message or "")
        self.source_drop_surface.heading.setText(
            "Choose another video"
            if state.source is SourceState.ERROR
            else "Drop a video here"
        )
        if ready:
            self.source_strip.set_metadata(state.source_value)
        elif state.source is SourceState.LOADING:
            self.source_strip.filename.setText("Reading video…")
        self.result_canvas.setText(model.result_message)
        self.result_canvas.setAccessibleDescription(
            model.stale_category or model.result_message
        )
        self.choose_video_button.setEnabled(model.choose_enabled)
        self.replace_video_button.setEnabled(model.replace_enabled)
        self.preview_button.setEnabled(model.preview_enabled)
        self.preview_button.setText(model.preview_label)
        self.render_button.setEnabled(model.render_enabled)
        self.rebuild_button.setEnabled(model.rebuild_enabled)
        self.rebuild_button.setVisible(model.show_rebuild)
        self.edited_cut_recovery.setVisible(state.edited_cuts_error is not None)
        self.open_output_button.setEnabled(model.open_output_enabled)
        self.open_folder_button.setEnabled(model.open_folder_enabled)
        self.success_banner.setVisible(model.show_success)
        self.success_banner.setText("Output ready" if model.show_success else "")
        self.inspector.setEnabled(
            not model.editor_locked or state.source is SourceState.EMPTY
        )
        for name, button in (
            ("preview", self.preview_button),
            ("render", self.render_button),
        ):
            button.setProperty("primary", model.primary_action == name)
            button.style().unpolish(button)
            button.style().polish(button)
        self._queue_focus(model.focus_target)

    def _queue_focus(self, target: FocusTarget) -> None:
        if target is self._last_focus:
            return
        self._last_focus = target
        widget = self._focus_widget(target)
        if widget is not None:
            QTimer.singleShot(0, widget.setFocus)

    def _focus_widget(self, target: FocusTarget) -> QWidget | None:
        return {
            FocusTarget.CHOOSE_VIDEO: self.choose_video_button,
            FocusTarget.SOURCE_ERROR_HEADING: self.source_error_heading,
            FocusTarget.PREVIEW_ACTION: self.preview_button,
            FocusTarget.RENDER_ACTION: self.render_button,
            FocusTarget.REBUILD_ACTION: self.rebuild_button,
            FocusTarget.RESULT_CANVAS: self.result_canvas,
            FocusTarget.SUCCESS_BANNER: self.success_banner,
            FocusTarget.EDITED_CUT_RECOVERY: self.rebuild_button,
        }.get(target)

    def primary_action_name(self) -> str | None:
        if self.preview_button.property("primary"):
            return "preview"
        if self.render_button.property("primary"):
            return "render"
        return None

    def requested_focus_name(self) -> str:
        return (self._last_focus or FocusTarget.NONE).value

    def closeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if self._unsubscribe is not None:
            unsubscribe, self._unsubscribe = self._unsubscribe, None
            unsubscribe()
        self._settings.setValue("window/geometry", self.saveGeometry())
        super().closeEvent(event)

    def _restore_geometry(self) -> None:
        geometry = self._settings.value("window/geometry")
        if geometry is not None and type(geometry).__name__ == "QByteArray":
            self.restoreGeometry(geometry)
