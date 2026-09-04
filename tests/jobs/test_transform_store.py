"""The dot-prefixed sidecar that persists a cut's applied transform (D1)."""

from __future__ import annotations

import json
from pathlib import Path

from matteloop.core.specs import CropSpec, MismatchMode, ResizeSpec, TransformSpec
from matteloop.core.state import JobKind
from matteloop.jobs.render import FilesystemWorkspacePort
from matteloop.jobs.transform_store import (
    TRANSFORM_SIDECAR_SCHEMA,
    TRANSFORM_SIDECAR_VERSION,
    discard_transform,
    load_transform,
    store_transform,
    transform_sidecar_path,
)
from matteloop.jobs.workspace import list_workspaces, validate_cut_set
from tests.jobs.render_support import job, render_service, request


def _seeded_cut(tmp_path: Path):
    workspace = FilesystemWorkspacePort()
    original = render_service(workspace=workspace).render(
        request(tmp_path), job(tmp_path, "seed-transform-store", JobKind.RENDER)
    )
    return original.cut_workspace


def test_round_trip_through_the_sidecar(tmp_path: Path) -> None:
    workspace = _seeded_cut(tmp_path)
    spec = TransformSpec(
        first_frame=1,
        crop=CropSpec(0, 0, 64, 64),
        resize=ResizeSpec(width=200, height=150, mismatch=MismatchMode.PAD),
    )
    notes: list[str] = []

    store_transform(workspace, spec, notes)

    assert notes == []
    path = transform_sidecar_path(workspace)
    assert path.exists()
    assert path.parent == workspace.cuts_root
    assert load_transform(workspace) == spec


def test_missing_sidecar_is_identity(tmp_path: Path) -> None:
    workspace = _seeded_cut(tmp_path)

    assert load_transform(workspace) == TransformSpec()


def test_corrupt_json_falls_back_to_identity_with_a_note(tmp_path: Path) -> None:
    workspace = _seeded_cut(tmp_path)
    transform_sidecar_path(workspace).write_text("{not json", encoding="utf-8")
    notes: list[str] = []

    result = load_transform(workspace, notes)

    assert result == TransformSpec()
    assert len(notes) == 1
    assert "corrupt" in notes[0]


def test_wrong_schema_version_falls_back_to_identity_with_a_note(
    tmp_path: Path,
) -> None:
    workspace = _seeded_cut(tmp_path)
    payload = {
        "schema": TRANSFORM_SIDECAR_SCHEMA,
        "schema_version": TRANSFORM_SIDECAR_VERSION + 1,
        "cache_key": workspace.cache_key,
        "transform": {
            "first_frame": 0,
            "last_frame": None,
            "crop": None,
            "resize": None,
        },
    }
    transform_sidecar_path(workspace).write_text(json.dumps(payload), encoding="utf-8")
    notes: list[str] = []

    result = load_transform(workspace, notes)

    assert result == TransformSpec()
    assert len(notes) == 1


def test_wrong_cache_key_falls_back_to_identity_with_a_note(tmp_path: Path) -> None:
    workspace = _seeded_cut(tmp_path)
    payload = {
        "schema": TRANSFORM_SIDECAR_SCHEMA,
        "schema_version": TRANSFORM_SIDECAR_VERSION,
        "cache_key": "f" * 64,
        "transform": {
            "first_frame": 1,
            "last_frame": None,
            "crop": None,
            "resize": None,
        },
    }
    transform_sidecar_path(workspace).write_text(json.dumps(payload), encoding="utf-8")
    notes: list[str] = []

    result = load_transform(workspace, notes)

    assert result == TransformSpec()
    assert len(notes) == 1


def test_identity_store_removes_an_existing_sidecar(tmp_path: Path) -> None:
    workspace = _seeded_cut(tmp_path)
    store_transform(workspace, TransformSpec(first_frame=1), [])
    assert transform_sidecar_path(workspace).exists()

    store_transform(workspace, TransformSpec(), [])

    assert not transform_sidecar_path(workspace).exists()


def test_discard_transform_on_a_missing_file_is_silent(tmp_path: Path) -> None:
    workspace = _seeded_cut(tmp_path)
    assert not transform_sidecar_path(workspace).exists()

    discard_transform(workspace)  # must not raise

    assert not transform_sidecar_path(workspace).exists()


def test_store_transform_notes_a_failed_sidecar_removal_but_still_completes(
    tmp_path: Path, monkeypatch
) -> None:
    """A non-identity transform followed by an identity rebuild removes the
    old sidecar; if the unlink itself fails, the stale sidecar would silently
    restore a transform the last render did not apply. The job must still
    complete (G4), but the failure must not vanish -- it belongs in notes."""
    workspace = _seeded_cut(tmp_path)
    store_transform(workspace, TransformSpec(first_frame=1), [])
    sidecar = transform_sidecar_path(workspace)
    assert sidecar.exists()
    real_unlink = Path.unlink

    def failing_unlink(self: Path, *args: object, **kwargs: object) -> None:
        if self == sidecar:
            raise PermissionError("simulated removal failure")
        real_unlink(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "unlink", failing_unlink)
    notes: list[str] = []

    store_transform(workspace, TransformSpec(), notes)

    assert len(notes) == 1
    assert "transform" in notes[0]


def test_sidecar_beside_the_cut_does_not_break_listing_or_validation(
    tmp_path: Path,
) -> None:
    """The whole premise of D1: a sidecar next to the cut directory must not
    make the cut disappear from the picker or fail cut-set validation."""
    workspace = _seeded_cut(tmp_path)
    store_transform(workspace, TransformSpec(first_frame=1), [])

    listing = list_workspaces(tmp_path)
    assert [entry.workspace.cache_key for entry in listing.entries] == [
        workspace.cache_key
    ]
    validate_cut_set(workspace)  # must not raise
