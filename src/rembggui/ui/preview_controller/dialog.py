"""Exclusive modal preview job dialog."""

from __future__ import annotations

import time

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from rembggui.jobs.context import ProgressEvent
from rembggui.ui.source_presentation import (
    JobProgressMetrics,
    JobProgressPresenter,
    format_model_download_progress,
)


class PreviewJobDialog(QDialog):
    """One reusable, non-blocking, application-modal job dialog."""

    cancel_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("preview_job_dialog")
        self.setAccessibleName("Preview job")
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setModal(True)
        self.stage_label = QLabel()
        self.stage_label.setObjectName("job_stage")
        self.detail_label = QLabel()
        self.detail_label.setObjectName("job_detail")
        self.model_provider_label = QLabel()
        self.model_provider_label.setObjectName("job_model_provider")
        self.model_provider_label.setAccessibleName("Active model and provider")
        self.output_label = QLabel()
        self.output_label.setObjectName("job_output")
        self.output_label.setAccessibleName("Output target")
        self.provider_notice_label = QLabel()
        self.provider_notice_label.setObjectName("job_provider_notice")
        self.provider_notice_label.setAccessibleName("Provider notice")
        self.elapsed_label = QLabel()
        self.elapsed_label.setObjectName("job_elapsed")
        self.rate_label = QLabel()
        self.rate_label.setObjectName("job_rate")
        self.estimate_label = QLabel()
        self.estimate_label.setObjectName("job_estimate")
        self.progress_bar = QProgressBar()
        self.progress_bar.setObjectName("job_progress")
        self.progress_bar.setAccessibleName("Stage progress")
        self.overall_progress_bar = QProgressBar()
        self.overall_progress_bar.setObjectName("job_overall_progress")
        self.overall_progress_bar.setAccessibleName("Overall progress")
        self.stage_progress_label = QLabel()
        self.stage_progress_label.setObjectName("job_stage_progress_label")
        self.overall_progress_label = QLabel()
        self.overall_progress_label.setObjectName("job_overall_progress_label")
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setObjectName("job_cancel")
        self._assemble_layout()
        self.cancel_button.clicked.connect(self._request_cancel)
        self.installEventFilter(self)
        self.cancel_button.installEventFilter(self)
        self._progress_presenter = JobProgressPresenter()
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(250)
        self._elapsed_timer.timeout.connect(self._refresh_metrics)
        self.reset()

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
        layout.addWidget(self.cancel_button)

    def reset(self, title: str = "Previewing selected frame") -> None:
        self._terminal_close_requested = False
        self.setWindowTitle(title)
        self.stage_label.setText("Preparing model")
        self.detail_label.setText("")
        self.model_provider_label.clear()
        self.model_provider_label.hide()
        self.output_label.clear()
        self.output_label.hide()
        self.provider_notice_label.clear()
        self.provider_notice_label.hide()
        self.stage_progress_label.setText("Stage progress (indeterminate)")
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setFormat("")
        self.overall_progress_label.setText("Overall progress (indeterminate)")
        self.overall_progress_bar.setRange(0, 0)
        self.overall_progress_bar.setFormat("")
        self._apply_metrics(self._progress_presenter.reset(time.monotonic()))
        self._elapsed_timer.start()
        self.cancel_button.setEnabled(True)
        self.cancel_button.setText("Cancel")
        self._cancel_emitted = False
        self._model_name = ""
        self._provider_name = ""

    def set_job_details(
        self, model_id: str, output_filename: str | None = None
    ) -> None:
        self._model_name = _MODEL_DISPLAY_NAMES.get(model_id, model_id)
        self._refresh_model_provider()
        if output_filename:
            self.output_label.setText(f"Output: {output_filename}")
            self.output_label.show()

    def set_execution_provider(self, provider: str) -> None:
        self._provider_name = _PROVIDER_DISPLAY_NAMES.get(provider, provider)
        self._refresh_model_provider()

    def _refresh_model_provider(self) -> None:
        if not self._model_name:
            self.model_provider_label.clear()
            self.model_provider_label.hide()
            return
        text = self._model_name
        if self._provider_name:
            text += f" · {self._provider_name}"
        self.model_provider_label.setText(text)
        self.model_provider_label.show()

    def set_provider_notice(self, notice: str) -> None:
        self.provider_notice_label.setText(notice)
        self.provider_notice_label.show()

    def set_progress(self, event: ProgressEvent) -> None:
        self.stage_label.setText(event.stage)
        self.detail_label.setText(event.detail)
        self._apply_metrics(
            self._progress_presenter.update(
                event.overall_completed,
                event.overall_total,
                time.monotonic(),
            )
        )
        if event.total is None:
            self.stage_progress_label.setText("Stage progress (indeterminate)")
            self.progress_bar.setRange(0, 0)
            self.progress_bar.setFormat("")
        else:
            self.stage_progress_label.setText("Stage progress")
            self.progress_bar.setRange(0, event.total)
            self.progress_bar.setValue(event.completed)
            if event.stage == "Downloading model":
                self.progress_bar.setFormat(
                    format_model_download_progress(event.completed, event.total)
                )
            elif event.stage == "Decode":
                self.progress_bar.setFormat("%v / %m frames")
            else:
                self.progress_bar.setFormat("%v / %m")
        if event.overall_completed is not None and event.overall_total is not None:
            self.overall_progress_label.setText("Overall progress")
            self.overall_progress_bar.setRange(0, event.overall_total)
            self.overall_progress_bar.setValue(event.overall_completed)
            self.overall_progress_bar.setFormat("%v / %m frames")
        else:
            self.overall_progress_label.setText("Overall progress (indeterminate)")
            self.overall_progress_bar.setRange(0, 0)
            self.overall_progress_bar.setFormat("")

    def set_cancelling(self) -> None:
        self.stage_label.setText("Cancelling…")
        self.detail_label.setText("Waiting for the current safe checkpoint…")
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setFormat("")
        self.cancel_button.setEnabled(False)
        self.cancel_button.setText("Cancelling…")

    def close_for_terminal(self) -> None:
        self._elapsed_timer.stop()
        self._terminal_close_requested = True
        self.done(0)
        self._terminal_close_requested = False

    def closeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if not self._terminal_close_requested:
            event.ignore()
            return
        self._terminal_close_requested = False
        super().closeEvent(event)

    def reject(self) -> None:
        if self._terminal_close_requested:
            super().reject()

    def _refresh_metrics(self) -> None:
        self._apply_metrics(self._progress_presenter.current(time.monotonic()))

    def _apply_metrics(self, metrics: JobProgressMetrics) -> None:
        self.elapsed_label.setText(f"Elapsed {metrics.elapsed}")
        self.rate_label.setText(metrics.rate)
        self.estimate_label.setText(metrics.estimate)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.KeyPress and isinstance(event, QKeyEvent):
            if event.key() == Qt.Key.Key_Escape:
                self._request_cancel()
                return True
        return super().eventFilter(watched, event)

    @Slot()
    def _request_cancel(self) -> None:
        if self._cancel_emitted:
            return
        self._cancel_emitted = True
        self.cancel_requested.emit()


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
