from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Signal, Slot

from matteloop.core.errors import AppError, ErrorCode
from matteloop.jobs.context import CancellationState

if TYPE_CHECKING:
    from ._controller import ModelRemovalService


class _ModelRemovalWorker(QObject):
    removed = Signal(str, bool)
    failed = Signal(str, object)
    finished = Signal()

    def __init__(self, manager: ModelRemovalService, model_id: str) -> None:
        super().__init__()
        self._manager = manager
        self._model_id = model_id

    @Slot()
    def run(self) -> None:
        try:
            self.removed.emit(self._model_id, self._manager.remove(self._model_id))
        except Exception as error:
            self.failed.emit(self._model_id, error)
        finally:
            self.finished.emit()


class _ModelDownloadWorker(QObject):
    downloaded = Signal(str)
    failed = Signal(str, object)
    progressed = Signal(int, int)
    finished = Signal()

    def __init__(
        self, manager: ModelRemovalService, model_id: str, cancel: CancellationState
    ) -> None:
        super().__init__()
        self._manager = manager
        self._model_id = model_id
        self._cancel = cancel

    @Slot()
    def run(self) -> None:
        try:
            self._manager.fetch(self._model_id, self._report, self._cancelled)
        except Exception as error:
            self.failed.emit(self._model_id, error)
        else:
            self.downloaded.emit(self._model_id)
        finally:
            self.finished.emit()

    def _report(self, completed: int, total: int) -> None:
        self.progressed.emit(completed, total)

    def _cancelled(self) -> bool:
        return self._cancel.requested


class _OutdatedRedownloadWorker(QObject):
    started = Signal(str)
    progressed = Signal(int, int)
    downloaded = Signal(str)
    failed = Signal(str, object)
    finished = Signal()

    def __init__(
        self,
        manager: ModelRemovalService,
        model_ids: tuple[str, ...],
        cancel: CancellationState,
    ) -> None:
        super().__init__()
        self._manager = manager
        self._model_ids = model_ids
        self._cancel = cancel

    @Slot()
    def run(self) -> None:
        try:
            for model_id in self._model_ids:
                if self._cancel.requested:
                    return
                self.started.emit(model_id)
                try:
                    self._manager.fetch(model_id, self._report, self._cancelled)
                    self._manager.remove_obsolete_versions(model_id)
                except AppError as error:
                    if error.code is ErrorCode.JOB_CANCELLED:
                        return
                    self.failed.emit(model_id, error)
                    return
                except Exception as error:
                    self.failed.emit(model_id, error)
                    return
                self.downloaded.emit(model_id)
        finally:
            self.finished.emit()

    def _report(self, completed: int, total: int) -> None:
        self.progressed.emit(completed, total)

    def _cancelled(self) -> bool:
        return self._cancel.requested


class _ObsoleteRemovalWorker(QObject):
    removed = Signal(int)
    failed = Signal(object)
    finished = Signal()

    def __init__(self, manager: ModelRemovalService) -> None:
        super().__init__()
        self._manager = manager

    @Slot()
    def run(self) -> None:
        try:
            self.removed.emit(self._manager.remove_obsolete_versions())
        except Exception as error:
            self.failed.emit(error)
        finally:
            self.finished.emit()
