from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from rembggui.core.errors import AppError, ErrorCode
from rembggui.jobs.models.catalog import ExecutionClass, ModelCatalog, ModelSpec
from rembggui.jobs.models.session import ModelSessionManager, PreparationResult
from rembggui.jobs.segmentation_host import (
    _create_rembg_session,
    _instantiate_verified_rembg_session,
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
    }
    assert payload["model_id"] == "u2net"
    assert payload["runtime_filename"] == "u2net.onnx"
    assert payload["model_home"] == str(tmp_path / "2.0.72" / "u2net")
    assert "model_path" not in payload
    assert "extras" not in payload


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


def test_child_launch_reproves_exact_manifest_bound_regular_file(
    tmp_path: Path,
) -> None:
    catalog, payload, artifact_path = _verified_launch(tmp_path)

    verified = _validate_verified_launch_payload(payload, catalog=catalog)

    assert verified.model_id == "u2net"
    assert verified.artifact_path == artifact_path
    assert verified.rembg_version == "2.0.72"


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
    calls: list[tuple[str, Path, str]] = []
    sentinel = object()

    monkeypatch.setattr(
        ModelCatalog,
        "load_resource",
        classmethod(lambda _cls: catalog),
    )
    monkeypatch.setattr(
        host_module,
        "_instantiate_verified_rembg_session",
        lambda model_id, path, rembg_version: (
            calls.append((model_id, path, rembg_version)),
            sentinel,
        )[1],
    )
    real_verify = host_module._verify_child_artifact
    proofs: list[Path] = []

    def recording_verify(
        path: Path, *, expected_size: int, expected_sha256: str
    ) -> None:
        proofs.append(path)
        real_verify(
            path,
            expected_size=expected_size,
            expected_sha256=expected_sha256,
        )

    monkeypatch.setattr(host_module, "_verify_child_artifact", recording_verify)
    before = dict(os.environ)

    assert _create_rembg_session(payload) is sentinel
    assert calls == [("u2net", artifact_path, "2.0.72")]
    assert proofs == [artifact_path, artifact_path]
    assert dict(os.environ) == before

    artifact_path.write_bytes(b"tampered")
    with pytest.raises(AppError):
        _create_rembg_session(payload)
    assert len(calls) == 1


def test_verified_instantiation_overrides_upstream_downloader_without_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import rembg.sessions as rembg_sessions

    artifact = tmp_path / "u2net.onnx"
    artifact.write_bytes(b"already-verified")
    original_download_called = False

    class FakeSession:
        @classmethod
        def name(cls) -> str:
            return "u2net"

        @classmethod
        def download_models(cls, *_args: object, **_kwargs: object) -> Path:
            nonlocal original_download_called
            original_download_called = True
            raise AssertionError("upstream downloader must not be called")

        def __init__(self, model_id: str, _options: object) -> None:
            assert model_id == "u2net"
            assert self.__class__.download_models() == artifact

    monkeypatch.setattr(rembg_sessions, "sessions_class", [FakeSession])
    before = dict(os.environ)

    session = _instantiate_verified_rembg_session("u2net", artifact, "2.0.72")

    assert isinstance(session, FakeSession)
    assert original_download_called is False
    assert dict(os.environ) == before
