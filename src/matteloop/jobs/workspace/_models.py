from __future__ import annotations

from typing import TYPE_CHECKING

# ruff: noqa: F403,F405,F811
from ._common import *  # noqa: F403,F401

_READABLE_WORKSPACE_NAME_RE = re.compile(r".+-[0-9a-f]{8}\Z")

if TYPE_CHECKING:
    from ._cut_ops import detect_external_edits, validate_cut_set
    from ._errors import _set_error, _stage_error, _unsafe_error
    from ._filesystem import _BoundDirectory
    from ._fs_helpers import _fdopen_owned, _same_lexical_path
    from ._manifest import CutManifest
    from ._manifest_io import _read_manifest, _recover_promotion, _write_manifest_atomic
    from ._manifest_validation import (
        _validate_cache_key,
        _validate_component,
        _validate_frame_index,
        _validate_job_id,
        _validate_path_value,
    )
    from ._platform import (
        WorkspaceFallback,
        _fallback_workspace_root,
        _workspace_layout,
        _workspace_root_for_output,
    )
    from ._runtime_helpers import _promotion_lock
    from ._scan import _inspect_frame

__all__ = (
    "CutWorkspace",
    "ScratchCleanupResult",
    "WorkspaceLifecycle",
    "WorkspaceListing",
    "WorkspaceSummary",
)


def _existing_promoted_path(cuts_root: Path, cache_key: str) -> Path:
    """Resolve a full key from either legacy or presentation naming."""
    legacy = cuts_root / cache_key
    if legacy.exists():
        return legacy
    if not cuts_root.exists():
        return legacy
    try:
        with _BoundDirectory.open(cuts_root) as bound:
            for name, info in bound.iter_entries():
                if (
                    name.startswith(".")
                    or stat.S_ISLNK(info.st_mode)
                    or not stat.S_ISDIR(info.st_mode)
                    or _READABLE_WORKSPACE_NAME_RE.fullmatch(name) is None
                ):
                    continue
                candidate = cuts_root / name
                try:
                    manifest, _identity = _read_manifest(candidate)
                except (AppError, OSError):
                    continue
                if manifest.cache_key == cache_key:
                    return candidate
    except (AppError, OSError):
        return legacy
    return legacy


def _existing_promoted_name(cuts_root: Path, cache_key: str) -> str | None:
    """Return an existing directory name for a key, if one is discoverable."""
    path = _existing_promoted_path(cuts_root, cache_key)
    return path.name if path.exists() else None


class WorkspaceLifecycle(StrEnum):
    STAGING = "staging"
    PROMOTED = "promoted"
    SNAPSHOT = "snapshot"


@dataclass(frozen=True, slots=True)
class CutWorkspace:
    """One staged, durable, or private-snapshot cut directory."""

    output_directory: Path
    workspace_root: Path
    cuts_root: Path
    scratch_root: Path
    cache_key: str
    path: Path
    lifecycle: WorkspaceLifecycle
    fallback: WorkspaceFallback | None = None
    directory_name: str = ""

    def __post_init__(self) -> None:
        _validate_cache_key(self.cache_key)
        directory_name = self.directory_name or self.cache_key
        _validate_component(directory_name)
        if directory_name.startswith("."):
            raise _unsafe_error("promoted workspace name must not be hidden")
        object.__setattr__(self, "directory_name", directory_name)
        for value in (
            self.output_directory,
            self.workspace_root,
            self.cuts_root,
            self.scratch_root,
            self.path,
        ):
            _validate_path_value(value)
        expected_root = (
            _fallback_workspace_root(self.output_directory)
            if self.fallback is not None
            else _workspace_root_for_output(self.output_directory)
        )
        if not _same_lexical_path(self.workspace_root, expected_root):
            raise _unsafe_error("workspace root is not bound to the output directory")
        if not _same_lexical_path(self.cuts_root, self.workspace_root / "cuts"):
            raise _unsafe_error("cuts root is not canonical")
        if not _same_lexical_path(self.scratch_root, self.workspace_root / "scratch"):
            raise _unsafe_error("scratch root is not canonical")
        if self.lifecycle is WorkspaceLifecycle.PROMOTED:
            expected = self.cuts_root / directory_name
            if not _same_lexical_path(self.path, expected):
                raise _unsafe_error("promoted workspace path is not canonical")
        elif self.lifecycle is WorkspaceLifecycle.STAGING:
            if (
                self.path.parent != self.cuts_root
                or _STAGE_RE.fullmatch(self.path.name) is None
            ):
                raise _unsafe_error("staging workspace path is not canonical")
            if not self.path.name.startswith(f".stage-{self.cache_key}-"):
                raise _unsafe_error("staging workspace key does not match its path")
        elif self.lifecycle is WorkspaceLifecycle.SNAPSHOT:
            if (
                self.path.name != "cuts-snapshot"
                or self.path.parent.parent != self.scratch_root
            ):
                raise _unsafe_error("snapshot workspace path is not canonical")
        else:  # pragma: no cover - enum exhaustiveness
            raise _unsafe_error("unknown workspace lifecycle")

    @classmethod
    @_filesystem_boundary("stage", "cannot create staged cut workspace")
    def create_staging(
        cls,
        output_directory: Path,
        cache_key: str,
        job_id: str,
        directory_name: str | None = None,
    ) -> Self:
        _validate_cache_key(cache_key)
        _validate_job_id(job_id)
        layout = _workspace_layout(output_directory, create=True)
        output, root, cuts, scratch = layout
        with _promotion_lock(str(cuts / cache_key)):
            _recover_promotion(cuts, cache_key)
        if directory_name is not None:
            existing_name = _existing_promoted_name(cuts, cache_key)
            if existing_name is not None:
                directory_name = existing_name
            elif directory_name != cache_key and (cuts / directory_name).exists():
                directory_name = f"{directory_name}-{cache_key}"
        directory_name = directory_name or cache_key
        _validate_component(directory_name)
        stage = cuts / f".stage-{cache_key}-{job_id}"
        try:
            with _BoundDirectory.open(cuts) as bound:
                bound.mkdir(stage.name, exist_ok=False)
                with bound.open_child(stage.name):
                    pass
        except FileExistsError as error:
            raise _stage_error(
                f"staging directory already exists for job {job_id!r}"
            ) from error
        except AppError:
            raise
        except OSError as error:
            raise _stage_error(
                f"cannot create staged cut directory: {error}"
            ) from error
        return cls(
            output,
            root,
            cuts,
            scratch,
            cache_key,
            stage,
            WorkspaceLifecycle.STAGING,
            layout.fallback,
            directory_name,
        )

    @classmethod
    @_filesystem_boundary("unsafe", "cannot open promoted cut workspace")
    def open(cls, output_directory: Path, cache_key: str) -> Self:
        layout = _workspace_layout(output_directory, create=False)
        output, root, cuts, scratch = layout
        if _CACHE_KEY_RE.fullmatch(cache_key) is None:
            _validate_component(cache_key)
            if (
                cache_key.startswith(".")
                or _READABLE_WORKSPACE_NAME_RE.fullmatch(cache_key) is None
            ):
                _validate_cache_key(cache_key)
            try:
                manifest, _identity = _read_manifest(cuts / cache_key)
            except (AppError, OSError):
                _validate_cache_key(cache_key)
            cache_key = manifest.cache_key
        _validate_cache_key(cache_key)
        with _promotion_lock(str(cuts / cache_key)):
            if cuts.exists():
                _recover_promotion(cuts, cache_key)
        path = _existing_promoted_path(cuts, cache_key)
        return cls(
            output,
            root,
            cuts,
            scratch,
            cache_key,
            path,
            WorkspaceLifecycle.PROMOTED,
            layout.fallback,
            path.name,
        )

    @_filesystem_boundary("set", "cannot read promoted cut")
    def read_promoted_cut(
        self,
        index: int,
        *,
        rgba_ownership_tracker: RgbaOwnershipTracker | None = None,
    ) -> Image.Image:
        """Load exactly one owned RGBA image; never retain the animation."""
        if self.lifecycle is WorkspaceLifecycle.STAGING:
            raise _set_error("staged cuts are not a promoted read source")
        if self.lifecycle is WorkspaceLifecycle.PROMOTED:
            manifest = detect_external_edits(self)
        else:
            manifest, _manifest_identity = _read_manifest(self.path)
        if manifest.cache_key != self.cache_key:
            raise _set_error("workspace cache key does not match its manifest")
        _validate_frame_index(index)
        if index >= manifest.frame_count:
            raise _set_error(f"frame index {index} is outside the promoted cut set")
        expected = manifest.frames[index]
        _inspect_frame(self.path, expected, compare_recorded=True, load_pixels=False)
        try:
            with _BoundDirectory.open(self.path) as bound:
                descriptor = bound.open_read(expected.filename)
                with _fdopen_owned(descriptor, "rb") as source:
                    with Image.open(source) as opened:
                        opened.load()
                        result = opened.copy()
                bound.assert_still_named()
        except AppError:
            raise
        except (OSError, UnidentifiedImageError, ValueError) as error:
            raise _set_error(
                f"cannot read promoted frame {expected.filename}: {error}"
            ) from error
        if result.mode != "RGBA" or result.size != (manifest.width, manifest.height):
            result.close()
            raise _set_error(f"promoted frame {expected.filename} is not exact RGBA")
        if rgba_ownership_tracker is not None:
            rgba_ownership_tracker.register(result)
        return result

    @_filesystem_boundary("set", "cannot update cut-workspace pin")
    def set_pinned(self, pinned: bool, *, now_ns: int | None = None) -> CutManifest:
        if type(pinned) is not bool:
            raise TypeError("pinned must be a bool")
        if self.lifecycle is not WorkspaceLifecycle.PROMOTED:
            raise _set_error("pin updates require a promoted cut workspace")
        with _promotion_lock(str(self.cuts_root / self.cache_key)):
            detect_external_edits(self, now_ns=now_ns)
            manifest, identity = _read_manifest(self.path)
            updated = replace(manifest, pinned=pinned)
            _write_manifest_atomic(self.path, updated, expected_identity=identity)
            return validate_cut_set(self)


@dataclass(frozen=True, slots=True)
class WorkspaceSummary:
    workspace: CutWorkspace
    manifest: CutManifest
    size_bytes: int

    @property
    def source_path(self) -> str:
        return self.manifest.source_path

    @property
    def last_used_at_ns(self) -> int:
        return self.manifest.last_used_at_ns

    @property
    def edited(self) -> bool:
        return self.manifest.edited

    @property
    def pinned(self) -> bool:
        return self.manifest.pinned


@dataclass(frozen=True, slots=True)
class WorkspaceListing(Sequence[WorkspaceSummary]):
    entries: tuple[WorkspaceSummary, ...]
    total_size_bytes: int
    warning_threshold_bytes: int
    warning_required: bool

    @overload
    def __getitem__(self, index: int) -> WorkspaceSummary: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[WorkspaceSummary, ...]: ...

    def __getitem__(
        self, index: int | slice
    ) -> WorkspaceSummary | tuple[WorkspaceSummary, ...]:
        return self.entries[index]

    def __len__(self) -> int:
        return len(self.entries)


@dataclass(frozen=True, slots=True)
class ScratchCleanupResult:
    removed_count: int
    removed_bytes: int
    has_more: bool
