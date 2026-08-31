from pathlib import Path

import pytest

from scripts.media_stack.manifest import (
    load_manifest,
    media_stack_identity,
)

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "packaging" / "media-stack" / "manifest.toml"


def _write_manifest(tmp_path: Path, replacement: tuple[str, str]) -> Path:
    source = MANIFEST.read_text(encoding="utf-8")
    old, new = replacement
    path = tmp_path / "manifest.toml"
    path.write_text(source.replace(old, new, 1), encoding="utf-8")
    return path


def test_manifest_pins_the_lgpl_media_sources() -> None:
    manifest = load_manifest(MANIFEST)
    assert [
        (source.name, source.version, source.sha256) for source in manifest.sources
    ] == [
        (
            "ffmpeg",
            "8.0.1",
            "05ee0b03119b45c0bdb4df654b96802e909e0a752f72e4fe3794f487229e5a41",
        ),
        (
            "libwebp",
            "1.6.0",
            "e4ab7009bf0629fd11982d4c2aa83964cf244cffba7347ecd39019a9e38c4564",
        ),
        (
            "pyav",
            "16.1.0",
            "a094b4fd87a3721dacf02794d3d2c82b8d712c85b9534437e82a8a978c175ffd",
        ),
    ]
    assert manifest.targets == ("macos-arm64", "windows-x64")
    assert manifest.python_abi == "cp313"
    assert manifest.macos_deployment_target == "13.0"
    assert manifest.verification.required_codecs == (
        "h264",
        "hevc",
        "libwebp_anim",
    )
    assert manifest.verification.required_formats == ("mov", "webp")


def test_manifest_rejects_a_malformed_source_digest(tmp_path: Path) -> None:
    path = _write_manifest(
        tmp_path,
        (
            "05ee0b03119b45c0bdb4df654b96802e909e0a752f72e4fe3794f487229e5a41",
            "05ee0b03119b45c0bdb4df654b96802e909e0a752f72e4fe3794f487229e5a4z",
        ),
    )

    with pytest.raises(ValueError, match="sha256"):
        load_manifest(path)


def test_manifest_rejects_a_floating_source_url(tmp_path: Path) -> None:
    path = _write_manifest(
        tmp_path,
        ("ffmpeg-8.0.1.tar.xz", "latest.tar.xz"),
    )

    with pytest.raises(ValueError, match="pinned"):
        load_manifest(path)


def test_manifest_rejects_a_floating_source_version(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path, ('version = "8.0.1"', 'version = "latest"'))

    with pytest.raises(ValueError, match="pinned"):
        load_manifest(path)


def test_manifest_rejects_an_unsupported_target(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path, ('"windows-x64"', '"linux-x64"'))

    with pytest.raises(ValueError, match="target"):
        load_manifest(path)


def test_manifest_rejects_an_unsupported_python_abi(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path, ('python_abi = "cp313"', 'python_abi = "cp312"'))

    with pytest.raises(ValueError, match="python_abi"):
        load_manifest(path)


def test_manifest_rejects_duplicate_source_names(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path, ('name = "libwebp"', 'name = "ffmpeg"'))

    with pytest.raises(ValueError, match="source names"):
        load_manifest(path)


def test_manifest_rejects_unknown_tool_keys(tmp_path: Path) -> None:
    path = _write_manifest(
        tmp_path,
        ('build = "1.6.0"', 'build = "1.6.0"\nextra = "value"'),
    )

    with pytest.raises(ValueError, match="tool"):
        load_manifest(path)


def test_identity_is_stable_for_the_same_manifest_and_target(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.toml"
    manifest_path.write_bytes(MANIFEST.read_bytes())
    common = dict(
        manifest_path=manifest_path,
        os_name="darwin",
        machine="arm64",
        python_tag="cp313",
        deployment_target="13.0",
    )

    first = media_stack_identity(**common, builder_revision=1)
    assert first == media_stack_identity(**common, builder_revision=1)
    assert len(first) == 24
    assert all(character in "0123456789abcdef" for character in first)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("os_name", "windows"),
        ("machine", "x86_64"),
        ("python_tag", "cp314"),
        ("deployment_target", "14.0"),
    ],
)
def test_identity_changes_for_each_platform_contract_field(
    tmp_path: Path, field: str, value: str
) -> None:
    manifest_path = tmp_path / "manifest.toml"
    manifest_path.write_bytes(MANIFEST.read_bytes())
    common = dict(
        manifest_path=manifest_path,
        os_name="darwin",
        machine="arm64",
        python_tag="cp313",
        deployment_target="13.0",
    )
    first = media_stack_identity(**common, builder_revision=1)

    changed = {**common, field: value}
    assert first != media_stack_identity(**changed, builder_revision=1)


def test_identity_changes_when_the_builder_contract_changes(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.toml"
    manifest_path.write_bytes(MANIFEST.read_bytes())
    common = dict(
        manifest_path=manifest_path,
        os_name="darwin",
        machine="arm64",
        python_tag="cp313",
        deployment_target="13.0",
    )
    first = media_stack_identity(**common, builder_revision=1)

    assert first != media_stack_identity(**common, builder_revision=2)


def test_identity_changes_when_manifest_bytes_change(tmp_path: Path) -> None:
    first_path = tmp_path / "manifest.toml"
    second_path = tmp_path / "manifest-copy.toml"
    first_path.write_bytes(MANIFEST.read_bytes())
    second_path.write_bytes(MANIFEST.read_bytes() + b"\n")
    common = dict(
        os_name="darwin",
        machine="arm64",
        python_tag="cp313",
        deployment_target="13.0",
        builder_revision=1,
    )

    assert media_stack_identity(first_path, **common) != media_stack_identity(
        second_path, **common
    )
