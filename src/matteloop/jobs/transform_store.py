"""Persist the transform applied to a cut, beside the cut directory.

Design decision D1 (issue #25): the transform cannot live in the manifest
(``_manifest.py`` rejects unknown keys and other schema versions, and the
manifest modules are frozen) and cannot live inside the cut directory either
(``_scan.py`` invalidates any cut with an unexpected entry). It is instead a
dot-prefixed sidecar next to the cut directory,
``cuts_root / f".transform-{cache_key}.json"`` -- a name every scanner and
recovery routine in ``jobs/workspace`` already ignores.

Reading and writing degrade rather than fail (guardrail G4): a missing,
corrupt, wrong-schema, or foreign-cache-key sidecar is not something a user
can fix from the UI, so ``load_transform`` returns identity plus a note
instead of raising, and ``store_transform`` swallows ``OSError`` the same
way. Writing follows the repository's durability ceiling and nothing more
(guardrail G3): a temp sibling is fsynced, renamed into place atomically, and
the parent directory is fsynced best-effort -- no locks, no journals, no
inode binding.
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
    """Rebuild a ``TransformSpec`` from a stored ``"transform"`` payload.

    Raises ``ValueError`` for any structural or type deviation -- the sole
    exception ``load_transform`` needs to catch before falling back to
    identity.
    """
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
    """Return the transform stored for *workspace*, or identity plus a note.

    A missing sidecar is not a failure -- it means no transform was ever
    applied, so it is silent. Anything else wrong with the sidecar (corrupt
    JSON, an unrecognised schema, or one written for a different cache key)
    degrades to identity with a note rather than raising.
    """
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
    """Persist *spec* beside the cut directory, or remove it for identity.

    Swallows ``OSError`` and appends a note instead: a corrupt or unwritable
    sidecar must never fail the job whose output has already been published
    (guardrail G4).
    """
    if spec.is_identity:
        discard_transform(workspace)
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
    """Best-effort directory fsync (guardrail G3); a silent no-op where the
    platform will not open a directory for reading (Windows)."""
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


def discard_transform(workspace: CutWorkspace) -> None:
    """Remove the sidecar if present. Best effort -- never raises."""
    try:
        transform_sidecar_path(workspace).unlink()
    except OSError:
        pass
