"""Durable editable cut sets and private rebuild snapshots.

The durable namespace is intentionally separate from disposable job scratch::

    <output>/.rembggui-work/
        cuts/<authoritative-cache-key>/   # explicit deletion only
        scratch/<job-id>/                 # bounded, explicit cleanup

Every public operation revalidates the canonical local root and opens files
without following links.  POSIX file access is relative to bound directory
descriptors.  Directory replacement uses a native atomic exchange where the
platform provides one; the portable fallback is journaled so an interrupted
two-rename replacement is recovered before the cache is observed again.
"""

from __future__ import annotations

# The package compatibility bridge injects split-module globals at import time.
# Keep the shared import namespace available to every implementation module.
# ruff: noqa: F401,F821
import ctypes
import errno
import hashlib
import importlib
import json
import os
import re
import stat
import sys
import time
import unicodedata
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from functools import wraps
from pathlib import Path, PurePath, PureWindowsPath
from threading import Lock, RLock
from types import TracebackType
from typing import (
    TYPE_CHECKING,
    Any,
    BinaryIO,
    Final,
    Literal,
    ParamSpec,
    Self,
    TypeVar,
    cast,
    overload,
)

from PIL import Image, UnidentifiedImageError

from rembggui.core.errors import AppError, ErrorCode
from rembggui.core.fingerprints import cut_cache_key_from_inputs
from rembggui.core.rgba import RgbaOwnershipTracker
from rembggui.jobs.models.cache_fs import BoundDirectoryCloseError, UnsafeCacheError

MANIFEST_FILENAME: Final = "manifest.json"
MANIFEST_SCHEMA: Final = "rembggui-cut-manifest"
MANIFEST_SCHEMA_VERSION: Final = 1
MAX_MANIFEST_BYTES: Final = 64 * 1024 * 1024
MAX_FRAME_COUNT: Final = 100_000
MAX_FRAME_FILE_BYTES: Final = 1024 * 1024 * 1024
MAX_CUT_DIMENSION: Final = 16_383
MAX_CUT_PIXELS: Final = 3840 * 2160
MAX_PATH_CHARS: Final = 4096
MAX_SOURCE_PATH_CHARS: Final = 4096
MAX_TEXT_CHARS: Final = 256
MAX_JOB_ID_CHARS: Final = 128
MAX_WORKSPACE_ENTRIES: Final = 10_000
MAX_SCRATCH_ENTRIES: Final = 10_000
WORKSPACE_WARNING_BYTES: Final = 20 * 1024**3
ABANDONED_SCRATCH_AGE_NS: Final = 24 * 60 * 60 * 1_000_000_000
COPY_CHUNK_BYTES: Final = 1024 * 1024
MAX_MOUNTINFO_BYTES: Final = 4 * 1024 * 1024
MAX_DEFERRED_BOUND_DIRECTORY_CLOSES = 128

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_CACHE_KEY_RE = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_JOB_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_FRAME_RE = re.compile(r"frame-([0-9]{6})\.png\Z")
_STAGE_RE = re.compile(r"\.stage-([0-9a-f]{64})-([A-Za-z0-9._-]+)\Z")
_MARKER_RE = re.compile(r"\.replace-([0-9a-f]{64})\.json\Z")
_BACKUP_RE = re.compile(r"\.backup-([0-9a-f]{64})-([0-9a-f]{32})\Z")
_MAX_INT64 = 2**63 - 1

type JsonScalar = str | int | bool | None
type JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
type CancellationCheck = Callable[[], bool]
type _BoundaryKind = Literal[
    "unsafe", "set", "stage", "promotion", "snapshot", "delete"
]
type _FilesystemFailure = OSError | UnsafeCacheError | BoundDirectoryCloseError
type FrozenJsonValue = (
    JsonScalar | tuple["FrozenJsonValue", ...] | Mapping[str, "FrozenJsonValue"]
)

_P = ParamSpec("_P")
_R = TypeVar("_R")


def _filesystem_boundary(
    kind: _BoundaryKind, operation: str
) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """Keep native/cache filesystem exceptions behind the public AppError API."""

    def decorate(function: Callable[_P, _R]) -> Callable[_P, _R]:
        @wraps(function)
        def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            try:
                return function(*args, **kwargs)
            except AppError:
                raise
            except (UnsafeCacheError, BoundDirectoryCloseError) as error:
                raise _structured_filesystem_failure(kind, operation, error) from error
            except OSError as error:
                raise _structured_filesystem_failure(kind, operation, error) from error

        return wrapped

    return decorate


def _structured_filesystem_failure(
    kind: _BoundaryKind, operation: str, error: _FilesystemFailure
) -> AppError:
    if isinstance(error, (UnsafeCacheError, BoundDirectoryCloseError)):
        return _unsafe_error(f"{operation}: {error}")
    return _boundary_error(kind, f"{operation}: {error}")




if TYPE_CHECKING:
    from ._errors import _boundary_error, _unsafe_error


__all__ = (
    "ABANDONED_SCRATCH_AGE_NS",
    "Any",
    "AppError",
    "BinaryIO",
    "BoundDirectoryCloseError",
    "Callable",
    "CancellationCheck",
    "COPY_CHUNK_BYTES",
    "Decimal",
    "ErrorCode",
    "ExitStack",
    "Final",
    "FrozenJsonValue",
    "Image",
    "InvalidOperation",
    "Iterator",
    "Literal",
    "Lock",
    "MANIFEST_FILENAME",
    "MANIFEST_SCHEMA",
    "MANIFEST_SCHEMA_VERSION",
    "Mapping",
    "MAX_CUT_DIMENSION",
    "MAX_CUT_PIXELS",
    "MAX_DEFERRED_BOUND_DIRECTORY_CLOSES",
    "MAX_FRAME_COUNT",
    "MAX_FRAME_FILE_BYTES",
    "MAX_JOB_ID_CHARS",
    "MAX_MANIFEST_BYTES",
    "MAX_MOUNTINFO_BYTES",
    "MAX_PATH_CHARS",
    "MAX_SCRATCH_ENTRIES",
    "MAX_SOURCE_PATH_CHARS",
    "MAX_TEXT_CHARS",
    "MAX_WORKSPACE_ENTRIES",
    "ParamSpec",
    "Path",
    "PurePath",
    "PureWindowsPath",
    "RLock",
    "RgbaOwnershipTracker",
    "Self",
    "Sequence",
    "StrEnum",
    "TracebackType",
    "TypeVar",
    "TYPE_CHECKING",
    "UnidentifiedImageError",
    "UnsafeCacheError",
    "WORKSPACE_WARNING_BYTES",
    "_BACKUP_RE",
    "_CACHE_KEY_RE",
    "_FRAME_RE",
    "_MARKER_RE",
    "_MAX_INT64",
    "_P",
    "_PNG_SIGNATURE",
    "_R",
    "_SAFE_JOB_RE",
    "_STAGE_RE",
    "_filesystem_boundary",
    "_structured_filesystem_failure",
    "cast",
    "ctypes",
    "cut_cache_key_from_inputs",
    "dataclass",
    "errno",
    "hashlib",
    "importlib",
    "json",
    "os",
    "overload",
    "re",
    "replace",
    "stat",
    "sys",
    "time",
    "unicodedata",
    "uuid",
    "wraps",
    "JsonScalar",
    "JsonValue",
    "_BoundaryKind",
    "_FilesystemFailure",
)
