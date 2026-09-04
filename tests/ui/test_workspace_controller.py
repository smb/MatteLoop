"""Picker lifecycle and safe actions for promoted cut sets."""

from __future__ import annotations

from matteloop.core.specs import TransformSpec
from matteloop.core.state import JobKind
from matteloop.jobs.render import FilesystemWorkspacePort
from matteloop.jobs.transform_store import store_transform, transform_sidecar_path
from matteloop.jobs.workspace import WorkspaceSummary
from matteloop.ui.workspace_controller import WorkspacePickerController
from tests.jobs.render_support import job, render_service, request


def test_delete_selected_removes_the_transform_sidecar_with_the_cut(
    tmp_path, qtbot
) -> None:
    artifact = render_service(workspace=FilesystemWorkspacePort()).render(
        request(tmp_path), job(tmp_path, "seed-delete", JobKind.RENDER)
    )
    workspace = artifact.cut_workspace
    store_transform(workspace, TransformSpec(first_frame=1), [])
    sidecar = transform_sidecar_path(workspace)
    assert sidecar.exists()

    controller = WorkspacePickerController(
        dialog_parent=None,
        request_factory=lambda: request(tmp_path),
        active_workspace=lambda: None,
    )
    qtbot.addWidget(controller.dialog)

    controller._delete_selected(  # noqa: SLF001
        WorkspaceSummary(workspace, artifact.manifest, 0)
    )

    assert not workspace.path.exists()
    assert not sidecar.exists()
