import hashlib
import json
import shutil
import tarfile
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from scripts.media_stack.manifest import SourceSpec
from scripts.qt_source import (
    ensure_qt_source_companion,
    installed_qt_distribution_inventory,
    validate_qt_source_companion,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PACKAGES = {
    name: {
        "files": sorted(
            [f"{name}/runtime", f"{name}-6.10.3.dist-info/WHEEL"]
        ),
        "version": "6.10.3",
        "wheel_tags": ["cp39-abi3-macosx_13_0_universal2"],
    }
    for name in ("PySide6", "PySide6_Essentials", "PySide6_Addons", "shiboken6")
}
PROJECT_EVIDENCE = (
    "packaging/entrypoint.py",
    "packaging/pysidedeploy.spec",
    "packaging/smoke_child.py",
    "pyproject.toml",
    "scripts/build.py",
    "scripts/qt_source.py",
    "uv.lock",
)
LEGAL_EVIDENCE = (
    "legal/GPL-3.0.txt",
    "legal/LGPL-3.0.txt",
    "legal/QT-PYSIDE-LGPL-NOTICE.md",
    "legal/RELINK.md",
    "legal/patches/README.md",
)


def _project_root(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    manifest = root / "packaging/qt-source/manifest.toml"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPOSITORY_ROOT / "packaging/qt-source/manifest.toml", manifest)
    for relative in (*PROJECT_EVIDENCE, *LEGAL_EVIDENCE):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"evidence for {relative}\n", encoding="utf-8")
    return root


def _fake_source(
    calls: list[SourceSpec], source: SourceSpec, cache_dir: Path
) -> Path:
    calls.append(source)
    cache_dir.mkdir(parents=True, exist_ok=True)
    destination = cache_dir / Path(urlsplit(source.url).path).name
    destination.write_bytes(f"official bytes for {source.name}".encode())
    return destination


def _build(tmp_path: Path, calls: list[SourceSpec], cache_name: str = "cache"):
    root = _project_root(tmp_path)
    return ensure_qt_source_companion(
        root,
        tmp_path / cache_name,
        PACKAGES,
        source_ensurer=lambda source, cache: _fake_source(calls, source, cache),
    )


def test_qt_companion_contains_exact_sources_inventory_and_relink_evidence(
    tmp_path: Path,
) -> None:
    calls: list[SourceSpec] = []
    companion = _build(tmp_path, calls)

    assert companion.archive.name == (
        f"MatteLoop-qt-sources-6.10.3-{companion.identity}.tar.gz"
    )
    assert companion.checksum.read_text() == (
        f"{hashlib.sha256(companion.archive.read_bytes()).hexdigest()}  "
        f"{companion.archive.name}\n"
    )
    assert {source.name for source in calls} == {
        "qtbase",
        "qtimageformats",
        "pyside-setup",
    }
    with tarfile.open(companion.archive, "r:gz") as archive:
        names = archive.getnames()
        assert names == sorted(names)
        assert set(names) == {
            "component-inventory.json",
            "legal/GPL-3.0.txt",
            "legal/LGPL-3.0.txt",
            "legal/QT-PYSIDE-LGPL-NOTICE.md",
            "legal/RELINK.md",
            "manifest.toml",
            "package-inventory.json",
            "patches/README.md",
            "project/packaging/entrypoint.py",
            "project/packaging/pysidedeploy.spec",
            "project/packaging/smoke_child.py",
            "project/pyproject.toml",
            "project/scripts/build.py",
            "project/scripts/qt_source.py",
            "project/uv.lock",
            "source-checksums.json",
            "sources/pyside-setup-everywhere-src-6.10.3.tar.xz",
            "sources/qtbase-everywhere-src-6.10.3.tar.xz",
            "sources/qtimageformats-everywhere-src-6.10.3.tar.xz",
        }
        packages = json.load(archive.extractfile("package-inventory.json"))
        sources = json.load(archive.extractfile("source-checksums.json"))
        assert packages == PACKAGES
        assert {item["name"]: item["sha256"] for item in sources} == {
            source.name: source.sha256 for source in calls
        }
        for source in calls:
            member = "sources/" + Path(urlsplit(source.url).path).name
            assert archive.extractfile(member).read() == (
                f"official bytes for {source.name}".encode()
            )


def test_qt_companion_normalizes_tar_and_gzip_bytes_deterministically(
    tmp_path: Path,
) -> None:
    first = _build(tmp_path / "first", [], "first-cache")
    second = _build(tmp_path / "second", [], "second-cache")

    assert first.identity == second.identity
    assert first.archive.read_bytes() == second.archive.read_bytes()
    with tarfile.open(first.archive, "r:gz") as archive:
        for member in archive.getmembers():
            assert member.mtime == 0
            assert member.mode == 0o644
            assert member.uid == member.gid == 0
            assert member.uname == member.gname == ""


def test_qt_companion_reuses_only_a_checksum_verified_cache(tmp_path: Path) -> None:
    calls: list[SourceSpec] = []
    first = _build(tmp_path, calls)
    cached = _build(tmp_path, calls)

    assert cached == first
    assert len(calls) == 3

    first.archive.write_bytes(b"corrupt")
    rebuilt = _build(tmp_path, calls)

    assert len(calls) == 6
    assert rebuilt.archive.read_bytes() != b"corrupt"
    assert rebuilt.checksum.read_text().endswith(f"  {rebuilt.archive.name}\n")


def test_qt_companion_validation_rejects_noncanonical_checksum(tmp_path: Path) -> None:
    companion = _build(tmp_path, [])
    assert validate_qt_source_companion(companion)

    companion.checksum.write_text("corrupt\n", encoding="utf-8")
    assert not validate_qt_source_companion(companion)


def test_qt_companion_fails_when_required_project_evidence_is_missing(
    tmp_path: Path,
) -> None:
    root = _project_root(tmp_path)
    (root / "legal/RELINK.md").unlink()

    with pytest.raises(FileNotFoundError, match="legal/RELINK.md"):
        ensure_qt_source_companion(root, tmp_path / "cache", PACKAGES)


def test_qt_companion_propagates_verified_source_failure(tmp_path: Path) -> None:
    root = _project_root(tmp_path)

    def reject_source(_source: SourceSpec, _cache: Path) -> Path:
        raise ValueError("source SHA-256 mismatch")

    with pytest.raises(ValueError, match="source SHA-256 mismatch"):
        ensure_qt_source_companion(
            root, tmp_path / "cache", PACKAGES, source_ensurer=reject_source
        )


def test_installed_qt_inventory_records_exact_wheels_and_package_files() -> None:
    inventory = installed_qt_distribution_inventory()

    assert set(inventory) == set(PACKAGES)
    for name, item in inventory.items():
        assert item["version"] == "6.10.3"
        assert item["wheel_tags"]
        assert item["files"]
        assert any(str(path).endswith(".dist-info/WHEEL") for path in item["files"])
        assert any(str(path).startswith(name.split("_")[0]) for path in item["files"])
