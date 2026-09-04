from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from matteloop.core.errors import AppError, ErrorCode
from matteloop.core.execution_providers import (
    CPU_EXECUTION_PROVIDER,
    CUDA_EXECUTION_PROVIDER,
    DML_EXECUTION_PROVIDER,
)
from matteloop.jobs.models.cache_fs import BoundModelDirectory
from matteloop.jobs.models.catalog import ExecutionClass, ModelCatalog, ModelSpec
from matteloop.jobs.models.session import ModelSessionManager, PreparationResult
from matteloop.jobs.protocol import SegmentOptions
from matteloop.jobs.segmentation_host import (
    _create_rembg_session,
    _instantiate_verified_rembg_session,
    _PreparedRembgSession,
    _run_rembg,
    _session_options,
    _validate_verified_launch_payload,
)


class FakeDownloader:
    def __init__(self, cache_root: Path, events: list[str]) -> None:
        self.cache_root = cache_root
        self.events = events
        self.fail: BaseException | None = None
        self.calls: list[str] = []
        self.cancelled_checks: list[Callable[[], bool]] = []

    def download(
        self,
        spec: ModelSpec,
        destination: Path,
        progress: Callable[[int, int], None],
        cancelled: Callable[[], bool],
    ) -> Path:
        self.events.append(f"download:{spec.id}")
        self.calls.append(spec.id)
        self.cancelled_checks.append(cancelled)
        if self.fail is not None:
            raise self.fail
        assert destination == self.cache_root
        assert cancelled() is False
        artifact = spec.artifact
        assert artifact is not None
        target = destination / "2.0.75" / spec.id / artifact.runtime_filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"test-double-verified")
        return target


class FakeClient:
    def __init__(self, payload: dict[str, object], events: list[str]) -> None:
        self.payload = payload
        self.events = events
        self.starts = 0
        self.replacements: list[dict[str, object]] = []
        self.closes = 0
        self.fail_start: BaseException | None = None
        self.fail_replace: BaseException | None = None
        self.close_failures = 0

    def start(self) -> None:
        self.events.append(f"start:{self.payload['model_id']}")
        self.starts += 1
        if self.fail_start is not None:
            raise self.fail_start

    def replace_model(self, payload: object) -> None:
        assert type(payload) is dict
        self.events.append(f"replace:{payload['model_id']}")
        self.replacements.append(payload)
        if self.fail_replace is not None:
            raise self.fail_replace
        self.payload = payload

    def close(self) -> None:
        self.events.append("close")
        self.closes += 1
        if self.close_failures:
            self.close_failures -= 1
            raise AppError(
                ErrorCode.SEGMENTATION_CLEANUP_FAILED,
                "segmentation-process",
                "error.segmentation.cleanup-failed",
                "synthetic cleanup failure",
                "retry-segmentation-cleanup",
            )


def _manager(
    tmp_path: Path,
) -> tuple[ModelSessionManager, FakeDownloader, list[FakeClient], list[str]]:
    catalog = ModelCatalog.load_resource()
    events: list[str] = []
    downloader = FakeDownloader(tmp_path, events)
    clients: list[FakeClient] = []

    def client_factory(payload: dict[str, object]) -> FakeClient:
        client = FakeClient(payload, events)
        clients.append(client)
        return client

    manager = ModelSessionManager(
        catalog=catalog,
        downloader=downloader,
        client_factory=client_factory,
        cache_root=tmp_path,
        progress=lambda _done, _total: None,
        cancelled=lambda: False,
    )
    return manager, downloader, clients, events


def _verified_launch(
    tmp_path: Path, data: bytes = b"verified-model"
) -> tuple[ModelCatalog, dict[str, object], Path]:
    manifest = json.loads(ModelCatalog.resource_path().read_text(encoding="utf-8"))
    models = manifest["models"]
    model = next(item for item in models if item["id"] == "u2net")
    model["artifact"]["size_bytes"] = len(data)
    model["artifact"]["sha256"] = hashlib.sha256(data).hexdigest()
    catalog = ModelCatalog.from_bytes(json.dumps(manifest).encode())
    spec = catalog.get("u2net")
    assert spec.artifact is not None
    home = tmp_path / "2.0.75" / "u2net"
    home.mkdir(parents=True)
    artifact_path = home / spec.artifact.runtime_filename
    artifact_path.write_bytes(data)
    payload: dict[str, object] = {
        "schema_version": 1,
        "model_id": spec.id,
        "upstream_id": spec.upstream_id,
        "rembg_version": catalog.rembg_version,
        "model_home": str(home),
        "runtime_filename": spec.artifact.runtime_filename,
        "sha256": spec.artifact.sha256,
        "size_bytes": spec.artifact.size_bytes,
        "inference_defaults": spec.inference_defaults.to_primitives(),
        "execution_provider": "CPUExecutionProvider",
    }
    return catalog, payload, artifact_path


def test_session_options_disable_mem_pattern_for_directml_only() -> None:
    class FakeOptions:
        enable_mem_pattern = True
        enable_profiling = True

    class FakeOrt:
        @staticmethod
        def SessionOptions() -> FakeOptions:
            return FakeOptions()

    directml_options = _session_options(FakeOrt, DML_EXECUTION_PROVIDER)
    cpu_options = _session_options(FakeOrt, CPU_EXECUTION_PROVIDER)

    assert directml_options.enable_mem_pattern is False  # type: ignore[attr-defined]
    assert cpu_options.enable_mem_pattern is True  # type: ignore[attr-defined]


def test_local_prepare_downloads_before_start_with_exact_safe_launch_payload(
    tmp_path: Path,
) -> None:
    manager, downloader, clients, events = _manager(tmp_path)

    result = manager.prepare("u2net", {})

    assert isinstance(result, PreparationResult)
    assert result.execution_class is ExecutionClass.LOCAL
    assert result.local_session_ready is True
    assert manager.active_id == "u2net"
    assert downloader.calls == ["u2net"]
    assert events[:2] == ["download:u2net", "start:u2net"]
    assert len(clients) == 1
    payload = clients[0].payload
    assert set(payload) == {
        "schema_version",
        "model_id",
        "upstream_id",
        "rembg_version",
        "model_home",
        "runtime_filename",
        "sha256",
        "size_bytes",
        "inference_defaults",
        "execution_provider",
    }
    assert payload["model_id"] == "u2net"
    assert payload["runtime_filename"] == "u2net.onnx"
    assert payload["execution_provider"] == "CPUExecutionProvider"
    assert payload["model_home"] == str(tmp_path / "2.0.75" / "u2net")
    assert "model_path" not in payload
    assert "extras" not in payload


def test_cloth_launch_uses_internal_full_default_and_preserves_source_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _downloader, clients, _events = _manager(tmp_path)

    manager.prepare("u2net_cloth_seg", {})

    assert clients[0].payload["inference_defaults"] == {"cloth_category": "full"}
    calls: list[dict[str, object]] = []

    class FakeClothSession:
        def predict(self, image: Image.Image, **kwargs: object) -> list[Image.Image]:
            calls.append(kwargs)
            mask = Image.new("L", image.size, 255)
            return [mask] if kwargs.get("cloth_category") == "full" else [mask] * 3

    source = np.zeros((4, 7, 3), dtype=np.uint8)
    prepared = _PreparedRembgSession(FakeClothSession(), (("cloth_category", "full"),))

    # The pinned ORT import creates a telemetry session marker in its current
    # directory on macOS; keep that third-party test artifact in pytest's temp.
    monkeypatch.chdir(tmp_path)
    result = _run_rembg(source, prepared, SegmentOptions("standard"))

    assert result.shape == (4, 7, 4)
    assert calls == [{"cloth_category": "full"}]


def test_same_active_model_is_idempotent_without_download_or_restart(
    tmp_path: Path,
) -> None:
    manager, downloader, clients, _events = _manager(tmp_path)
    first = manager.prepare("u2net", {})

    second = manager.prepare("u2net", {})

    assert second == first
    assert downloader.calls == ["u2net"]
    assert clients[0].starts == 1
    assert clients[0].replacements == []


def test_provider_change_replaces_the_active_model_session_once(
    tmp_path: Path,
) -> None:
    manager, downloader, clients, events = _manager(tmp_path)

    first = manager.prepare("u2net", {"execution_provider": CPU_EXECUTION_PROVIDER})
    reused = manager.prepare("u2net", {"execution_provider": CPU_EXECUTION_PROVIDER})
    switched = manager.prepare("u2net", {"execution_provider": CUDA_EXECUTION_PROVIDER})
    switched_again = manager.prepare(
        "u2net", {"execution_provider": CUDA_EXECUTION_PROVIDER}
    )

    assert reused == first
    assert switched.execution_provider == CUDA_EXECUTION_PROVIDER
    assert switched_again == switched
    assert downloader.calls == ["u2net", "u2net"]
    assert events == [
        "download:u2net",
        "start:u2net",
        "download:u2net",
        "replace:u2net",
    ]
    assert len(clients) == 1
    assert len(clients[0].replacements) == 1
    assert clients[0].replacements[0]["execution_provider"] == CUDA_EXECUTION_PROVIDER


def test_model_change_downloads_and_verifies_before_process_replacement(
    tmp_path: Path,
) -> None:
    manager, _downloader, clients, events = _manager(tmp_path)
    manager.prepare("u2net", {})

    manager.prepare("u2netp", {})

    assert events == [
        "download:u2net",
        "start:u2net",
        "download:u2netp",
        "replace:u2netp",
    ]
    assert manager.active_id == "u2netp"
    assert len(clients) == 1


def test_download_failure_preserves_previous_truthful_active_session(
    tmp_path: Path,
) -> None:
    manager, downloader, clients, _events = _manager(tmp_path)
    manager.prepare("u2net", {})
    downloader.fail = AppError(
        ErrorCode.MODEL_DOWNLOAD_NETWORK,
        "model-download",
        "error.model.download-network",
        "offline",
        "retry-model-download",
    )

    with pytest.raises(AppError):
        manager.prepare("u2netp", {})

    assert manager.active_id == "u2net"
    assert clients[0].replacements == []


def test_replacement_failure_clears_active_state_truthfully(tmp_path: Path) -> None:
    manager, _downloader, clients, _events = _manager(tmp_path)
    manager.prepare("u2net", {})
    clients[0].fail_replace = RuntimeError("spawn failed")

    with pytest.raises(RuntimeError, match="spawn failed"):
        manager.prepare("u2netp", {})

    assert manager.active_id is None
    assert manager.active_spec is None


def test_sam_is_not_found_before_any_side_effect(tmp_path: Path) -> None:
    manager, downloader, clients, _events = _manager(tmp_path)

    with pytest.raises(AppError) as exc:
        manager.prepare("sam", {})

    assert exc.value.code is ErrorCode.MODEL_NOT_FOUND
    assert downloader.calls == []
    assert clients == []
    assert manager.active_id is None


def test_unknown_sam_id_preserves_an_active_local_session(
    tmp_path: Path,
) -> None:
    manager, downloader, clients, _events = _manager(tmp_path)
    manager.prepare("u2net", {})

    with pytest.raises(AppError) as exc:
        manager.prepare("sam", {})

    assert exc.value.code is ErrorCode.MODEL_NOT_FOUND
    assert downloader.calls == ["u2net"]
    assert clients[0].closes == 0
    assert manager.active_id == "u2net"


def test_unknown_retired_model_id_starts_no_download_or_client(
    tmp_path: Path,
) -> None:
    manager, downloader, clients, _events = _manager(tmp_path)
    manager.prepare("u2net", {})
    legacy_extras = {"legacy_option": True}

    with pytest.raises(AppError) as exc:
        manager.prepare("legacy-retired-model", legacy_extras)

    assert exc.value.code is ErrorCode.MODEL_NOT_FOUND
    assert downloader.calls == ["u2net"]
    assert len(clients) == 1
    assert clients[0].closes == 0
    assert manager.active_id == "u2net"


def test_unknown_retired_model_id_precedes_cleanup_pending_and_preserves_state(
    tmp_path: Path,
) -> None:
    manager, downloader, clients, events = _manager(tmp_path)
    manager.prepare("u2net", {})
    clients[0].fail_replace = RuntimeError("replacement failed")
    clients[0].close_failures = 1
    with pytest.raises(AppError) as cleanup:
        manager.prepare("u2netp", {})
    assert cleanup.value.code is ErrorCode.SEGMENTATION_CLEANUP_FAILED
    calls_before = list(downloader.calls)
    events_before = list(events)

    with pytest.raises(AppError) as exc:
        manager.prepare(
            "legacy-retired-model",
            {"legacy_option": True},
        )

    assert exc.value.code is ErrorCode.MODEL_NOT_FOUND
    assert downloader.calls == calls_before
    assert events == events_before
    assert len(clients) == 1
    assert manager.active_id is None
    assert manager.cleanup_pending_id == "u2net"


@pytest.mark.parametrize(
    "extras",
    [
        {"model_path": "/tmp/custom.onnx"},
        {"custom": True},
        {"prompt": []},
    ],
)
def test_task9_rejects_custom_options_and_unimplemented_prompts(
    tmp_path: Path, extras: dict[str, object]
) -> None:
    manager, _downloader, _clients, _events = _manager(tmp_path)

    with pytest.raises(AppError) as exc:
        manager.prepare("u2net", extras)
    assert exc.value.code is ErrorCode.MODEL_PREPARATION_INVALID


def test_remove_active_and_inactive_model_weights(tmp_path: Path) -> None:
    manager, _downloader, _clients, _events = _manager(tmp_path)
    inactive = manager.prepare("u2net", {})
    active = manager.prepare("u2netp", {})
    assert inactive.artifact_path is not None
    assert active.artifact_path is not None

    assert manager.remove("u2net") is True
    assert not inactive.artifact_path.exists()
    assert manager.remove("u2netp") is True
    assert not active.artifact_path.exists()
    assert manager.remove("u2net") is False


def test_remove_obsolete_versions_removes_only_catalog_listed_directories(
    tmp_path: Path,
) -> None:
    manager, _downloader, _clients, _events = _manager(tmp_path)
    catalog = ModelCatalog.load_resource()
    obsolete = tmp_path / catalog.obsolete_rembg_versions[0]
    current = tmp_path / catalog.rembg_version
    unrelated = tmp_path / "not-a-rembg-version"
    obsolete.mkdir()
    current.mkdir()
    unrelated.mkdir()

    assert manager.remove_obsolete_versions() == 1
    assert not obsolete.exists()
    assert current.is_dir()
    assert unrelated.is_dir()


def test_fetch_passes_the_callers_cancellation_check_to_the_downloader(
    tmp_path: Path,
) -> None:
    manager, downloader, _clients, _events = _manager(tmp_path)

    def own() -> bool:
        return False

    manager.fetch("u2netp", cancelled=own)
    manager.fetch("u2netp")

    assert downloader.cancelled_checks[0] is own
    assert downloader.cancelled_checks[1] is manager._cancelled


def test_remove_obsolete_versions_removes_only_the_named_model_directory(
    tmp_path: Path,
) -> None:
    manager, _downloader, _clients, _events = _manager(tmp_path)
    catalog = ModelCatalog.load_resource()
    obsolete = tmp_path / catalog.obsolete_rembg_versions[0]
    named = obsolete / "u2netp"
    other = obsolete / "u2net"
    named.mkdir(parents=True)
    (named / "u2netp.onnx").write_bytes(b"old")
    other.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    symlinked = obsolete / "u2net_human_seg"
    symlinked.symlink_to(outside, target_is_directory=True)

    assert manager.remove_obsolete_versions("u2netp") == 1
    assert not named.exists()
    assert other.is_dir()
    assert obsolete.is_dir()

    assert manager.remove_obsolete_versions("u2net_human_seg") == 0
    assert symlinked.is_symlink()
    assert outside.is_dir()

    with pytest.raises(AppError) as unknown:
        manager.remove_obsolete_versions("not-a-model")
    assert unknown.value.code is ErrorCode.MODEL_NOT_FOUND
    assert other.is_dir()


def test_remove_rejects_symlink_traversal_and_unknown_ids(tmp_path: Path) -> None:
    manager, _downloader, _clients, _events = _manager(tmp_path)
    outside = tmp_path / "outside.onnx"
    outside.write_bytes(b"outside")
    model_dir = tmp_path / "2.0.75" / "u2net"
    model_dir.mkdir(parents=True)
    (model_dir / "u2net.onnx").symlink_to(outside)

    with pytest.raises(AppError) as exc:
        manager.remove("u2net")
    assert exc.value.code is ErrorCode.MODEL_CACHE_UNSAFE
    assert outside.read_bytes() == b"outside"

    with pytest.raises(AppError) as unknown:
        manager.remove("u2net_custom")
    assert unknown.value.code is ErrorCode.MODEL_NOT_FOUND


def test_remove_parent_swap_never_unlinks_outside_bound_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import matteloop.jobs.models.session as session_module

    manager, _downloader, _clients, _events = _manager(tmp_path)
    target = tmp_path / "2.0.75" / "u2net" / "u2net.onnx"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"cache")
    outside = tmp_path / "outside-remove"
    outside.mkdir()
    outside_target = outside / "u2net.onnx"
    outside_target.write_bytes(b"outside")
    swapped = False

    def swap(bound: BoundModelDirectory) -> None:
        nonlocal swapped
        held = bound.path.with_name("u2net-held")
        try:
            bound.path.rename(held)
            bound.path.symlink_to(outside, target_is_directory=True)
        except OSError:
            if held.exists() and not bound.path.exists():
                held.rename(bound.path)
            return
        swapped = True

    monkeypatch.setattr(session_module, "_after_remove_directory_bound", swap)

    if swapped:
        pytest.fail("hook did not run")
    try:
        removed = manager.remove("u2net")
    except AppError as error:
        assert swapped is True
        assert error.code is ErrorCode.MODEL_CACHE_UNSAFE
    else:
        assert swapped is False
        assert removed is True
    assert outside_target.read_bytes() == b"outside"


def test_remove_maps_bound_directory_close_oserror_after_visible_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _downloader, _clients, _events = _manager(tmp_path)
    target = tmp_path / "2.0.75" / "u2net" / "u2net.onnx"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"cache")
    real_close = BoundModelDirectory.close

    def close_then_fail(bound: BoundModelDirectory) -> None:
        real_close(bound)
        raise OSError("synthetic CloseHandle failure")

    monkeypatch.setattr(BoundModelDirectory, "close", close_then_fail)

    with pytest.raises(AppError) as exc:
        manager.remove("u2net")

    assert exc.value.code is ErrorCode.MODEL_DOWNLOAD_DISK
    assert "CloseHandle" in exc.value.technical_detail
    assert not target.exists()


def test_close_is_idempotent_and_calls_owned_client_exactly_once(
    tmp_path: Path,
) -> None:
    manager, _downloader, clients, _events = _manager(tmp_path)
    manager.prepare("u2net", {})

    manager.close()
    manager.close()

    assert clients[0].closes == 1
    assert manager.active_id is None
    with pytest.raises(AppError) as exc:
        manager.prepare("u2net", {})
    assert exc.value.code is ErrorCode.MODEL_MANAGER_CLOSED


def test_failed_close_retains_cleanup_handle_and_blocks_remove_until_retry(
    tmp_path: Path,
) -> None:
    manager, _downloader, clients, _events = _manager(tmp_path)
    manager.prepare("u2net", {})
    clients[0].close_failures = 1

    with pytest.raises(AppError) as cleanup:
        manager.close()
    assert cleanup.value.code is ErrorCode.SEGMENTATION_CLEANUP_FAILED
    assert manager.active_id is None
    assert manager.cleanup_pending_id == "u2net"
    with pytest.raises(AppError) as in_use:
        manager.remove("u2net")
    assert in_use.value.code is ErrorCode.MODEL_IN_USE
    with pytest.raises(AppError) as pending:
        manager.prepare("birefnet-portrait", {})
    assert pending.value.code is ErrorCode.MODEL_MANAGER_CLOSED

    manager.close()
    manager.close()

    assert clients[0].closes == 2
    assert manager.cleanup_pending_id is None


def test_replacement_cleanup_failure_is_truthful_and_retryable(tmp_path: Path) -> None:
    manager, _downloader, clients, _events = _manager(tmp_path)
    manager.prepare("u2net", {})
    clients[0].fail_replace = RuntimeError("replacement failed")
    clients[0].close_failures = 1

    with pytest.raises(AppError) as cleanup:
        manager.prepare("u2netp", {})
    assert cleanup.value.code is ErrorCode.SEGMENTATION_CLEANUP_FAILED
    assert manager.active_id is None
    assert manager.cleanup_pending_id == "u2net"
    with pytest.raises(AppError) as in_use:
        manager.remove("u2net")
    assert in_use.value.code is ErrorCode.MODEL_IN_USE
    with pytest.raises(AppError) as attempted_in_use:
        manager.remove("u2netp")
    assert attempted_in_use.value.code is ErrorCode.MODEL_IN_USE
    with pytest.raises(AppError) as pending:
        manager.prepare("birefnet-portrait", {})
    assert pending.value.code is ErrorCode.SEGMENTATION_CLEANUP_FAILED

    manager.close()
    assert clients[0].closes == 2
    assert manager.cleanup_pending_id is None


def test_child_launch_reproves_exact_manifest_bound_regular_file(
    tmp_path: Path,
) -> None:
    catalog, payload, artifact_path = _verified_launch(tmp_path)

    verified = _validate_verified_launch_payload(payload, catalog=catalog)

    assert verified.model_id == "u2net"
    assert verified.artifact_path == artifact_path
    assert verified.rembg_version == "2.0.75"
    assert verified.model_bytes == b"verified-model"
    assert verified.inference_kwargs == ()


def test_child_launch_binds_cache_without_resolving_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog, payload, _artifact_path = _verified_launch(tmp_path)

    def forbid_resolve(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("verified child launch must not resolve cache paths")

    monkeypatch.setattr(Path, "resolve", forbid_resolve)

    verified = _validate_verified_launch_payload(payload, catalog=catalog)

    assert verified.model_bytes == b"verified-model"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model_id", "u2net_custom"),
        ("upstream_id", "u2net_custom"),
        ("runtime_filename", "custom.onnx"),
        ("sha256", "0" * 64),
        ("size_bytes", 1),
        ("rembg_version", "2.0.71"),
    ],
)
def test_child_launch_rejects_custom_or_unbound_payload_fields(
    tmp_path: Path, field: str, value: object
) -> None:
    catalog, payload, _artifact_path = _verified_launch(tmp_path)
    payload[field] = value

    with pytest.raises(AppError) as exc:
        _validate_verified_launch_payload(payload, catalog=catalog)
    assert exc.value.code is ErrorCode.MODEL_PREPARATION_INVALID


def test_child_launch_rejects_tampering_and_symlinked_artifact(tmp_path: Path) -> None:
    catalog, payload, artifact_path = _verified_launch(tmp_path)
    artifact_path.write_bytes(b"tampered")

    with pytest.raises(AppError):
        _validate_verified_launch_payload(payload, catalog=catalog)

    artifact_path.unlink()
    outside = tmp_path / "outside.onnx"
    outside.write_bytes(b"verified-model")
    artifact_path.symlink_to(outside)
    with pytest.raises(AppError) as exc:
        _validate_verified_launch_payload(payload, catalog=catalog)
    assert exc.value.code is ErrorCode.MODEL_CACHE_UNSAFE


def test_child_creates_session_only_after_hash_proof_without_parent_env_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import matteloop.jobs.segmentation_host as host_module

    catalog, payload, artifact_path = _verified_launch(tmp_path)
    calls: list[tuple[str, bytes, str, tuple[tuple[str, str], ...]]] = []
    sentinel = object()

    monkeypatch.setattr(
        ModelCatalog,
        "load_resource",
        classmethod(lambda _cls: catalog),
    )
    monkeypatch.setattr(
        host_module,
        "_instantiate_verified_rembg_session",
        lambda model_id, model_bytes, rembg_version, inference_kwargs=(), **_kwargs: (
            calls.append((model_id, model_bytes, rembg_version, inference_kwargs)),
            sentinel,
        )[1],
    )
    before = dict(os.environ)

    assert _create_rembg_session(payload) is sentinel
    assert calls == [("u2net", b"verified-model", "2.0.75", ())]
    assert dict(os.environ) == before

    artifact_path.write_bytes(b"tampered")
    with pytest.raises(AppError):
        _create_rembg_session(payload)
    assert len(calls) == 1


def test_verified_instantiation_uses_bytes_without_onnxruntime_profiling() -> None:
    original_download_called = False
    captured: list[bytes] = []
    model_bytes = b"already-verified"

    class FakeSession:
        @classmethod
        def name(cls) -> str:
            return "u2net"

        @classmethod
        def download_models(cls, *_args: object, **_kwargs: object) -> Path:
            nonlocal original_download_called
            original_download_called = True
            raise AssertionError("upstream downloader must not be called")

    class FakeOptions:
        inter_op_num_threads = 0
        intra_op_num_threads = 0
        enable_profiling = True

    class FakeOrt:
        @staticmethod
        def SessionOptions() -> FakeOptions:
            return FakeOptions()

        @staticmethod
        def get_device() -> str:
            return "CPU"

        @staticmethod
        def get_available_providers() -> list[str]:
            return ["CPUExecutionProvider"]

        @staticmethod
        def InferenceSession(
            content: bytes, *, sess_options: object, providers: list[str]
        ) -> object:
            assert isinstance(sess_options, FakeOptions)
            assert sess_options.enable_profiling is False
            assert providers == ["CPUExecutionProvider"]
            captured.append(content)
            return object()

    before = dict(os.environ)

    session = _instantiate_verified_rembg_session(
        "u2net",
        model_bytes,
        "2.0.75",
        session_classes=[FakeSession],
        ort_module=FakeOrt,
        installed_version="2.0.75",
    )

    assert isinstance(session, _PreparedRembgSession)
    assert isinstance(session.session, FakeSession)
    assert session.session.inner_session is not None
    assert captured == [model_bytes]
    assert captured[0] is model_bytes
    assert original_download_called is False
    assert dict(os.environ) == before


def test_failed_hardware_provider_falls_back_to_cpu_with_a_startup_notice() -> None:
    calls: list[list[str]] = []

    class FakeSession:
        @classmethod
        def name(cls) -> str:
            return "u2net"

    class FakeOptions:
        enable_profiling = True

    class FakeOrt:
        @staticmethod
        def SessionOptions() -> FakeOptions:
            return FakeOptions()

        @staticmethod
        def get_available_providers() -> list[str]:
            return [CUDA_EXECUTION_PROVIDER, CPU_EXECUTION_PROVIDER]

        @staticmethod
        def InferenceSession(
            _content: bytes, *, sess_options: object, providers: list[str]
        ) -> object:
            del sess_options
            calls.append(providers)
            if providers[0] == CUDA_EXECUTION_PROVIDER:
                raise RuntimeError("CUDA cannot initialise this model")
            return object()

    prepared = _instantiate_verified_rembg_session(
        "u2net",
        b"already-verified",
        "2.0.75",
        execution_provider=CUDA_EXECUTION_PROVIDER,
        session_classes=[FakeSession],
        ort_module=FakeOrt,
        installed_version="2.0.75",
    )

    assert isinstance(prepared, _PreparedRembgSession)
    assert prepared.execution_provider == CPU_EXECUTION_PROVIDER
    assert prepared.startup_notice is not None
    assert "CPU fortgesetzt" in prepared.startup_notice
    assert calls == [
        [CUDA_EXECUTION_PROVIDER, CPU_EXECUTION_PROVIDER],
        [CPU_EXECUTION_PROVIDER],
    ]


def test_session_consumes_verified_bytes_even_if_path_swaps_back_during_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import matteloop.jobs.segmentation_host as host_module

    catalog, payload, artifact_path = _verified_launch(tmp_path)
    good = artifact_path.read_bytes()
    evil = b"x" * len(good)
    captured: list[bytes] = []

    monkeypatch.setattr(
        ModelCatalog, "load_resource", classmethod(lambda _cls: catalog)
    )

    def instantiate(
        _model_id: str,
        model_bytes: bytes,
        _version: str,
        _inference_kwargs: tuple[tuple[str, str], ...] = (),
        **_kwargs: object,
    ) -> object:
        captured.append(model_bytes)
        artifact_path.write_bytes(evil)
        artifact_path.write_bytes(good)
        return object()

    monkeypatch.setattr(host_module, "_instantiate_verified_rembg_session", instantiate)

    _create_rembg_session(payload)

    assert captured == [good]
