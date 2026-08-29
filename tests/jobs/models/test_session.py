from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from rembggui.core.errors import AppError, ErrorCode
from rembggui.jobs.models.cache_fs import BoundModelDirectory
from rembggui.jobs.models.catalog import ExecutionClass, ModelCatalog, ModelSpec
from rembggui.jobs.models.session import ModelSessionManager, PreparationResult
from rembggui.jobs.segmentation_host import (
    _create_rembg_session,
    _instantiate_verified_rembg_session,
    _PreparedRembgSession,
    _run_rembg,
    _validate_verified_launch_payload,
)


class FakeDownloader:
    def __init__(self, cache_root: Path, events: list[str]) -> None:
        self.cache_root = cache_root
        self.events = events
        self.fail: BaseException | None = None
        self.calls: list[str] = []

    def download(
        self,
        spec: ModelSpec,
        destination: Path,
        progress: Callable[[int, int], None],
        cancelled: Callable[[], bool],
    ) -> Path:
        self.events.append(f"download:{spec.id}")
        self.calls.append(spec.id)
        if self.fail is not None:
            raise self.fail
        assert destination == self.cache_root
        assert cancelled() is False
        artifact = spec.artifact
        assert artifact is not None
        target = destination / "2.0.72" / spec.id / artifact.runtime_filename
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
    home = tmp_path / "2.0.72" / "u2net"
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
    }
    return catalog, payload, artifact_path


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
    }
    assert payload["model_id"] == "u2net"
    assert payload["runtime_filename"] == "u2net.onnx"
    assert payload["model_home"] == str(tmp_path / "2.0.72" / "u2net")
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
    result = _run_rembg(source, prepared)

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


@pytest.mark.parametrize(
    ("model_id", "execution_class"),
    [
        ("sam", ExecutionClass.SAM_PREVIEW),
        ("withoutbg", ExecutionClass.CLOUD_WITHOUTBG),
    ],
)
def test_special_models_return_capability_without_starting_local_child(
    tmp_path: Path, model_id: str, execution_class: ExecutionClass
) -> None:
    manager, downloader, clients, _events = _manager(tmp_path)

    result = manager.prepare(model_id, {})

    assert result.execution_class is execution_class
    assert result.local_session_ready is False
    assert result.artifact_path is None
    assert downloader.calls == []
    assert clients == []
    assert manager.active_id is None


def test_switching_from_local_to_capability_closes_local_exactly_once(
    tmp_path: Path,
) -> None:
    manager, _downloader, clients, _events = _manager(tmp_path)
    manager.prepare("u2net", {})

    manager.prepare("sam", {})
    manager.prepare("withoutbg", {})

    assert clients[0].closes == 1
    assert manager.active_id is None


@pytest.mark.parametrize(
    "extras",
    [
        {"model_path": "/tmp/custom.onnx"},
        {"custom": True},
        {"token": "secret-token"},
        {"sam_prompt": []},
    ],
)
def test_task9_rejects_all_custom_or_sensitive_extras_without_leaking_values(
    tmp_path: Path, extras: dict[str, object]
) -> None:
    manager, _downloader, _clients, _events = _manager(tmp_path)

    with pytest.raises(AppError) as exc:
        manager.prepare("u2net", extras)
    assert exc.value.code is ErrorCode.MODEL_PREPARATION_INVALID
    assert "secret-token" not in repr(exc.value)


def test_remove_rejects_active_model_then_removes_exact_inactive_artifact(
    tmp_path: Path,
) -> None:
    manager, _downloader, _clients, _events = _manager(tmp_path)
    result = manager.prepare("u2net", {})
    assert result.artifact_path is not None

    with pytest.raises(AppError) as exc:
        manager.remove("u2net")
    assert exc.value.code is ErrorCode.MODEL_IN_USE

    manager.prepare("sam", {})
    assert manager.remove("u2net") is True
    assert not result.artifact_path.exists()
    assert manager.remove("u2net") is False


def test_remove_rejects_symlink_traversal_and_unknown_ids(tmp_path: Path) -> None:
    manager, _downloader, _clients, _events = _manager(tmp_path)
    outside = tmp_path / "outside.onnx"
    outside.write_bytes(b"outside")
    model_dir = tmp_path / "2.0.72" / "u2net"
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
    import rembggui.jobs.models.session as session_module

    manager, _downloader, _clients, _events = _manager(tmp_path)
    target = tmp_path / "2.0.72" / "u2net" / "u2net.onnx"
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
    target = tmp_path / "2.0.72" / "u2net" / "u2net.onnx"
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
    assert verified.rembg_version == "2.0.72"
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
    import rembggui.jobs.segmentation_host as host_module

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
        lambda model_id, model_bytes, rembg_version, inference_kwargs=(): (
            calls.append((model_id, model_bytes, rembg_version, inference_kwargs)),
            sentinel,
        )[1],
    )
    before = dict(os.environ)

    assert _create_rembg_session(payload) is sentinel
    assert calls == [("u2net", b"verified-model", "2.0.72", ())]
    assert dict(os.environ) == before

    artifact_path.write_bytes(b"tampered")
    with pytest.raises(AppError):
        _create_rembg_session(payload)
    assert len(calls) == 1


def test_verified_instantiation_passes_same_immutable_bytes_directly_to_ort() -> None:
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

    class FakeOrt:
        @staticmethod
        def SessionOptions() -> FakeOptions:
            return FakeOptions()

        @staticmethod
        def get_device() -> str:
            return "CPU"

        @staticmethod
        def InferenceSession(
            content: bytes, *, sess_options: object, providers: list[str]
        ) -> object:
            assert isinstance(sess_options, FakeOptions)
            assert providers == ["CPUExecutionProvider"]
            captured.append(content)
            return object()

    before = dict(os.environ)

    session = _instantiate_verified_rembg_session(
        "u2net",
        model_bytes,
        "2.0.72",
        session_classes=[FakeSession],
        ort_module=FakeOrt,
        installed_version="2.0.72",
    )

    assert isinstance(session, _PreparedRembgSession)
    assert isinstance(session.session, FakeSession)
    assert session.session.inner_session is not None
    assert captured == [model_bytes]
    assert captured[0] is model_bytes
    assert original_download_called is False
    assert dict(os.environ) == before


def test_session_consumes_verified_bytes_even_if_path_swaps_back_during_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import rembggui.jobs.segmentation_host as host_module

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
    ) -> object:
        captured.append(model_bytes)
        artifact_path.write_bytes(evil)
        artifact_path.write_bytes(good)
        return object()

    monkeypatch.setattr(host_module, "_instantiate_verified_rembg_session", instantiate)

    _create_rembg_session(payload)

    assert captured == [good]
