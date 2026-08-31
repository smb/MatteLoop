from __future__ import annotations

from matteloop.core.state import JobKind
from matteloop.jobs.models.catalog import ModelCatalog
from matteloop.ui.aligned_rows import ACCESSIBLE_DESCRIPTION_ROLE, STATUS_ROLE
from matteloop.ui.workspace_dialog import SUMMARY_ROLE, WorkspacePickerDialog
from tests.jobs.render_support import job, render_service, request


def test_cut_set_picker_lists_manifest_fields_and_external_edit_status(
    tmp_path, qtbot
) -> None:
    render_request = request(tmp_path)
    artifact = render_service().render(
        render_request, job(tmp_path, "picker", JobKind.RENDER)
    )
    with artifact.cut_workspace.read_promoted_cut(0) as frame:
        frame.save(artifact.cut_workspace.path / "frame-000000.png")

    dialog = WorkspacePickerDialog(ModelCatalog.load_resource())
    qtbot.addWidget(dialog)
    dialog.load(tmp_path)

    assert len(dialog.summaries) == 1
    assert dialog.summaries[0].manifest.edited is True
    item = dialog.cut_set_list.item(0)
    assert item.data(SUMMARY_ROLE) == dialog.summaries[0]
    assert "source.mp4" in item.text()
    assert item.data(STATUS_ROLE) == "edited"
    assert "edited" in item.data(ACCESSIBLE_DESCRIPTION_ROLE)
    assert "frames" in item.data(ACCESSIBLE_DESCRIPTION_ROLE)
    assert dialog.use_button.isEnabled()
    assert dialog.open_button.isEnabled()
    assert dialog.delete_button.isEnabled()
