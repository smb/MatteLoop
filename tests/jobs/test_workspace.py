from __future__ import annotations

import errno
import gc
import hashlib
import json
import multiprocessing
import os
import threading
import time
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import FrozenInstanceError
from decimal import Decimal
from fractions import Fraction
from pathlib import Path, PureWindowsPath

import psutil
import pytest
from PIL import Image

import matteloop.paths as paths_module
from matteloop.core.errors import AppError, ErrorCode
from matteloop.core.fingerprints import cut_cache_key
from matteloop.core.rgba import RgbaOwnershipTracker
from matteloop.core.specs import (
    CropSpec,
    FramingSpec,
    OutputSpec,
    RenderRequest,
    SamplingSpec,
    SegmentationSpec,
)
from matteloop.jobs import workspace as workspace_module
from matteloop.jobs.models.cache_fs import BoundDirectoryCloseError, UnsafeCacheError
from matteloop.jobs.workspace import (
    MANIFEST_FILENAME,
    MAX_MANIFEST_BYTES,
    CutManifest,
    CutUnionMetadata,
    CutWorkspace,
    PublicationDirectory,
    RecoveryDirectory,
    cleanup_abandoned_scratch,
    cleanup_scratch,
    compare_and_set_union_metadata,
    delete_workspace,
    detect_external_edits,
    discard_staged_set,
    list_workspaces,
    promote_cut_set,
    promote_for_render,
    snapshot_for_rebuild,
    stage_cut,
    validate_cut_set,
)


def _cache_inputs(*, source: str = "a" * 64) -> dict[str, object]:
    return {
        "source_sha256": source,
        "sampling": {
            "start": {"numerator": 0, "denominator": 1},
            "end": {"numerator": 1, "denominator": 1},
            "fps": 15,
        },
        "crop": {"x": 0, "y": 0, "width": 8, "height": 6},
        "model": {"id": "birefnet-portrait", "weight_sha256": "b" * 64},
        "rembg_version": "2.0.72",
        "pipeline_schema_version": "pipeline-v1",
        "orientation_color_version": "orientation-color-v1",
        "edge_settings": {
            "mode": "standard",
            "alpha_matting": {
                "foreground_threshold": 240,
                "background_threshold": 10,
                "erode_size": 10,
            },
        },
    }


def _image(index: int, *, size: tuple[int, int] = (8, 6)) -> Image.Image:
    return Image.new("RGBA", size, (index + 1, 40, 90, 128 + index))


def _force_descriptor_reuse(
    path: Path,
    descriptor: int,
    close_descriptor: Callable[[int], None],
) -> int:
    source = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    if source != descriptor:
        assert os.dup2(source, descriptor) == descriptor
        close_descriptor(source)
    return descriptor


def _inject_post_open_anchor_disappearance(
    publication: workspace_module.PublicationDirectory,
    anchor_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> list[int]:
    opened_anchor_descriptors: list[int] = []
    anchor_lstat_calls = 0
    actual_lstat = workspace_module.PublicationDirectory.lstat
    actual_open_read = workspace_module.PublicationDirectory.open_read

    def record_anchor_open(
        bound: workspace_module.PublicationDirectory,
        name: str,
    ) -> int:
        descriptor = actual_open_read(bound, name)
        if bound is publication and name == anchor_name:
            opened_anchor_descriptors.append(descriptor)
        return descriptor

    def disappear_after_anchor_open(
        bound: workspace_module.PublicationDirectory,
        name: str,
    ) -> os.stat_result:
        nonlocal anchor_lstat_calls
        if bound is publication and name == anchor_name:
            anchor_lstat_calls += 1
            if anchor_lstat_calls == 2:
                raise FileNotFoundError(errno.ENOENT, "synthetic anchor disappearance")
        return actual_lstat(bound, name)

    monkeypatch.setattr(
        workspace_module.PublicationDirectory,
        "open_read",
        record_anchor_open,
    )
    monkeypatch.setattr(
        workspace_module.PublicationDirectory,
        "lstat",
        disappear_after_anchor_open,
    )
    return opened_anchor_descriptors


def _completed_staging(
    output: Path,
    *,
    job_id: str = "render-1",
    count: int = 3,
    inputs: dict[str, object] | None = None,
    pinned: bool = False,
    image_offset: int = 0,
) -> tuple[CutWorkspace, CutManifest]:
    authoritative = _cache_inputs() if inputs is None else inputs
    key = CutManifest.cache_key_for(authoritative)
    workspace = CutWorkspace.create_staging(output, key, job_id)
    frames = tuple(
        stage_cut(workspace, index, _image(index + image_offset))
        for index in range(count)
    )
    manifest = CutManifest.create(
        cache_key_inputs=authoritative,
        source_path="/local/example source.mp4",
        source_size_bytes=1234,
        source_mtime_ns=5678,
        frames=frames,
        union_metadata=CutUnionMetadata(
            bounds=(1, 1, 7, 5),
            alpha_threshold="2",
            fingerprint="c" * 64,
        ),
        pinned=pinned,
        now_ns=10_000,
    )
    return workspace, manifest


def _promoted(
    output: Path,
    *,
    job_id: str = "render-1",
    count: int = 3,
    inputs: dict[str, object] | None = None,
    pinned: bool = False,
) -> CutWorkspace:
    workspace, manifest = _completed_staging(
        output,
        job_id=job_id,
        count=count,
        inputs=inputs,
        pinned=pinned,
    )
    return promote_cut_set(workspace, manifest)


def _rewrite_frame(path: Path, color: tuple[int, int, int, int]) -> None:
    temporary = path.with_suffix(".editing")
    Image.new("RGBA", (8, 6), color).save(temporary, format="PNG")
    os.replace(temporary, path)


def test_discard_staged_set_removes_only_the_private_candidate(tmp_path: Path) -> None:
    durable = _promoted(tmp_path, job_id="old")
    staged, _manifest = _completed_staging(tmp_path, job_id="new", image_offset=4)

    assert discard_staged_set(staged)
    assert not staged.path.exists()
    assert validate_cut_set(durable).frames[0].sha256 != _manifest.frames[0].sha256


def test_promote_for_render_returns_durable_and_prepublication_snapshot(
    tmp_path: Path,
) -> None:
    staged, manifest = _completed_staging(tmp_path, job_id="render-private")
    scratch = staged.scratch_root / "render-private"

    durable, private, promoted_manifest = promote_for_render(staged, manifest, scratch)

    assert durable.lifecycle is workspace_module.WorkspaceLifecycle.PROMOTED
    assert private.lifecycle is workspace_module.WorkspaceLifecycle.SNAPSHOT
    assert promoted_manifest == validate_cut_set(durable)
    with private.read_promoted_cut(0) as before:
        before_bytes = before.tobytes()
    _rewrite_frame(durable.path / "frame-000000.png", (200, 10, 20, 255))
    with private.read_promoted_cut(0) as after:
        assert after.tobytes() == before_bytes


def test_union_metadata_compare_and_set_uses_expected_frame_hashes(
    tmp_path: Path,
) -> None:
    durable = _promoted(tmp_path)
    baseline = validate_cut_set(durable)
    expected_hashes = tuple(frame.sha256 for frame in baseline.frames)
    first = CutUnionMetadata((0, 0, 8, 6), "2", "d" * 64)
    second = CutUnionMetadata((1, 1, 7, 5), "2", "e" * 64)

    assert compare_and_set_union_metadata(durable, expected_hashes, first)
    _rewrite_frame(durable.path / "frame-000000.png", (3, 4, 5, 255))
    assert not compare_and_set_union_metadata(durable, expected_hashes, second)
    assert detect_external_edits(durable).union_metadata is None


class _ExclusiveWindowsCutsApi:
    def __init__(
        self,
        cuts_root: Path,
        *,
        fail_journal_replace: bool = False,
    ) -> None:
        self.cuts_root = cuts_root
        self.fail_journal_replace = fail_journal_replace
        self.handle_paths: dict[int, Path] = {}
        self.handle_opens: dict[int, tuple[tuple[int, int], int, int]] = {}
        self.next_handle = 200
        self.sharing_violations: list[Path] = []

    def bind_root(self) -> int:
        return self.open_directory(
            self.cuts_root,
            desired_access=0x80000000 | 0x40000000,
            share_mode=0x00000001,
            flags=0,
        )

    def open_anchor(
        self,
        path: Path,
        *,
        desired_access: int,
        share_mode: int,
        flags: int,
    ) -> int:
        return self.open_directory(
            path,
            desired_access=desired_access,
            share_mode=share_mode,
            flags=flags,
        )

    def open_publication_anchor(
        self,
        path: Path,
        *,
        desired_access: int,
        share_mode: int,
        flags: int,
    ) -> int:
        return self.open_anchor(
            path,
            desired_access=desired_access,
            share_mode=share_mode,
            flags=flags,
        )

    @staticmethod
    def _share_allows(share_mode: int, desired_access: int) -> bool:
        return bool(
            (not desired_access & 0x80000000 or share_mode & 0x00000001)
            and (not desired_access & 0x40000000 or share_mode & 0x00000002)
            and (not desired_access & 0x00010000 or share_mode & 0x00000004)
        )

    def open_directory(
        self,
        path: Path,
        *,
        desired_access: int,
        share_mode: int,
        flags: int,
    ) -> int:
        del flags
        info = path.stat()
        identity = (info.st_dev, info.st_ino)
        for (
            existing_identity,
            existing_access,
            existing_share,
        ) in self.handle_opens.values():
            if existing_identity != identity:
                continue
            if not self._share_allows(
                existing_share, desired_access
            ) or not self._share_allows(share_mode, existing_access):
                self.sharing_violations.append(path)
                raise OSError(32, "synthetic Windows sharing violation")
        handle = self.next_handle
        self.next_handle += 1
        self.handle_paths[handle] = path
        self.handle_opens[handle] = (identity, desired_access, share_mode)
        return handle

    def open_child_directory(
        self,
        parent: int,
        name: str,
        *,
        create: bool,
        desired_access: int,
        share_mode: int,
        flags: int,
    ) -> int:
        assert create is False
        path = self.handle_paths[parent] / name
        if not path.is_dir():
            raise FileNotFoundError(path)
        return self.open_directory(
            path,
            desired_access=desired_access,
            share_mode=share_mode,
            flags=flags,
        )

    def open_publication_child_directory(
        self,
        parent: int,
        name: str,
        *,
        create: bool,
        desired_access: int,
        share_mode: int,
        flags: int,
    ) -> int:
        return self.open_child_directory(
            parent,
            name,
            create=create,
            desired_access=desired_access,
            share_mode=share_mode,
            flags=flags,
        )

    def file_attributes(self, handle: int) -> int:
        from matteloop.jobs.models import cache_fs

        path = self.handle_paths[handle]
        attributes = cache_fs._FILE_ATTRIBUTE_DIRECTORY if path.is_dir() else 0
        if path.is_symlink():
            attributes |= cache_fs._FILE_ATTRIBUTE_REPARSE_POINT
        return attributes

    def close_handle(self, handle: int) -> None:
        del self.handle_opens[handle]
        del self.handle_paths[handle]

    def lstat_at(self, handle: int, name: str) -> os.stat_result:
        return (self.handle_paths[handle] / name).lstat()

    def publication_lstat_at(self, handle: int, name: str) -> os.stat_result:
        return self.lstat_at(handle, name)

    def open_read_at(self, handle: int, name: str) -> int:
        return os.open(self.handle_paths[handle] / name, os.O_RDONLY)

    def open_publication_read_at(self, handle: int, name: str) -> int:
        return self.open_read_at(handle, name)

    def open_publication_read_write_at(self, handle: int, name: str) -> int:
        return os.open(self.handle_paths[handle] / name, os.O_RDWR)

    def open_new_read_write_at(self, handle: int, name: str) -> int:
        return os.open(
            self.handle_paths[handle] / name,
            os.O_RDWR | os.O_CREAT | os.O_EXCL,
            0o600,
        )

    def open_new_publication_read_write_at(self, handle: int, name: str) -> int:
        return self.open_new_read_write_at(handle, name)

    def replace_at(self, handle: int, source: str, destination: str) -> None:
        if self.fail_journal_replace and destination.startswith(".replace-"):
            raise OSError("injected handle-relative journal failure")
        root = self.handle_paths[handle]
        os.replace(root / source, root / destination)

    def replace_publication_at(
        self, handle: int, source: str, destination: str
    ) -> None:
        self.replace_at(handle, source, destination)

    def rename_publication_at(
        self,
        source_handle: int,
        source: str,
        destination_handle: int,
        destination: str,
        *,
        replace: bool,
    ) -> None:
        source_path = self.handle_paths[source_handle] / source
        destination_path = self.handle_paths[destination_handle] / destination
        if not replace and destination_path.exists():
            raise FileExistsError(destination_path)
        os.replace(source_path, destination_path)

    def replace_directory_at(self, handle: int, source: str, destination: str) -> None:
        root = self.handle_paths[handle]
        os.replace(root / source, root / destination)

    def unlink_at(self, handle: int, name: str, *, require_regular: bool) -> None:
        path = self.handle_paths[handle] / name
        if require_regular and not path.is_file():
            raise OSError("refusing to unlink a non-regular entry")
        path.unlink()

    def rmdir_at(self, handle: int, name: str) -> None:
        os.rmdir(self.handle_paths[handle] / name)

    def iter_entries_at(self, handle: int, *, max_entries: int) -> object:
        entries = tuple(self.handle_paths[handle].iterdir())
        assert len(entries) <= max_entries
        return iter((entry.name, entry.lstat()) for entry in entries)

    def flush_directory_strict(self, _handle: int) -> None:
        pass

    def assert_directory_handle(self, handle: int) -> None:
        path = self.handle_paths[handle]
        if path.is_symlink() or not path.is_dir():
            raise UnsafeCacheError("bound test directory was redirected")


def _install_exclusive_windows_cuts_api(
    monkeypatch: pytest.MonkeyPatch,
    cuts_root: Path,
    *,
    fail_journal_replace: bool = False,
    api: _ExclusiveWindowsCutsApi | None = None,
) -> _ExclusiveWindowsCutsApi:
    active_api = (
        api
        if api is not None
        else _ExclusiveWindowsCutsApi(
            cuts_root,
            fail_journal_replace=fail_journal_replace,
        )
    )
    original_open = workspace_module._BoundDirectory.open

    def injected_open(
        cls: type[workspace_module._BoundDirectory], path: Path
    ) -> workspace_module._BoundDirectory:
        if path == cuts_root or cuts_root in path.parents:
            relative_parts = path.relative_to(cuts_root).parts
            handles: list[int] = []
            try:
                handle = active_api.open_directory(
                    cuts_root,
                    desired_access=(
                        0x80000000 if relative_parts else 0x80000000 | 0x40000000
                    ),
                    share_mode=0x00000001,
                    flags=0x02000000 | 0x00200000,
                )
                handles.append(handle)
                for index, component in enumerate(relative_parts):
                    handle = active_api.open_child_directory(
                        handle,
                        component,
                        create=False,
                        desired_access=(
                            0x80000000 | 0x40000000
                            if index == len(relative_parts) - 1
                            else 0x80000000
                        ),
                        share_mode=0x00000001,
                        flags=0x02000000 | 0x00200000,
                    )
                    handles.append(handle)
            except BaseException:
                for opened in reversed(handles):
                    active_api.close_handle(opened)
                raise
            return cls(
                path,
                None,
                windows_handles=tuple(handles),
                windows_api=active_api,
            )
        return original_open(path)

    monkeypatch.setattr(
        workspace_module._BoundDirectory, "open", classmethod(injected_open)
    )
    return active_api


class _PublicationSharingWindowsApi(_ExclusiveWindowsCutsApi):
    _SHARE_READ = 0x00000001
    _SHARE_WRITE = 0x00000002
    _SHARE_DELETE = 0x00000004
    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _DELETE = 0x00010000

    def __init__(self, cuts_root: Path) -> None:
        super().__init__(cuts_root)
        self.file_opens: dict[int, tuple[tuple[int, int], int, int]] = {}

    @classmethod
    def _allows(cls, share: int, access: int) -> bool:
        return bool(
            (not access & cls._GENERIC_READ or share & cls._SHARE_READ)
            and (not access & cls._GENERIC_WRITE or share & cls._SHARE_WRITE)
            and (not access & cls._DELETE or share & cls._SHARE_DELETE)
        )

    def _require_compatible(self, path: Path, access: int, share: int) -> None:
        info = path.stat()
        identity = (info.st_dev, info.st_ino)
        for opened_identity, opened_access, opened_share in self.file_opens.values():
            if opened_identity != identity:
                continue
            if not self._allows(opened_share, access) or not self._allows(
                share, opened_access
            ):
                raise OSError(32, "synthetic bidirectional sharing violation")

    def _open_file(
        self,
        handle: int,
        name: str,
        *,
        access: int,
        share: int,
        flags: int,
    ) -> int:
        path = self.handle_paths[handle] / name
        self._require_compatible(path, access, share)
        descriptor = os.open(path, flags, 0o600)
        info = os.fstat(descriptor)
        self.file_opens[descriptor] = ((info.st_dev, info.st_ino), access, share)
        return descriptor

    def release_file(self, descriptor: int) -> None:
        self.file_opens.pop(descriptor)
        os.close(descriptor)

    def lstat_at(self, handle: int, name: str) -> os.stat_result:
        path = self.handle_paths[handle] / name
        self._require_compatible(path, 0x00100080, self._SHARE_READ)
        return path.lstat()

    def publication_lstat_at(self, handle: int, name: str) -> os.stat_result:
        path = self.handle_paths[handle] / name
        self._require_compatible(
            path,
            0x00100080,
            self._SHARE_READ | self._SHARE_WRITE | self._SHARE_DELETE,
        )
        return path.lstat()

    def open_new_read_write_at(self, handle: int, name: str) -> int:
        path = self.handle_paths[handle] / name
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        info = os.fstat(descriptor)
        self.file_opens[descriptor] = (
            (info.st_dev, info.st_ino),
            self._GENERIC_READ | self._GENERIC_WRITE,
            self._SHARE_READ,
        )
        return descriptor

    def open_new_publication_read_write_at(self, handle: int, name: str) -> int:
        path = self.handle_paths[handle] / name
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        info = os.fstat(descriptor)
        self.file_opens[descriptor] = (
            (info.st_dev, info.st_ino),
            self._GENERIC_READ | self._GENERIC_WRITE,
            self._SHARE_READ | self._SHARE_WRITE | self._SHARE_DELETE,
        )
        return descriptor

    def open_publication_read_at(self, handle: int, name: str) -> int:
        return self._open_file(
            handle,
            name,
            access=self._GENERIC_READ,
            share=self._SHARE_READ | self._SHARE_WRITE | self._SHARE_DELETE,
            flags=os.O_RDONLY,
        )

    def open_publication_read_write_at(self, handle: int, name: str) -> int:
        return self._open_file(
            handle,
            name,
            access=self._GENERIC_READ | self._GENERIC_WRITE,
            share=self._SHARE_READ | self._SHARE_WRITE | self._SHARE_DELETE,
            flags=os.O_RDWR,
        )

    def replace_at(self, handle: int, source: str, destination: str) -> None:
        path = self.handle_paths[handle] / source
        self._require_compatible(path, self._DELETE, self._SHARE_READ)
        super().replace_at(handle, source, destination)

    def replace_publication_at(
        self, handle: int, source: str, destination: str
    ) -> None:
        path = self.handle_paths[handle] / source
        self._require_compatible(
            path,
            self._DELETE,
            self._SHARE_READ | self._SHARE_WRITE | self._SHARE_DELETE,
        )
        super().replace_at(handle, source, destination)


def test_windows_recovery_lstat_is_compatible_with_held_writable_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recovery_path = tmp_path / ".matteloop-recovery"
    recovery_path.mkdir(mode=0o700)
    sharing_api = _PublicationSharingWindowsApi(tmp_path)
    _install_exclusive_windows_cuts_api(
        monkeypatch,
        tmp_path,
        api=sharing_api,
    )
    recovery = RecoveryDirectory.open(tmp_path, recovery_path.name)
    descriptor = recovery.open_fixed_pending(".pending")
    try:
        os.write(descriptor, b"old-output")

        assert recovery.lstat(".pending").st_size == len(b"old-output")
    finally:
        sharing_api.release_file(descriptor)
        recovery.close()


def test_windows_recovery_replace_is_compatible_with_held_writable_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recovery_path = tmp_path / ".matteloop-recovery"
    recovery_path.mkdir(mode=0o700)
    sharing_api = _PublicationSharingWindowsApi(tmp_path)
    _install_exclusive_windows_cuts_api(
        monkeypatch,
        tmp_path,
        api=sharing_api,
    )
    recovery = RecoveryDirectory.open(tmp_path, recovery_path.name)
    descriptor = recovery.open_fixed_pending(".pending")
    reopened: int | None = None
    try:
        os.write(descriptor, b"old-output")

        recovery.replace(".pending", "output.recovery")
        reopened = recovery.open_read("output.recovery")

        assert os.fstat(descriptor).st_size == len(b"old-output")
        assert os.fstat(reopened).st_size == len(b"old-output")
        assert (recovery_path / "output.recovery").read_bytes() == b"old-output"
    finally:
        if reopened is not None:
            sharing_api.release_file(reopened)
        sharing_api.release_file(descriptor)
        recovery.close()


def test_windows_existing_recovery_remains_shareable_while_read_write_fd_is_held(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recovery_path = tmp_path / ".matteloop-recovery"
    recovery_path.mkdir(mode=0o700)
    (recovery_path / "output.recovery").write_bytes(b"old-output")
    sharing_api = _PublicationSharingWindowsApi(tmp_path)
    _install_exclusive_windows_cuts_api(
        monkeypatch,
        tmp_path,
        api=sharing_api,
    )
    recovery = RecoveryDirectory.open(tmp_path, recovery_path.name)
    descriptor = recovery.open_read_write("output.recovery")
    try:
        assert recovery.lstat("output.recovery").st_size == len(b"old-output")
        recovery.replace("output.recovery", "output.recovery-shadow")
        assert os.fstat(descriptor).st_size == len(b"old-output")
    finally:
        sharing_api.release_file(descriptor)
        recovery.close()


@pytest.mark.skipif(os.name == "nt", reason="exercises the POSIX lock adapter")
def test_private_output_lock_is_process_exclusive_and_stale_name_is_reusable(
    tmp_path: Path,
) -> None:
    private_path = tmp_path / ".matteloop-publish"
    private_path.mkdir(mode=0o700)
    context = multiprocessing.get_context("fork")
    ready = context.Event()
    terminate = context.Event()

    def acquire_then_exit() -> None:
        directory = RecoveryDirectory.open(tmp_path, private_path.name)
        directory.acquire_advisory_lock("target.transaction.lock")
        ready.set()
        if not terminate.wait(10):
            raise RuntimeError("test did not terminate lock owner")
        os._exit(0)

    process = context.Process(target=acquire_then_exit)
    process.start()
    assert ready.wait(5)

    contender = RecoveryDirectory.open(tmp_path, private_path.name)
    try:
        with pytest.raises(BlockingIOError):
            contender.acquire_advisory_lock("target.transaction.lock")
    finally:
        contender.close()

    terminate.set()
    process.join(5)
    assert process.exitcode == 0
    assert (private_path / "target.transaction.lock").is_file()

    reused_directory = RecoveryDirectory.open(tmp_path, private_path.name)
    reused = reused_directory.acquire_advisory_lock("target.transaction.lock")
    reused.close()
    reused_directory.close()


def test_output_target_component_key_coalesces_windows_path_aliases() -> None:
    direct = PureWindowsPath(r"C:\Exports\Output.WEBP")
    aliased = PureWindowsPath(r"c:/Exports/../Exports/output.webp")

    assert workspace_module._output_target_component_key(
        direct.name,
        platform="windows",
    ) == workspace_module._output_target_component_key(
        aliased.name,
        platform="windows",
    )


def test_output_target_component_key_coalesces_macos_unicode_aliases() -> None:
    composed = "caf\N{LATIN SMALL LETTER E WITH ACUTE}.webp"
    decomposed = "cafe\N{COMBINING ACUTE ACCENT}.webp"

    assert workspace_module._output_target_component_key(
        composed,
        platform="darwin",
    ) == workspace_module._output_target_component_key(
        decomposed,
        platform="darwin",
    )


def test_output_target_component_key_keeps_distinct_posix_targets_independent() -> None:
    assert workspace_module._output_target_component_key(
        "first.webp",
        platform="posix",
    ) != workspace_module._output_target_component_key(
        "second.webp",
        platform="posix",
    )


@pytest.mark.skipif(os.name == "nt", reason="exercises POSIX descriptor identities")
def test_output_lock_anchor_is_parent_bound_and_stale_pending_reuses_exact_inode(
    tmp_path: Path,
) -> None:
    publication = workspace_module.PublicationDirectory.open(tmp_path)
    private = publication.open_private_directory(
        ".matteloop-publish",
        "publication",
    )
    key = publication.target_key(tmp_path / "output.webp")
    lock = publication.acquire_output_lock(private, key)
    pending_name = f".{key}.publish-pending"
    first = private.open_fixed_pending(pending_name)
    os.write(first, b"stale")
    first_identity = os.fstat(first).st_dev, os.fstat(first).st_ino
    os.close(first)
    source = os.open(tmp_path / "source.bin", os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    try:
        anchors = tuple(tmp_path.glob(".*.transaction-anchor"))
        assert len(anchors) == 1
        assert anchors[0].parent == tmp_path
        assert not tuple(private.path.glob(".*.transaction-anchor"))

        recycled = private.open_locked_slot(pending_name, lock)
        try:
            recycled.reset_for_write(source)
            assert (
                os.fstat(recycled.descriptor).st_dev,
                os.fstat(recycled.descriptor).st_ino,
            ) == (first_identity)
            assert os.fstat(recycled.descriptor).st_size == 0
        finally:
            recycled.close()
    finally:
        os.close(source)
        lock.close()
        private.close()
        publication.close()


@pytest.mark.skipif(os.name == "nt", reason="uses fork to simulate owner death")
def test_parent_anchored_output_lock_is_reusable_after_process_death(
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("fork")
    ready = context.Event()

    def acquire_then_die() -> None:
        publication = workspace_module.PublicationDirectory.open(tmp_path)
        private = publication.open_private_directory(
            ".matteloop-publish",
            "publication",
        )
        key = publication.target_key(tmp_path / "output.webp")
        publication.acquire_output_lock(private, key)
        ready.set()
        os._exit(0)

    process = context.Process(target=acquire_then_die)
    process.start()
    assert ready.wait(5)
    process.join(5)
    assert process.exitcode == 0

    publication = workspace_module.PublicationDirectory.open(tmp_path)
    private = publication.open_private_directory(
        ".matteloop-publish",
        "publication",
    )
    key = publication.target_key(tmp_path / "output.webp")
    reused = publication.acquire_output_lock(private, key)
    reused.close()
    private.close()
    publication.close()


@pytest.mark.skipif(os.name == "nt", reason="uses fork to simulate slot-owner death")
def test_fixed_output_slot_is_reusable_after_process_death(tmp_path: Path) -> None:
    context = multiprocessing.get_context("fork")
    ready = context.Event()

    def acquire_slot_then_die() -> None:
        publication = workspace_module.PublicationDirectory.open(tmp_path)
        private = publication.open_private_directory(
            ".matteloop-publish",
            "publication",
        )
        key = publication.target_key(tmp_path / "output.webp")
        lock = publication.acquire_output_lock(private, key)
        slot = private.open_locked_slot(f".{key}.publish-pending", lock)
        os.write(slot.descriptor, b"crash-stale")
        ready.set()
        os._exit(0)

    process = context.Process(target=acquire_slot_then_die)
    process.start()
    assert ready.wait(5)
    process.join(5)
    assert process.exitcode == 0

    publication = workspace_module.PublicationDirectory.open(tmp_path)
    private = publication.open_private_directory(
        ".matteloop-publish",
        "publication",
    )
    key = publication.target_key(tmp_path / "output.webp")
    lock = publication.acquire_output_lock(private, key)
    slot = private.open_locked_slot(f".{key}.publish-pending", lock)
    try:
        assert os.pread(slot.descriptor, len(b"crash-stale"), 0) == b"crash-stale"
    finally:
        slot.close()
        lock.close()
        private.close()
        publication.close()


@pytest.mark.skipif(os.name == "nt", reason="exercises a POSIX hardlink source alias")
def test_locked_hardlink_slot_is_never_truncated_as_stale(tmp_path: Path) -> None:
    source = tmp_path / "output.webp"
    source.write_bytes(b"held-source")
    publication = workspace_module.PublicationDirectory.open(tmp_path)
    private = publication.open_private_directory(
        ".matteloop-publish",
        "publication",
    )
    key = publication.target_key(source)
    lock = publication.acquire_output_lock(private, key)
    pending_name = f".{key}.publish-pending"
    assert private.link_parent_file(source.name, pending_name, lock)
    source_descriptor = publication.open_read(source.name)
    slot = private.open_locked_slot(pending_name, lock, create_if_missing=False)
    identity = os.fstat(source_descriptor).st_dev, os.fstat(source_descriptor).st_ino
    try:
        with pytest.raises(AppError, match="aliases its write source"):
            slot.reset_for_write(source_descriptor)

        assert (os.fstat(slot.descriptor).st_dev, os.fstat(slot.descriptor).st_ino) == (
            identity
        )
        assert os.pread(slot.descriptor, len(b"held-source"), 0) == b"held-source"
        assert source.read_bytes() == b"held-source"
    finally:
        slot.close()
        os.close(source_descriptor)
        lock.close()
        private.close()
        publication.close()


@pytest.mark.skipif(os.name == "nt", reason="exercises POSIX hardlink aliases")
def test_owned_replace_keeps_same_inode_alias_instead_of_blind_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = workspace_module.PublicationDirectory.open(tmp_path)
    private = publication.open_private_directory(
        ".matteloop-publish",
        "publication",
    )
    key = publication.target_key(tmp_path / "output.webp")
    owner = publication.acquire_output_lock(private, key)
    pending_name = f".{key}.publish-pending"
    final_name = f"{key}.publish"
    slot = private.open_locked_slot(pending_name, owner)
    os.write(slot.descriptor, b"held-slot")
    assert private.link_file(pending_name, final_name, owner)

    def reject_blind_unlink(_self, _name: str) -> None:
        raise AssertionError("same-inode fixed slot must never be blindly unlinked")

    monkeypatch.setattr(workspace_module._BoundDirectory, "unlink", reject_blind_unlink)
    try:
        private.replace_owned(pending_name, final_name, slot, owner)

        slot.assert_owned()
        pending = private.lstat(pending_name)
        final = private.lstat(final_name)
        assert (pending.st_dev, pending.st_ino) == slot.identity
        assert (final.st_dev, final.st_ino) == slot.identity
        assert os.pread(slot.descriptor, len(b"held-slot"), 0) == b"held-slot"
    finally:
        slot.close()
        owner.close()
        private.close()
        publication.close()


def test_existing_private_pending_without_anchored_owner_is_never_unlinked(
    tmp_path: Path,
) -> None:
    private_path = tmp_path / ".matteloop-publish"
    private_path.mkdir(mode=0o700)
    pending = private_path / ".pending"
    pending.write_bytes(b"foreign-pending")
    identity = pending.stat().st_dev, pending.stat().st_ino
    directory = RecoveryDirectory.open(tmp_path, private_path.name)
    try:
        with pytest.raises(AppError, match="no transaction owner") as exc:
            directory.open_fixed_pending(pending.name)
    finally:
        directory.close()

    assert (pending.stat().st_dev, pending.stat().st_ino) == identity
    assert pending.read_bytes() == b"foreign-pending"
    assert exc.value.code is ErrorCode.CUT_WORKSPACE_UNSAFE


@pytest.mark.skipif(os.name == "nt", reason="exercises the POSIX lock adapter")
def test_private_output_locks_for_different_targets_do_not_serialize(
    tmp_path: Path,
) -> None:
    private_path = tmp_path / ".matteloop-publish"
    private_path.mkdir(mode=0o700)
    directory = RecoveryDirectory.open(tmp_path, private_path.name)
    first = directory.acquire_advisory_lock("first.transaction.lock")
    second = directory.acquire_advisory_lock("second.transaction.lock")
    try:
        assert first.name != second.name
    finally:
        second.close()
        first.close()
        directory.close()


@pytest.mark.skipif(os.name == "nt", reason="exercises the POSIX lock adapter")
def test_parent_anchored_locks_for_distinct_targets_are_independent(
    tmp_path: Path,
) -> None:
    publication = workspace_module.PublicationDirectory.open(tmp_path)
    private = publication.open_private_directory(
        ".matteloop-publish",
        "publication",
    )
    first = publication.acquire_output_lock(
        private,
        publication.target_key(tmp_path / "first.webp"),
    )
    second = publication.acquire_output_lock(
        private,
        publication.target_key(tmp_path / "second.webp"),
    )
    try:
        first.assert_owned()
        second.assert_owned()
    finally:
        second.close()
        first.close()
        private.close()
        publication.close()


def test_post_open_anchor_disappearance_closes_the_abandoned_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = workspace_module.PublicationDirectory.open(tmp_path)
    private = publication.open_private_directory(
        ".matteloop-publish",
        "publication",
    )
    key = publication.target_key(tmp_path / "output.webp")
    initial = publication.acquire_output_lock(private, key)
    initial.close()
    anchor_name = f".{key}.transaction-anchor"
    actual_close = os.close
    opened_anchor_descriptors = _inject_post_open_anchor_disappearance(
        publication,
        anchor_name,
        monkeypatch,
    )
    try:
        with pytest.raises(FileExistsError):
            publication.acquire_output_lock(private, key)

        assert len(opened_anchor_descriptors) == 1
        opened_anchor_descriptor = opened_anchor_descriptors[0]
        with pytest.raises(OSError) as closed:
            os.fstat(opened_anchor_descriptor)
        assert closed.value.errno == errno.EBADF
    finally:
        for opened_anchor_descriptor in opened_anchor_descriptors:
            try:
                actual_close(opened_anchor_descriptor)
            except OSError:
                pass
        private.close()
        publication.close()


def test_post_open_anchor_disappearance_surfaces_ambiguous_close_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = workspace_module.PublicationDirectory.open(tmp_path)
    private = publication.open_private_directory(
        ".matteloop-publish",
        "publication",
    )
    key = publication.target_key(tmp_path / "output.webp")
    initial = publication.acquire_output_lock(private, key)
    initial.close()
    anchor_name = f".{key}.transaction-anchor"
    close_failed = False
    actual_close = os.close
    opened_anchor_descriptors = _inject_post_open_anchor_disappearance(
        publication,
        anchor_name,
        monkeypatch,
    )

    def close_anchor_then_raise(descriptor: int) -> None:
        nonlocal close_failed
        actual_close(descriptor)
        if (
            not close_failed
            and opened_anchor_descriptors
            and descriptor == opened_anchor_descriptors[0]
        ):
            close_failed = True
            raise OSError(errno.EIO, "synthetic post-close raced anchor failure")

    monkeypatch.setattr(workspace_module.os, "close", close_anchor_then_raise)
    reused: int | None = None
    try:
        with pytest.raises(AppError, match="post-close raced anchor failure") as exc:
            publication.acquire_output_lock(private, key)

        assert close_failed
        assert len(opened_anchor_descriptors) == 1
        reused = _force_descriptor_reuse(
            tmp_path / "raced-anchor-fd-reuse",
            opened_anchor_descriptors[0],
            actual_close,
        )
        assert workspace_module._drain_attached_bound_directory_closes(exc.value) == 0
        os.fstat(reused)
    finally:
        if reused is not None:
            try:
                actual_close(reused)
            except OSError:
                pass
        else:
            for opened_anchor_descriptor in opened_anchor_descriptors:
                try:
                    actual_close(opened_anchor_descriptor)
                except OSError:
                    pass
        private.close()
        publication.close()


@pytest.mark.skipif(os.name == "nt", reason="exercises POSIX descriptor reuse")
def test_parent_anchor_close_then_raise_is_consumed_without_fd_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = workspace_module.PublicationDirectory.open(tmp_path)
    private = publication.open_private_directory(
        ".matteloop-publish",
        "publication",
    )
    key = publication.target_key(tmp_path / "output.webp")
    lock = publication.acquire_output_lock(private, key)
    anchor_descriptor = lock._anchor_descriptor
    assert anchor_descriptor is not None
    actual_close = os.close

    def close_anchor_then_raise(descriptor: int) -> None:
        actual_close(descriptor)
        if descriptor == anchor_descriptor:
            raise OSError(errno.EIO, "synthetic post-close anchor failure")

    monkeypatch.setattr(workspace_module.os, "close", close_anchor_then_raise)
    with pytest.raises(AppError, match="post-close anchor failure") as exc:
        lock.close()

    assert lock._descriptor is None
    assert lock._anchor_descriptor is None
    reused = _force_descriptor_reuse(
        tmp_path / "anchor-fd-reuse",
        anchor_descriptor,
        actual_close,
    )
    try:
        lock.close()
        os.fstat(reused)
        assert workspace_module._drain_attached_bound_directory_closes(exc.value) == 0
    finally:
        try:
            actual_close(reused)
        except OSError:
            pass
    private.close()
    publication.close()


@pytest.mark.skipif(os.name == "nt", reason="uses fork to exercise POSIX lock lifetime")
def test_transaction_lock_stays_exclusive_until_descriptor_close_completes(
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("fork")
    close_started = context.Event()
    finish_close = context.Event()
    result = context.Queue()

    def close_owner() -> None:
        publication = workspace_module.PublicationDirectory.open(tmp_path)
        private = publication.open_private_directory(
            ".matteloop-publish",
            "publication",
        )
        key = publication.target_key(tmp_path / "output.webp")
        lock = publication.acquire_output_lock(private, key)
        descriptor = lock._descriptor
        assert descriptor is not None
        actual_close = workspace_module.os.close

        def pause_locked_descriptor_close(value: int) -> None:
            if value == descriptor:
                close_started.set()
                if not finish_close.wait(10):
                    raise RuntimeError("test did not finish transaction-lock close")
            actual_close(value)

        workspace_module.os.close = pause_locked_descriptor_close
        try:
            lock.close()
            lock.close()
        except BaseException as error:
            result.put((type(error).__name__, str(error)))
        else:
            result.put(("ok", ""))
        finally:
            private.close()
            publication.close()

    process = context.Process(target=close_owner)
    process.start()
    contender_descriptor: int | None = None
    acquired = False
    try:
        assert close_started.wait(5)
        lock_path = next((tmp_path / ".matteloop-publish").glob("*.transaction.lock"))
        original_bytes = lock_path.read_bytes()
        contender_descriptor = os.open(lock_path, os.O_RDWR)
        contender = workspace_module._SystemAdvisoryFileLock()
        acquired = contender.acquire_nonblocking(contender_descriptor)
        if acquired:
            os.ftruncate(contender_descriptor, 0)

        assert not acquired
        assert lock_path.read_bytes() == original_bytes
    finally:
        if contender_descriptor is not None:
            os.close(contender_descriptor)
        finish_close.set()
        process.join(10)

    assert process.exitcode == 0
    assert result.get(timeout=2) == ("ok", "")
    publication = workspace_module.PublicationDirectory.open(tmp_path)
    private = publication.open_private_directory(
        ".matteloop-publish",
        "publication",
    )
    key = publication.target_key(tmp_path / "output.webp")
    reused = publication.acquire_output_lock(private, key)
    reused.close()
    private.close()
    publication.close()


@pytest.mark.skipif(os.name == "nt", reason="uses fork to exercise POSIX lock lifetime")
def test_fixed_slot_lock_stays_exclusive_until_descriptor_close_completes(
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("fork")
    close_started = context.Event()
    finish_close = context.Event()
    result = context.Queue()

    def close_owner() -> None:
        publication = workspace_module.PublicationDirectory.open(tmp_path)
        private = publication.open_private_directory(
            ".matteloop-publish",
            "publication",
        )
        key = publication.target_key(tmp_path / "output.webp")
        lock = publication.acquire_output_lock(private, key)
        slot = private.open_locked_slot(f".{key}.publish-pending", lock)
        os.write(slot.descriptor, b"live-slot")
        descriptor = slot.descriptor
        actual_close = workspace_module.os.close

        def pause_locked_descriptor_close(value: int) -> None:
            if value == descriptor:
                close_started.set()
                if not finish_close.wait(10):
                    raise RuntimeError("test did not finish fixed-slot close")
            actual_close(value)

        workspace_module.os.close = pause_locked_descriptor_close
        try:
            slot.close()
            slot.close()
        except BaseException as error:
            result.put((type(error).__name__, str(error)))
        else:
            result.put(("ok", ""))
        finally:
            lock.close()
            private.close()
            publication.close()

    process = context.Process(target=close_owner)
    process.start()
    contender_descriptor: int | None = None
    acquired = False
    try:
        assert close_started.wait(5)
        slot_path = next((tmp_path / ".matteloop-publish").glob(".*.publish-pending"))
        contender_descriptor = os.open(slot_path, os.O_RDWR)
        contender = workspace_module._SystemAdvisoryFileLock()
        acquired = contender.acquire_nonblocking(contender_descriptor)
        if acquired:
            os.ftruncate(contender_descriptor, 0)

        assert not acquired
        assert slot_path.read_bytes() == b"live-slot"
    finally:
        if contender_descriptor is not None:
            os.close(contender_descriptor)
        finish_close.set()
        process.join(10)

    assert process.exitcode == 0
    assert result.get(timeout=2) == ("ok", "")
    slot_path = next((tmp_path / ".matteloop-publish").glob(".*.publish-pending"))
    descriptor = os.open(slot_path, os.O_RDWR)
    adapter = workspace_module._SystemAdvisoryFileLock()
    try:
        assert adapter.acquire_nonblocking(descriptor)
    finally:
        os.close(descriptor)


@pytest.mark.skipif(os.name == "nt", reason="exercises POSIX descriptor reuse")
def test_locked_slot_close_then_raise_consumes_fd_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = workspace_module.PublicationDirectory.open(tmp_path)
    private = publication.open_private_directory(
        ".matteloop-publish",
        "publication",
    )
    key = publication.target_key(tmp_path / "output.webp")
    lock = publication.acquire_output_lock(private, key)
    slot = private.open_locked_slot(f".{key}.publish-pending", lock)
    slot_descriptor = slot.descriptor
    actual_close = os.close

    def close_slot_then_raise(descriptor: int) -> None:
        actual_close(descriptor)
        if descriptor == slot_descriptor:
            raise OSError(errno.EIO, "synthetic post-close slot failure")

    monkeypatch.setattr(workspace_module.os, "close", close_slot_then_raise)
    with pytest.raises(AppError, match="post-close slot failure") as exc:
        slot.close()

    with pytest.raises(AppError, match="closed"):
        _ = slot.descriptor
    reused = _force_descriptor_reuse(
        tmp_path / "slot-fd-reuse",
        slot_descriptor,
        actual_close,
    )
    try:
        slot.close()
        os.fstat(reused)
        assert workspace_module._drain_attached_bound_directory_closes(exc.value) == 0
        contender = private.open_locked_slot(
            slot.name,
            lock,
            create_if_missing=False,
        )
        contender.close()
    finally:
        try:
            actual_close(reused)
        except OSError:
            pass
    lock.close()
    private.close()
    publication.close()


@pytest.mark.skipif(os.name == "nt", reason="exercises POSIX descriptor reuse")
def test_transaction_close_then_raise_consumes_fd_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = workspace_module.PublicationDirectory.open(tmp_path)
    private = publication.open_private_directory(
        ".matteloop-publish",
        "publication",
    )
    key = publication.target_key(tmp_path / "output.webp")
    lock = publication.acquire_output_lock(private, key)
    descriptor = lock._descriptor
    assert descriptor is not None
    actual_close = workspace_module.os.close

    def close_transaction_then_raise(value: int) -> None:
        actual_close(value)
        if value == descriptor:
            raise OSError(errno.EIO, "synthetic post-close transaction failure")

    monkeypatch.setattr(workspace_module.os, "close", close_transaction_then_raise)
    with pytest.raises(AppError, match="post-close transaction failure") as exc:
        lock.close()

    assert lock._descriptor is None
    assert lock._anchor_descriptor is None
    reused = _force_descriptor_reuse(
        tmp_path / "transaction-fd-reuse",
        descriptor,
        actual_close,
    )
    try:
        lock.close()
        os.fstat(reused)
        assert workspace_module._drain_attached_bound_directory_closes(exc.value) == 0
        next_owner = publication.acquire_output_lock(private, key)
        next_owner.close()
    finally:
        try:
            actual_close(reused)
        except OSError:
            pass
    private.close()
    publication.close()


def test_private_output_lock_serializes_threads_even_if_platform_lock_reenters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_path = tmp_path / ".matteloop-publish"
    private_path.mkdir(mode=0o700)
    monkeypatch.setattr(
        workspace_module._SystemAdvisoryFileLock,
        "acquire_nonblocking",
        lambda _self, _descriptor: True,
    )
    first_directory = RecoveryDirectory.open(tmp_path, private_path.name)
    second_directory = RecoveryDirectory.open(tmp_path, private_path.name)
    first = first_directory.acquire_advisory_lock("target.transaction.lock")
    try:
        with pytest.raises(BlockingIOError):
            second_directory.acquire_advisory_lock("target.transaction.lock")
    finally:
        first.close()
        second_directory.close()
        first_directory.close()


def test_fixed_slot_serializes_threads_even_if_platform_lock_reenters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = workspace_module.PublicationDirectory.open(tmp_path)
    first_directory = publication.open_private_directory(
        ".matteloop-publish",
        "publication",
    )
    second_directory = RecoveryDirectory.open(tmp_path, ".matteloop-publish")
    key = publication.target_key(tmp_path / "output.webp")
    owner = publication.acquire_output_lock(first_directory, key)
    monkeypatch.setattr(
        workspace_module._SystemAdvisoryFileLock,
        "acquire_nonblocking",
        lambda _self, _descriptor: True,
    )
    first = first_directory.open_locked_slot(f".{key}.publish-pending", owner)
    try:
        with pytest.raises(BlockingIOError):
            second_directory.open_locked_slot(
                first.name,
                owner,
                create_if_missing=False,
            )
    finally:
        first.close()
        owner.close()
        second_directory.close()
        first_directory.close()
        publication.close()


def test_contended_output_close_then_raise_is_consumed_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_path = tmp_path / ".matteloop-publish"
    private_path.mkdir(mode=0o700)
    blocked_descriptor: int | None = None
    actual_close = workspace_module.os.close

    def report_platform_contention(_self, descriptor: int) -> bool:
        nonlocal blocked_descriptor
        blocked_descriptor = descriptor
        return False

    def close_lock_descriptor_then_raise(descriptor: int) -> None:
        actual_close(descriptor)
        if descriptor == blocked_descriptor:
            raise OSError(errno.EIO, "synthetic post-close lock failure")

    monkeypatch.setattr(
        workspace_module._SystemAdvisoryFileLock,
        "acquire_nonblocking",
        report_platform_contention,
    )
    monkeypatch.setattr(
        workspace_module.os,
        "close",
        close_lock_descriptor_then_raise,
    )
    directory = RecoveryDirectory.open(tmp_path, private_path.name)
    try:
        with pytest.raises(BlockingIOError) as exc:
            directory.acquire_advisory_lock("target.transaction.lock")

        assert blocked_descriptor is not None
        assert any(
            "post-close lock failure" in note
            for note in getattr(exc.value, "__notes__", ())
        )
        reused = _force_descriptor_reuse(
            tmp_path / "contended-fd-reuse",
            blocked_descriptor,
            actual_close,
        )
        try:
            assert (
                workspace_module._drain_attached_bound_directory_closes(exc.value) == 0
            )
            os.fstat(reused)
        finally:
            try:
                actual_close(reused)
            except OSError:
                pass
    finally:
        directory.close()


def test_transaction_adapter_construction_failure_closes_owned_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_path = tmp_path / ".matteloop-publish"
    private_path.mkdir(mode=0o700)
    directory = RecoveryDirectory.open(tmp_path, private_path.name)
    opened_descriptor: int | None = None
    directory_type = type(directory._directory)
    actual_open = directory_type.open_new_publication_fd

    def record_open(bound: object, name: str) -> int:
        nonlocal opened_descriptor
        descriptor = actual_open(bound, name)
        opened_descriptor = descriptor
        return descriptor

    class BrokenAdapter:
        def __init__(self) -> None:
            raise RuntimeError("synthetic advisory adapter construction failure")

    monkeypatch.setattr(directory_type, "open_new_publication_fd", record_open)
    monkeypatch.setattr(workspace_module, "_SystemAdvisoryFileLock", BrokenAdapter)
    try:
        with pytest.raises(RuntimeError, match="adapter construction failure"):
            directory.acquire_advisory_lock("target.transaction.lock")

        assert opened_descriptor is not None
        with pytest.raises(OSError) as closed:
            os.fstat(opened_descriptor)
        assert closed.value.errno == errno.EBADF
    finally:
        directory.close()


def test_slot_acquisition_close_then_raise_is_consumed_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = workspace_module.PublicationDirectory.open(tmp_path)
    private = publication.open_private_directory(
        ".matteloop-publish",
        "publication",
    )
    key = publication.target_key(tmp_path / "output.webp")
    transaction = publication.acquire_output_lock(private, key)
    failed_descriptor: int | None = None
    actual_close = workspace_module.os.close

    def fail_acquisition(_self, descriptor: int) -> bool:
        nonlocal failed_descriptor
        failed_descriptor = descriptor
        raise OSError(errno.EIO, "synthetic slot-lock acquisition failure")

    def close_descriptor_then_raise(descriptor: int) -> None:
        actual_close(descriptor)
        if descriptor == failed_descriptor:
            raise OSError(errno.EIO, "synthetic post-close slot cleanup failure")

    monkeypatch.setattr(
        workspace_module._SystemAdvisoryFileLock,
        "acquire_nonblocking",
        fail_acquisition,
    )
    monkeypatch.setattr(workspace_module.os, "close", close_descriptor_then_raise)
    try:
        with pytest.raises(OSError, match="slot-lock acquisition failure") as exc:
            private.open_locked_slot(f".{key}.publish-pending", transaction)

        assert failed_descriptor is not None
        assert any(
            "post-close slot cleanup failure" in note
            for note in getattr(exc.value, "__notes__", ())
        )
        reused = _force_descriptor_reuse(
            tmp_path / "slot-cleanup-fd-reuse",
            failed_descriptor,
            actual_close,
        )
        try:
            assert (
                workspace_module._drain_attached_bound_directory_closes(exc.value) == 0
            )
            os.fstat(reused)
        finally:
            try:
                actual_close(reused)
            except OSError:
                pass
    finally:
        transaction.close()
        private.close()
        publication.close()


def test_windows_close_releases_range_and_ambiguous_fd_is_never_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeMsvcrt:
        LK_NBLCK = 3

        def __init__(self) -> None:
            self.calls: list[tuple[int, int, int]] = []
            self.locked_descriptor: int | None = None

        def locking(self, descriptor: int, operation: int, size: int) -> None:
            self.calls.append((descriptor, operation, size))
            if self.locked_descriptor is not None:
                raise OSError(errno.EACCES, "synthetic Windows lock contention")
            self.locked_descriptor = descriptor

        def closed(self, descriptor: int) -> None:
            if descriptor == self.locked_descriptor:
                self.locked_descriptor = None

    lock_path = tmp_path / "output.transaction.lock"
    lock_path.write_bytes(b"\0")
    descriptor = os.open(lock_path, os.O_RDWR)
    windows = FakeMsvcrt()
    adapter = workspace_module._SystemAdvisoryFileLock(
        platform="windows",
        windows_module=windows,
    )
    assert adapter.acquire_nonblocking(descriptor)
    local_lock = threading.Lock()
    local_lock.acquire()
    owner = workspace_module.AdvisoryFileLock(
        "output.transaction.lock",
        descriptor,
        adapter,
        "windows-owner-close",
        local_lock,
    )
    actual_close = workspace_module.os.close
    fail_owner_close = True

    def close_aware(descriptor_to_close: int) -> None:
        nonlocal fail_owner_close
        windows.closed(descriptor_to_close)
        actual_close(descriptor_to_close)
        if fail_owner_close and descriptor_to_close == descriptor:
            fail_owner_close = False
            raise OSError(errno.EIO, "synthetic Windows post-close failure")

    monkeypatch.setattr(workspace_module.os, "close", close_aware)
    with pytest.raises(AppError, match="Windows post-close failure"):
        owner.close()

    assert owner._descriptor is None
    assert windows.locked_descriptor is None
    reused = _force_descriptor_reuse(
        tmp_path / "windows-fd-reuse",
        descriptor,
        actual_close,
    )
    owner.close()
    os.fstat(reused)

    contender_descriptor = os.open(lock_path, os.O_RDWR)
    try:
        assert adapter.acquire_nonblocking(contender_descriptor)
    finally:
        os.close(contender_descriptor)
        try:
            actual_close(reused)
        except OSError:
            pass

    assert windows.calls == [
        (descriptor, windows.LK_NBLCK, 1),
        (contender_descriptor, windows.LK_NBLCK, 1),
    ]


def test_windows_recovery_directory_uses_bound_copy_replace_and_flush(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recovery_path = tmp_path / ".matteloop-recovery"
    recovery_path.mkdir(mode=0o700)
    api = _install_exclusive_windows_cuts_api(monkeypatch, tmp_path)
    replacements: list[tuple[int, str, str]] = []
    flushes: list[int] = []
    actual_replace = api.replace_at

    def observe_replace(handle: int, source: str, destination: str) -> None:
        replacements.append((handle, source, destination))
        actual_replace(handle, source, destination)

    def observe_flush(handle: int) -> None:
        flushes.append(handle)

    monkeypatch.setattr(api, "replace_at", observe_replace)
    monkeypatch.setattr(api, "flush_directory_strict", observe_flush)

    recovery = RecoveryDirectory.open(tmp_path, recovery_path.name)
    try:
        assert not recovery.link_parent_file("output.webp", ".pending")
        descriptor = recovery.open_fixed_pending(".pending")
        try:
            os.write(descriptor, b"old-output")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

        recovery.replace(".pending", "output.recovery")
        recovery.fsync()

        recovery_handle = next(
            handle for handle, path in api.handle_paths.items() if path == recovery_path
        )
        assert replacements == [(recovery_handle, ".pending", "output.recovery")]
        assert flushes == [recovery_handle]
        assert (recovery_path / "output.recovery").read_bytes() == b"old-output"
    finally:
        recovery.close()
    assert api.handle_paths == {}


def test_windows_share_fake_checks_existing_share_before_granting_new_write(
    tmp_path: Path,
) -> None:
    cuts_root = tmp_path / "cuts"
    child = cuts_root / "candidate"
    child.mkdir(parents=True)
    api = _ExclusiveWindowsCutsApi(cuts_root)
    root_handle = api.bind_root()
    api.open_child_directory(
        root_handle,
        child.name,
        create=False,
        desired_access=0x80000000,
        share_mode=0x00000001,
        flags=0,
    )

    with pytest.raises(OSError, match="sharing violation"):
        api.open_child_directory(
            root_handle,
            child.name,
            create=False,
            desired_access=0x80000000 | 0x40000000,
            share_mode=0x00000001 | 0x00000002,
            flags=0,
        )


def test_windows_share_fake_checks_new_share_against_existing_write(
    tmp_path: Path,
) -> None:
    cuts_root = tmp_path / "cuts"
    child = cuts_root / "candidate"
    child.mkdir(parents=True)
    api = _ExclusiveWindowsCutsApi(cuts_root)
    root_handle = api.bind_root()
    api.open_child_directory(
        root_handle,
        child.name,
        create=False,
        desired_access=0x80000000 | 0x40000000,
        share_mode=0x00000001 | 0x00000002,
        flags=0,
    )

    with pytest.raises(OSError, match="sharing violation"):
        api.open_child_directory(
            root_handle,
            child.name,
            create=False,
            desired_access=0x80000000,
            share_mode=0x00000001,
            flags=0,
        )


def test_windows_share_fake_keys_lexical_aliases_by_directory_identity(
    tmp_path: Path,
) -> None:
    cuts_root = tmp_path / "cuts"
    child = cuts_root / "candidate"
    child.mkdir(parents=True)
    alias = child / ".." / child.name
    direct_info = child.stat()
    alias_info = alias.stat()
    assert child != alias
    assert (direct_info.st_dev, direct_info.st_ino) == (
        alias_info.st_dev,
        alias_info.st_ino,
    )
    api = _ExclusiveWindowsCutsApi(cuts_root)

    read_handle = api.open_directory(
        child,
        desired_access=0x80000000,
        share_mode=0x00000001,
        flags=0,
    )
    with pytest.raises(OSError, match="sharing violation"):
        api.open_directory(
            alias,
            desired_access=0x80000000 | 0x40000000,
            share_mode=0x00000001 | 0x00000002,
            flags=0,
        )
    api.close_handle(read_handle)

    write_handle = api.open_directory(
        child,
        desired_access=0x80000000 | 0x40000000,
        share_mode=0x00000001 | 0x00000002,
        flags=0,
    )
    with pytest.raises(OSError, match="sharing violation"):
        api.open_directory(
            alias,
            desired_access=0x80000000,
            share_mode=0x00000001,
            flags=0,
        )
    api.close_handle(write_handle)


def test_windows_share_fake_distinguishes_ancestor_from_same_child_object(
    tmp_path: Path,
) -> None:
    cuts_root = tmp_path / "cuts"
    child = cuts_root / "candidate"
    child.mkdir(parents=True)
    (child / "proof.txt").write_text("bound child", encoding="utf-8")
    api = _ExclusiveWindowsCutsApi(cuts_root)
    root_handle = api.bind_root()

    child_handle = api.open_child_directory(
        root_handle,
        child.name,
        create=False,
        desired_access=0x80000000 | 0x40000000,
        share_mode=0x00000001,
        flags=0,
    )

    assert [
        name for name, _info in api.iter_entries_at(child_handle, max_entries=2)
    ] == ["proof.txt"]
    with pytest.raises(OSError, match="sharing violation"):
        api.open_child_directory(
            root_handle,
            child.name,
            create=False,
            desired_access=0x80000000,
            share_mode=0x00000001,
            flags=0,
        )


def test_windows_share_fake_close_removes_only_that_open_record(tmp_path: Path) -> None:
    cuts_root = tmp_path / "cuts"
    child = cuts_root / "candidate"
    child.mkdir(parents=True)
    api = _ExclusiveWindowsCutsApi(cuts_root)
    root_handle = api.bind_root()
    read_handle = api.open_child_directory(
        root_handle,
        child.name,
        create=False,
        desired_access=0x80000000,
        share_mode=0x00000001 | 0x00000002,
        flags=0,
    )
    write_handle = api.open_child_directory(
        root_handle,
        child.name,
        create=False,
        desired_access=0x80000000 | 0x40000000,
        share_mode=0x00000001,
        flags=0,
    )

    api.close_handle(read_handle)

    with pytest.raises(OSError, match="sharing violation"):
        api.open_child_directory(
            root_handle,
            child.name,
            create=False,
            desired_access=0x80000000,
            share_mode=0x00000001,
            flags=0,
        )

    api.close_handle(write_handle)
    reopened = api.open_child_directory(
        root_handle,
        child.name,
        create=False,
        desired_access=0x80000000,
        share_mode=0x00000001,
        flags=0,
    )
    assert api.file_attributes(reopened) & 0x00000010


def test_manifest_is_deeply_immutable_strict_and_deterministic(tmp_path: Path) -> None:
    staged, manifest = _completed_staging(tmp_path)

    with pytest.raises(FrozenInstanceError):
        manifest.edited = True  # type: ignore[misc]
    with pytest.raises(TypeError):
        manifest.cache_key_inputs["source_sha256"] = "d" * 64  # type: ignore[index]
    sampling = manifest.cache_key_inputs["sampling"]
    assert isinstance(sampling, dict | workspace_module.FrozenJsonMap)
    with pytest.raises(TypeError):
        sampling["fps"] = 30  # type: ignore[index]

    encoded = manifest.to_json_bytes()
    assert encoded == manifest.to_json_bytes()
    assert encoded.endswith(b"\n")
    decoded = json.loads(encoded)
    assert list(decoded) == sorted(decoded)
    assert CutManifest.from_json_bytes(encoded) == manifest
    assert staged.cache_key == manifest.cache_key


def test_manifest_frozen_json_has_no_reachable_mutable_lookup(tmp_path: Path) -> None:
    _staged, manifest = _completed_staging(tmp_path)

    frozen = manifest.cache_key_inputs
    assert frozen.__slots__ == ("_items",)
    for slot in frozen.__slots__:
        assert not isinstance(object.__getattribute__(frozen, slot), dict)
    before = manifest.cache_key
    with pytest.raises(AttributeError):
        object.__getattribute__(frozen, "_lookup")
    assert CutManifest.cache_key_for(frozen) == before


def test_manifest_frozen_json_rejects_slot_reassignment_and_deletion(
    tmp_path: Path,
) -> None:
    _staged, manifest = _completed_staging(tmp_path)
    frozen = manifest.cache_key_inputs
    original_key = manifest.cache_key

    with pytest.raises(FrozenInstanceError):
        frozen._items = ()  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        del frozen._items  # type: ignore[misc]

    assert CutManifest.cache_key_for(frozen) == original_key


def test_manifest_rejects_frozen_json_subclasses_with_mutable_state(
    tmp_path: Path,
) -> None:
    _staged, manifest = _completed_staging(tmp_path)

    class MutableFrozenJsonMap(workspace_module.FrozenJsonMap):
        __slots__ = ("mutable_state",)

        def __init__(self, original: workspace_module.FrozenJsonMap) -> None:
            super().__init__(tuple(original.items()))
            object.__setattr__(self, "mutable_state", {"mode": "standard"})

    supplied = MutableFrozenJsonMap(manifest.cache_key_inputs)

    with pytest.raises(AppError) as exc:
        CutManifest(
            cache_key=manifest.cache_key,
            cache_key_inputs=supplied,
            source_path=manifest.source_path,
            source_size_bytes=manifest.source_size_bytes,
            source_mtime_ns=manifest.source_mtime_ns,
            width=manifest.width,
            height=manifest.height,
            frames=manifest.frames,
            union_metadata=manifest.union_metadata,
            edited=manifest.edited,
            pinned=manifest.pinned,
            created_at_ns=manifest.created_at_ns,
            last_used_at_ns=manifest.last_used_at_ns,
        )

    assert exc.value.code is ErrorCode.CUT_MANIFEST_INVALID
    supplied.mutable_state["mode"] = "mutated-after-validation"
    assert manifest.cache_key_inputs is not supplied


def test_frozen_json_constructor_recursively_detaches_mutable_nested_state() -> None:
    nested_list: list[object] = [{"value": "initial"}]
    frozen = workspace_module.FrozenJsonMap((("nested", nested_list),))
    before = repr(frozen)

    nested_list[0] = {"value": "changed"}
    nested_list.append({"value": "extra"})

    assert repr(frozen) == before
    assert frozen["nested"] == (
        workspace_module.FrozenJsonMap((("value", "initial"),)),
    )
    with pytest.raises(TypeError):
        frozen["nested"][0]["value"] = "mutated"  # type: ignore[index,union-attr]

    inputs = _cache_inputs()
    mutable_model = inputs["model"]
    assert isinstance(mutable_model, dict)
    frozen_inputs = workspace_module.FrozenJsonMap(tuple(inputs.items()))
    before_key = CutManifest.cache_key_for(frozen_inputs)
    before_json = json.dumps(workspace_module._thaw_json(frozen_inputs), sort_keys=True)

    mutable_model["id"] = "changed-after-freeze"

    assert CutManifest.cache_key_for(frozen_inputs) == before_key
    assert (
        json.dumps(workspace_module._thaw_json(frozen_inputs), sort_keys=True)
        == before_json
    )


def test_manifest_rejects_non_authoritative_cache_key_inputs() -> None:
    inputs = _cache_inputs()
    inputs["provisional_source_fingerprint"] = "d" * 64

    with pytest.raises(AppError) as exc:
        CutManifest.cache_key_for(inputs)

    assert exc.value.code is ErrorCode.CUT_MANIFEST_INVALID


def test_manifest_cache_key_matches_the_task_4_authoritative_fingerprint(
    tmp_path: Path,
) -> None:
    request = RenderRequest(
        source=tmp_path / "source.mp4",
        sampling=SamplingSpec(Fraction(0), Fraction(1), 15),
        crop=CropSpec(0, 0, 8, 6),
        segmentation=SegmentationSpec(),
        framing=FramingSpec(
            trim=True,
            alpha_threshold=Decimal("2"),
            padding=4,
            stretch_x=Decimal("1.25"),
        ),
        output=OutputSpec(tmp_path, "result.webp"),
    )

    assert CutManifest.cache_key_for(_cache_inputs()) == cut_cache_key(
        request,
        source_sha256="a" * 64,
        model_weight_sha256="b" * 64,
    )


@pytest.mark.parametrize("cache_key", ["../escape", "A" * 64, "a" * 63, "a/" * 32])
def test_workspace_rejects_noncanonical_cache_keys(
    tmp_path: Path, cache_key: str
) -> None:
    with pytest.raises(AppError) as exc:
        CutWorkspace.open(tmp_path, cache_key)
    assert exc.value.code is ErrorCode.CUT_WORKSPACE_UNSAFE


def test_workspace_rejects_symlinked_root_component(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / ".matteloop-work").symlink_to(outside, target_is_directory=True)

    with pytest.raises(AppError) as exc:
        CutWorkspace.create_staging(tmp_path, "a" * 64, "job-1")

    assert exc.value.code is ErrorCode.CUT_WORKSPACE_UNSAFE
    assert list(outside.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor-race probe")
def test_component_binding_rejects_real_ancestor_swap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cuts = _promoted(tmp_path)
    root = cuts.workspace_root
    moved = tmp_path / "workspace-moved"
    outside = tmp_path / "outside"
    outside.mkdir()
    original_open = workspace_module.os.open
    swapped = False

    def swap_before_component_open(
        path: object, flags: int, *args: object, **kwargs: object
    ) -> int:
        nonlocal swapped
        if (
            path == ".matteloop-work"
            and kwargs.get("dir_fd") is not None
            and not swapped
        ):
            swapped = True
            root.rename(moved)
            root.symlink_to(outside, target_is_directory=True)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(workspace_module.os, "open", swap_before_component_open)
    with pytest.raises(AppError) as exc:
        validate_cut_set(cuts)

    assert exc.value.code is ErrorCode.CUT_WORKSPACE_UNSAFE
    assert not (outside / MANIFEST_FILENAME).exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor-count probe")
def test_component_binding_closes_descriptors_when_fstat_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    process = psutil.Process()
    before = process.num_fds()
    original_fstat = workspace_module.os.fstat
    expected = tmp_path.stat()

    def fail_for_target(descriptor: int) -> os.stat_result:
        info = original_fstat(descriptor)
        if (info.st_dev, info.st_ino) == (expected.st_dev, expected.st_ino):
            raise OSError("injected post-open identity failure")
        return info

    monkeypatch.setattr(workspace_module.os, "fstat", fail_for_target)
    with pytest.raises(OSError, match="injected"):
        workspace_module._BoundDirectory.open(tmp_path)

    assert process.num_fds() <= before


def test_local_filesystem_policy_degrades_for_nonlocal_and_unknown_storage(
    tmp_path: Path,
) -> None:
    assert (
        workspace_module._locality_fallback(
            tmp_path, probe=lambda _bound: True
        )
        is None
    )
    nonlocal_fallback = workspace_module._locality_fallback(
        tmp_path, probe=lambda _bound: False
    )
    assert nonlocal_fallback is not None
    assert nonlocal_fallback.reason == "network-filesystem"

    def undecidable(_bound: workspace_module._BoundDirectory) -> bool:
        raise OSError("injected unsupported probe")

    unknown_fallback = workspace_module._locality_fallback(
        tmp_path, probe=undecidable
    )
    assert unknown_fallback is not None
    assert unknown_fallback.reason == "locality-unknown"
    assert workspace_module._windows_drive_type_is_local(2) is True
    assert workspace_module._windows_drive_type_is_local(3) is True
    assert workspace_module._windows_drive_type_is_local(4) is False


def test_nonlocal_workspace_uses_deterministic_local_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cache_root = tmp_path / "user-cache"
    monkeypatch.setattr(paths_module, "user_cache_dir", lambda _app: str(cache_root))
    monkeypatch.setattr(
        workspace_module,
        "_default_local_filesystem_probe",
        lambda _bound: False,
    )

    layout = workspace_module._workspace_layout(tmp_path, create=True)

    assert layout.fallback_used
    assert layout.fallback is not None
    assert layout.fallback.reason == "network-filesystem"
    assert layout.workspace_root == cache_root / "workspaces" / (
        hashlib.sha256(os.fsencode(str(tmp_path))).hexdigest()
    )
    assert layout.workspace_root != tmp_path / ".matteloop-work"


@pytest.mark.parametrize(
    ("filesystem", "expected"),
    [
        ("ext4", True),
        ("xfs", True),
        ("btrfs", True),
        ("tmpfs", True),
        ("overlay", True),
        ("nfs4", False),
        ("cifs", False),
        ("fuse.rclone", False),
        ("futurefs", False),
    ],
)
def test_linux_mountinfo_uses_a_fail_closed_local_filesystem_allowlist(
    filesystem: str, expected: bool
) -> None:
    encoded = f"36 25 8:1 / /workspace rw,relatime - {filesystem} device rw\n".encode()

    assert workspace_module._linux_mountinfo_is_local(encoded, "8:1") is expected


def test_windows_component_binding_rejects_reparse_and_closes_all_handles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from matteloop.jobs.models import cache_fs

    class FakeApi:
        def __init__(self) -> None:
            self.names: dict[int, str] = {1: "/"}
            self.closed: list[int] = []

        def open_anchor(self, _path: Path, **_kwargs: object) -> int:
            return 1

        def open_child_directory(
            self, _parent: int, name: str, **_kwargs: object
        ) -> int:
            handle = len(self.names) + 1
            self.names[handle] = name
            return handle

        def file_attributes(self, handle: int) -> int:
            attributes = cache_fs._FILE_ATTRIBUTE_DIRECTORY
            if self.names[handle] == "reparse":
                attributes |= cache_fs._FILE_ATTRIBUTE_REPARSE_POINT
            return attributes

        def close_handle(self, handle: int) -> None:
            self.closed.append(handle)

    api = FakeApi()
    monkeypatch.setattr(cache_fs, "_CtypesWindowsDirectoryApi", lambda: api)

    with pytest.raises(AppError) as exc:
        workspace_module._BoundDirectory._open_windows(Path("/trusted/reparse/leaf"))

    assert exc.value.code is ErrorCode.CUT_WORKSPACE_UNSAFE
    assert api.closed == [3, 2, 1]


def test_windows_publication_parent_has_scoped_access_and_bidirectional_share(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from matteloop.jobs.models import cache_fs

    class FakeApi:
        def __init__(self) -> None:
            self.opens: list[tuple[str, int, int]] = []
            self.names: dict[int, str] = {1: "/"}

        def open_anchor(
            self,
            _path: Path,
            *,
            desired_access: int,
            share_mode: int,
            flags: int,
        ) -> int:
            assert flags == cache_fs._WINDOWS_DIRECTORY_FLAGS
            self.opens.append(("anchor", desired_access, share_mode))
            return 1

        def open_child_directory(
            self,
            parent: int,
            name: str,
            **kwargs: int | bool,
        ) -> int:
            del parent
            handle = len(self.names) + 1
            self.names[handle] = name
            self.opens.append(
                (name, int(kwargs["desired_access"]), int(kwargs["share_mode"]))
            )
            return handle

        def open_publication_child_directory(
            self,
            parent: int,
            name: str,
            **kwargs: int | bool,
        ) -> int:
            return self.open_child_directory(parent, name, **kwargs)

        def file_attributes(self, _handle: int) -> int:
            return cache_fs._FILE_ATTRIBUTE_DIRECTORY

        def close_handle(self, _handle: int) -> None:
            pass

    api = FakeApi()
    monkeypatch.setattr(cache_fs, "_CtypesWindowsDirectoryApi", lambda: api)

    bound = workspace_module._BoundDirectory._open_windows_publication(
        Path("/trusted/output")
    )
    try:
        assert api.opens[:-1] == [
            (
                "anchor",
                cache_fs._WINDOWS_DIRECTORY_ACCESS,
                cache_fs._WINDOWS_DIRECTORY_SHARE,
            ),
            (
                "trusted",
                cache_fs._WINDOWS_DIRECTORY_ACCESS,
                cache_fs._WINDOWS_DIRECTORY_SHARE,
            ),
        ]
        assert api.opens[-1] == (
            "output",
            cache_fs._WINDOWS_PUBLICATION_DIRECTORY_ACCESS,
            cache_fs._WINDOWS_PUBLICATION_SHARE,
        )
    finally:
        bound.close()


def test_windows_publication_parent_sharing_is_bidirectionally_compatible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from matteloop.jobs.models import cache_fs

    api = _ExclusiveWindowsCutsApi(tmp_path)
    publication_share = cache_fs._WINDOWS_PUBLICATION_SHARE
    existing = api.open_directory(
        tmp_path,
        desired_access=0x80000000 | 0x40000000 | 0x00010000,
        share_mode=publication_share,
        flags=cache_fs._WINDOWS_DIRECTORY_FLAGS,
    )
    monkeypatch.setattr(cache_fs, "_CtypesWindowsDirectoryApi", lambda: api)

    bound = workspace_module._BoundDirectory._open_windows_publication(tmp_path)
    try:
        assert not api.sharing_violations
        final_open = api.handle_opens[bound._windows_handles[-1]]
        assert final_open[1:] == (
            cache_fs._WINDOWS_PUBLICATION_DIRECTORY_ACCESS,
            publication_share,
        )
    finally:
        bound.close()
        api.close_handle(existing)


def test_publication_close_attaches_retry_owner_when_registry_is_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FailingCloseApi:
        fail = True

        def close_handle(self, _handle: int) -> None:
            if self.fail:
                raise OSError("persistent synthetic CloseHandle failure")

    api = FailingCloseApi()
    bound = workspace_module._BoundDirectory(
        tmp_path,
        None,
        windows_handles=(41,),
        windows_api=api,
    )
    stack = ExitStack()
    stack.enter_context(bound)
    publication = PublicationDirectory(bound, stack)
    primary = AppError(
        ErrorCode.INVALID_OUTPUT,
        "output",
        "error.output.failed",
        "synthetic primary",
        "retry-output",
    )
    monkeypatch.setattr(workspace_module, "MAX_DEFERRED_BOUND_DIRECTORY_CLOSES", 0)

    publication.close(primary)
    del publication
    gc.collect()

    owners = getattr(primary, "_matteloop_bound_directory_close_owners")
    assert owners == (bound,)
    assert bound.owns_resources()
    api.fail = False
    assert workspace_module._drain_attached_bound_directory_closes(primary) == 1
    assert not bound.owns_resources()


def test_failed_windows_publication_open_retains_unclosed_ancestor_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from matteloop.jobs.models import cache_fs

    class FailingApi:
        fail_close = True

        def open_anchor(self, _path: Path, **_kwargs: int) -> int:
            return 41

        def open_child_directory(
            self, _parent: int, name: str, **_kwargs: int | bool
        ) -> int:
            if name == "trusted":
                return 42
            raise AssertionError(name)

        def open_publication_child_directory(
            self, _parent: int, _name: str, **_kwargs: int | bool
        ) -> int:
            raise OSError("synthetic publication-parent open failure")

        def file_attributes(self, _handle: int) -> int:
            return cache_fs._FILE_ATTRIBUTE_DIRECTORY

        def close_handle(self, _handle: int) -> None:
            if self.fail_close:
                raise OSError("persistent synthetic CloseHandle failure")

    api = FailingApi()
    monkeypatch.setattr(cache_fs, "_CtypesWindowsDirectoryApi", lambda: api)
    monkeypatch.setattr(workspace_module, "MAX_DEFERRED_BOUND_DIRECTORY_CLOSES", 0)

    with pytest.raises(OSError, match="publication-parent open failure") as exc:
        workspace_module._BoundDirectory._open_windows_publication(
            Path("/trusted/output")
        )

    gc.collect()
    owners = getattr(exc.value, "_matteloop_bound_directory_close_owners")
    assert len(owners) == 1
    assert owners[0].owns_resources()
    api.fail_close = False
    assert workspace_module._drain_attached_bound_directory_closes(exc.value) == 1
    assert not owners[0].owns_resources()


def test_windows_bound_mkdir_never_uses_redirected_lexical_path(
    tmp_path: Path,
) -> None:
    lexical = tmp_path / "redirected"
    lexical.mkdir()

    class FakeApi:
        def __init__(self) -> None:
            self.created: list[tuple[int, str, bool]] = []

        def mkdir_at(self, handle: int, name: str, *, exist_ok: bool) -> None:
            self.created.append((handle, name, exist_ok))

        def close_handle(self, _handle: int) -> None:
            pass

    api = FakeApi()
    bound = workspace_module._BoundDirectory(
        lexical, None, windows_handles=(41,), windows_api=api
    )
    bound.mkdir("native-child", exist_ok=False)

    assert api.created == [(41, "native-child", False)]
    assert not (lexical / "native-child").exists()
    bound.close()


def test_windows_bound_enumeration_and_recursive_delete_ignore_lexical_redirect(
    tmp_path: Path,
) -> None:
    lexical = tmp_path / "redirected"
    lexical.mkdir()
    (lexical / "outside-sentinel").write_bytes(b"outside")
    regular = os.stat_result((0o100600, 1, 1, 1, 0, 0, 1, 0, 0, 0))
    directory = os.stat_result((0o040700, 2, 1, 1, 0, 0, 0, 0, 0, 0))

    class FakeApi:
        def __init__(self) -> None:
            self.trees: dict[int, dict[str, os.stat_result]] = {
                50: {"inside.bin": regular, "nested": directory},
                51: {"leaf.bin": regular},
            }
            self.closed: list[int] = []

        def iter_entries_at(self, handle: int, *, max_entries: int) -> object:
            assert len(self.trees[handle]) <= max_entries
            return iter(tuple(self.trees[handle].items()))

        def open_child_directory(
            self, parent: int, name: str, **_kwargs: object
        ) -> int:
            assert (parent, name) == (50, "nested")
            return 51

        def file_attributes(self, handle: int) -> int:
            from matteloop.jobs.models import cache_fs

            assert handle == 51
            return cache_fs._FILE_ATTRIBUTE_DIRECTORY

        def unlink_at(self, handle: int, name: str, *, require_regular: bool) -> None:
            assert require_regular is False
            del self.trees[handle][name]

        def rmdir_at(self, handle: int, name: str) -> None:
            assert (handle, name) == (50, "nested")
            assert not self.trees[51]
            del self.trees[50][name]

        def close_handle(self, handle: int) -> None:
            self.closed.append(handle)

    api = FakeApi()
    bound = workspace_module._BoundDirectory(
        lexical, None, windows_handles=(50,), windows_api=api
    )

    assert {name for name, _info in bound.iter_entries()} == {
        "inside.bin",
        "nested",
    }
    workspace_module._remove_bound_contents(bound, [0])

    assert api.trees[50] == {}
    assert (lexical / "outside-sentinel").read_bytes() == b"outside"
    bound.close()


def test_windows_bound_promotion_and_durability_ignore_lexical_redirect(
    tmp_path: Path,
) -> None:
    lexical = tmp_path / "redirected"
    lexical.mkdir()
    (lexical / "old").mkdir()
    (lexical / "new").mkdir()

    class FakeApi:
        def __init__(self) -> None:
            self.renames: list[tuple[int, str, str]] = []
            self.flushes: list[int] = []

        def replace_directory_at(
            self, handle: int, source: str, destination: str
        ) -> None:
            self.renames.append((handle, source, destination))

        def flush_directory_strict(self, handle: int) -> None:
            self.flushes.append(handle)

        def close_handle(self, _handle: int) -> None:
            pass

    api = FakeApi()
    bound = workspace_module._BoundDirectory(
        lexical, None, windows_handles=(61,), windows_api=api
    )

    bound.replace_directory("old", "new")
    bound.fsync()

    assert api.renames == [(61, "old", "new")]
    assert api.flushes == [61]
    assert (lexical / "old").is_dir()
    assert (lexical / "new").is_dir()
    bound.close()


def test_windows_close_attempts_every_handle_and_retains_failed_ownership(
    tmp_path: Path,
) -> None:
    class FakeApi:
        def __init__(self) -> None:
            self.attempts: list[int] = []
            self.fail = {3, 1}

        def close_handle(self, handle: int) -> None:
            self.attempts.append(handle)
            if handle in self.fail:
                raise OSError(handle, f"close-{handle}")

    api = FakeApi()
    bound = workspace_module._BoundDirectory(
        tmp_path, None, windows_handles=(1, 2, 3), windows_api=api
    )

    with pytest.raises(AppError) as exc:
        bound.close()

    assert exc.value.code is ErrorCode.CUT_WORKSPACE_UNSAFE
    assert api.attempts == [3, 2, 1]
    assert bound._windows_handles == (1, 3)

    api.fail.clear()
    bound.close()
    assert api.attempts == [3, 2, 1, 3, 1]
    assert bound._windows_handles == ()


def test_bound_directory_exit_preserves_primary_app_error_when_close_fails(
    tmp_path: Path,
) -> None:
    class FakeApi:
        def __init__(self) -> None:
            self.attempts: list[int] = []
            self.fail = True

        def close_handle(self, handle: int) -> None:
            self.attempts.append(handle)
            if self.fail:
                raise OSError("injected context close failure")

    api = FakeApi()
    bound = workspace_module._BoundDirectory(
        tmp_path, None, windows_handles=(91,), windows_api=api
    )
    primary = AppError(
        ErrorCode.JOB_CANCELLED,
        "cut-snapshot",
        "error.job.cancelled",
        "snapshot cancellation must remain primary",
        "dismiss",
    )

    with pytest.raises(AppError) as exc:
        with bound:
            raise primary

    assert exc.value is primary
    assert any(
        "bound-directory close failure" in note
        and "injected context close failure" in note
        for note in getattr(primary, "__notes__", ())
    )
    assert bound._windows_handles == (91,)

    api.fail = False
    bound.close()
    assert api.attempts == [91, 91]
    assert bound._windows_handles == ()


def test_inline_context_retains_persistent_close_for_later_drain(
    tmp_path: Path,
) -> None:
    class FakeApi:
        def __init__(self) -> None:
            self.attempts: list[int] = []
            self.fail = True

        def close_handle(self, handle: int) -> None:
            self.attempts.append(handle)
            if self.fail:
                raise OSError("persistent inline close failure")

    assert workspace_module._pending_deferred_bound_directory_closes() == 0
    api = FakeApi()
    primary = AppError(
        ErrorCode.JOB_CANCELLED,
        "cut-snapshot",
        "error.job.cancelled",
        "inline cancellation must remain primary",
        "dismiss",
    )

    with pytest.raises(AppError) as exc:
        with workspace_module._BoundDirectory(
            tmp_path, None, windows_handles=(93,), windows_api=api
        ):
            raise primary

    assert exc.value is primary
    primary.__traceback__ = None
    gc.collect()
    assert api.attempts == [93]
    assert workspace_module._pending_deferred_bound_directory_closes() == 1

    with pytest.raises(AppError) as drain_error:
        workspace_module._drain_deferred_bound_directory_closes()
    assert drain_error.value.code is ErrorCode.CUT_WORKSPACE_UNSAFE
    assert api.attempts == [93, 93]
    assert workspace_module._pending_deferred_bound_directory_closes() == 1

    api.fail = False
    assert workspace_module._drain_deferred_bound_directory_closes() == 1
    assert api.attempts == [93, 93, 93]
    assert workspace_module._pending_deferred_bound_directory_closes() == 0


def test_deferred_close_registry_is_bounded_and_overflow_stays_with_primary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FakeApi:
        def __init__(self) -> None:
            self.attempts: list[int] = []
            self.fail = True

        def close_handle(self, handle: int) -> None:
            self.attempts.append(handle)
            if self.fail:
                raise OSError(f"persistent close failure for {handle}")

    assert workspace_module._pending_deferred_bound_directory_closes() == 0
    monkeypatch.setattr(workspace_module, "MAX_DEFERRED_BOUND_DIRECTORY_CLOSES", 1)
    first_api = FakeApi()
    second_api = FakeApi()
    primaries = [
        AppError(
            ErrorCode.JOB_CANCELLED,
            "cut-snapshot",
            "error.job.cancelled",
            f"bounded cancellation {index}",
            "dismiss",
        )
        for index in range(2)
    ]

    for handle, api, primary in zip((94, 95), (first_api, second_api), primaries):
        with pytest.raises(AppError) as exc:
            with workspace_module._BoundDirectory(
                tmp_path, None, windows_handles=(handle,), windows_api=api
            ):
                raise primary
        assert exc.value is primary
        primary.__traceback__ = None

    gc.collect()
    assert workspace_module._pending_deferred_bound_directory_closes() == 1
    assert any(
        "deferred-close registry capacity" in note
        for note in getattr(primaries[1], "__notes__", ())
    )

    first_api.fail = False
    second_api.fail = False
    assert workspace_module._drain_deferred_bound_directory_closes() == 1
    assert workspace_module._drain_attached_bound_directory_closes(primaries[1]) == 1
    assert first_api.attempts == [94, 94]
    assert second_api.attempts == [95, 95]
    assert workspace_module._pending_deferred_bound_directory_closes() == 0


def test_windows_open_child_preserves_redirect_error_and_retains_failed_close(
    tmp_path: Path,
) -> None:
    from matteloop.jobs.models import cache_fs

    class FakeApi:
        def __init__(self) -> None:
            self.attempts: list[int] = []
            self.fail = {92}

        def open_child_directory(
            self, _parent: int, name: str, **_kwargs: object
        ) -> int:
            assert name == "redirected"
            return 92

        def file_attributes(self, handle: int) -> int:
            assert handle == 92
            return (
                cache_fs._FILE_ATTRIBUTE_DIRECTORY
                | cache_fs._FILE_ATTRIBUTE_REPARSE_POINT
            )

        def close_handle(self, handle: int) -> None:
            self.attempts.append(handle)
            if handle in self.fail:
                raise OSError("injected child close failure")

    api = FakeApi()
    parent = workspace_module._BoundDirectory(
        tmp_path, None, windows_handles=(90,), windows_api=api
    )

    with pytest.raises(AppError) as exc:
        parent.open_child("redirected")

    assert exc.value.code is ErrorCode.CUT_WORKSPACE_UNSAFE
    assert "redirected" in exc.value.technical_detail
    assert any(
        "child-handle cleanup failure" in note
        and "injected child close failure" in note
        for note in getattr(exc.value, "__notes__", ())
    )
    assert parent._windows_cleanup_handles == (92,)

    api.fail.clear()
    parent.close()
    assert api.attempts == [92, 92, 90]
    assert parent._windows_cleanup_handles == ()
    assert parent._windows_handles == ()


def test_create_staging_maps_native_reparse_mkdir_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _output, _root, cuts, _scratch = workspace_module._workspace_layout(
        tmp_path, create=True
    )
    original_open = workspace_module._BoundDirectory.open

    class FakeApi:
        def lstat_at(self, _handle: int, _name: str) -> os.stat_result:
            raise FileNotFoundError

        def mkdir_at(self, _handle: int, _name: str, *, exist_ok: bool) -> None:
            assert exist_ok is False
            raise UnsafeCacheError("native mkdir target became a reparse point")

        def close_handle(self, _handle: int) -> None:
            pass

    api = FakeApi()

    def injected_open(
        cls: type[workspace_module._BoundDirectory], path: Path
    ) -> workspace_module._BoundDirectory:
        if path == cuts:
            return cls(path, None, windows_handles=(71,), windows_api=api)
        return original_open(path)

    monkeypatch.setattr(
        workspace_module._BoundDirectory, "open", classmethod(injected_open)
    )

    with pytest.raises(AppError) as exc:
        CutWorkspace.create_staging(tmp_path, "a" * 64, "native-failure")

    assert exc.value.code is ErrorCode.CUT_WORKSPACE_UNSAFE


@pytest.mark.parametrize(
    ("native_error", "expected_code"),
    [
        (
            UnsafeCacheError("native enumeration was redirected"),
            ErrorCode.CUT_WORKSPACE_UNSAFE,
        ),
        (OSError("native enumeration failed"), ErrorCode.CUT_WORKSPACE_UNSAFE),
    ],
)
def test_listing_maps_native_iterator_failures_raised_during_iteration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    native_error: BaseException,
    expected_code: ErrorCode,
) -> None:
    _output, _root, cuts, _scratch = workspace_module._workspace_layout(
        tmp_path, create=True
    )
    original_open = workspace_module._BoundDirectory.open

    class FakeApi:
        def iter_entries_at(self, _handle: int, *, max_entries: int) -> object:
            assert max_entries > 0
            if False:  # pragma: no cover - makes this an iterator boundary
                yield "unreachable"
            raise native_error

        def close_handle(self, _handle: int) -> None:
            pass

    api = FakeApi()

    def injected_open(
        cls: type[workspace_module._BoundDirectory], path: Path
    ) -> workspace_module._BoundDirectory:
        if path == cuts:
            return cls(path, None, windows_handles=(72,), windows_api=api)
        return original_open(path)

    monkeypatch.setattr(
        workspace_module._BoundDirectory, "open", classmethod(injected_open)
    )

    with pytest.raises(AppError) as exc:
        list_workspaces(tmp_path)

    assert exc.value.code is expected_code


@pytest.mark.parametrize(
    ("native_error", "expected_code"),
    [
        (
            UnsafeCacheError("scratch enumeration was redirected"),
            ErrorCode.CUT_WORKSPACE_UNSAFE,
        ),
        (OSError("scratch enumeration failed"), ErrorCode.CUT_SNAPSHOT_FAILED),
    ],
)
def test_abandoned_cleanup_maps_native_iterator_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    native_error: BaseException,
    expected_code: ErrorCode,
) -> None:
    _output, _root, _cuts, scratch = workspace_module._workspace_layout(
        tmp_path, create=True
    )
    original_open = workspace_module._BoundDirectory.open

    class FakeApi:
        def iter_entries_at(self, _handle: int, *, max_entries: int) -> object:
            assert max_entries > 0
            if False:  # pragma: no cover - makes this an iterator boundary
                yield "unreachable"
            raise native_error

        def close_handle(self, _handle: int) -> None:
            pass

    api = FakeApi()

    def injected_open(
        cls: type[workspace_module._BoundDirectory], path: Path
    ) -> workspace_module._BoundDirectory:
        if path == scratch:
            return cls(path, None, windows_handles=(73,), windows_api=api)
        return original_open(path)

    monkeypatch.setattr(
        workspace_module._BoundDirectory, "open", classmethod(injected_open)
    )

    with pytest.raises(AppError) as exc:
        cleanup_abandoned_scratch(tmp_path, now_ns=time.time_ns())

    assert exc.value.code is expected_code


def test_immediate_cleanup_maps_native_lstat_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _output, _root, _cuts, scratch = workspace_module._workspace_layout(
        tmp_path, create=True
    )
    original_open = workspace_module._BoundDirectory.open

    class FakeApi:
        def lstat_at(self, _handle: int, _name: str) -> os.stat_result:
            raise UnsafeCacheError("scratch lookup crossed a reparse point")

        def close_handle(self, _handle: int) -> None:
            pass

    api = FakeApi()

    def injected_open(
        cls: type[workspace_module._BoundDirectory], path: Path
    ) -> workspace_module._BoundDirectory:
        if path == scratch:
            return cls(path, None, windows_handles=(74,), windows_api=api)
        return original_open(path)

    monkeypatch.setattr(
        workspace_module._BoundDirectory, "open", classmethod(injected_open)
    )

    with pytest.raises(AppError) as exc:
        cleanup_scratch(tmp_path, "job-native")

    assert exc.value.code is ErrorCode.CUT_WORKSPACE_UNSAFE


def test_promotion_maps_native_rename_safety_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    staged, manifest = _completed_staging(tmp_path)

    def fail_native_rename(
        _bound: workspace_module._BoundDirectory,
        _source: str,
        _destination: str,
    ) -> None:
        raise UnsafeCacheError("native rename target was redirected")

    monkeypatch.setattr(
        workspace_module._BoundDirectory, "replace_directory", fail_native_rename
    )

    with pytest.raises(AppError) as exc:
        promote_cut_set(staged, manifest)

    assert exc.value.code is ErrorCode.CUT_WORKSPACE_UNSAFE


def test_delete_maps_native_remove_safety_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    promoted = _promoted(tmp_path)

    def fail_native_remove(_path: Path) -> None:
        raise UnsafeCacheError("native remove target was redirected")

    monkeypatch.setattr(workspace_module, "_remove_tree", fail_native_remove)

    with pytest.raises(AppError) as exc:
        delete_workspace(promoted)

    assert exc.value.code is ErrorCode.CUT_WORKSPACE_UNSAFE


def test_listing_maps_bound_directory_close_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _output, _root, cuts, _scratch = workspace_module._workspace_layout(
        tmp_path, create=True
    )
    original_close = workspace_module._BoundDirectory.close

    def fail_cuts_close(bound: workspace_module._BoundDirectory) -> None:
        if bound.path == cuts:
            raise BoundDirectoryCloseError(OSError("native close failed"), None)
        original_close(bound)

    monkeypatch.setattr(workspace_module._BoundDirectory, "close", fail_cuts_close)

    with pytest.raises(AppError) as exc:
        list_workspaces(tmp_path)

    assert exc.value.code is ErrorCode.CUT_WORKSPACE_UNSAFE


def test_stage_cut_writes_only_sequential_rgba_pngs(tmp_path: Path) -> None:
    key = CutManifest.cache_key_for(_cache_inputs())
    workspace = CutWorkspace.create_staging(tmp_path, key, "job-1")

    with pytest.raises(AppError):
        stage_cut(workspace, 1, _image(1))
    with pytest.raises(AppError):
        stage_cut(workspace, 0, Image.new("RGB", (8, 6)))
    first = stage_cut(workspace, 0, _image(0))

    assert first.filename == "frame-000000.png"
    assert first.width == 8
    assert first.height == 6
    assert len(first.sha256) == 64


def test_stage_cut_rejects_a_preexisting_gap_in_canonical_names(tmp_path: Path) -> None:
    key = CutManifest.cache_key_for(_cache_inputs())
    workspace = CutWorkspace.create_staging(tmp_path, key, "job-1")
    Image.new("RGBA", (8, 6)).save(workspace.path / "frame-000002.png", format="PNG")

    with pytest.raises(AppError) as exc:
        stage_cut(workspace, 1, _image(1))

    assert exc.value.code is ErrorCode.CUT_STAGE_FAILED
    assert not (workspace.path / "frame-000001.png").exists()


def test_open_rejects_a_symlinked_promotion_journal(tmp_path: Path) -> None:
    cuts = _promoted(tmp_path)
    outside = tmp_path / "outside-journal.json"
    outside.write_text("{}", encoding="utf-8")
    marker = cuts.cuts_root / f".replace-{cuts.cache_key}.json"
    marker.symlink_to(outside)

    with pytest.raises(AppError) as exc:
        CutWorkspace.open(tmp_path, cuts.cache_key)

    assert exc.value.code is ErrorCode.CUT_WORKSPACE_UNSAFE
    assert outside.read_text(encoding="utf-8") == "{}"


def test_encode_failure_after_promotion_keeps_valid_cuts(tmp_path: Path) -> None:
    promoted = _promoted(tmp_path, count=3)

    # Encoding is deliberately outside workspace ownership. A later failure must
    # have no cleanup path capable of deleting the durable promoted directory.
    with pytest.raises(RuntimeError, match="encode failed"):
        raise RuntimeError("encode failed")

    assert validate_cut_set(promoted).frame_count == 3
    assert promoted.path.is_dir()


def test_invalid_replacement_preserves_previous_valid_cache(tmp_path: Path) -> None:
    old = _promoted(tmp_path, job_id="old", count=3)
    before = validate_cut_set(old).to_json_bytes()
    replacement, manifest = _completed_staging(tmp_path, job_id="new", count=3)
    (replacement.path / "frame-000001.png").write_bytes(b"not a png")

    with pytest.raises(AppError) as exc:
        promote_cut_set(replacement, manifest)

    assert exc.value.code is ErrorCode.CUT_SET_INVALID
    assert not replacement.path.exists()
    assert validate_cut_set(old).to_json_bytes() == before


def test_windows_promotion_reuses_exclusive_cuts_binding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    old = _promoted(tmp_path, job_id="old")
    replacement, manifest = _completed_staging(tmp_path, job_id="new", image_offset=10)
    api = _install_exclusive_windows_cuts_api(
        monkeypatch,
        old.cuts_root,
    )

    promoted = promote_cut_set(replacement, manifest)

    assert promoted.read_promoted_cut(0).tobytes() == _image(10).tobytes()
    assert not replacement.path.exists()
    assert api.sharing_violations == []


def test_windows_journal_failure_cleans_stage_through_exclusive_cuts_binding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    old = _promoted(tmp_path, job_id="old")
    before = validate_cut_set(old).to_json_bytes()
    replacement, manifest = _completed_staging(tmp_path, job_id="new", image_offset=10)
    api = _install_exclusive_windows_cuts_api(
        monkeypatch,
        old.cuts_root,
        fail_journal_replace=True,
    )

    with pytest.raises(AppError) as exc:
        promote_cut_set(replacement, manifest)

    assert exc.value.code is ErrorCode.CUT_PROMOTION_FAILED
    assert "journal" in exc.value.technical_detail
    assert not replacement.path.exists()
    assert validate_cut_set(old).to_json_bytes() == before
    assert api.sharing_violations == []


def test_failed_atomic_exchange_rolls_back_to_previous_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    old = _promoted(tmp_path, job_id="old")
    before = validate_cut_set(old).to_json_bytes()
    replacement, manifest = _completed_staging(tmp_path, job_id="new")

    def fail_exchange(_left: Path, _right: Path) -> bool:
        raise OSError("injected exchange failure")

    monkeypatch.setattr(workspace_module, "_atomic_directory_exchange", fail_exchange)
    with pytest.raises(AppError) as exc:
        promote_cut_set(replacement, manifest)

    assert exc.value.code is ErrorCode.CUT_PROMOTION_FAILED
    assert validate_cut_set(old).to_json_bytes() == before


def test_journaled_fallback_restores_old_cache_when_second_rename_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    old = _promoted(tmp_path, job_id="old")
    before = validate_cut_set(old).to_json_bytes()
    replacement, manifest = _completed_staging(tmp_path, job_id="new", image_offset=10)
    original_replace = workspace_module._BoundDirectory.replace_directory

    monkeypatch.setattr(
        workspace_module, "_atomic_directory_exchange", lambda _left, _right: False
    )

    def fail_candidate_activation(bound: object, source: str, destination: str) -> None:
        if source == replacement.path.name and destination == old.path.name:
            raise OSError("injected candidate activation failure")
        original_replace(bound, source, destination)  # type: ignore[arg-type]

    monkeypatch.setattr(
        workspace_module._BoundDirectory,
        "replace_directory",
        fail_candidate_activation,
    )
    with pytest.raises(AppError) as exc:
        promote_cut_set(replacement, manifest)

    assert exc.value.code is ErrorCode.CUT_PROMOTION_FAILED
    assert validate_cut_set(old).to_json_bytes() == before


def test_open_and_listing_wait_for_in_progress_fallback_exchange(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    old = _promoted(tmp_path, job_id="old")
    replacement, manifest = _completed_staging(tmp_path, job_id="new", image_offset=10)
    original_replace = workspace_module._BoundDirectory.replace_directory
    exchange_started = threading.Event()
    release_exchange = threading.Event()

    monkeypatch.setattr(
        workspace_module, "_atomic_directory_exchange", lambda _left, _right: False
    )

    def pause_after_old_move(bound: object, source: str, destination: str) -> None:
        original_replace(bound, source, destination)  # type: ignore[arg-type]
        if source == old.path.name:
            exchange_started.set()
            assert release_exchange.wait(timeout=5)

    monkeypatch.setattr(
        workspace_module._BoundDirectory,
        "replace_directory",
        pause_after_old_move,
    )
    failures: list[BaseException] = []
    observations: list[object] = []

    def promote() -> None:
        try:
            promote_cut_set(replacement, manifest)
        except BaseException as error:
            failures.append(error)

    def observe_open() -> None:
        try:
            observations.append(CutWorkspace.open(tmp_path, old.cache_key))
        except BaseException as error:
            failures.append(error)

    def observe_list() -> None:
        try:
            observations.append(list_workspaces(tmp_path))
        except BaseException as error:
            failures.append(error)

    promoting = threading.Thread(target=promote)
    promoting.start()
    assert exchange_started.wait(timeout=5)
    opening = threading.Thread(target=observe_open)
    listing = threading.Thread(target=observe_list)
    opening.start()
    listing.start()
    time.sleep(0.05)
    assert not observations
    release_exchange.set()
    for thread in (promoting, opening, listing):
        thread.join(timeout=5)

    assert not failures
    assert len(observations) == 2
    assert validate_cut_set(CutWorkspace.open(tmp_path, old.cache_key)).frame_count == 3


def test_recovery_finishes_cleanup_after_promoted_exchange_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    old = _promoted(tmp_path, job_id="old")
    old_bytes = old.read_promoted_cut(0).tobytes()
    replacement, manifest = _completed_staging(tmp_path, job_id="new", image_offset=10)
    replacement_bytes = _image(10).tobytes()
    original_cleanup = workspace_module._remove_bound_tree
    crashed = False

    def crash_once(parent: workspace_module._BoundDirectory, name: str) -> None:
        nonlocal crashed
        if not crashed and name.startswith(".stage-"):
            crashed = True
            raise OSError("injected post-exchange crash")
        original_cleanup(parent, name)

    monkeypatch.setattr(workspace_module, "_remove_bound_tree", crash_once)
    with pytest.raises(AppError) as exc:
        promote_cut_set(replacement, manifest)
    assert exc.value.code is ErrorCode.CUT_PROMOTION_FAILED

    monkeypatch.setattr(workspace_module, "_remove_bound_tree", original_cleanup)
    inventory = list_workspaces(tmp_path)
    assert len(inventory) == 1
    recovered = inventory[0].workspace
    assert recovered.read_promoted_cut(0).tobytes() == replacement_bytes
    assert recovered.read_promoted_cut(0).tobytes() != old_bytes
    assert not any(
        path.name.startswith(".replace-") for path in old.cuts_root.iterdir()
    )


@pytest.mark.parametrize(
    ("break_set", "detail"),
    [
        (lambda path: (path / "frame-000001.png").unlink(), "missing"),
        (
            lambda path: (path / "frame-000001.png").rename(path / "frame-000004.png"),
            "sequential",
        ),
        (
            lambda path: Image.new("RGBA", (7, 6)).save(
                path / "frame-000001.png", format="PNG"
            ),
            "dimensions",
        ),
        (lambda path: (path / "frame-000001.png").write_bytes(b"broken"), "PNG"),
        (
            lambda path: Image.new("RGB", (8, 6)).save(
                path / "frame-000001.png", format="PNG"
            ),
            "RGBA",
        ),
        (
            lambda path: Image.new("RGBA", (8, 6)).save(
                path / "unexpected.png", format="PNG"
            ),
            "unexpected",
        ),
    ],
)
def test_validation_rejects_corrupt_or_noncanonical_sets(
    tmp_path: Path, break_set: object, detail: str
) -> None:
    promoted = _promoted(tmp_path)
    assert callable(break_set)
    break_set(promoted.path)

    with pytest.raises(AppError) as exc:
        validate_cut_set(promoted)

    assert exc.value.code is ErrorCode.CUT_SET_INVALID
    assert detail.lower() in exc.value.technical_detail.lower()


def test_validation_rejects_symlinked_frame_without_reading_target(
    tmp_path: Path,
) -> None:
    promoted = _promoted(tmp_path)
    outside = tmp_path / "outside.png"
    Image.new("RGBA", (8, 6), (9, 9, 9, 9)).save(outside)
    frame = promoted.path / "frame-000001.png"
    frame.unlink()
    frame.symlink_to(outside)

    with pytest.raises(AppError) as exc:
        validate_cut_set(promoted)

    assert exc.value.code is ErrorCode.CUT_WORKSPACE_UNSAFE
    assert outside.is_file()


def test_manifest_parser_rejects_malformed_unknown_and_oversize_data(
    tmp_path: Path,
) -> None:
    staged, manifest = _completed_staging(tmp_path)
    manifest_path = staged.path / MANIFEST_FILENAME
    manifest_path.write_bytes(b'{"unterminated":')
    with pytest.raises(AppError) as malformed:
        validate_cut_set(staged)
    assert malformed.value.code is ErrorCode.CUT_MANIFEST_INVALID

    payload = json.loads(manifest.to_json_bytes())
    payload["unknown"] = True
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(AppError) as unknown:
        validate_cut_set(staged)
    assert unknown.value.code is ErrorCode.CUT_MANIFEST_INVALID

    with manifest_path.open("wb") as output:
        output.truncate(MAX_MANIFEST_BYTES + 1)
    with pytest.raises(AppError) as oversize:
        validate_cut_set(staged)
    assert oversize.value.code is ErrorCode.CUT_MANIFEST_INVALID


def test_external_content_edit_updates_hash_marks_edited_and_invalidates_union(
    tmp_path: Path,
) -> None:
    promoted = _promoted(tmp_path)
    before = validate_cut_set(promoted)
    _rewrite_frame(promoted.path / "frame-000001.png", (250, 1, 2, 3))

    detected = detect_external_edits(promoted, now_ns=20_000)

    assert detected.edited is True
    assert detected.union_metadata is None
    assert detected.last_used_at_ns == 20_000
    assert detected.frames[1].sha256 != before.frames[1].sha256
    assert validate_cut_set(promoted) == detected


def test_external_metadata_only_edit_is_detected(tmp_path: Path) -> None:
    promoted = _promoted(tmp_path)
    before = validate_cut_set(promoted)
    frame = promoted.path / "frame-000000.png"
    os.utime(frame, ns=(before.frames[0].mtime_ns + 10, before.frames[0].mtime_ns + 10))

    detected = detect_external_edits(promoted, now_ns=30_000)

    assert detected.edited is True
    assert detected.frames[0].sha256 == before.frames[0].sha256
    assert detected.frames[0].mtime_ns != before.frames[0].mtime_ns


def test_save_during_snapshot_is_rejected_and_incomplete_snapshot_removed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cuts = _promoted(tmp_path)
    scratch = cuts.scratch_root / "rebuild-1"
    original = workspace_module._copy_frame_descriptor_bound
    changed = False

    def mutate_after_copy(*args: object, **kwargs: object) -> object:
        nonlocal changed
        result = original(*args, **kwargs)
        if not changed:
            changed = True
            _rewrite_frame(cuts.path / "frame-000001.png", (1, 2, 3, 4))
        return result

    monkeypatch.setattr(
        workspace_module, "_copy_frame_descriptor_bound", mutate_after_copy
    )
    with pytest.raises(AppError) as exc:
        snapshot_for_rebuild(cuts, scratch)

    assert exc.value.code is ErrorCode.CUTS_CHANGED_DURING_SNAPSHOT
    assert not scratch.exists()
    assert cuts.path.is_dir()


def test_invalid_cuts_before_snapshot_report_the_specific_validation_failure(
    tmp_path: Path,
) -> None:
    cuts = _promoted(tmp_path)
    (cuts.path / "frame-000001.png").write_bytes(b"not a png")
    scratch = cuts.scratch_root / "invalid-before-start"

    with pytest.raises(AppError) as exc:
        snapshot_for_rebuild(cuts, scratch)

    assert exc.value.code is ErrorCode.CUT_SET_INVALID
    assert "frame-000001.png" in exc.value.technical_detail
    assert not scratch.exists()


def test_metadata_change_during_snapshot_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cuts = _promoted(tmp_path)
    scratch = cuts.scratch_root / "rebuild-2"
    original = workspace_module._copy_frame_descriptor_bound
    changed = False

    def mutate_mtime_after_copy(*args: object, **kwargs: object) -> object:
        nonlocal changed
        result = original(*args, **kwargs)
        if not changed:
            changed = True
            frame = cuts.path / "frame-000002.png"
            info = frame.stat()
            os.utime(frame, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000))
        return result

    monkeypatch.setattr(
        workspace_module, "_copy_frame_descriptor_bound", mutate_mtime_after_copy
    )
    with pytest.raises(AppError) as exc:
        snapshot_for_rebuild(cuts, scratch)

    assert exc.value.code is ErrorCode.CUTS_CHANGED_DURING_SNAPSHOT
    assert not scratch.exists()


def test_completed_snapshot_is_private_and_later_edits_wait_for_next_job(
    tmp_path: Path,
) -> None:
    cuts = _promoted(tmp_path)
    scratch = cuts.scratch_root / "rebuild-3"
    snapshot = snapshot_for_rebuild(cuts, scratch)
    before = snapshot.read_promoted_cut(0).tobytes()

    _rewrite_frame(cuts.path / "frame-000000.png", (255, 254, 253, 252))

    assert snapshot.read_promoted_cut(0).tobytes() == before
    assert cuts.read_promoted_cut(0).tobytes() != before
    assert validate_cut_set(snapshot).frame_count == 3


def test_snapshot_rejects_every_manifest_mutator_without_changing_bytes(
    tmp_path: Path,
) -> None:
    cuts = _promoted(tmp_path)
    snapshot = snapshot_for_rebuild(cuts, cuts.scratch_root / "immutable")
    before = (snapshot.path / MANIFEST_FILENAME).read_bytes()

    with pytest.raises(AppError) as pin_error:
        snapshot.set_pinned(True, now_ns=99_000)
    with pytest.raises(AppError) as edit_error:
        detect_external_edits(snapshot, now_ns=99_000)

    assert pin_error.value.code is ErrorCode.CUT_SET_INVALID
    assert edit_error.value.code is ErrorCode.CUT_SET_INVALID
    assert (snapshot.path / MANIFEST_FILENAME).read_bytes() == before
    assert validate_cut_set(snapshot).to_json_bytes() == before


def test_snapshot_descriptor_copy_fallback_is_verified(tmp_path: Path) -> None:
    cuts = _promoted(tmp_path)
    scratch = cuts.scratch_root / "copy-fallback"

    snapshot = snapshot_for_rebuild(cuts, scratch, prefer_reflink=False)

    assert (
        validate_cut_set(snapshot).to_json_bytes()
        == validate_cut_set(cuts).to_json_bytes()
    )


def test_snapshot_cancellation_removes_scratch_and_preserves_durable_cuts(
    tmp_path: Path,
) -> None:
    cuts = _promoted(tmp_path)
    scratch = cuts.scratch_root / "cancelled"

    with pytest.raises(AppError) as exc:
        snapshot_for_rebuild(cuts, scratch, cancelled=lambda: True)

    assert exc.value.code is ErrorCode.JOB_CANCELLED
    assert not scratch.exists()
    assert validate_cut_set(cuts).frame_count == 3


def test_snapshot_cleanup_safety_failure_does_not_replace_cancellation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cuts = _promoted(tmp_path)
    scratch = cuts.scratch_root / "cancelled-cleanup-failure"
    checks = 0

    def cancel_after_scratch_creation() -> bool:
        nonlocal checks
        checks += 1
        return checks >= 3

    def fail_native_cleanup(_path: Path) -> None:
        raise UnsafeCacheError("scratch cleanup target was redirected")

    monkeypatch.setattr(workspace_module, "_remove_tree", fail_native_cleanup)

    with pytest.raises(AppError) as exc:
        snapshot_for_rebuild(
            cuts,
            scratch,
            cancelled=cancel_after_scratch_creation,
            prefer_reflink=False,
        )

    assert exc.value.code is ErrorCode.JOB_CANCELLED
    assert any("cleanup" in note for note in getattr(exc.value, "__notes__", ()))


def test_snapshot_cleanup_app_error_does_not_replace_cancellation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cuts = _promoted(tmp_path)
    scratch = cuts.scratch_root / "cancelled-structured-cleanup-failure"
    checks = 0

    def cancel_after_scratch_creation() -> bool:
        nonlocal checks
        checks += 1
        return checks >= 3

    cleanup_error = AppError(
        ErrorCode.CUT_WORKSPACE_UNSAFE,
        "cut-workspace",
        "error.cuts.workspace-unsafe",
        "structured scratch cleanup failure",
        "choose-local-output",
    )

    def fail_structured_cleanup(_path: Path) -> None:
        raise cleanup_error

    monkeypatch.setattr(workspace_module, "_remove_tree", fail_structured_cleanup)

    with pytest.raises(AppError) as exc:
        snapshot_for_rebuild(
            cuts,
            scratch,
            cancelled=cancel_after_scratch_creation,
            prefer_reflink=False,
        )

    assert exc.value.code is ErrorCode.JOB_CANCELLED
    assert "snapshot was cancelled" in exc.value.technical_detail
    assert any(
        "additional scratch cleanup failure" in note
        and "structured scratch cleanup failure" in note
        for note in getattr(exc.value, "__notes__", ())
    )


def test_read_promoted_cut_returns_independent_tracked_rgba_images(
    tmp_path: Path,
) -> None:
    cuts = _promoted(tmp_path)
    tracker = RgbaOwnershipTracker((8, 6))

    first = cuts.read_promoted_cut(0, rgba_ownership_tracker=tracker)
    second = cuts.read_promoted_cut(0, rgba_ownership_tracker=tracker)
    assert first.mode == second.mode == "RGBA"
    assert first is not second
    assert first.tobytes() == second.tobytes()
    assert tracker.current == 2
    first.close()
    second.close()
    del first, second
    gc.collect()
    assert tracker.current == 0


def test_validation_and_reads_do_not_leak_file_descriptors(tmp_path: Path) -> None:
    cuts = _promoted(tmp_path)
    process = psutil.Process()
    before = process.num_fds()

    for _ in range(20):
        validate_cut_set(cuts)
        image = cuts.read_promoted_cut(0)
        image.close()

    assert process.num_fds() <= before + 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor ownership probe")
def test_fdopen_failure_closes_transferred_manifest_descriptor_exactly_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cuts = _promoted(tmp_path)
    original_open_read = workspace_module._BoundDirectory.open_read
    original_close = workspace_module.os.close
    transferred: list[int] = []
    closed: list[int] = []

    def observed_open_read(bound: object, name: str) -> int:
        descriptor = original_open_read(bound, name)  # type: ignore[arg-type]
        if name == MANIFEST_FILENAME:
            transferred.append(descriptor)
        return descriptor

    def fail_fdopen(_descriptor: int, _mode: str) -> object:
        raise OSError("injected fdopen failure")

    def observed_close(descriptor: int) -> None:
        if descriptor in transferred:
            closed.append(descriptor)
        original_close(descriptor)

    monkeypatch.setattr(
        workspace_module._BoundDirectory, "open_read", observed_open_read
    )
    monkeypatch.setattr(workspace_module.os, "fdopen", fail_fdopen)
    monkeypatch.setattr(workspace_module.os, "close", observed_close)

    with pytest.raises(AppError) as exc:
        workspace_module._read_manifest(cuts.path)

    assert exc.value.code is ErrorCode.CUT_MANIFEST_INVALID
    assert len(transferred) == 1
    assert closed == transferred
    with pytest.raises(OSError):
        os.fstat(transferred[0])


def test_listing_reports_size_warning_and_stable_metadata(tmp_path: Path) -> None:
    first = _promoted(tmp_path, job_id="first")
    second = _promoted(
        tmp_path,
        job_id="second",
        inputs=_cache_inputs(source="d" * 64),
        pinned=True,
    )

    inventory = list_workspaces(tmp_path, warning_threshold_bytes=1)

    assert len(inventory) == 2
    assert inventory.total_size_bytes == sum(item.size_bytes for item in inventory)
    assert inventory.warning_required is True
    assert {item.workspace.cache_key for item in inventory} == {
        first.cache_key,
        second.cache_key,
    }
    assert [item.last_used_at_ns for item in inventory] == sorted(
        (item.last_used_at_ns for item in inventory), reverse=True
    )


def test_pinned_workspace_requires_explicit_delete_override(tmp_path: Path) -> None:
    cuts = _promoted(tmp_path, pinned=True)

    with pytest.raises(AppError) as exc:
        delete_workspace(cuts)

    assert exc.value.code is ErrorCode.CUT_WORKSPACE_PINNED
    assert cuts.path.is_dir()
    delete_workspace(cuts, allow_pinned=True)
    assert not cuts.path.exists()


def test_corrupt_workspace_delete_requires_explicit_override_only_when_pin_unreadable(
    tmp_path: Path,
) -> None:
    cuts = _promoted(tmp_path)
    (cuts.path / MANIFEST_FILENAME).write_bytes(b"not-json\n")

    with pytest.raises(AppError) as exc:
        delete_workspace(cuts)

    assert exc.value.code is ErrorCode.CUT_WORKSPACE_PINNED
    delete_workspace(cuts, allow_pinned=True)
    assert not cuts.path.exists()


def test_corrupt_frames_can_be_deleted_when_readable_manifest_is_unpinned(
    tmp_path: Path,
) -> None:
    cuts = _promoted(tmp_path)
    (cuts.path / "frame-000000.png").write_bytes(b"broken")

    delete_workspace(cuts)

    assert not cuts.path.exists()


def test_delete_workspace_is_explicit_and_does_not_touch_siblings(
    tmp_path: Path,
) -> None:
    first = _promoted(tmp_path, job_id="first")
    second = _promoted(tmp_path, job_id="second", inputs=_cache_inputs(source="d" * 64))

    delete_workspace(first)

    assert not first.path.exists()
    assert validate_cut_set(second).frame_count == 3


def test_abandoned_scratch_cleanup_is_age_and_count_bounded_and_never_deletes_cuts(
    tmp_path: Path,
) -> None:
    cuts = _promoted(tmp_path)
    old_a = cuts.scratch_root / "old-a"
    old_b = cuts.scratch_root / "old-b"
    recent = cuts.scratch_root / "recent"
    for path in (old_a, old_b, recent):
        path.mkdir()
        (path / "partial.bin").write_bytes(b"partial")
    now_ns = time.time_ns()
    old_ns = now_ns - 25 * 60 * 60 * 1_000_000_000
    os.utime(old_a, ns=(old_ns, old_ns))
    os.utime(old_b, ns=(old_ns, old_ns))

    first = cleanup_abandoned_scratch(tmp_path, now_ns=now_ns, max_entries=1)

    assert first.removed_count == 1
    assert first.has_more is True
    assert recent.is_dir()
    assert validate_cut_set(cuts).frame_count == 3

    second = cleanup_abandoned_scratch(tmp_path, now_ns=now_ns, max_entries=10)
    assert second.removed_count == 1
    assert second.has_more is False
    assert recent.is_dir()
    assert cuts.path.is_dir()


def test_immediate_scratch_cleanup_is_idempotent_and_scoped(tmp_path: Path) -> None:
    cuts = _promoted(tmp_path)
    selected = cuts.scratch_root / "job-success"
    sibling = cuts.scratch_root / "job-other"
    selected.mkdir()
    sibling.mkdir()
    (selected / "partial.bin").write_bytes(b"partial")

    assert cleanup_scratch(tmp_path, "job-success") is True
    assert cleanup_scratch(tmp_path, "job-success") is False
    assert sibling.is_dir()
    assert cuts.path.is_dir()


def test_listing_and_removal_stop_at_namespace_bounds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cuts = _promoted(tmp_path)
    for index in range(4):
        (cuts.cuts_root / f"junk-{index}").write_bytes(b"x")
    monkeypatch.setattr(workspace_module, "MAX_WORKSPACE_ENTRIES", 1)
    with pytest.raises(AppError) as listing_error:
        list_workspaces(tmp_path)
    assert listing_error.value.code is ErrorCode.CUT_WORKSPACE_UNSAFE

    scratch = cuts.scratch_root / "bounded-remove"
    scratch.mkdir()
    for index in range(18):
        (scratch / f"entry-{index}").write_bytes(b"x")
    monkeypatch.setattr(workspace_module, "MAX_FRAME_COUNT", 1)
    with pytest.raises(AppError) as cleanup_error:
        cleanup_scratch(tmp_path, scratch.name)
    assert cleanup_error.value.code is ErrorCode.CUT_WORKSPACE_DELETE_FAILED
    assert cuts.path.is_dir()


def test_detect_external_edits_never_regresses_last_use(tmp_path: Path) -> None:
    cuts = _promoted(tmp_path)
    future = cuts.set_pinned(False, now_ns=50_000)

    detected = detect_external_edits(cuts, now_ns=20_000)

    assert detected.last_used_at_ns == future.last_used_at_ns == 50_000


def test_manifest_mutations_share_one_workspace_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cuts = _promoted(tmp_path)
    _rewrite_frame(cuts.path / "frame-000000.png", (3, 4, 5, 6))
    entered_scan = threading.Event()
    release_scan = threading.Event()
    original_scan = workspace_module._scan_cut_set
    first_scan = True

    def paused_scan(*args: object, **kwargs: object) -> object:
        nonlocal first_scan
        result = original_scan(*args, **kwargs)
        if first_scan:
            first_scan = False
            entered_scan.set()
            assert release_scan.wait(timeout=5)
        return result

    monkeypatch.setattr(workspace_module, "_scan_cut_set", paused_scan)
    failures: list[BaseException] = []

    def detect() -> None:
        try:
            detect_external_edits(cuts, now_ns=30_000)
        except BaseException as error:
            failures.append(error)

    def pin() -> None:
        try:
            cuts.set_pinned(True, now_ns=40_000)
        except BaseException as error:
            failures.append(error)

    detecting = threading.Thread(target=detect)
    detecting.start()
    assert entered_scan.wait(timeout=5)
    pinning = threading.Thread(target=pin)
    pinning.start()
    release_scan.set()
    detecting.join(timeout=5)
    pinning.join(timeout=5)

    assert not failures
    manifest = validate_cut_set(cuts)
    assert manifest.pinned is True
    assert manifest.edited is True
    assert manifest.union_metadata is None
    assert manifest.last_used_at_ns == 40_000


def test_initial_journal_failure_is_structured_and_cleans_stage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    old = _promoted(tmp_path)
    stage, manifest = _completed_staging(tmp_path, job_id="replacement", image_offset=8)

    def fail_journal(*_args: object, **_kwargs: object) -> None:
        raise OSError("injected journal failure")

    monkeypatch.setattr(workspace_module, "_write_journal", fail_journal)
    with pytest.raises(AppError) as exc:
        promote_cut_set(stage, manifest)

    assert exc.value.code is ErrorCode.CUT_PROMOTION_FAILED
    assert not stage.path.exists()
    assert validate_cut_set(old).frame_count == 3


@pytest.mark.parametrize(
    "native_error",
    [
        UnsafeCacheError("native journal target was redirected"),
        BoundDirectoryCloseError(OSError("native journal close failed"), None),
    ],
    ids=["unsafe-cache", "bound-directory-close"],
)
def test_native_initial_journal_failure_cleans_stage_and_keeps_primary_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    native_error: BaseException,
) -> None:
    old = _promoted(tmp_path)
    before = validate_cut_set(old).to_json_bytes()
    stage, manifest = _completed_staging(tmp_path, job_id="replacement", image_offset=8)
    original_remove = workspace_module._remove_bound_tree

    def fail_journal(*_args: object, **_kwargs: object) -> None:
        raise native_error

    def remove_then_report_failure(
        parent: workspace_module._BoundDirectory, name: str
    ) -> None:
        original_remove(parent, name)
        raise AppError(
            ErrorCode.CUT_WORKSPACE_UNSAFE,
            "cut-workspace",
            "error.cuts.workspace-unsafe",
            "structured post-removal cleanup failure",
            "choose-local-output",
        )

    monkeypatch.setattr(workspace_module, "_write_journal", fail_journal)
    monkeypatch.setattr(
        workspace_module, "_remove_bound_tree", remove_then_report_failure
    )

    with pytest.raises(AppError) as exc:
        promote_cut_set(stage, manifest)

    assert exc.value.code is ErrorCode.CUT_WORKSPACE_UNSAFE
    assert str(native_error) in exc.value.technical_detail
    assert any(
        "additional staged-cut cleanup failure" in note
        and "structured post-removal cleanup failure" in note
        for note in getattr(exc.value, "__notes__", ())
    )
    assert not stage.path.exists()
    assert validate_cut_set(old).to_json_bytes() == before
