"""Exclusive modal preview job dialog."""

from __future__ import annotations

import time
from fractions import Fraction
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from matteloop.core.state import ArtifactResult
from matteloop.core.tokens import ProgressStage
from matteloop.jobs.context import ProgressEvent
from matteloop.ui.copy import (
    model_display_name,
    progress_detail,
    provider_label,
    provider_notice,
)
from matteloop.ui.source_presentation import (
    JobProgressMetrics,
    JobProgressPresenter,
    format_model_download_progress,
    format_source_dimensions,
    format_source_duration,
    format_source_file_size,
    format_source_frame_rate,
)


def _stage_copy(stage: str | ProgressStage) -> str:
    if stage is ProgressStage.PREPARING_MODEL:
        return QCoreApplication.translate("PreviewJobDialog", "Preparing model")
    if stage is ProgressStage.DOWNLOADING_MODEL:
        return QCoreApplication.translate("PreviewJobDialog", "Downloading model")
    if stage is ProgressStage.SEGMENTATION:
        return QCoreApplication.translate("PreviewJobDialog", "Segmentation")
    if stage in {ProgressStage.DECODE, ProgressStage.RENDER_CUT}:
        return QCoreApplication.translate("PreviewJobDialog", "Decode")
    if stage == "Validation":
        return QCoreApplication.translate("PreviewJobDialog", "Validation")
    if stage == "Post-process":
        return QCoreApplication.translate("PreviewJobDialog", "Post-process")
    if stage == "Auto-fit":
        return QCoreApplication.translate("PreviewJobDialog", "Auto-fit")
    if stage == "Encode":
        return QCoreApplication.translate("PreviewJobDialog", "Encode")
    if stage == "Validate":
        return QCoreApplication.translate("PreviewJobDialog", "Validate")
    if stage == "Complete":
        return QCoreApplication.translate("PreviewJobDialog", "Complete")
    return stage


class PreviewJobDialog(QDialog):
    """One reusable, non-blocking, application-modal job dialog."""

    cancel_requested = Signal()
    open_output_requested = Signal()
    open_folder_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("preview_job_dialog")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(
            QCoreApplication.translate("PreviewJobDialog", "Preview job")
        )
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setModal(True)
        self._build_widgets()
        self._assemble_layout()
        self._connect_controls()
        self._progress_presenter = JobProgressPresenter()
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(250)
        self._elapsed_timer.timeout.connect(self._refresh_metrics)
        self.reset()

    def _build_widgets(self) -> None:
        self.stage_label = QLabel()
        self.stage_label.setObjectName("job_stage")
        self.detail_label = QLabel()
        self.detail_label.setObjectName("job_detail")
        self.model_provider_label = QLabel()
        self.model_provider_label.setObjectName("job_model_provider")
        self.model_provider_label.setAccessibleName(
            QCoreApplication.translate("PreviewJobDialog", "Active model and provider")
        )
        self.output_label = QLabel()
        self.output_label.setObjectName("job_output")
        self.output_label.setAccessibleName(
            QCoreApplication.translate("PreviewJobDialog", "Output file")
        )
        self._build_completion_summary()
        self._build_completion_actions()
        self.provider_notice_label = QLabel()
        self.provider_notice_label.setObjectName("job_provider_notice")
        self.provider_notice_label.setAccessibleName(
            QCoreApplication.translate("PreviewJobDialog", "Provider notice")
        )
        self.elapsed_label = QLabel()
        self.elapsed_label.setObjectName("job_elapsed")
        self.rate_label = QLabel()
        self.rate_label.setObjectName("job_rate")
        self.estimate_label = QLabel()
        self.estimate_label.setObjectName("job_estimate")
        self.progress_bar = QProgressBar()
        self.progress_bar.setObjectName("job_progress")
        self.progress_bar.setAccessibleName(
            QCoreApplication.translate("PreviewJobDialog", "Stage progress")
        )
        self.overall_progress_bar = QProgressBar()
        self.overall_progress_bar.setObjectName("job_overall_progress")
        self.overall_progress_bar.setAccessibleName(
            QCoreApplication.translate("PreviewJobDialog", "Overall progress")
        )
        self.stage_progress_label = QLabel()
        self.stage_progress_label.setObjectName("job_stage_progress_label")
        self.overall_progress_label = QLabel()
        self.overall_progress_label.setObjectName("job_overall_progress_label")
        self.cancel_button = QPushButton(
            QCoreApplication.translate("PreviewJobDialog", "Cancel")
        )
        self.cancel_button.setObjectName("job_cancel")

    def _connect_controls(self) -> None:
        self.cancel_button.clicked.connect(self._request_cancel)
        self.open_output_button.clicked.connect(self.open_output_requested.emit)
        self.open_folder_button.clicked.connect(self.open_folder_requested.emit)
        self.close_button.clicked.connect(self._close_completion)
        self.installEventFilter(self)
        self.cancel_button.installEventFilter(self)

    def _assemble_layout(self) -> None:
        """Stack stage, detail, job identity, both bars and the metrics row."""
        layout = QVBoxLayout(self)
        layout.addWidget(self.stage_label)
        layout.addWidget(self.detail_label)
        job_details = QHBoxLayout()
        job_details.addWidget(self.model_provider_label)
        job_details.addStretch(1)
        job_details.addWidget(self.output_label)
        layout.addLayout(job_details)
        layout.addWidget(self.completion_summary)
        layout.addWidget(self.provider_notice_label)
        layout.addWidget(self.stage_progress_label)
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.overall_progress_label)
        layout.addWidget(self.overall_progress_bar)
        metrics = QHBoxLayout()
        metrics.addWidget(self.elapsed_label)
        metrics.addWidget(self.rate_label)
        metrics.addWidget(self.estimate_label)
        layout.addLayout(metrics)
        layout.addWidget(self.completion_actions)
        layout.addWidget(self.cancel_button)

    def _build_completion_summary(self) -> None:
        self.completion_summary = QWidget()
        self.completion_summary.setObjectName("job_completion_summary")
        self.completion_summary.setAccessibleName(
            QCoreApplication.translate("PreviewJobDialog", "Render completion summary")
        )
        summary_layout = QFormLayout(self.completion_summary)
        self.completion_dimensions = self._summary_value(
            QCoreApplication.translate("PreviewJobDialog", "Output dimensions")
        )
        self.completion_frames = self._summary_value(
            QCoreApplication.translate("PreviewJobDialog", "Output frame count")
        )
        self.completion_duration = self._summary_value(
            QCoreApplication.translate("PreviewJobDialog", "Animation duration")
        )
        self.completion_size = self._summary_value(
            QCoreApplication.translate("PreviewJobDialog", "Output file size")
        )
        self.completion_fps = self._summary_value(
            QCoreApplication.translate("PreviewJobDialog", "Output frame rate")
        )
        self.completion_job_time = self._summary_value(
            QCoreApplication.translate("PreviewJobDialog", "Render duration")
        )
        self.completion_cuts = self._summary_value(
            QCoreApplication.translate("PreviewJobDialog", "Cut segmentation")
        )
        for label, value in (
            (
                QCoreApplication.translate("PreviewJobDialog", "Dimensions"),
                self.completion_dimensions,
            ),
            (
                QCoreApplication.translate("PreviewJobDialog", "Frames"),
                self.completion_frames,
            ),
            (
                QCoreApplication.translate("PreviewJobDialog", "Animation duration"),
                self.completion_duration,
            ),
            (
                QCoreApplication.translate("PreviewJobDialog", "File size"),
                self.completion_size,
            ),
            (
                QCoreApplication.translate("PreviewJobDialog", "Output FPS"),
                self.completion_fps,
            ),
            (
                QCoreApplication.translate("PreviewJobDialog", "Job time"),
                self.completion_job_time,
            ),
            (
                QCoreApplication.translate("PreviewJobDialog", "Cuts"),
                self.completion_cuts,
            ),
        ):
            summary_layout.addRow(label, value)

    def _build_completion_actions(self) -> None:
        self.completion_actions = QWidget()
        self.completion_actions.setObjectName("job_completion_actions")
        actions_layout = QHBoxLayout(self.completion_actions)
        actions_layout.addStretch(1)
        self.open_output_button = QPushButton(
            QCoreApplication.translate("PreviewJobDialog", "Open output")
        )
        self.open_output_button.setObjectName("job_open_output")
        self.open_output_button.setAccessibleName(
            QCoreApplication.translate("PreviewJobDialog", "Open output")
        )
        self.open_folder_button = QPushButton(
            QCoreApplication.translate("PreviewJobDialog", "Open folder")
        )
        self.open_folder_button.setObjectName("job_open_folder")
        self.open_folder_button.setAccessibleName(
            QCoreApplication.translate("PreviewJobDialog", "Open folder")
        )
        self.close_button = QPushButton(
            QCoreApplication.translate("PreviewJobDialog", "Close")
        )
        self.close_button.setObjectName("job_close")
        self.close_button.setAccessibleName(
            QCoreApplication.translate("PreviewJobDialog", "Close")
        )
        actions_layout.addWidget(self.open_output_button)
        actions_layout.addWidget(self.open_folder_button)
        actions_layout.addWidget(self.close_button)

    def reset(self, title: str | None = None) -> None:
        self._completion_visible = False
        self._terminal_close_requested = False
        self.setAccessibleName(
            QCoreApplication.translate("PreviewJobDialog", "Preview job")
        )
        self.setWindowTitle(
            QCoreApplication.translate("PreviewJobDialog", "Previewing selected frame")
            if title is None
            else title
        )
        self.stage_label.setText(
            QCoreApplication.translate("PreviewJobDialog", "Preparing model")
        )
        self.detail_label.setText("")
        self._reset_completion_widgets()
        self._reset_progress_widgets()
        self._apply_metrics(self._progress_presenter.reset(time.monotonic()))
        self._elapsed_timer.start()
        self.cancel_button.setEnabled(True)
        self.cancel_button.setText(
            QCoreApplication.translate("PreviewJobDialog", "Cancel")
        )
        self._cancel_emitted = False
        self._model_name = ""
        self._provider_name = ""

    def _reset_completion_widgets(self) -> None:
        self.model_provider_label.clear()
        self.model_provider_label.hide()
        self.output_label.clear()
        self.output_label.hide()
        self.output_label.setToolTip("")
        self.output_label.setAccessibleDescription("")
        self.completion_summary.hide()
        self.completion_actions.hide()
        for value in (
            self.completion_dimensions,
            self.completion_frames,
            self.completion_duration,
            self.completion_size,
            self.completion_fps,
            self.completion_job_time,
            self.completion_cuts,
        ):
            value.clear()

    def _reset_progress_widgets(self) -> None:
        self.stage_progress_label.show()
        self.progress_bar.show()
        self.overall_progress_label.show()
        self.overall_progress_bar.show()
        self.elapsed_label.show()
        self.rate_label.show()
        self.estimate_label.show()
        self.cancel_button.show()
        self.provider_notice_label.clear()
        self.provider_notice_label.hide()
        self.stage_progress_label.setText(
            QCoreApplication.translate(
                "PreviewJobDialog", "Stage progress (indeterminate)"
            )
        )
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setFormat("")
        self.overall_progress_label.setText(
            QCoreApplication.translate(
                "PreviewJobDialog", "Overall progress (indeterminate)"
            )
        )
        self.overall_progress_bar.setRange(0, 0)
        self.overall_progress_bar.setFormat("")

    @property
    def completion_visible(self) -> bool:
        return self._completion_visible

    def _summary_value(self, accessible_name: str) -> QLabel:
        value = QLabel()
        object_name = accessible_name.lower().replace(" ", "_")
        value.setObjectName(f"job_completion_{object_name}")
        value.setAccessibleName(accessible_name)
        value.setProperty("mono", True)
        return value

    def set_job_details(
        self, model_id: str, output_filename: str | None = None
    ) -> None:
        self._model_name = model_display_name(model_id, model_id)
        self._refresh_model_provider()
        if output_filename:
            self.output_label.setText(
                QCoreApplication.translate("PreviewJobDialog", "Output: %s")
                % output_filename
            )
            self.output_label.show()

    def set_execution_provider(self, provider: str) -> None:
        self._provider_name = provider_label(provider, recommended=False, model_id="")
        self._refresh_model_provider()

    def _refresh_model_provider(self) -> None:
        if not self._model_name:
            self.model_provider_label.clear()
            self.model_provider_label.hide()
            return
        text = self._model_name
        if self._provider_name:
            text += (
                QCoreApplication.translate("PreviewJobDialog", " · %s")
                % self._provider_name
            )
        self.model_provider_label.setText(text)
        self.model_provider_label.show()

    def set_provider_notice(self, notice: str) -> None:
        self.provider_notice_label.setText(provider_notice(notice))
        self.provider_notice_label.show()

    def set_stage(self, stage: str | ProgressStage) -> None:
        self.stage_label.setText(_stage_copy(stage))

    def set_progress(self, event: ProgressEvent) -> None:
        self.set_stage(event.stage)
        self.detail_label.setText(progress_detail(event.detail))
        self._apply_metrics(
            self._progress_presenter.update(
                event.overall_completed,
                event.overall_total,
                time.monotonic(),
            )
        )
        if event.total is None:
            self.stage_progress_label.setText(
                QCoreApplication.translate(
                    "PreviewJobDialog", "Stage progress (indeterminate)"
                )
            )
            self.progress_bar.setRange(0, 0)
            self.progress_bar.setFormat("")
        else:
            self.stage_progress_label.setText(
                QCoreApplication.translate("PreviewJobDialog", "Stage progress")
            )
            self.progress_bar.setRange(0, event.total)
            self.progress_bar.setValue(event.completed)
            if event.stage is ProgressStage.DOWNLOADING_MODEL:
                self.progress_bar.setFormat(
                    format_model_download_progress(event.completed, event.total)
                )
            elif event.stage is ProgressStage.DECODE:
                self.progress_bar.setFormat(
                    QCoreApplication.translate("PreviewJobDialog", "%v / %m frames")
                )
            else:
                self.progress_bar.setFormat(
                    QCoreApplication.translate("PreviewJobDialog", "%v / %m")
                )
        if event.overall_completed is not None and event.overall_total is not None:
            self.overall_progress_label.setText(
                QCoreApplication.translate("PreviewJobDialog", "Overall progress")
            )
            self.overall_progress_bar.setRange(0, event.overall_total)
            self.overall_progress_bar.setValue(event.overall_completed)
            self.overall_progress_bar.setFormat(
                QCoreApplication.translate("PreviewJobDialog", "%v / %m frames")
            )
        else:
            self.overall_progress_label.setText(
                QCoreApplication.translate(
                    "PreviewJobDialog", "Overall progress (indeterminate)"
                )
            )
            self.overall_progress_bar.setRange(0, 0)
            self.overall_progress_bar.setFormat("")

    def set_cancelling(self) -> None:
        self.stage_label.setText(
            QCoreApplication.translate("PreviewJobDialog", "Cancelling…")
        )
        self.detail_label.setText(
            QCoreApplication.translate(
                "PreviewJobDialog", "Waiting for the current safe checkpoint…"
            )
        )
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setFormat("")
        self.cancel_button.setEnabled(False)
        self.cancel_button.setText(
            QCoreApplication.translate("PreviewJobDialog", "Cancelling…")
        )

    def show_completion(self, result: ArtifactResult) -> None:
        """Show the finished render summary and keep the modal open for review."""
        output_path = Path(str(result.output_path))
        if result.model_id:
            self.set_job_details(result.model_id)
        if result.execution_provider:
            self.set_execution_provider(result.execution_provider)
        self._completion_visible = True
        self._populate_completion(result, output_path)
        self._show_completion_widgets()

    def _populate_completion(self, result: ArtifactResult, output_path: Path) -> None:
        self.setAccessibleName(
            QCoreApplication.translate("PreviewJobDialog", "Render complete")
        )
        self.setWindowTitle(
            QCoreApplication.translate("PreviewJobDialog", "Render complete")
        )
        self.stage_label.setText(
            QCoreApplication.translate("PreviewJobDialog", "Complete")
        )
        self.detail_label.setText(
            QCoreApplication.translate("PreviewJobDialog", "Output is ready")
        )
        self.output_label.setText(
            QCoreApplication.translate("PreviewJobDialog", "Output: %s")
            % output_path.name
        )
        self.output_label.setToolTip(str(output_path))
        self.output_label.setAccessibleDescription(str(output_path))
        self.output_label.show()
        self.completion_dimensions.setText(
            format_source_dimensions(result.width, result.height)
            or QCoreApplication.translate("PreviewJobDialog", "—")
        )
        self.completion_frames.setText(self._format_frames(result.frame_count))
        self.completion_duration.setText(_format_milliseconds(result.duration_ms))
        self.completion_size.setText(
            format_source_file_size(result.file_size)
            or QCoreApplication.translate("PreviewJobDialog", "—")
        )
        fps = (
            format_source_frame_rate(Fraction(result.output_fps))
            if isinstance(result.output_fps, int)
            and not isinstance(result.output_fps, bool)
            and result.output_fps > 0
            else ""
        )
        self.completion_fps.setText(
            fps or QCoreApplication.translate("PreviewJobDialog", "—")
        )
        self.completion_job_time.setText(_format_milliseconds(result.job_duration_ms))
        self.completion_cuts.setText(
            QCoreApplication.translate("PreviewJobDialog", "Reused existing cuts")
            if result.cuts_reused
            else QCoreApplication.translate("PreviewJobDialog", "Fresh segmentation")
            if result.cuts_reused is False
            else QCoreApplication.translate("PreviewJobDialog", "—")
        )

    def _show_completion_widgets(self) -> None:
        self.stage_progress_label.hide()
        self.progress_bar.hide()
        self.overall_progress_label.hide()
        self.overall_progress_bar.hide()
        self.elapsed_label.hide()
        self.rate_label.hide()
        self.estimate_label.hide()
        self.cancel_button.hide()
        self.completion_summary.show()
        self.completion_actions.show()
        self.activateWindow()
        self.raise_()
        self.close_button.setFocus(Qt.FocusReason.OtherFocusReason)

    def close_for_terminal(self) -> None:
        self._completion_visible = False
        self._elapsed_timer.stop()
        self._terminal_close_requested = True
        self.done(0)
        self._terminal_close_requested = False

    def closeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if not self._terminal_close_requested and not self._completion_visible:
            event.ignore()
            return
        self._completion_visible = False
        self._terminal_close_requested = False
        super().closeEvent(event)

    def reject(self) -> None:
        if self._terminal_close_requested or self._completion_visible:
            super().reject()

    def _refresh_metrics(self) -> None:
        self._apply_metrics(self._progress_presenter.current(time.monotonic()))

    def _apply_metrics(self, metrics: JobProgressMetrics) -> None:
        self.elapsed_label.setText(
            QCoreApplication.translate("PreviewJobDialog", "Elapsed %s")
            % metrics.elapsed
        )
        self.rate_label.setText(metrics.rate)
        self.estimate_label.setText(metrics.estimate)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.KeyPress and isinstance(event, QKeyEvent):
            if event.key() == Qt.Key.Key_Escape:
                if self._completion_visible:
                    self._close_completion()
                else:
                    self._request_cancel()
                return True
        return super().eventFilter(watched, event)

    @Slot()
    def _request_cancel(self) -> None:
        if self._cancel_emitted:
            return
        self._cancel_emitted = True
        self.cancel_requested.emit()

    def _format_frames(self, frame_count: int | None) -> str:
        if (
            isinstance(frame_count, int)
            and not isinstance(frame_count, bool)
            and frame_count >= 0
        ):
            return self.tr("%n frames", "", frame_count)
        return QCoreApplication.translate("PreviewJobDialog", "—")

    @Slot()
    def _close_completion(self) -> None:
        if not self._completion_visible:
            return
        self._completion_visible = False
        self._terminal_close_requested = True
        self.done(0)
        self._terminal_close_requested = False

def _format_milliseconds(value: int | None) -> str:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return format_source_duration(Fraction(value, 1000))
    return QCoreApplication.translate("PreviewJobDialog", "—")


_MODEL_DISPLAY_NAMES = {
    "u2net": "U2Net",
    "u2netp": "U2Netp",
    "u2net_human_seg": "U2Net Human Seg",
    "silueta": "Silueta",
    "isnet-general-use": "ISNet General Use",
    "isnet-anime": "ISNet Anime",
    "birefnet-general": "BiRefNet General",
    "birefnet-general-lite": "BiRefNet General Lite",
    "birefnet-portrait": "BiRefNet Portrait",
    "birefnet-dis": "BiRefNet DIS",
    "birefnet-hrsod": "BiRefNet HRSOD",
    "birefnet-cod": "BiRefNet COD",
    "birefnet-massive": "BiRefNet Massive",
}

_PROVIDER_DISPLAY_NAMES = {
    "CPUExecutionProvider": "CPU",
    "CoreMLExecutionProvider": "Core ML",
    "CUDAExecutionProvider": "CUDA",
    "ROCMExecutionProvider": "ROCm",
    "MIGraphXExecutionProvider": "MIGraphX",
    "DmlExecutionProvider": "DirectML",
}
