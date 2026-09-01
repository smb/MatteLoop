from __future__ import annotations

from pathlib import Path
from threading import get_ident

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QMessageBox

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
        self.active_id = active_id
        self.targets = targets or {}
        self.removed: list[str] = []
        self.fetched: list[str] = []
        self.thread_ids: list[int] = []

    def fetch(self, model_id: str, progress=None) -> Path:
        self.thread_ids.append(get_ident())
        self.fetched.append(model_id)
        if progress is not None:
            progress(2_000_000, 8_000_000)
        target = self.targets.get(model_id)
        assert target is not None
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"downloaded-weight")
        return target

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
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
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
        lambda: controller.dialog.entries[
            next(
                index
                for index, entry in enumerate(controller.dialog.entries)
                if entry.model_id == "u2netp"
            )
        ].cached
        is False,
        timeout=5000,
    )
    assert reported == [
        "Removed U²-Net P's downloaded weight.\nFreed 13.0 B."
    ]
    assert not target.exists()
    assert controller._store.state.model_available is True
    controller.close()


def test_model_manager_blocks_active_and_running_job_removal(
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

    assert controller.dialog.remove_button.isEnabled() is False
    assert "active session" in controller.dialog.remove_button.toolTip()

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

    qtbot.waitUntil(lambda: manager.fetched == ["u2netp"], timeout=5000)
    assert manager.thread_ids and manager.thread_ids[0] != get_ident()
    # The dialog reports the same size detail the job popup would, so a
    # multi-hundred-megabyte download is not a frozen-looking button.
    qtbot.waitUntil(
        lambda: any("1.9 MiB of 7.6 MiB" in message for message in messages),
        timeout=5000,
    )
    assert any(message.startswith("Downloading ") for message in messages)
    qtbot.waitUntil(
        lambda: controller.dialog.entries[index].cached, timeout=5000
    )
    assert not controller.dialog.download_button.isEnabled()
    assert controller.dialog.remove_button.isEnabled()
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
    controller.close()
