import hashlib
import tarfile
from contextlib import closing
from io import BytesIO
from pathlib import Path

import pytest

from scripts.media_stack.manifest import SourceSpec
from scripts.media_stack.sources import ensure_source, extract_source


def _source(payload: bytes) -> SourceSpec:
    return SourceSpec(
        name="fixture",
        version="1",
        url="https://example.invalid/fixture.tar.gz",
        sha256=hashlib.sha256(payload).hexdigest(),
        archive_root="fixture-1",
    )


def _archive(path: Path, root: str) -> None:
    contents = b"source contents"
    with tarfile.open(path, "w:gz") as archive:
        member = tarfile.TarInfo(f"{root}/README")
        member.size = len(contents)
        archive.addfile(member, BytesIO(contents))


def _root_entry_archive(path: Path, root: str, entry_type: bytes) -> None:
    with tarfile.open(path, "w:gz") as archive:
        member = tarfile.TarInfo(root)
        member.type = entry_type
        if entry_type == tarfile.REGTYPE:
            contents = b"not a directory"
            member.size = len(contents)
            archive.addfile(member, BytesIO(contents))
        else:
            member.linkname = "another-entry"
            archive.addfile(member)


def test_source_download_is_promoted_only_after_digest_matches(tmp_path: Path) -> None:
    payload = b"verified source"

    result = ensure_source(
        _source(payload), tmp_path, opener=lambda _url: closing(BytesIO(payload))
    )

    assert result.read_bytes() == payload
    assert not tuple(tmp_path.glob("*.part"))


def test_source_checksum_mismatch_leaves_no_promoted_archive(tmp_path: Path) -> None:
    source = _source(b"expected source")

    with pytest.raises(ValueError, match="SHA-256"):
        ensure_source(
            source, tmp_path, opener=lambda _url: closing(BytesIO(b"wrong source"))
        )

    assert not (tmp_path / "fixture.tar.gz").exists()
    assert not tuple(tmp_path.glob("*.part"))


def test_source_rejects_http_before_invoking_the_opener(tmp_path: Path) -> None:
    source = SourceSpec(
        name="fixture",
        version="1",
        url="http://example.invalid/fixture.tar.gz",
        sha256=hashlib.sha256(b"source").hexdigest(),
        archive_root="fixture-1",
    )

    with pytest.raises(ValueError, match="HTTPS"):
        ensure_source(
            source,
            tmp_path,
            opener=lambda _url: pytest.fail("an HTTP source must not be opened"),
        )


def test_source_reuses_an_archive_with_a_matching_digest(tmp_path: Path) -> None:
    payload = b"verified source"
    source = _source(payload)
    cached = tmp_path / "fixture.tar.gz"
    cached.write_bytes(payload)

    result = ensure_source(
        source,
        tmp_path,
        opener=lambda _url: pytest.fail("a valid cached archive must not download"),
    )

    assert result == cached
    assert result.read_bytes() == payload


def test_source_replaces_an_archive_with_an_invalid_digest(tmp_path: Path) -> None:
    payload = b"verified replacement"
    source = _source(payload)
    cached = tmp_path / "fixture.tar.gz"
    cached.write_bytes(b"invalid cached source")

    result = ensure_source(
        source, tmp_path, opener=lambda _url: closing(BytesIO(payload))
    )

    assert result == cached
    assert result.read_bytes() == payload


def test_source_extraction_rejects_an_unexpected_top_level_directory(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "fixture.tar.gz"
    destination = tmp_path / "destination"
    _archive(archive, "unexpected-root")

    with pytest.raises(ValueError, match="archive root"):
        extract_source(archive, destination, "fixture-1")

    assert not destination.exists() or not tuple(destination.iterdir())


def test_source_extraction_rejects_a_regular_file_as_the_declared_root(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "fixture.tar.gz"
    _root_entry_archive(archive, "fixture-1", tarfile.REGTYPE)

    with pytest.raises(ValueError, match="directory"):
        extract_source(archive, tmp_path / "destination", "fixture-1")


def test_source_extraction_rejects_a_symlink_as_the_declared_root(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "fixture.tar.gz"
    _root_entry_archive(archive, "fixture-1", tarfile.SYMTYPE)

    with pytest.raises(ValueError, match="directory"):
        extract_source(archive, tmp_path / "destination", "fixture-1")


def test_source_extraction_accepts_nested_members_without_a_root_entry(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "fixture.tar.gz"
    _archive(archive, "fixture-1")

    extracted = extract_source(archive, tmp_path / "destination", "fixture-1")

    assert extracted.joinpath("README").read_bytes() == b"source contents"
