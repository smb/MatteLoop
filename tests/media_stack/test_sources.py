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
