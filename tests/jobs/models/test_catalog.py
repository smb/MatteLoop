from __future__ import annotations

import importlib.metadata
import json
from dataclasses import FrozenInstanceError, fields, replace

import pytest

from matteloop.core.errors import AppError, ErrorCode
from matteloop.core.fingerprints import REMBG_VERSION
from matteloop.jobs.models.catalog import (
    ClothCategory,
    ExecutionClass,
    InferenceDefaults,
    ModelCatalog,
    ModelSpec,
)

APPROVED_IDS = {
    "u2net",
    "u2netp",
    "u2net_human_seg",
    "u2net_cloth_seg",
    "silueta",
    "isnet-general-use",
    "isnet-anime",
    "birefnet-general",
    "birefnet-general-lite",
    "birefnet-portrait",
    "birefnet-dis",
    "birefnet-hrsod",
    "birefnet-cod",
    "birefnet-massive",
    "bria-rmbg",
}


def _manifest() -> dict[str, object]:
    return json.loads(ModelCatalog.resource_path().read_text(encoding="utf-8"))


def _load(payload: object) -> ModelCatalog:
    return ModelCatalog.from_bytes(json.dumps(payload).encode())


def _model(payload: dict[str, object], model_id: str) -> dict[str, object]:
    models = payload["models"]
    assert isinstance(models, list)
    return next(
        item for item in models if isinstance(item, dict) and item.get("id") == model_id
    )


def test_manifest_contains_exact_approved_catalog_and_default() -> None:
    catalog = ModelCatalog.load_resource()

    assert catalog.rembg_version == "2.0.75"
    assert catalog.obsolete_rembg_versions == ("2.0.72",)
    assert catalog.default_id == "birefnet-portrait"
    assert set(catalog.ids) == APPROVED_IDS
    assert len(catalog.ids) == len(APPROVED_IDS)


def test_manifest_pin_matches_the_installed_rembg_distribution() -> None:
    catalog = ModelCatalog.load_resource()

    assert catalog.rembg_version == REMBG_VERSION
    assert catalog.rembg_version == importlib.metadata.version("rembg")


def test_manifest_without_obsolete_versions_remains_compatible() -> None:
    payload = _manifest()
    del payload["obsolete_rembg_versions"]

    catalog = _load(payload)

    assert catalog.obsolete_rembg_versions == ()


@pytest.mark.parametrize(
    "obsolete_versions",
    [
        ["2.0.75"],
        ["2.0.72", "2.0.72"],
        ["2.0.72", 2],
        ["2.0.75."],
        ["2.0.75 "],
        ["../x"],
        ["a/b"],
        ["a\\b"],
        ["C:"],
        [""],
        ["not-a-version"],
        ["."],
        [".."],
        ["/"],
        ["\\"],
    ],
)
def test_manifest_rejects_invalid_obsolete_rembg_versions(
    obsolete_versions: object,
) -> None:
    payload = _manifest()
    payload["obsolete_rembg_versions"] = obsolete_versions

    with pytest.raises(AppError) as exc:
        _load(payload)

    assert exc.value.code is ErrorCode.MODEL_MANIFEST_INVALID


def test_local_catalog_metadata_is_pinned_and_deeply_immutable() -> None:
    catalog = ModelCatalog.load_resource()
    portrait = catalog.get("birefnet-portrait")

    assert portrait.execution_class is ExecutionClass.LOCAL
    assert portrait.upstream_id == "birefnet-portrait"
    assert portrait.supports_render is True
    assert portrait.required_inputs == ()
    assert portrait.artifact is not None
    assert portrait.artifact.runtime_filename == "birefnet-portrait.onnx"
    assert portrait.artifact.size_bytes == 972_666_916
    assert portrait.artifact.sha256 == (
        "1ba1c8ff5a7bbfadc8d8d13fb11d7be793f91f23d9d466549e37a854f6668f99"
    )
    assert portrait.artifact.upstream_checksum == (
        "md5:c3a64a6abf20250d090cd055f12a3b67"
    )
    with pytest.raises(FrozenInstanceError):
        portrait.purpose = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        catalog.specs[portrait.id] = portrait  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        catalog.default_id = "u2net"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        catalog.ids = ()  # type: ignore[misc]


def test_cloth_model_has_only_the_canonical_input_free_full_default() -> None:
    catalog = ModelCatalog.load_resource()

    assert catalog.get("u2net_cloth_seg").inference_defaults == InferenceDefaults(
        ClothCategory.FULL
    )


def test_direct_catalog_construction_cannot_retain_mutable_collections() -> None:
    original = ModelCatalog.load_resource()
    specs = dict(original.specs)
    catalog = ModelCatalog(
        original.rembg_version,
        original.default_id,
        list(original.ids),  # type: ignore[arg-type]
        specs,
    )

    specs.clear()

    assert catalog.ids == original.ids
    assert len(catalog.specs) == 15
    with pytest.raises(TypeError):
        catalog.specs["u2net"] = original.get("u2net")  # type: ignore[index]
    assert all(
        spec.inference_defaults == InferenceDefaults()
        for model_id, spec in catalog.specs.items()
        if model_id != "u2net_cloth_seg"
    )


def test_direct_catalog_construction_maps_a_malformed_id_element() -> None:
    original = ModelCatalog.load_resource()
    ids = list(original.ids)
    ids[0] = ["u2net"]  # type: ignore[list-item]

    with pytest.raises(AppError) as exc:
        ModelCatalog(original.rembg_version, original.default_id, ids, original.specs)  # type: ignore[arg-type]

    assert exc.value.code is ErrorCode.MODEL_MANIFEST_INVALID


def test_direct_catalog_construction_rejects_a_hidden_former_id() -> None:
    original = ModelCatalog.load_resource()
    specs = dict(original.specs)
    donor = specs.pop("bria-rmbg")
    specs["legacy-retired-model"] = replace(
        donor,
        id="legacy-retired-model",
        upstream_id="legacy-retired-model",
    )
    ids = tuple(
        "legacy-retired-model" if model_id == "bria-rmbg" else model_id
        for model_id in original.ids
    )

    with pytest.raises(AppError) as exc:
        ModelCatalog(original.rembg_version, original.default_id, ids, specs)

    assert exc.value.code is ErrorCode.MODEL_MANIFEST_INVALID


def test_direct_catalog_construction_rejects_a_former_upstream_alias() -> None:
    original = ModelCatalog.load_resource()
    specs = dict(original.specs)
    specs["u2net"] = replace(
        original.get("u2net"),
        upstream_id="withoutbg",
    )

    with pytest.raises(AppError) as exc:
        ModelCatalog(original.rembg_version, original.default_id, original.ids, specs)

    assert exc.value.code is ErrorCode.MODEL_MANIFEST_INVALID


def test_direct_catalog_construction_rejects_a_custom_artifact_url() -> None:
    original = ModelCatalog.load_resource()
    specs = dict(original.specs)
    spec = original.get("u2net")
    assert spec.artifact is not None
    specs[spec.id] = replace(
        spec,
        artifact=replace(
            spec.artifact,
            url="https://provider.example/u2net.onnx",
        ),
    )

    with pytest.raises(AppError) as exc:
        ModelCatalog(original.rembg_version, original.default_id, original.ids, specs)

    assert exc.value.code is ErrorCode.MODEL_MANIFEST_INVALID


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (
            "url",
            "https://github.com/danielgatis/rembg/releases/download/v0.0.1/u2net.onnx",
        ),
        ("url", object()),
        ("runtime_filename", "custom.onnx"),
        ("size_bytes", True),
        ("size_bytes", 0),
        ("sha256", "0" * 63),
        ("upstream_checksum", "md5:" + "0" * 31),
    ],
)
def test_direct_catalog_construction_rejects_malformed_artifact_fields(
    field: str, value: object
) -> None:
    original = ModelCatalog.load_resource()
    specs = dict(original.specs)
    spec = original.get("u2net")
    assert spec.artifact is not None
    artifact = replace(spec.artifact, **{field: value})
    specs[spec.id] = replace(spec, artifact=artifact)

    with pytest.raises(AppError) as exc:
        ModelCatalog(original.rembg_version, original.default_id, original.ids, specs)

    assert exc.value.code is ErrorCode.MODEL_MANIFEST_INVALID


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("display_name", ""),
        ("purpose", object()),
        ("execution_class", "local"),
        ("required_inputs", []),
        ("edge_modes", ()),
        ("edge_modes", ("standard", "standard")),
        ("edge_modes", ("provider",)),
        ("supports_render", 1),
        ("license_note", ""),
        ("privacy_note", object()),
        ("warning", None),
    ],
)
def test_direct_catalog_construction_rejects_malformed_spec_fields(
    field: str, value: object
) -> None:
    original = ModelCatalog.load_resource()
    specs = dict(original.specs)
    spec = original.get("u2net")
    specs[spec.id] = replace(spec, **{field: value})

    with pytest.raises(AppError) as exc:
        ModelCatalog(original.rembg_version, original.default_id, original.ids, specs)

    assert exc.value.code is ErrorCode.MODEL_MANIFEST_INVALID


def test_direct_catalog_construction_rejects_a_stringly_typed_cloth_default() -> None:
    original = ModelCatalog.load_resource()
    specs = dict(original.specs)
    cloth = original.get("u2net_cloth_seg")
    specs[cloth.id] = replace(
        cloth,
        inference_defaults=InferenceDefaults("full"),  # type: ignore[arg-type]
    )

    with pytest.raises(AppError) as exc:
        ModelCatalog(original.rembg_version, original.default_id, original.ids, specs)

    assert exc.value.code is ErrorCode.MODEL_MANIFEST_INVALID


def test_execution_classes_are_exactly_local() -> None:
    assert set(ExecutionClass) == {ExecutionClass.LOCAL}


def test_model_spec_has_no_retired_remote_fields() -> None:
    assert {field.name for field in fields(ModelSpec)} == {
        "id",
        "display_name",
        "upstream_id",
        "purpose",
        "execution_class",
        "artifact",
        "inference_defaults",
        "required_inputs",
        "edge_modes",
        "supports_render",
        "license_note",
        "privacy_note",
        "warning",
    }


def test_every_approved_model_is_a_renderable_local_artifact() -> None:
    catalog = ModelCatalog.load_resource()

    for spec in catalog.specs.values():
        assert spec.execution_class is ExecutionClass.LOCAL
        assert spec.artifact is not None
        assert spec.required_inputs == ()
        assert spec.supports_render is True


def test_bria_exposes_download_license_and_commercial_warning() -> None:
    bria = ModelCatalog.load_resource().get("bria-rmbg")

    assert bria.artifact is not None
    assert bria.artifact.size_bytes == 1_024_331_469
    assert "license" in bria.license_note.lower()
    assert "commercial" in bria.warning.lower()


def test_every_local_artifact_matches_the_app_pinned_release_index() -> None:
    expected = {
        "u2net": (
            "u2net.onnx",
            175_997_641,
            "8d10d2f3bb75ae3b6d527c77944fc5e7dcd94b29809d47a739a7a728a912b491",
            "md5:60024c5c889badc19c04ad937298a77b",
        ),
        "u2netp": (
            "u2netp.onnx",
            4_574_861,
            "309c8469258dda742793dce0ebea8e6dd393174f89934733ecc8b14c76f4ddd8",
            "md5:8e83ca70e441ab06c318d82300c84806",
        ),
        "u2net_human_seg": (
            "u2net_human_seg.onnx",
            175_997_641,
            "01eb6a29a5c4d8edb30b56adad9bb3a2a0535338e480724a213e0acfd2d1c73c",
            "md5:c09ddc2e0104f800e3e1bb4652583d1f",
        ),
        "u2net_cloth_seg": (
            "u2net_cloth_seg.onnx",
            176_194_565,
            "6d2cbc27bfbdc989e1fd325656d65902ecc6a3ccbe94b2d3655ec114efcb128e",
            "md5:2434d1f3cb744e0e49386c906e5a08bb",
        ),
        "silueta": (
            "silueta.onnx",
            44_173_029,
            "75da6c8d2f8096ec743d071951be73b4a8bc7b3e51d9a6625d63644f90ffeedb",
            "md5:55e59e0d8062d2f5d013f4725ee84782",
        ),
        "isnet-general-use": (
            "isnet-general-use.onnx",
            178_648_008,
            "60920e99c45464f2ba57bee2ad08c919a52bbf852739e96947fbb4358c0d964a",
            "md5:fc16ebd8b0c10d971d3513d564d01e29",
        ),
        "isnet-anime": (
            "isnet-anime.onnx",
            176_069_933,
            "f15622d853e8260172812b657053460e20806f04b9e05147d49af7bed31a6e99",
            "md5:6f184e756bb3bd901c8849220a83e38e",
        ),
        "birefnet-general": (
            "BiRefNet-general-epoch_244.onnx",
            972_666_916,
            "58f621f00f5d756097615970a88a791584600dcf7c45b18a0a6267535a1ebd3c",
            "md5:7a35a0141cbbc80de11d9c9a28f52697",
        ),
        "birefnet-general-lite": (
            "BiRefNet-general-bb_swin_v1_tiny-epoch_232.onnx",
            224_005_088,
            "5600024376f572a557870a5eb0afb1e5961636bef4e1e22132025467d0f03333",
            "md5:4fab47adc4ff364be1713e97b7e66334",
        ),
        "birefnet-portrait": (
            "BiRefNet-portrait-epoch_150.onnx",
            972_666_916,
            "1ba1c8ff5a7bbfadc8d8d13fb11d7be793f91f23d9d466549e37a854f6668f99",
            "md5:c3a64a6abf20250d090cd055f12a3b67",
        ),
        "birefnet-dis": (
            "BiRefNet-DIS-epoch_590.onnx",
            972_666_916,
            "6470117bac6f8d82a3f62921056f52d0f5c4d36d1d832096331d5ea38a03acb5",
            "md5:2d4d44102b446f33a4ebb2e56c051f2b",
        ),
        "birefnet-hrsod": (
            "BiRefNet-HRSOD_DHU-epoch_115.onnx",
            972_666_916,
            "4f5837663194fb88f603b76782eae05a3c29f5749872ca1bfb636bd26e7f6bfc",
            "md5:c017ade5de8a50ff0fd74d790d268dda",
        ),
        "birefnet-cod": (
            "BiRefNet-COD-epoch_125.onnx",
            972_666_916,
            "91ec48f566db475cf6e4caa7e9cd997f352edfcc372372f437e2fbefc1557b13",
            "md5:f6d0d21ca89d287f17e7afe9f5fd3b45",
        ),
        "birefnet-massive": (
            "BiRefNet-massive-TR_DIS5K_TR_TEs-epoch_420.onnx",
            972_666_916,
            "a94814cac438a31f95287811882628644a04b22d313ef3071d2ba904b5f627b8",
            "md5:33e726a2136a3d59eb0fdf613e31e3e9",
        ),
        "bria-rmbg": (
            "bria-rmbg-2.0.onnx",
            1_024_331_469,
            "5b486f08200f513f460da46dd701db5fbb47d79b4be4b708a19444bcd4e79958",
            "sha256:5b486f08200f513f460da46dd701db5fbb47d79b4be4b708a19444bcd4e79958",
        ),
    }
    catalog = ModelCatalog.load_resource()

    assert set(expected) == {
        model_id
        for model_id in catalog.ids
        if catalog.get(model_id).execution_class is ExecutionClass.LOCAL
    }
    for model_id, (url_name, size, sha256, upstream_checksum) in expected.items():
        artifact = catalog.get(model_id).artifact
        assert artifact is not None
        assert artifact.url.rsplit("/", 1)[1] == url_name
        assert artifact.size_bytes == size
        assert artifact.sha256 == sha256
        assert artifact.upstream_checksum == upstream_checksum


def test_each_local_pin_has_honest_auditable_provenance() -> None:
    classic_revision = "cd3a3d6767a7859efea31ef0f2f373582cf06d82"
    biref_revision = "43d1d62b06bac8b7d3886a209771f6d7ca10d899"
    bria_revision = "302f8bb8c9606587dae63532702ef3b72208cce7"
    expected_witness_urls = {
        "u2net": f"https://huggingface.co/tomjackson2023/rembg/blob/{classic_revision}/u2net.onnx",
        "u2netp": f"https://huggingface.co/tomjackson2023/rembg/blob/{classic_revision}/u2netp.onnx",
        "u2net_human_seg": f"https://huggingface.co/tomjackson2023/rembg/blob/{classic_revision}/u2net_human_seg.onnx",
        "u2net_cloth_seg": f"https://huggingface.co/tomjackson2023/rembg/blob/{classic_revision}/u2net_cloth_seg.onnx",
        "silueta": f"https://huggingface.co/tomjackson2023/rembg/blob/{classic_revision}/silueta.onnx",
        "isnet-general-use": f"https://huggingface.co/tomjackson2023/rembg/blob/{classic_revision}/isnet-general-use.onnx",
        "isnet-anime": f"https://huggingface.co/tomjackson2023/rembg/blob/{classic_revision}/isnet-anime.onnx",
        "birefnet-general": "https://huggingface.co/EmmaJohnson311/TensorRT-ONNX-collect/blob/"
        f"{biref_revision}/BiRefNet-v2-onnx/BiRefNet-general-epoch_244.onnx",
        "birefnet-general-lite": "https://huggingface.co/EmmaJohnson311/TensorRT-ONNX-collect/blob/"
        f"{biref_revision}/BiRefNet-v2-onnx/BiRefNet-general-bb_swin_v1_tiny-epoch_232.onnx",
        "birefnet-portrait": "https://huggingface.co/EmmaJohnson311/TensorRT-ONNX-collect/blob/"
        f"{biref_revision}/BiRefNet-v2-onnx/BiRefNet-portrait-epoch_150.onnx",
        "birefnet-dis": "https://huggingface.co/EmmaJohnson311/TensorRT-ONNX-collect/blob/"
        f"{biref_revision}/BiRefNet-v2-onnx/BiRefNet-DIS-epoch_590.onnx",
        "birefnet-hrsod": "https://huggingface.co/EmmaJohnson311/TensorRT-ONNX-collect/blob/"
        f"{biref_revision}/BiRefNet-v2-onnx/BiRefNet-HRSOD_DHU-epoch_115.onnx",
        "birefnet-cod": "https://huggingface.co/EmmaJohnson311/TensorRT-ONNX-collect/blob/"
        f"{biref_revision}/BiRefNet-v2-onnx/BiRefNet-COD-epoch_125.onnx",
        "birefnet-massive": "https://huggingface.co/EmmaJohnson311/TensorRT-ONNX-collect/blob/"
        f"{biref_revision}/BiRefNet-v2-onnx/BiRefNet-massive-TR_DIS5K_TR_TEs-epoch_420.onnx",
        "bria-rmbg": "https://huggingface.co/ChuuniZ/comfyui-image-models/blob/"
        f"{bria_revision}/BiRefNet/RMBG-2.0/onnx/model.onnx",
    }
    catalog = ModelCatalog.load_resource()
    provenance_path = ModelCatalog.resource_path().with_name("model-provenance.json")
    payload = json.loads(provenance_path.read_text(encoding="utf-8"))

    assert payload["schema_version"] == 1
    assert payload["rembg_version"] == catalog.rembg_version
    assert payload["recorded_at"] == "2026-08-29"
    assert payload["qualification_status"] == (
        "fail-closed-pending-official-release-live-qualification"
    )
    assert payload["qualification_owner"] == "Task 17 release qualification"
    entries = payload["entries"]
    assert isinstance(entries, list)
    by_id = {entry["model_id"]: entry for entry in entries}
    local = {
        model_id: spec
        for model_id, spec in catalog.specs.items()
        if spec.execution_class is ExecutionClass.LOCAL
    }
    assert set(by_id) == set(local)
    assert set(expected_witness_urls) == set(local)
    assert len(entries) == len(by_id)
    assert len(entries) == 15
    for model_id, spec in local.items():
        artifact = spec.artifact
        assert artifact is not None
        entry = by_id[model_id]
        assert set(entry) == {
            "model_id",
            "asset_url",
            "expected_size_bytes",
            "app_pinned_sha256",
            "pin_method",
            "pin_status",
            "witness_url",
            "witness_sha256",
            "witness_size",
            "witness_trust_status",
            "upstream_checksum",
            "upstream_checksum_source",
            "upstream_checksum_status",
        }
        assert entry["asset_url"] == artifact.url
        assert entry["expected_size_bytes"] == artifact.size_bytes
        assert entry["app_pinned_sha256"] == artifact.sha256
        assert entry["upstream_checksum"] == artifact.upstream_checksum
        assert entry["pin_method"] == "commit-pinned-secondary-lfs-witness"
        assert entry["pin_status"] == (
            "derived-from-secondary-witness-pending-official-live-qualification"
        )
        assert entry["witness_url"] == expected_witness_urls[model_id]
        assert entry["witness_sha256"] == artifact.sha256
        assert entry["witness_size"] == artifact.size_bytes
        assert entry["witness_trust_status"] == (
            "secondary-lfs-metadata-witness-not-official-release-byte-proof"
        )
        revision = str(entry["witness_url"]).split("/blob/", 1)[1].split("/", 1)[0]
        assert len(revision) == 40
        assert all(character in "0123456789abcdef" for character in revision)
        assert str(entry["upstream_checksum_source"]).startswith(
            "rembg-2.0.75/rembg/sessions/"
        )
        assert str(entry["upstream_checksum_status"]).startswith(
            "declared-in-pinned-source-"
        )
    assert "/blob/main/" not in json.dumps(payload)
    assert "brief" not in json.dumps(payload).lower()


@pytest.mark.parametrize(
    "raw",
    [
        b'{"schema_version":1,"schema_version":1}',
        b'{"schema_version":NaN}',
        b"[]",
        b"\xff",
    ],
)
def test_manifest_rejects_duplicate_nonfinite_nonobject_and_non_utf8_json(
    raw: bytes,
) -> None:
    with pytest.raises(AppError) as exc:
        ModelCatalog.from_bytes(raw)
    assert exc.value.code is ErrorCode.MODEL_MANIFEST_INVALID


@pytest.mark.parametrize(
    ("mutation", "detail"),
    [
        (lambda root: root.update({"unknown": True}), "unknown root field"),
        (lambda root: root.update({"schema_version": True}), "boolean integer"),
        (lambda root: root.update({"default_id": "u2net"}), "wrong default"),
        (lambda root: root.update({"rembg_version": "2.0.71"}), "wrong pin"),
        (
            lambda root: _model(root, "u2net").update({"id": "custom"}),
            "unknown model ID",
        ),
        (
            lambda root: _model(root, "u2net").update({"extra": "field"}),
            "unknown model field",
        ),
        (
            lambda root: _model(root, "u2net")["artifact"].update(  # type: ignore[union-attr]
                {"url": "http://github.com/model.onnx"}
            ),
            "non-HTTPS URL",
        ),
        (
            lambda root: _model(root, "u2net")["artifact"].update(  # type: ignore[union-attr]
                {"url": "https://evil.example/u2net.onnx"}
            ),
            "untrusted host",
        ),
        (
            lambda root: _model(root, "u2net")["artifact"].update(  # type: ignore[union-attr]
                {
                    "url": "https://github.com/danielgatis/rembg/releases/"
                    "download/v0.0.0/nested\\u2net.onnx"
                }
            ),
            "backslash path ambiguity",
        ),
        (
            lambda root: _model(root, "u2net")["artifact"].update(  # type: ignore[union-attr]
                {"runtime_filename": "../u2net.onnx"}
            ),
            "unsafe filename",
        ),
        (
            lambda root: _model(root, "u2net")["artifact"].update(  # type: ignore[union-attr]
                {"sha256": "0" * 63}
            ),
            "invalid sha256",
        ),
        (
            lambda root: _model(root, "u2net")["artifact"].update(  # type: ignore[union-attr]
                {"size_bytes": 0}
            ),
            "invalid size",
        ),
        (
            lambda root: _model(root, "u2net").update(
                {"execution_class": "unsupported"}
            ),
            "unsupported execution class",
        ),
        (
            lambda root: _model(root, "u2net").update({"max_upload_bytes": None}),
            "retired remote field",
        ),
        (
            lambda root: _model(root, "u2net_cloth_seg").update(
                {"inference_defaults": {}}
            ),
            "missing cloth default",
        ),
        (
            lambda root: _model(root, "u2net_cloth_seg").update(
                {"inference_defaults": {"cloth_category": "upper"}}
            ),
            "unapproved cloth default",
        ),
        (
            lambda root: _model(root, "u2net").update(
                {"inference_defaults": {"cloth_category": "full"}}
            ),
            "default on wrong model",
        ),
    ],
)
def test_manifest_strictly_rejects_hostile_schema_mutations(
    mutation: object, detail: str
) -> None:
    payload = _manifest()
    assert callable(mutation)
    mutation(payload)

    with pytest.raises(AppError) as exc:
        _load(payload)
    assert exc.value.code is ErrorCode.MODEL_MANIFEST_INVALID, detail


def test_catalog_rejects_unknown_lookup_and_does_not_offer_custom_ids() -> None:
    catalog = ModelCatalog.load_resource()

    for model_id in ("u2net_custom", "withoutbg"):
        with pytest.raises(AppError) as exc:
            catalog.get(model_id)
        assert exc.value.code is ErrorCode.MODEL_NOT_FOUND
