"""Persist the transform applied to a cut, beside the cut directory (D1).

The manifest is frozen and rejects unknown keys/versions, and a sidecar
inside the cut directory would invalidate the whole set for the picker
(``_scan.py``), so this is a dot-prefixed sidecar next to it instead --
``cuts_root / f".transform-{cache_key}.json"`` -- a name every scanner and
recovery routine in ``jobs/workspace`` already ignores. Reading and writing
degrade rather than fail (G4); writing follows the durability ceiling and
nothing more (G3): temp sibling -> fsync -> atomic rename -> best-effort
parent fsync, no locks, no journals, no inode binding.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Final

from matteloop.core.errors import ValidationError
from matteloop.core.specs import CropSpec, MismatchMode, ResizeSpec, TransformSpec
from matteloop.jobs.workspace import CutWorkspace

TRANSFORM_SIDECAR_SCHEMA: Final = "matteloop-cut-transform"
TRANSFORM_SIDECAR_VERSION: Final = 1


def transform_sidecar_path(workspace: CutWorkspace) -> Path:
    """Return the sidecar path for *workspace* -- never inside the cut itself."""
    return workspace.cuts_root / f".transform-{workspace.cache_key}.json"


def transform_to_payload(spec: TransformSpec) -> dict[str, object]:
    """Convert *spec* to its JSON-serialisable ``"transform"`` payload."""
    return {
        "first_frame": spec.first_frame,
        "last_frame": spec.last_frame,
        "crop": _crop_to_payload(spec.crop),
        "resize": _resize_to_payload(spec.resize),
    }


def _crop_to_payload(crop: CropSpec | None) -> dict[str, int] | None:
    if crop is None:
        return None
    return {"x": crop.x, "y": crop.y, "width": crop.width, "height": crop.height}


def _resize_to_payload(resize: ResizeSpec | None) -> dict[str, object] | None:
    if resize is None:
        return None
    return {
        "width": resize.width,
        "height": resize.height,
        "mismatch": resize.mismatch.value,
    }


def transform_from_payload(payload: object) -> TransformSpec:
    """Rebuild a ``TransformSpec``; raises ``ValueError`` on any deviation."""
    if not isinstance(payload, dict):
        raise ValueError("transform payload must be an object")
    try:
        return TransformSpec(
            first_frame=payload["first_frame"],
            last_frame=payload["last_frame"],
            crop=_crop_from_payload(payload.get("crop")),
            resize=_resize_from_payload(payload.get("resize")),
        )
    except (KeyError, TypeError, ValidationError) as error:
        raise ValueError(f"invalid transform payload: {error}") from error


def _crop_from_payload(payload: object) -> CropSpec | None:
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise ValueError("crop must be an object or null")
    return CropSpec(
        x=payload["x"],
        y=payload["y"],
        width=payload["width"],
        height=payload["height"],
    )


def _resize_from_payload(payload: object) -> ResizeSpec | None:
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise ValueError("resize must be an object or null")
    return ResizeSpec(
        width=payload["width"],
        height=payload["height"],
        mismatch=MismatchMode(payload["mismatch"]),
    )


def _note(notes: list[str] | None, message: str) -> None:
    if notes is not None:
        notes.append(message)


def load_transform(
    workspace: CutWorkspace, notes: list[str] | None = None
) -> TransformSpec:
    """Return the stored transform, or identity (+ a note if something was
    wrong: missing is silent; corrupt, foreign, or mismatched is not)."""
    path = transform_sidecar_path(workspace)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return TransformSpec()
    except OSError as error:
        _note(notes, f"stored transform could not be read: {error}")
        return TransformSpec()
    try:
        payload = json.loads(raw)
    except ValueError as error:
        _note(notes, f"stored transform is corrupt: {error}")
        return TransformSpec()
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != TRANSFORM_SIDECAR_SCHEMA
        or payload.get("schema_version") != TRANSFORM_SIDECAR_VERSION
        or payload.get("cache_key") != workspace.cache_key
    ):
        _note(notes, "stored transform does not match this cut; using identity")
        return TransformSpec()
    try:
        return transform_from_payload(payload.get("transform"))
    except ValueError as error:
        _note(notes, f"stored transform is invalid: {error}")
        return TransformSpec()


def store_transform(
    workspace: CutWorkspace, spec: TransformSpec, notes: list[str]
) -> None:
    """Persist *spec* beside the cut, or remove it for identity. Swallows
    ``OSError`` into a note (G4): the job already published its output."""
    if spec.is_identity:
        discard_transform(workspace, notes)
        return
    payload = {
        "schema": TRANSFORM_SIDECAR_SCHEMA,
        "schema_version": TRANSFORM_SIDECAR_VERSION,
        "cache_key": workspace.cache_key,
        "transform": transform_to_payload(spec),
    }
    encoded = json.dumps(payload, sort_keys=True, allow_nan=False).encode("utf-8")
    try:
        _write_sidecar(transform_sidecar_path(workspace), encoded)
    except OSError as error:
        notes.append(f"could not record the applied transform: {error}")


def _write_sidecar(path: Path, encoded: bytes) -> None:
    """Temp sibling -> fsync -> atomic rename -> best-effort parent fsync."""
    descriptor, raw = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(raw)
    try:
        os.write(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
    _fsync_parent(path)


def _fsync_parent(path: Path) -> None:
    """Best-effort directory fsync (G3); a no-op where unsupported (Windows)."""
    try:
        descriptor = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def discard_transform(
    workspace: CutWorkspace, notes: list[str] | None = None
) -> None:
    """Remove the sidecar if present. Best effort -- never raises.

    A missing sidecar is the common, silent case. Any other removal
    failure is routed through *notes* (G4): the job already published its
    output, but leaving a stale sidecar in place would silently restore a
    transform the last render did not apply.
    """
    try:
        transform_sidecar_path(workspace).unlink()
    except FileNotFoundError:
        pass
    except OSError as error:
        _note(notes, f"could not remove the stored transform: {error}")
