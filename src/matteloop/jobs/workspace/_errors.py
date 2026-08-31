from __future__ import annotations

from typing import TYPE_CHECKING

# ruff: noqa: F403,F405
from ._common import *  # noqa: F403,F401

if TYPE_CHECKING:
    from ._models import CutWorkspace
    from ._platform import _assert_safe_directory, _workspace_layout

__all__ = (
    "_boundary_error",
    "_cuts_changed",
    "_delete_error",
    "_manifest_error",
    "_promotion_error",
    "_require_workspace",
    "_set_error",
    "_snapshot_error",
    "_stage_error",
    "_unsafe_error",
)


def _boundary_error(kind: _BoundaryKind, detail: str) -> AppError:
    if kind == "unsafe":
        return _unsafe_error(detail)
    if kind == "set":
        return _set_error(detail)
    if kind == "stage":
        return _stage_error(detail)
    if kind == "promotion":
        return _promotion_error(detail)
    if kind == "snapshot":
        return _snapshot_error(detail)
    return _delete_error(detail)


def _require_workspace(workspace: CutWorkspace) -> CutWorkspace:
    if type(workspace) is not CutWorkspace:
        raise TypeError("workspace must be an exact CutWorkspace")
    _workspace_layout(workspace.output_directory, create=False)
    if workspace.path.exists():
        _assert_safe_directory(workspace.path)
    return workspace


def _manifest_error(detail: str) -> AppError:
    return AppError(
        ErrorCode.CUT_MANIFEST_INVALID,
        "cut-manifest",
        "error.cuts.manifest-invalid",
        detail,
        "regenerate-or-repair-cuts",
    )


def _set_error(detail: str) -> AppError:
    return AppError(
        ErrorCode.CUT_SET_INVALID,
        "cut-validation",
        "error.cuts.invalid",
        detail,
        "repair-or-regenerate-cuts",
    )


def _stage_error(detail: str) -> AppError:
    return AppError(
        ErrorCode.CUT_STAGE_FAILED,
        "cut-stage",
        "error.cuts.stage-failed",
        detail,
        "retry-render",
    )


def _promotion_error(detail: str) -> AppError:
    return AppError(
        ErrorCode.CUT_PROMOTION_FAILED,
        "cut-promotion",
        "error.cuts.promotion-failed",
        detail,
        "retry-render",
    )


def _snapshot_error(detail: str) -> AppError:
    return AppError(
        ErrorCode.CUT_SNAPSHOT_FAILED,
        "cut-snapshot",
        "error.cuts.snapshot-failed",
        detail,
        "retry-rebuild",
    )


def _cuts_changed(detail: str) -> AppError:
    return AppError(
        ErrorCode.CUTS_CHANGED_DURING_SNAPSHOT,
        "cut-snapshot",
        "error.cuts.changed-during-snapshot",
        detail,
        "retry-rebuild",
    )


def _unsafe_error(detail: str) -> AppError:
    return AppError(
        ErrorCode.CUT_WORKSPACE_UNSAFE,
        "cut-workspace",
        "error.cuts.workspace-unsafe",
        detail,
        "choose-local-output-directory",
    )


def _delete_error(detail: str) -> AppError:
    return AppError(
        ErrorCode.CUT_WORKSPACE_DELETE_FAILED,
        "cut-workspace-delete",
        "error.cuts.delete-failed",
        detail,
        "retry-workspace-delete",
    )
