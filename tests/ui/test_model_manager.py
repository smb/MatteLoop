from __future__ import annotations

import shutil
from pathlib import Path
from threading import Event, get_ident

import pytest
from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QMessageBox

from matteloop.core.errors import AppError, ErrorCode
from matteloop.core.parameters import ParameterState
from matteloop.core.state import (
    ActiveJob,
    AppState,
    FocusTarget,
    JobKind,
    JobState,
    SourceState,
)
from matteloop.jobs.models.catalog import ModelCatalog
from matteloop.ui.aligned_rows import ROW_DATA_ROLE, AlignedRow, AlignedRowDelegate
from matteloop.ui.model_manager import ModelManagerController, ModelManagerDialog
from matteloop.ui.store import ReducerStore


class FakeModelRemovalService:
    def __init__(
        self,
        active_id: str | None = None,
        targets: dict[str, Path] | None = None,
    ) -> None:
        self._active_id = active_id
        self.targets = targets or {}
        self.removed: list[str] = []
        self.fetched: list[str] = []
        self.thread_ids: list[int] = []
        self.obsolete_roots: tuple[Path, ...] = ()
        self.obsolete_calls = 0
        self.operations: list[str] = []
        self.fetch_cancel_checks: list[bool] = []
        self.in_flight = False
        self.block: Event | None = None
        self.block_ids: set[str] = set()
        self.cancel_observed = Event()
        self.fail_ids: dict[str, BaseException] = {}
        self.obsolete_block: Event | None = None

    @property
    def active_id(self) -> str | None:
        """Fail if the GUI reads manager state while fetch holds its lock."""
        if self.in_flight:
            raise AssertionError("manager read while a fetch holds its lock")
        return self._active_id

    def fetch(self, model_id: str, progress=None, cancelled=None) -> Path:
        self.in_flight = True
        try:
            self.thread_ids.append(get_ident())
            self.fetched.append(model_id)
            self.operations.append(f"fetch:{model_id}")
            self.fetch_cancel_checks.append(cancelled is not None)
            if self.block is not None and (
                not self.block_ids or model_id in self.block_ids
            ):
                assert cancelled is not None
                while not cancelled() and not self.block.is_set():
                    self.block.wait(0.01)
                if cancelled():
                    self.cancel_observed.set()
                    raise AppError(
                        ErrorCode.JOB_CANCELLED,
                        "model-download",
                        "error.job.cancelled",
                        "download cancelled",
                        "retry-model-download",
                    )
            error = self.fail_ids.get(model_id)
            if error is not None:
                raise error
            if progress is not None:
                progress(2_000_000, 8_000_000)
            target = self.targets.get(model_id)
            assert target is not None
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"downloaded-weight")
            return target
        finally:
            self.in_flight = False

    def remove_obsolete_versions(self, model_id: str | None = None) -> int:
        self.thread_ids.append(get_ident())
        self.obsolete_calls += 1
        self.operations.append(
            "remove-obsolete" if model_id is None else f"remove-obsolete:{model_id}"
        )
        if self.obsolete_block is not None:
            while not self.obsolete_block.wait(0.01):
                pass
        removed = 0
        for root in self.obsolete_roots:
            target = root if model_id is None else root / model_id
            if target.is_dir():
                shutil.rmtree(target)
                removed += 1
        return removed

    def remove(self, model_id: str) -> bool:
        self.thread_ids.append(get_ident())
        self.removed.append(model_id)
        target = self.targets.get(model_id)
        if target is not None:
            target.unlink()
        return True


def _model_path(root: Path, model_id: str) -> Path:
    catalog = ModelCatalog.load_resource()
    artifact = catalog.get(model_id).artifact
    assert artifact is not None
    return root / catalog.rembg_version / model_id / artifact.runtime_filename


def _outdated_model_path(root: Path, model_id: str) -> Path:
    catalog = ModelCatalog.load_resource()
    artifact = catalog.get(model_id).artifact
    assert artifact is not None
    return (
        root / catalog.obsolete_rembg_versions[0] / model_id / artifact.runtime_filename
    )


def test_model_manager_lists_cache_metadata_with_shared_aligned_rows(
    tmp_path: Path, qtbot
) -> None:
    target = _model_path(tmp_path, "u2netp")
    target.parent.mkdir(parents=True)
    target.write_bytes(b"cached-weight")
    dialog = ModelManagerDialog(
        ModelCatalog.load_resource(),
        tmp_path,
        active_model=lambda: "u2netp",
    )
    qtbot.addWidget(dialog)

    dialog.refresh()

    assert dialog.model_list.count() == 13
    assert isinstance(dialog.model_list.itemDelegate(), AlignedRowDelegate)
    index = next(
        index
        for index, entry in enumerate(dialog.entries)
        if entry.model_id == "u2netp"
    )
    entry = dialog.entries[index]
    assert entry.cached is True
    assert entry.active is True
    assert entry.disk_size_bytes == len(b"cached-weight")
    row = dialog.model_list.item(index).data(ROW_DATA_ROLE)
    assert isinstance(row, AlignedRow)
    assert row.columns[0].text == "U²-Net P"
    assert row.columns[2].text == "cached locally"
    assert row.columns[3].text == "active model"
    assert "cached locally" in dialog.model_list.item(index).toolTip()
    assert "active model" in dialog.model_list.item(index).toolTip()
    assert dialog.total_size_label.text() == "Total on disk: 13.0 B"


def test_model_manager_removes_only_confirmed_selected_weight_in_background(
    tmp_path: Path, monkeypatch, qtbot
) -> None:
    target = _model_path(tmp_path, "u2netp")
    target.parent.mkdir(parents=True)
    target.write_bytes(b"cached-weight")
    manager = FakeModelRemovalService(targets={"u2netp": target})
    controller = ModelManagerController(
        ReducerStore(AppState(model_available=True)),
        catalog=ModelCatalog.load_resource(),
        cache_root=tmp_path,
        manager=manager,
    )
    qtbot.addWidget(controller.dialog)
    controller.open()
    controller.dialog.model_list.setCurrentRow(
        next(
            index
            for index, entry in enumerate(controller.dialog.entries)
            if entry.model_id == "u2netp"
        )
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: int(QMessageBox.StandardButton.Yes),
    )
    reported: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "information",
        lambda _parent, _title, message: reported.append(message),
    )

    controller.dialog.remove_button.click()

    qtbot.waitUntil(lambda: manager.removed == ["u2netp"], timeout=5000)
    assert manager.thread_ids and manager.thread_ids[0] != get_ident()
    qtbot.waitUntil(
        lambda: (
            controller.dialog.entries[
                next(
                    index
                    for index, entry in enumerate(controller.dialog.entries)
                    if entry.model_id == "u2netp"
                )
            ].cached
            is False
        ),
        timeout=5000,
    )
    assert reported == ["Removed U²-Net P's downloaded weight.\nFreed 13.0 B."]
    assert not target.exists()
    assert controller._store.state.model_available is True
    controller.close()


def test_model_manager_removes_weight_after_real_confirmation(
    tmp_path: Path, qtbot
) -> None:
    target = _model_path(tmp_path, "u2netp")
    target.parent.mkdir(parents=True)
    target.write_bytes(b"cached-weight")
    manager = FakeModelRemovalService(targets={"u2netp": target})
    controller = ModelManagerController(
        ReducerStore(AppState(model_available=True)),
        catalog=ModelCatalog.load_resource(),
        cache_root=tmp_path,
        manager=manager,
    )
    qtbot.addWidget(controller.dialog)
    controller.open()
    controller.dialog.model_list.setCurrentRow(
        next(
            index
            for index, entry in enumerate(controller.dialog.entries)
            if entry.model_id == "u2netp"
        )
    )

    def click_active_modal_button() -> None:
        app = QApplication.instance()
        assert isinstance(app, QApplication)
        modal = app.activeModalWidget()
        if not isinstance(modal, QMessageBox):
            return
        yes_button = next(
            (
                button
                for button in modal.buttons()
                if modal.buttonRole(button) == QMessageBox.ButtonRole.YesRole
            ),
            None,
        )
        if yes_button is not None:
            yes_button.click()
            return
        if manager.removed != ["u2netp"]:
            return
        ok_button = next(
            (
                button
                for button in modal.buttons()
                if modal.buttonRole(button) == QMessageBox.ButtonRole.AcceptRole
            ),
            None,
        )
        if ok_button is not None:
            ok_button.click()

    modal_timer = QTimer(controller.dialog)
    modal_timer.setInterval(10)
    modal_timer.timeout.connect(click_active_modal_button)
    modal_timer.start()
    try:
        controller.dialog.remove_button.click()
        qtbot.waitUntil(lambda: manager.removed == ["u2netp"], timeout=5000)
        qtbot.waitUntil(lambda: not target.exists(), timeout=5000)
    finally:
        modal_timer.stop()
    controller.close()


def test_model_manager_allows_active_and_blocks_running_job_removal(
    tmp_path: Path, qtbot
) -> None:
    target = _model_path(tmp_path, "u2netp")
    target.parent.mkdir(parents=True)
    target.write_bytes(b"cached-weight")
    manager = FakeModelRemovalService("u2netp")
    controller = ModelManagerController(
        ReducerStore(),
        catalog=ModelCatalog.load_resource(),
        cache_root=tmp_path,
        manager=manager,
    )
    qtbot.addWidget(controller.dialog)
    controller.open()
    controller.dialog.model_list.setCurrentRow(
        next(
            index
            for index, entry in enumerate(controller.dialog.entries)
            if entry.model_id == "u2netp"
        )
    )

    assert controller.dialog.remove_button.isEnabled() is True

    running = ReducerStore(
        AppState(
            source=SourceState.READY,
            source_id="source-id",
            source_value=object(),
            job_request_id="request-id",
            job=ActiveJob(
                "job-id",
                JobKind.RENDER,
                JobState.RENDERING,
                "Encode",
                FocusTarget.NONE,
            ),
        )
    )
    running_controller = ModelManagerController(
        running,
        catalog=ModelCatalog.load_resource(),
        cache_root=tmp_path,
        manager=FakeModelRemovalService(),
    )
    qtbot.addWidget(running_controller.dialog)
    running_controller.open()
    running_controller.dialog.model_list.setCurrentRow(
        next(
            index
            for index, entry in enumerate(running_controller.dialog.entries)
            if entry.model_id == "u2netp"
        )
    )

    assert running_controller.dialog.remove_button.isEnabled() is False
    assert "job is running" in running_controller.dialog.remove_button.toolTip()
    selected = running_controller.dialog.selected_entry
    assert selected is not None
    running_controller._remove_requested(selected)
    assert running_controller.dialog._message.text() == (
        "Cannot remove a model while a job is running."
    )
    controller.close()
    running_controller.close()


def test_model_manager_opens_the_canonical_cache_directory(
    tmp_path: Path, monkeypatch, qtbot
) -> None:
    controller = ModelManagerController(
        ReducerStore(),
        catalog=ModelCatalog.load_resource(),
        cache_root=tmp_path,
        manager=None,
    )
    qtbot.addWidget(controller.dialog)
    opened: list[QUrl] = []

    def open_url(url: QUrl) -> bool:
        opened.append(url)
        return True

    monkeypatch.setattr(QDesktopServices, "openUrl", open_url)

    controller.dialog.show_cache_button.click()

    assert len(opened) == 1
    assert opened[0].toLocalFile() == str(tmp_path)
    controller.close()


def test_model_manager_creates_the_cache_directory_before_showing_it(
    tmp_path: Path, monkeypatch, qtbot
) -> None:
    # Nothing creates the cache directory before the first download, and
    # QDesktopServices.openUrl on a missing directory fails silently, so the
    # button did nothing at all on a fresh install.
    cache_root = tmp_path / "models"
    controller = ModelManagerController(
        ReducerStore(),
        catalog=ModelCatalog.load_resource(),
        cache_root=cache_root,
        manager=None,
    )
    qtbot.addWidget(controller.dialog)
    opened: list[QUrl] = []

    def open_url(url: QUrl) -> bool:
        opened.append(url)
        return True

    monkeypatch.setattr(QDesktopServices, "openUrl", open_url)

    controller.dialog.show_cache_button.click()

    assert cache_root.is_dir()
    assert [url.toLocalFile() for url in opened] == [str(cache_root)]
    controller.close()


def test_model_manager_downloads_a_missing_weight_in_the_background(
    tmp_path: Path, qtbot
) -> None:
    # Removing a weight was possible from the dialog but getting one back
    # meant running a preview; a manager that can only delete is a trap.
    target = _model_path(tmp_path, "u2netp")
    manager = FakeModelRemovalService(targets={"u2netp": target})
    controller = ModelManagerController(
        ReducerStore(AppState()),
        catalog=ModelCatalog.load_resource(),
        cache_root=tmp_path,
        manager=manager,
    )
    qtbot.addWidget(controller.dialog)
    controller.open()
    index = next(
        position
        for position, entry in enumerate(controller.dialog.entries)
        if entry.model_id == "u2netp"
    )
    controller.dialog.model_list.setCurrentRow(index)

    assert controller.dialog.download_button.isEnabled()
    assert not controller.dialog.remove_button.isEnabled()
    messages: list[str] = []
    original = controller.dialog.set_message

    def record(message: str) -> None:
        messages.append(message)
        original(message)

    controller.dialog.set_message = record  # type: ignore[method-assign]

    controller.dialog.download_button.click()

    assert controller.dialog.cancel_button.isVisible()
    qtbot.waitUntil(lambda: manager.fetched == ["u2netp"], timeout=5000)
    assert manager.fetch_cancel_checks == [True]
    assert manager.thread_ids and manager.thread_ids[0] != get_ident()
    # The dialog reports the same size detail the job popup would, so a
    # multi-hundred-megabyte download is not a frozen-looking button.
    qtbot.waitUntil(
        lambda: any("1.9 MiB of 7.6 MiB" in message for message in messages),
        timeout=5000,
    )
    assert any(message.startswith("Downloading ") for message in messages)
    # Waits for the worker thread to clear the dialog's busy state too, which
    # is what re-enables the buttons.
    qtbot.waitUntil(
        lambda: (
            controller.dialog.entries[index].cached
            and controller.dialog.remove_button.isEnabled()
        ),
        timeout=5000,
    )
    assert not controller.dialog.download_button.isEnabled()
    assert controller.dialog.cancel_button.isHidden()
    controller.close()


def test_model_manager_refuses_to_download_while_a_job_runs(
    tmp_path: Path, qtbot
) -> None:
    manager = FakeModelRemovalService(
        targets={"u2netp": _model_path(tmp_path, "u2netp")}
    )
    controller = ModelManagerController(
        ReducerStore(
            AppState(
                source=SourceState.READY,
                source_id="source-id",
                source_value=object(),
                job_request_id="request-id",
                job=ActiveJob(
                    "job-id",
                    JobKind.RENDER,
                    JobState.RENDERING,
                    "Encode",
                    FocusTarget.NONE,
                ),
            )
        ),
        catalog=ModelCatalog.load_resource(),
        cache_root=tmp_path,
        manager=manager,
    )
    qtbot.addWidget(controller.dialog)
    controller.open()
    controller.dialog.model_list.setCurrentRow(
        next(
            position
            for position, entry in enumerate(controller.dialog.entries)
            if entry.model_id == "u2netp"
        )
    )

    controller.dialog.download_button.click()

    assert manager.fetched == []
    assert controller.dialog._message.text() == (
        "Cannot download a model while a job is running."
    )
    controller.close()


def test_model_manager_lists_an_outdated_weight_with_size_and_rembg_version(
    tmp_path: Path, qtbot
) -> None:
    target = _outdated_model_path(tmp_path, "u2netp")
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old-weight")
    dialog = ModelManagerDialog(
        ModelCatalog.load_resource(),
        tmp_path,
        active_model=lambda: "u2netp",
    )
    qtbot.addWidget(dialog)

    dialog.refresh()

    index = next(
        index
        for index, entry in enumerate(dialog.entries)
        if entry.model_id == "u2netp"
    )
    entry = dialog.entries[index]
    row = dialog.model_list.item(index).data(ROW_DATA_ROLE)
    assert entry.cached is False
    assert entry.outdated_size_bytes == len(b"old-weight")
    assert row.glyph == "⟳"
    assert row.columns[2].text == "outdated weight"
    assert "rembg 2.0.72" in row.accessible_description
    assert dialog.outdated_notice_label.text() == (
        "Outdated weights from rembg 2.0.72 occupy 10.0 B on disk and cannot "
        "be used by this version."
    )
    assert dialog.delete_outdated_button.isHidden() is False
    assert dialog.redownload_outdated_button.isHidden() is False


def test_model_manager_reports_and_deletes_outdated_copy_alongside_current_weight(
    tmp_path: Path, monkeypatch, qtbot
) -> None:
    current = _model_path(tmp_path, "u2netp")
    outdated = _outdated_model_path(tmp_path, "u2netp")
    current.parent.mkdir(parents=True)
    current.write_bytes(b"current-weight")
    outdated.parent.mkdir(parents=True)
    outdated.write_bytes(b"old-weight")
    unlisted = tmp_path / "2.0.72" / "bria-rmbg" / "model.onnx"
    unlisted.parent.mkdir(parents=True)
    unlisted.write_bytes(b"unlisted-weight")
    manager = FakeModelRemovalService()
    manager.obsolete_roots = (tmp_path / "2.0.72",)
    controller = ModelManagerController(
        ReducerStore(AppState()),
        catalog=ModelCatalog.load_resource(),
        cache_root=tmp_path,
        manager=manager,
    )
    qtbot.addWidget(controller.dialog)
    controller.open()

    index = next(
        index
        for index, entry in enumerate(controller.dialog.entries)
        if entry.model_id == "u2netp"
    )
    entry = controller.dialog.entries[index]
    assert entry.cached is True
    assert entry.outdated_size_bytes == len(b"old-weight")
    assert entry in controller.dialog.outdated_entries
    assert controller.dialog.delete_outdated_button.isVisible()
    assert controller.dialog.total_size_label.text() == "Total on disk: 39.0 B"
    assert (
        "outdated copy from rembg 2.0.72"
        in controller.dialog.model_list.item(index).toolTip()
    )
    assert controller.dialog.outdated_notice_label.text() == (
        "Outdated weights from rembg 2.0.72 occupy 25.0 B on disk and cannot "
        "be used by this version."
    )

    confirmation: list[tuple[str, str]] = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda _parent, title, message, *_args: (
            confirmation.append((title, message)) or int(QMessageBox.StandardButton.Yes)
        ),
    )
    controller.dialog.delete_outdated_button.click()

    assert confirmation == [
        (
            "Delete all weights from rembg 2.0.72?",
            "This removes the whole 2.0.72 directory: 25.0 B on disk. "
            "Weights this version needs are downloaded again on demand.",
        )
    ]
    qtbot.waitUntil(lambda: manager.obsolete_calls == 1, timeout=5000)
    qtbot.waitUntil(lambda: controller._remove_thread is None, timeout=5000)
    assert not manager.obsolete_roots[0].exists()
    assert current.exists()
    controller.close()


def test_model_manager_deletes_outdated_directory_without_touching_other_cache(
    tmp_path: Path, monkeypatch, qtbot
) -> None:
    outdated = _outdated_model_path(tmp_path, "u2netp")
    outdated.parent.mkdir(parents=True)
    outdated.write_bytes(b"old-weight")
    unrelated = tmp_path / "not-obsolete" / "keep.txt"
    unrelated.parent.mkdir()
    unrelated.write_bytes(b"keep")
    manager = FakeModelRemovalService()
    manager.obsolete_roots = (tmp_path / "2.0.72",)
    controller = ModelManagerController(
        ReducerStore(AppState()),
        catalog=ModelCatalog.load_resource(),
        cache_root=tmp_path,
        manager=manager,
    )
    qtbot.addWidget(controller.dialog)
    controller.open()
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: int(QMessageBox.StandardButton.Yes),
    )

    controller.dialog.delete_outdated_button.click()

    qtbot.waitUntil(lambda: manager.obsolete_calls == 1, timeout=5000)
    qtbot.waitUntil(lambda: controller._remove_thread is None, timeout=5000)
    assert not outdated.exists()
    assert unrelated.read_bytes() == b"keep"
    controller.close()


def test_model_manager_shows_re_download_only_when_an_outdated_row_lacks_a_current_weight(  # noqa: E501
    tmp_path: Path, qtbot
) -> None:
    outdated = _outdated_model_path(tmp_path, "u2netp")
    outdated.parent.mkdir(parents=True)
    outdated.write_bytes(b"old-weight")
    controller = ModelManagerController(
        ReducerStore(),
        catalog=ModelCatalog.load_resource(),
        cache_root=tmp_path,
        manager=FakeModelRemovalService(),
    )
    qtbot.addWidget(controller.dialog)
    controller.open()

    assert controller.dialog.redownload_outdated_button.isVisible()
    assert controller.dialog.delete_outdated_button.isVisible()
    controller.close()

    current = _model_path(tmp_path, "u2netp")
    current.parent.mkdir(parents=True)
    current.write_bytes(b"current-weight")
    cached_controller = ModelManagerController(
        ReducerStore(),
        catalog=ModelCatalog.load_resource(),
        cache_root=tmp_path,
        manager=FakeModelRemovalService(),
    )
    qtbot.addWidget(cached_controller.dialog)
    cached_controller.open()

    assert cached_controller.dialog.redownload_outdated_button.isHidden()
    assert cached_controller.dialog.delete_outdated_button.isVisible()
    cached_controller.close()


def test_model_manager_confirms_the_download_size_before_re_downloading_outdated_weights(  # noqa: E501
    tmp_path: Path, monkeypatch, qtbot
) -> None:
    for model_id in ("u2netp", "silueta"):
        outdated = _outdated_model_path(tmp_path, model_id)
        outdated.parent.mkdir(parents=True)
        outdated.write_bytes(b"old-weight")
    manager = FakeModelRemovalService(
        targets={
            model_id: _model_path(tmp_path, model_id)
            for model_id in ("u2netp", "silueta")
        }
    )
    controller = ModelManagerController(
        ReducerStore(),
        catalog=ModelCatalog.load_resource(),
        cache_root=tmp_path,
        manager=manager,
    )
    qtbot.addWidget(controller.dialog)
    controller.open()
    confirmation: list[tuple[str, str]] = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda _parent, title, body, *_args: (
            confirmation.append((title, body)) or int(QMessageBox.StandardButton.No)
        ),
    )

    controller.dialog.redownload_outdated_button.click()

    assert confirmation == [
        (
            "Re-download outdated model weights?",
            "Download 2 model weight(s) (46.5 MiB)? "
            "Each outdated copy from rembg 2.0.72 is deleted once its replacement "
            "has been verified. Other files in the 2.0.72 directory stay until you "
            "use Delete outdated.",
        )
    ]
    assert manager.fetched == []
    controller.close()


def test_model_manager_re_downloads_each_outdated_weight_then_deletes_its_old_copy(
    tmp_path: Path, monkeypatch, qtbot
) -> None:
    model_ids = ("u2netp", "silueta")
    for model_id in model_ids:
        outdated = _outdated_model_path(tmp_path, model_id)
        outdated.parent.mkdir(parents=True)
        outdated.write_bytes(b"old-weight")
    unlisted = tmp_path / "2.0.72" / "bria-rmbg" / "model.onnx"
    unlisted.parent.mkdir(parents=True)
    unlisted.write_bytes(b"unlisted-weight")
    manager = FakeModelRemovalService(
        targets={model_id: _model_path(tmp_path, model_id) for model_id in model_ids},
    )
    manager.block = Event()
    manager.block_ids = {"silueta"}
    manager.obsolete_roots = (tmp_path / "2.0.72",)
    controller = ModelManagerController(
        ReducerStore(AppState(parameters=ParameterState(model_id="u2netp"))),
        catalog=ModelCatalog.load_resource(),
        cache_root=tmp_path,
        manager=manager,
    )
    qtbot.addWidget(controller.dialog)
    controller.open()
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: int(QMessageBox.StandardButton.Yes),
    )

    controller.dialog.redownload_outdated_button.click()

    qtbot.waitUntil(
        lambda: "Silueta (2 of 2)" in controller.dialog._message.text(), timeout=5000
    )
    manager.block.set()
    qtbot.waitUntil(lambda: controller._remove_thread is None, timeout=5000)
    assert manager.operations == [
        "fetch:u2netp",
        "remove-obsolete:u2netp",
        "fetch:silueta",
        "remove-obsolete:silueta",
    ]
    assert manager.fetch_cancel_checks == [True, True]
    assert manager.thread_ids
    assert all(thread_id != get_ident() for thread_id in manager.thread_ids)
    assert not _outdated_model_path(tmp_path, "u2netp").exists()
    assert not _outdated_model_path(tmp_path, "silueta").exists()
    assert unlisted.read_bytes() == b"unlisted-weight"
    assert controller.dialog._message.text() == (
        "Re-downloaded 2 outdated model weight(s)."
    )
    assert controller.dialog.outdated_notice_label.text() == (
        "Outdated weights from rembg 2.0.72 occupy 15.0 B on disk and cannot "
        "be used by this version."
    )
    assert controller.dialog.redownload_outdated_button.isHidden()
    assert controller.dialog.delete_outdated_button.isVisible()
    assert controller._store.state.model_available is True
    controller.close()


def test_model_manager_shows_the_batch_position_while_downloading(
    tmp_path: Path, monkeypatch, qtbot
) -> None:
    model_ids = ("u2netp", "silueta")
    for model_id in model_ids:
        outdated = _outdated_model_path(tmp_path, model_id)
        outdated.parent.mkdir(parents=True)
        outdated.write_bytes(b"old-weight")
    manager = FakeModelRemovalService(
        targets={model_id: _model_path(tmp_path, model_id) for model_id in model_ids}
    )
    manager.obsolete_roots = (tmp_path / "2.0.72",)
    controller = ModelManagerController(
        ReducerStore(),
        catalog=ModelCatalog.load_resource(),
        cache_root=tmp_path,
        manager=manager,
    )
    qtbot.addWidget(controller.dialog)
    controller.open()
    messages: list[str] = []
    original = controller.dialog.set_message

    def record(message: str) -> None:
        messages.append(message)
        original(message)

    controller.dialog.set_message = record  # type: ignore[method-assign]
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: int(QMessageBox.StandardButton.Yes),
    )

    controller.dialog.redownload_outdated_button.click()

    qtbot.waitUntil(lambda: controller._remove_thread is None, timeout=5000)
    assert any("Silueta (2 of 2)" in message for message in messages)
    assert any("Downloaded U²-Net P (1 of 2)." in message for message in messages)
    controller.close()


def test_model_manager_cancel_stops_the_batch_and_keeps_the_rest_outdated(
    tmp_path: Path, monkeypatch, qtbot
) -> None:
    model_ids = ("u2netp", "silueta")
    for model_id in model_ids:
        outdated = _outdated_model_path(tmp_path, model_id)
        outdated.parent.mkdir(parents=True)
        outdated.write_bytes(b"old-weight")
    manager = FakeModelRemovalService(
        targets={model_id: _model_path(tmp_path, model_id) for model_id in model_ids}
    )
    manager.block = Event()
    manager.block_ids = {"u2netp"}
    controller = ModelManagerController(
        ReducerStore(),
        catalog=ModelCatalog.load_resource(),
        cache_root=tmp_path,
        manager=manager,
    )
    qtbot.addWidget(controller.dialog)
    controller.open()
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: int(QMessageBox.StandardButton.Yes),
    )

    controller.dialog.redownload_outdated_button.click()
    qtbot.waitUntil(lambda: manager.in_flight, timeout=5000)
    controller.dialog.cancel_button.click()

    assert controller.dialog.cancel_button.text() == "Cancelling…"
    assert controller.dialog.cancel_button.isEnabled() is False
    qtbot.waitUntil(manager.cancel_observed.is_set, timeout=5000)
    qtbot.waitUntil(lambda: controller._remove_thread is None, timeout=5000)
    assert manager.fetched == ["u2netp"]
    assert not _model_path(tmp_path, "u2netp").exists()
    assert _outdated_model_path(tmp_path, "u2netp").exists()
    assert _outdated_model_path(tmp_path, "silueta").exists()
    assert controller.dialog._message.text() == (
        "Download cancelled. Re-downloaded 0 of 2 outdated model weight(s)."
    )
    assert controller.dialog.outdated_notice_label.isVisible()
    assert controller.dialog.redownload_outdated_button.isEnabled()
    assert controller.dialog.cancel_button.isHidden()
    assert controller.dialog.close_button.isEnabled()
    controller.close()


@pytest.mark.parametrize("dismissal", ("reject", "close"))
def test_model_manager_escape_cancels_a_running_download_instead_of_hiding_the_dialog(
    tmp_path: Path, monkeypatch, qtbot, dismissal: str
) -> None:
    outdated = _outdated_model_path(tmp_path, "u2netp")
    outdated.parent.mkdir(parents=True)
    outdated.write_bytes(b"old-weight")
    manager = FakeModelRemovalService(
        targets={"u2netp": _model_path(tmp_path, "u2netp")}
    )
    manager.block = Event()
    manager.block_ids = {"u2netp"}
    controller = ModelManagerController(
        ReducerStore(),
        catalog=ModelCatalog.load_resource(),
        cache_root=tmp_path,
        manager=manager,
    )
    qtbot.addWidget(controller.dialog)
    controller.open()
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: int(QMessageBox.StandardButton.Yes),
    )
    controller.dialog.redownload_outdated_button.click()
    qtbot.waitUntil(lambda: manager.in_flight, timeout=5000)

    if dismissal == "reject":
        controller.dialog.reject()
    else:
        controller.dialog.close()

    assert controller.dialog.isVisible()
    qtbot.waitUntil(manager.cancel_observed.is_set, timeout=5000)
    qtbot.waitUntil(lambda: controller._remove_thread is None, timeout=5000)
    assert controller.dialog._message.text().startswith("Download cancelled.")
    controller.close()


def test_model_manager_ignores_dismissal_while_a_removal_runs(
    tmp_path: Path, monkeypatch, qtbot
) -> None:
    outdated = _outdated_model_path(tmp_path, "u2netp")
    outdated.parent.mkdir(parents=True)
    outdated.write_bytes(b"old-weight")
    manager = FakeModelRemovalService()
    manager.obsolete_roots = (tmp_path / "2.0.72",)
    manager.obsolete_block = Event()
    controller = ModelManagerController(
        ReducerStore(),
        catalog=ModelCatalog.load_resource(),
        cache_root=tmp_path,
        manager=manager,
    )
    qtbot.addWidget(controller.dialog)
    controller.open()
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: int(QMessageBox.StandardButton.Yes),
    )
    controller.dialog.delete_outdated_button.click()
    qtbot.waitUntil(lambda: manager.obsolete_calls == 1, timeout=5000)

    controller.dialog.reject()

    assert controller.dialog.isVisible()
    assert controller._cancel is None
    manager.obsolete_block.set()
    qtbot.waitUntil(lambda: controller._remove_thread is None, timeout=5000)
    controller.dialog.reject()
    assert controller.dialog.isVisible() is False
    controller.close()


def test_model_manager_stops_the_batch_at_the_first_failed_download(
    tmp_path: Path, monkeypatch, qtbot
) -> None:
    model_ids = ("u2netp", "silueta", "birefnet-dis")
    for model_id in model_ids:
        outdated = _outdated_model_path(tmp_path, model_id)
        outdated.parent.mkdir(parents=True)
        outdated.write_bytes(b"old-weight")
    manager = FakeModelRemovalService(
        targets={model_id: _model_path(tmp_path, model_id) for model_id in model_ids}
    )
    manager.fail_ids["silueta"] = OSError("no route")
    manager.obsolete_roots = (tmp_path / "2.0.72",)
    controller = ModelManagerController(
        ReducerStore(),
        catalog=ModelCatalog.load_resource(),
        cache_root=tmp_path,
        manager=manager,
    )
    qtbot.addWidget(controller.dialog)
    controller.open()
    warnings: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, _title, message: warnings.append(message),
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: int(QMessageBox.StandardButton.Yes),
    )

    controller.dialog.redownload_outdated_button.click()

    qtbot.waitUntil(lambda: controller._remove_thread is None, timeout=5000)
    assert manager.operations == [
        "fetch:u2netp",
        "remove-obsolete:u2netp",
        "fetch:silueta",
    ]
    assert _model_path(tmp_path, "u2netp").exists()
    assert _outdated_model_path(tmp_path, "u2netp").exists() is False
    assert _outdated_model_path(tmp_path, "silueta").exists()
    assert _outdated_model_path(tmp_path, "birefnet-dis").exists()
    failure_name = next(
        entry.display_name
        for entry in controller.dialog.entries
        if entry.model_id == "silueta"
    )
    assert controller.dialog._message.text() == (
        f"Could not download {failure_name}: no route. "
        "Re-downloaded 1 of 3 outdated model weight(s)."
    )
    assert warnings == []
    assert controller.dialog.redownload_outdated_button.isVisible()
    controller.close()


def test_model_manager_close_cancels_the_running_download_before_waiting(
    tmp_path: Path, monkeypatch, qtbot
) -> None:
    outdated = _outdated_model_path(tmp_path, "u2netp")
    outdated.parent.mkdir(parents=True)
    outdated.write_bytes(b"old-weight")
    manager = FakeModelRemovalService(
        targets={"u2netp": _model_path(tmp_path, "u2netp")}
    )
    manager.block = Event()
    manager.block_ids = {"u2netp"}
    controller = ModelManagerController(
        ReducerStore(),
        catalog=ModelCatalog.load_resource(),
        cache_root=tmp_path,
        manager=manager,
    )
    qtbot.addWidget(controller.dialog)
    controller.open()
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: int(QMessageBox.StandardButton.Yes),
    )
    controller.dialog.redownload_outdated_button.click()
    qtbot.waitUntil(lambda: manager.in_flight, timeout=5000)

    controller.close()

    assert manager.cancel_observed.is_set()
    assert manager.in_flight is False
    assert controller.dialog.isVisible() is False


def test_model_manager_close_cancels_a_single_download_before_waiting(
    tmp_path: Path, qtbot
) -> None:
    target = _model_path(tmp_path, "u2netp")
    manager = FakeModelRemovalService(targets={"u2netp": target})
    manager.block = Event()
    manager.block_ids = {"u2netp"}
    controller = ModelManagerController(
        ReducerStore(),
        catalog=ModelCatalog.load_resource(),
        cache_root=tmp_path,
        manager=manager,
    )
    qtbot.addWidget(controller.dialog)
    controller.open()
    index = next(
        index
        for index, entry in enumerate(controller.dialog.entries)
        if entry.model_id == "u2netp"
    )
    controller.dialog.model_list.setCurrentRow(index)
    controller.dialog.download_button.click()
    qtbot.waitUntil(lambda: manager.in_flight, timeout=5000)

    controller.close()

    assert manager.cancel_observed.is_set()
    assert manager.in_flight is False
    assert controller.dialog.isVisible() is False


def test_model_manager_single_download_can_be_cancelled_without_a_warning_box(
    tmp_path: Path, monkeypatch, qtbot
) -> None:
    target = _model_path(tmp_path, "u2netp")
    manager = FakeModelRemovalService(targets={"u2netp": target})
    manager.block = Event()
    manager.block_ids = {"u2netp"}
    controller = ModelManagerController(
        ReducerStore(),
        catalog=ModelCatalog.load_resource(),
        cache_root=tmp_path,
        manager=manager,
    )
    qtbot.addWidget(controller.dialog)
    controller.open()
    index = next(
        index
        for index, entry in enumerate(controller.dialog.entries)
        if entry.model_id == "u2netp"
    )
    controller.dialog.model_list.setCurrentRow(index)
    warnings: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, _title, message: warnings.append(message),
    )
    controller.dialog.download_button.click()
    qtbot.waitUntil(lambda: manager.in_flight, timeout=5000)

    controller.dialog.cancel_button.click()

    qtbot.waitUntil(manager.cancel_observed.is_set, timeout=5000)
    qtbot.waitUntil(lambda: controller._remove_thread is None, timeout=5000)
    assert controller.dialog._message.text() == "Download cancelled."
    assert warnings == []
    controller.close()


def test_model_manager_refuses_re_download_while_a_job_runs(
    tmp_path: Path, monkeypatch, qtbot
) -> None:
    outdated = _outdated_model_path(tmp_path, "u2netp")
    outdated.parent.mkdir(parents=True)
    outdated.write_bytes(b"old-weight")
    manager = FakeModelRemovalService()
    running = ReducerStore(
        AppState(
            source=SourceState.READY,
            source_id="source-id",
            source_value=object(),
            job_request_id="request-id",
            job=ActiveJob(
                "job-id",
                JobKind.RENDER,
                JobState.RENDERING,
                "Encode",
                FocusTarget.NONE,
            ),
        )
    )
    controller = ModelManagerController(
        running,
        catalog=ModelCatalog.load_resource(),
        cache_root=tmp_path,
        manager=manager,
    )
    qtbot.addWidget(controller.dialog)
    controller.open()
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: int(QMessageBox.StandardButton.Yes),
    )

    controller.dialog.redownload_outdated_button.click()

    assert controller.dialog._message.text() == (
        "Cannot download a model while a job is running."
    )
    assert manager.fetched == []
    controller.close()


def test_model_manager_refreshes_rows_after_outdated_removal_fails(
    tmp_path: Path, monkeypatch, qtbot
) -> None:
    outdated = _outdated_model_path(tmp_path, "u2netp")
    outdated.parent.mkdir(parents=True)
    outdated.write_bytes(b"old-weight")
    manager = FakeModelRemovalService()
    manager.obsolete_roots = (tmp_path / "2.0.72",)
    controller = ModelManagerController(
        ReducerStore(AppState()),
        catalog=ModelCatalog.load_resource(),
        cache_root=tmp_path,
        manager=manager,
    )
    qtbot.addWidget(controller.dialog)
    controller.open()

    def fail_after_external_removal() -> int:
        outdated.unlink()
        raise PermissionError("cache is read-only")

    monkeypatch.setattr(
        manager, "remove_obsolete_versions", fail_after_external_removal
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: int(QMessageBox.StandardButton.Yes),
    )

    controller.dialog.delete_outdated_button.click()

    qtbot.waitUntil(lambda: controller._remove_thread is None, timeout=5000)
    entry = next(
        entry for entry in controller.dialog.entries if entry.model_id == "u2netp"
    )
    assert entry.outdated_size_bytes is None
    assert controller.dialog.delete_outdated_button.isHidden()
    assert controller.dialog.outdated_notice_label.isHidden()
    assert controller.dialog._message.text() == (
        "Could not remove outdated weights: PermissionError: cache is read-only"
    )
    controller.close()


def test_model_manager_refuses_outdated_removal_while_a_job_runs(
    tmp_path: Path, qtbot
) -> None:
    target = _outdated_model_path(tmp_path, "u2netp")
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old-weight")
    manager = FakeModelRemovalService()
    running = ReducerStore(
        AppState(
            source=SourceState.READY,
            source_id="source-id",
            source_value=object(),
            job_request_id="request-id",
            job=ActiveJob(
                "job-id",
                JobKind.RENDER,
                JobState.RENDERING,
                "Encode",
                FocusTarget.NONE,
            ),
        )
    )
    controller = ModelManagerController(
        running,
        catalog=ModelCatalog.load_resource(),
        cache_root=tmp_path,
        manager=manager,
    )
    qtbot.addWidget(controller.dialog)
    controller.open()

    controller.dialog.delete_outdated_button.click()
    assert controller.dialog._message.text() == (
        "Cannot remove a model while a job is running."
    )
    assert manager.obsolete_calls == 0
    controller.close()
