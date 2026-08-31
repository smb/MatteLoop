import json
import tarfile
from pathlib import Path

from scripts.media_stack.compliance import create_compliance_archive
from scripts.media_stack.platforms import BuildTarget

MACOS = BuildTarget("darwin", "arm64", "macos-arm64", "cp313", "13.0")


def _write(path: Path, contents: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contents)
    return path


def _archive_inputs(tmp_path: Path) -> dict[str, object]:
    sources = {
        name: _write(tmp_path / "downloads" / filename, contents)
        for name, filename, contents in (
            ("ffmpeg", "ffmpeg-8.0.1.tar.xz", b"exact ffmpeg source"),
            ("libwebp", "libwebp-1.6.0.tar.gz", b"exact libwebp source"),
            ("pyav", "av-16.1.0.tar.gz", b"exact pyav source"),
        )
    }
    licences = {
        component: (_write(tmp_path / "licences" / component / name, text),)
        for component, name, text in (
            ("ffmpeg", "COPYING.LGPLv2.1", b"FFmpeg LGPL"),
            ("libwebp", "COPYING", b"libwebp licence"),
            ("pyav", "LICENSE.txt", b"PyAV licence"),
            ("build", "LICENSE", b"build licence"),
            ("setuptools", "LICENSE", b"setuptools licence"),
            ("cython", "COPYING.txt", b"Cython licence"),
            ("wheel", "LICENSE.txt", b"wheel licence"),
            ("delocate", "LICENSE", b"delocate licence"),
        )
    }
    report = {
        "evidence": {"dependencies": ["libavcodec.dylib", "libwebp.dylib"]},
        "identity": "abc123",
    }
    return {
        "target": MACOS,
        "identity": "abc123",
        "source_archives": sources,
        "manifest_path": _write(tmp_path / "manifest.toml", b"schema_version = 1\n"),
        "provenance_path": _write(
            tmp_path / "av.whl.provenance.json", b'{"identity":"abc123"}\n'
        ),
        "report_path": _write(
            tmp_path / "verification-report.json",
            (json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n").encode(),
        ),
        "commands": (
            ("cmake", "-S", "libwebp", "-DBUILD_SHARED_LIBS=ON"),
            (
                "env",
                "sh",
                "-c",
                "../ffmpeg/configure --disable-gpl --enable-libwebp",
            ),
        ),
        "tool_versions": {
            "build": "1.6.0",
            "setuptools": "84.0.0",
            "cython": "3.3.0",
            "wheel": "0.48.0",
            "delocate": "0.13.0",
        },
        "compiler_evidence": "Apple clang version 17.0.0\ncmake version 4.1.1\n",
        "licence_files": licences,
    }


def test_archive_contains_exact_sources_and_complete_rebuild_evidence(
    tmp_path: Path,
) -> None:
    inputs = _archive_inputs(tmp_path)

    result = create_compliance_archive(tmp_path / "output", **inputs)

    with tarfile.open(result) as archive:
        names = archive.getnames()
        assert names == sorted(names)
        assert archive.extractfile("sources/ffmpeg-8.0.1.tar.xz").read() == (
            b"exact ffmpeg source"
        )
        assert archive.extractfile("sources/libwebp-1.6.0.tar.gz").read() == (
            b"exact libwebp source"
        )
        assert archive.extractfile("sources/av-16.1.0.tar.gz").read() == (
            b"exact pyav source"
        )
        assert archive.extractfile("manifest.toml").read() == b"schema_version = 1\n"
        assert archive.extractfile("changes.diff").read() == b""
        assert (
            b"ffmpeg/configure --disable-gpl"
            in archive.extractfile("build/commands.txt").read()
        )
        assert (
            b'"build":"1.6.0"' in archive.extractfile("build/tool-versions.json").read()
        )
        assert (
            b"Apple clang version"
            in archive.extractfile("build/compiler-versions.txt").read()
        )
        assert archive.extractfile("dependency-inventory.txt").read() == (
            b"libavcodec.dylib\nlibwebp.dylib\n"
        )
        assert (
            b"python scripts/build_media_stack.py --force"
            in archive.extractfile("REBUILD.md").read()
        )
        assert "verification-report.json" in names
        assert "av.whl.provenance.json" in names
        assert "licences/delocate/LICENSE" in names
        assert not any(name.endswith(".whl") for name in names)
        assert not any("tool-venv" in name for name in names)


def test_archive_bytes_are_deterministic_and_metadata_is_normalized(
    tmp_path: Path,
) -> None:
    inputs = _archive_inputs(tmp_path)

    first = create_compliance_archive(tmp_path / "first", **inputs)
    second = create_compliance_archive(tmp_path / "second", **inputs)

    assert first.read_bytes() == second.read_bytes()
    with tarfile.open(first) as archive:
        assert all(
            (member.mtime, member.uid, member.gid, member.uname, member.gname)
            == (0, 0, 0, "", "")
            for member in archive.getmembers()
        )
