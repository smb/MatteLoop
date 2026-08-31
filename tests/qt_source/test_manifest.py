from pathlib import Path

import pytest

from scripts.qt_source import load_qt_source_manifest, qt_source_identity

MANIFEST = Path("packaging/qt-source/manifest.toml")
PACKAGES = {
    "PySide6": "6.10.3",
    "PySide6_Essentials": "6.10.3",
    "PySide6_Addons": "6.10.3",
    "shiboken6": "6.10.3",
}
EXPECTED_SOURCES = {
    "qtbase": (
        "https://download.qt.io/official_releases/qt/6.10/6.10.3/"
        "submodules/qtbase-everywhere-src-6.10.3.tar.xz",
        "383dc907816338f0cba72088a524c07458dfc69ce684ca9132fcc4fe91c24b0b",
        "qtbase-everywhere-src-6.10.3",
    ),
    "qtimageformats": (
        "https://download.qt.io/official_releases/qt/6.10/6.10.3/"
        "submodules/qtimageformats-everywhere-src-6.10.3.tar.xz",
        "84605dd91037482b5b7c7ecc5c27aee8acc1cd7f1fe77bc564777ddf365d7d28",
        "qtimageformats-everywhere-src-6.10.3",
    ),
    "pyside-setup": (
        "https://download.qt.io/official_releases/QtForPython/pyside6/"
        "PySide6-6.10.3-src/pyside-setup-everywhere-src-6.10.3.tar.xz",
        "2c7462fe0cecb5b8ac0a3d92014b8d0b88bd4d9f8646709dab5286d9416f45bc",
        "pyside-setup-everywhere-src-6.10.3",
    ),
}


def test_qt_source_manifest_pins_exact_delivered_sources_and_packages() -> None:
    manifest = load_qt_source_manifest(MANIFEST)

    assert manifest.schema_version == 1
    assert manifest.qt_version == "6.10.3"
    assert manifest.distributions == PACKAGES
    assert {source.name for source in manifest.sources} == set(EXPECTED_SOURCES)
    for source in manifest.sources:
        url, sha256, archive_root = EXPECTED_SOURCES[source.name]
        assert (source.url, source.sha256, source.archive_root) == (
            url,
            sha256,
            archive_root,
        )
        assert source.version == "6.10.3"


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("schema_version = 1", "schema_version = 1\nunexpected = true", "keys"),
        ('qt_version = "6.10.3"', 'qt_version = "latest"', "pinned"),
        ("https://download.qt.io", "http://download.qt.io", "HTTPS"),
        (
            'archive_root = "qtbase-everywhere-src-6.10.3"',
            'archive_root = "qtbase-src"',
            "archive_root",
        ),
        (
            "383dc907816338f0cba72088a524c07458dfc69ce684ca9132fcc4fe91c24b0b",
            "not-a-digest",
            "sha256",
        ),
        ('name = "qtbase"', 'name = "other"', "source names"),
        (
            'shiboken6 = "6.10.3"',
            'shiboken6 = "6.10.3"\nother = "6.10.3"',
            "distribution names",
        ),
    ],
)
def test_qt_source_manifest_rejects_unbounded_or_unknown_contracts(
    tmp_path: Path, old: str, new: str, message: str
) -> None:
    path = tmp_path / "manifest.toml"
    path.write_text(MANIFEST.read_text(encoding="utf-8").replace(old, new, 1))

    with pytest.raises(ValueError, match=message):
        load_qt_source_manifest(path)


def test_qt_source_identity_changes_with_raw_manifest_or_evidence_bytes(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "manifest.toml"
    manifest.write_bytes(MANIFEST.read_bytes())
    evidence = {"legal/RELINK.md": b"instructions"}
    first = qt_source_identity(manifest, PACKAGES, evidence, recipe_revision=1)

    manifest.write_bytes(MANIFEST.read_bytes() + b"\n")
    changed_manifest = qt_source_identity(
        manifest, PACKAGES, evidence, recipe_revision=1
    )
    changed_evidence = qt_source_identity(
        manifest,
        PACKAGES,
        {"legal/RELINK.md": b"different instructions"},
        recipe_revision=1,
    )
    changed_recipe = qt_source_identity(
        manifest, PACKAGES, evidence, recipe_revision=2
    )

    assert len(first) == 24
    assert len({first, changed_manifest, changed_evidence, changed_recipe}) == 4
