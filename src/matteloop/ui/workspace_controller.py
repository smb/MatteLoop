"""Picker lifecycle and safe actions for promoted cut sets."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QMessageBox, QWidget

from matteloop.core.errors import AppError, ErrorCode
from matteloop.core.specs import RenderRequest
from matteloop.jobs.models.catalog import ModelCatalog
from matteloop.jobs.transform_store import discard_transform
from matteloop.jobs.workspace import CutWorkspace, WorkspaceSummary, delete_workspace
from matteloop.ui.workspace_dialog import WorkspacePickerDialog


class WorkspacePickerController:
    """Own the picker widget and its read/open/delete actions."""

    def __init__(
        self,
        *,
        dialog_parent: QWidget | None,
        request_factory: Callable[[], RenderRequest | None],
        active_workspace: Callable[[], CutWorkspace | None],
    ) -> None:
        self.dialog = WorkspacePickerDialog(ModelCatalog.load_resource(), dialog_parent)
        self._request_factory = request_factory
        self._active_workspace = active_workspace
        self.dialog.open_requested.connect(self._open_selected)
        self.dialog.delete_requested.connect(self._delete_selected)

    def open(self, output_directory: Path) -> None:
        self.dialog.load(output_directory)
        self.dialog.open()

    def close(self) -> None:
        self.dialog.close()

    def _open_selected(self, value: object) -> None:
        if isinstance(value, WorkspaceSummary):
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(value.workspace.path)))

    def _delete_selected(self, value: object) -> None:
        if not isinstance(value, WorkspaceSummary):
            return
        active = self._active_workspace()
        if active is not None and active.path == value.workspace.path:
            QMessageBox.warning(
                self.dialog,
                QCoreApplication.translate("WorkspacePicker", "Cut set is in use"),
                QCoreApplication.translate(
                    "WorkspacePicker", "This cut set is being used by a running job."
                ),
            )
            return
        allow_pinned = value.pinned
        if allow_pinned and not self._confirm_pinned_delete():
            return
        try:
            delete_workspace(value.workspace, allow_pinned=allow_pinned)
            discard_transform(value.workspace)
        except AppError as error:
            if error.code is not ErrorCode.CUT_WORKSPACE_PINNED or allow_pinned:
                QMessageBox.warning(
                    self.dialog,
                    QCoreApplication.translate(
                        "WorkspacePicker", "Could not delete cut set"
                    ),
                    str(error),
                )
                return
            if not self._confirm_pinned_delete():
                return
            try:
                delete_workspace(value.workspace, allow_pinned=True)
                discard_transform(value.workspace)
            except AppError:
                return
        request = self._request_factory()
        if request is not None:
            self.dialog.load(request.output.directory)

    def _confirm_pinned_delete(self) -> bool:
        answer = QMessageBox.question(
            self.dialog,
            QCoreApplication.translate("WorkspacePicker", "Delete pinned cut set?"),
            QCoreApplication.translate(
                "WorkspacePicker", "This set is pinned. Delete it anyway?"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes
