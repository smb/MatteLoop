"""Main timeline-first application shell driven exclusively by immutable state."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QByteArray, QSettings, Qt, QTimer
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from rembggui.core.execution_providers import ProviderOption
from rembggui.core.state import AppState, FocusTarget
from rembggui.ui.action_shelf import ActionShelf
from rembggui.ui.crop_view import render_source_editor
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
    VideoDropped,
    WindowServices,
)
from rembggui.ui.presentation_model import PresentationModel
from rembggui.ui.presenter import present
from rembggui.ui.preview_canvas import PreviewStage
from rembggui.ui.source_strip import SourceDropSurface, SourceStrip
from rembggui.ui.timeline import TimelineWidget


class MainWindow(QMainWindow):
    """A passive view: widgets send UI commands and render reducer state."""

    def __init__(
        self,
        store: StateStore,
        services: WindowServices,
        settings: QSettings,
        parent: QWidget | None = None,
        *,
        model_options: tuple[tuple[str, bool], ...] | None = None,
        provider_options: tuple[ProviderOption, ...] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("main_window")
        self.setAccessibleName("rembgGUI")
        self.setMinimumSize(1100, 720)
        self.resize(1440, 900)
        self._store = store
        self._services = services
        self._settings = settings
        self._model_options = model_options
        self._provider_options = provider_options
        self._unsubscribe: Callable[[], None] | None = None
        self._last_focus: FocusTarget | None = None
        self._build()
        self._set_tab_order()
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
        self.source_drop_target = SourceDropSurface()
        # Compatibility alias retained for the Task 13 public test seam.
        self.source_drop_surface = self.source_drop_target
        self.choose_video_button = self.source_drop_target.button
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
        self.timeline_widget = TimelineWidget()
        self.timeline_placeholder = self.timeline_widget
        self.source_error_heading = QLabel("Couldn’t read this video")
        self.source_error_heading.setObjectName("source_error_heading")
        self.source_error_heading.setAccessibleName("Video load error")
        self.source_error_heading.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.source_error_heading.hide()
        self.source_error_copy = QLabel()
        self.source_error_copy.setObjectName("source_error_copy")
        self.source_error_copy.setProperty("secondary", True)
        self.source_error_copy.hide()
        left_layout.addWidget(self.source_drop_surface, 1)
        left_layout.addWidget(self.source_strip)
        left_layout.addWidget(self.source_error_heading)
        left_layout.addWidget(self.source_error_copy)
        left_layout.addWidget(self.preview_stage, 1)
        left_layout.addWidget(self.timeline_widget)
        root.addWidget(self.left_workspace, 1)
        inspector_column = QWidget()
        inspector_layout = QVBoxLayout(inspector_column)
        inspector_layout.setContentsMargins(0, 0, 0, 0)
        inspector_layout.setSpacing(0)
        self.inspector = Inspector(
            self._settings,
            model_options=self._model_options,
            provider_options=self._provider_options,
        )
        self.inspector_scroll = self.inspector.scroll_area
        self.inspector_content = self.inspector.scroll_area.widget()
        self.action_shelf = ActionShelf()
        self.preview_button = self.action_shelf.preview_button
        self.render_button = self.action_shelf.render_button
        self.rebuild_button = self.inspector.rebuild_button
        self.success_container = QFrame()
        self.success_container.setObjectName("success_banner_container")
        success_layout = QVBoxLayout(self.success_container)
        success_layout.setContentsMargins(16, 8, 16, 8)
        success_layout.setSpacing(4)
        success_info_row = QWidget()
        success_info_row.setObjectName("success_info_row")
        info_layout = QHBoxLayout(success_info_row)
        info_layout.setContentsMargins(0, 0, 0, 0)
        self.success_banner = QLabel("Render complete")
        self.success_banner.setObjectName("success_banner")
        self.success_banner.setAccessibleName("Render complete")
        self.success_banner.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.success_artifact = QLabel()
        self.success_artifact.setObjectName("success_artifact")
        self.success_artifact.setProperty("mono", True)
        self.success_artifact.setProperty("secondary", True)
        self.success_artifact.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        info_layout.addWidget(self.success_banner)
        info_layout.addWidget(self.success_artifact, 1)
        success_actions_row = QWidget()
        success_actions_row.setObjectName("success_actions_row")
        actions_layout = QHBoxLayout(success_actions_row)
        actions_layout.setContentsMargins(0, 0, 0, 0)
        actions_layout.addStretch(1)
        self.open_output_button = QPushButton("Open output")
        self.open_output_button.setObjectName("open_output")
        self.open_output_button.setAccessibleName("Open output")
        self.open_folder_button = QPushButton("Open folder")
        self.open_folder_button.setObjectName("open_output_folder")
        self.open_folder_button.setAccessibleName("Open folder")
        actions_layout.addWidget(self.open_output_button)
        actions_layout.addWidget(self.open_folder_button)
        success_layout.addWidget(success_info_row)
        success_layout.addWidget(success_actions_row)
        self.success_container.hide()
        self.manage_models_button = self.inspector.manage_models
        self.manage_workspaces_button = self.inspector.manage_workspaces
        self.edited_cut_recovery = self.inspector.edited_cut_recovery
        inspector_layout.addWidget(self.inspector, 1)
        inspector_layout.addWidget(self.success_container)
        inspector_layout.addWidget(self.action_shelf)
        root.addWidget(inspector_column)
        self._resize_inspector()
        self.choose_video_button.clicked.connect(
            lambda: self._services.dispatch(ChooseVideoRequested())
        )
        self.replace_video_button.clicked.connect(
            lambda: self._services.dispatch(ChooseVideoRequested(replace=True))
        )
        self.source_drop_target.video_dropped.connect(
            lambda path: self._services.dispatch(VideoDropped(path))
        )
        self.preview_button.clicked.connect(
            lambda: self._services.dispatch(PreviewFrameRequested())
        )
        for widget in (self.timeline_widget, self.original_canvas, self.inspector):
            widget.command_requested.connect(self._services.dispatch)
        self.render_button.clicked.connect(
            lambda: self._services.dispatch(RenderVideoRequested())
        )
        self.rebuild_button.clicked.connect(
            lambda: self._services.dispatch(RebuildEditedCutsRequested())
        )
        self.edited_cut_recovery.clicked.connect(
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

    def _set_tab_order(self) -> None:
        """Declare a deterministic keyboard route through the shell."""
        widgets = [
            self.source_drop_target, self.choose_video_button,
            self.replace_video_button,
            self.original_canvas,
            self.result_canvas,
            self.timeline_widget,
        ]
        widgets.extend(
            [
                *self.inspector.tab_widgets(),
                self.success_banner,
                self.open_output_button,
                self.open_folder_button,
                self.preview_button,
                self.render_button,
            ]
        )
        for before, after in zip(widgets, widgets[1:], strict=False):
            self.setTabOrder(before, after)

    def resizeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().resizeEvent(event)
        if hasattr(self, "inspector"):
            self._resize_inspector()
            self._update_success_artifact()

    def _resize_inspector(self) -> None:
        width = min(400, max(340, 340 + max(0, self.width() - 1100)))
        self.inspector.parentWidget().setFixedWidth(width)  # type: ignore[union-attr]

    def render_state(self, state: AppState) -> None:
        """Render a complete immutable snapshot; never starts work itself."""
        model = present(state)
        self._render(model)

    def _render(self, model: PresentationModel) -> None:
        self.source_drop_surface.setVisible(model.source_surface_visible)
        self.source_strip.setVisible(model.source_strip_visible)
        self.preview_stage.setVisible(model.show_stage)
        self.timeline_widget.setVisible(model.show_timeline)
        self.timeline_widget.apply_presentation(model.timeline, not model.editor_locked)
        self.source_error_heading.setVisible(model.source_error_visible)
        self.source_error_copy.setVisible(model.source_error_visible)
        self.source_error_heading.setText("Couldn’t read this video")
        self.source_error_heading.setToolTip(model.source_error_detail or "")
        self.source_error_heading.setAccessibleDescription(
            model.source_error_detail or ""
        )
        self.source_error_copy.setText(model.source_error_message or "")
        self.source_drop_surface.heading.setText(model.source_surface_heading)
        self.source_strip.set_presented_metadata(model)
        render_source_editor(self.original_canvas, self.inspector, model)
        self.result_canvas.set_presented_frame(model.result_frame, model.result_message)
        self.result_canvas.setAccessibleName(model.result_accessible_name)
        self.result_canvas.setAccessibleDescription(model.result_accessible_description)
        self.result_canvas.setProperty("status", model.result_status)
        self.result_canvas.setProperty("checkerboard", model.result_checkerboard)
        self.result_canvas.set_status_marker(model.result_status_marker)
        self.result_canvas.style().unpolish(self.result_canvas)
        self.result_canvas.style().polish(self.result_canvas)
        self.choose_video_button.setEnabled(model.choose_enabled)
        self.replace_video_button.setEnabled(model.replace_enabled)
        self.preview_button.setEnabled(model.preview_enabled)
        self.preview_button.setText(model.preview_label)
        self.render_button.setEnabled(model.render_enabled)
        self.rebuild_button.setEnabled(model.rebuild_enabled)
        self.rebuild_button.setVisible(model.show_rebuild)
        self.edited_cut_recovery.setVisible(model.recovery_visible)
        self.edited_cut_recovery.setText(model.recovery_label)
        self.inspector.set_workspace_state(
            model.workspace_attention, model.workspace_open
        )
        self.open_output_button.setEnabled(model.open_output_enabled)
        self.open_folder_button.setEnabled(model.open_folder_enabled)
        self.success_container.setVisible(model.show_success)
        self.success_banner.setText(model.success_label)
        artifact_path = model.artifact_path or ""
        self._success_artifact_path = artifact_path
        self._update_success_artifact()
        self.success_artifact.setToolTip(artifact_path)
        self.success_artifact.setAccessibleDescription(artifact_path)
        self.success_banner.setToolTip(artifact_path)
        self.success_banner.setAccessibleDescription(model.success_accessible_description)
        self.inspector.setEnabled(model.inspector_enabled)
        for name, button in (
            ("preview", self.preview_button),
            ("render", self.render_button),
        ):
            button.setProperty("primaryAction", model.primary_action == name)
            button.setProperty("primary", model.primary_action == name)
            button.style().unpolish(button)
            button.style().polish(button)
        self.preview_button.setAccessibleName(model.preview_label)
        self._queue_focus(model.focus_target)

    def _update_success_artifact(self) -> None:
        if not hasattr(self, "success_artifact"):
            return
        artifact_path = getattr(self, "_success_artifact_path", "")
        available_width = self.success_artifact.width() or 220
        metrics = QFontMetrics(self.success_artifact.font())
        text = metrics.elidedText(
            artifact_path, Qt.TextElideMode.ElideMiddle, available_width
        )
        self.success_artifact.setText(text)

    def _queue_focus(self, target: FocusTarget) -> None:
        if target is self._last_focus:
            return
        self._last_focus = target
        widget = self._focus_widget(target)
        if widget is not None:
            QTimer.singleShot(
                0,
                lambda: (
                    widget.setFocus()
                    if widget.isVisible() and widget.isEnabled()
                    else None
                ),
            )

    def _focus_widget(self, target: FocusTarget) -> QWidget | None:
        return {
            FocusTarget.CHOOSE_VIDEO: self.choose_video_button,
            FocusTarget.SOURCE_ERROR_HEADING: self.source_error_heading,
            FocusTarget.PREVIEW_ACTION: self.preview_button,
            FocusTarget.RENDER_ACTION: self.render_button,
            FocusTarget.REBUILD_ACTION: self.rebuild_button,
            FocusTarget.RESULT_CANVAS: self.result_canvas,
            FocusTarget.SUCCESS_BANNER: self.success_banner,
            FocusTarget.EDITED_CUT_RECOVERY: self.edited_cut_recovery,
        }.get(target)

    def primary_action_name(self) -> str | None:
        if self.preview_button.property("primaryAction"):
            return "preview"
        if self.render_button.property("primaryAction"):
            return "render"
        return None

    def requested_focus_name(self) -> str:
        return (self._last_focus or FocusTarget.NONE).value

    def closeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if self._unsubscribe is not None:
            unsubscribe, self._unsubscribe = self._unsubscribe, None
            unsubscribe()
        self._settings.setValue("window/geometry", self.saveGeometry())
        self.timeline_widget.shutdown()
        super().closeEvent(event)

    def _restore_geometry(self) -> None:
        geometry = self._settings.value("window/geometry")
        if isinstance(geometry, QByteArray) and not self.restoreGeometry(geometry):
            self.resize(1440, 900)
