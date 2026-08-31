# LGPL Media Stack Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build, verify, cache, and package a reproducible LGPL-compatible PyAV/FFmpeg/libwebp stack for MatteLoop's macOS arm64 and Windows x64 native artifacts.

**Architecture:** A checked-in manifest pins all source and build-tool inputs. A small `scripts.media_stack` package downloads and stages those inputs, builds a shared libwebp and FFmpeg, produces a repaired PyAV wheel, verifies its runtime metadata and native dependency graph, and emits a matching compliance archive. The existing native packager consumes only that verified wheel; the manually dispatched GitHub Actions matrix invokes the same local commands and re-verifies cache hits.

**Tech Stack:** CPython 3.13, PyAV 16.1.0, FFmpeg 8.0.1, libwebp 1.6.0, CMake, Xcode Command Line Tools, MSVC 2022, MSYS2, `build` 1.6.0, `setuptools` 84.0.0, Cython 3.3.0, `wheel` 0.48.0, `delocate` 0.13.0, `delvewheel` 1.13.0, Nuitka 2.8.10, pytest.

**Spec:** `docs/superpowers/specs/2026-08-31-lgpl-media-stack-design.md`

## Global Constraints

- Read `docs/engineering-guardrails.md` and `docs/v1-scope.md` before implementation; they override this plan if repository policy changes.
- V1 native targets are macOS 13+ arm64 and Windows x64 only; Linux, macOS x64, and universal artifacts remain deferred.
- MatteLoop's own source, documentation, and assets remain 0BSD.
- Native release artifacts must not contain GPL, non-free, x264, x265, or OpenH264 components.
- FFmpeg and libwebp remain dynamically linked, separately identifiable `.dylib` or `.dll` files.
- Preserve MP4/MOV H.264 and H.265 decoding and lossless animated alpha WebP output.
- Do not add H.264/H.265 encoding, a media CLI runtime dependency, or platform-specific media APIs.
- The stock PyAV wheel may remain in the development environment but must never be accepted by `scripts/build.py` for a native artifact.
- Cache identity includes manifest bytes, builder revision, target OS/architecture, CPython ABI, and macOS deployment target. Every cache hit is re-verified.
- No new source module may exceed 800 lines and no function may exceed 60 lines.
- Tests describe behavior, not task numbers or review rounds.
- Preserve all unrelated dirty-worktree changes. Before modifying `pyproject.toml`, `uv.lock`, `README.md`, `docs/building.md`, `packaging/pysidedeploy.spec`, or `THIRD_PARTY_NOTICES.md`, inspect and merge the user's existing edits rather than replacing them.
- Every change ends with `uv run ruff check . && uv run mypy src && QT_QPA_PLATFORM=offscreen uv run pytest -q` before completion.

## File Structure

| File | Responsibility |
|---|---|
| `packaging/media-stack/manifest.toml` | Exact source URLs/digests, tool versions, supported targets, required capabilities, and forbidden components |
| `scripts/media_stack/manifest.py` | Typed manifest loading, validation, and deterministic cache identity |
| `scripts/media_stack/sources.py` | Verified download, safe archive extraction, and source-cache paths |
| `scripts/media_stack/platforms.py` | Host detection and side-effect-free libwebp/FFmpeg/PyAV/repair command construction |
| `scripts/media_stack/verifier.py` | Wheel metadata, FFmpeg configuration, codec, format, and native dependency verification |
| `scripts/media_stack/builder.py` | Build orchestration, provenance-sidecar creation, cache promotion, and `MediaStackArtifacts` result |
| `scripts/media_stack/compliance.py` | Target-specific compliance inventory and archive creation |
| `scripts/build_media_stack.py` | Thin builder CLI |
| `scripts/verify_media_stack.py` | Thin verifier CLI |
| `scripts/build.py` | Native application build; obtains and packages only verified custom PyAV files |
| `tests/media_stack/` | Focused unit and integration contracts for the new build subsystem |
| `tests/fixtures/codecs/` | Authored tiny H.264/H.265 V1 decode fixtures and provenance |

---

### Task 1: Pin and validate the media-stack manifest

**Files:**
- Create: `packaging/media-stack/manifest.toml`
- Create: `scripts/media_stack/__init__.py`
- Create: `scripts/media_stack/manifest.py`
- Create: `tests/media_stack/__init__.py`
- Create: `tests/media_stack/test_manifest.py`
- Modify: `.gitignore`

**Interfaces:**
- Produces: `SourceSpec`, `ToolVersions`, `VerificationContract`, `MediaStackManifest` frozen dataclasses.
- Produces: `load_manifest(path: Path) -> MediaStackManifest`.
- Produces: `media_stack_identity(manifest_path: Path, *, os_name: str, machine: str, python_tag: str, deployment_target: str, builder_revision: int = 1) -> str`.
- Consumes: Python 3.13 `tomllib`, `hashlib`, and `dataclasses` only.

- [ ] **Step 1: Write failing manifest tests**

Create tests that load the real manifest, assert its exact pinned inputs, reject a malformed SHA, reject a floating URL/version, reject unsupported targets, and prove every identity input changes the digest:

```python
from pathlib import Path

import pytest

from scripts.media_stack.manifest import load_manifest, media_stack_identity


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "packaging" / "media-stack" / "manifest.toml"


def test_manifest_pins_the_lgpl_media_sources() -> None:
    manifest = load_manifest(MANIFEST)
    assert [(source.name, source.version, source.sha256) for source in manifest.sources] == [
        ("ffmpeg", "8.0.1", "05ee0b03119b45c0bdb4df654b96802e909e0a752f72e4fe3794f487229e5a41"),
        ("libwebp", "1.6.0", "e4ab7009bf0629fd11982d4c2aa83964cf244cffba7347ecd39019a9e38c4564"),
        ("pyav", "16.1.0", "a094b4fd87a3721dacf02794d3d2c82b8d712c85b9534437e82a8a978c175ffd"),
    ]
    assert manifest.targets == ("macos-arm64", "windows-x64")
    assert manifest.verification.required_codecs == ("h264", "hevc", "libwebp_anim")
    assert manifest.verification.required_formats == ("mov", "webp")


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
    assert first == media_stack_identity(**common, builder_revision=1)
    assert first != media_stack_identity(**common, builder_revision=2)
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```sh
uv run pytest tests/media_stack/test_manifest.py -q
```

Expected: collection fails because `scripts.media_stack.manifest` does not exist.

- [ ] **Step 3: Add the exact manifest**

Use this data shape and values:

```toml
schema_version = 1
targets = ["macos-arm64", "windows-x64"]
python_abi = "cp313"
macos_deployment_target = "13.0"

[[sources]]
name = "ffmpeg"
version = "8.0.1"
url = "https://ffmpeg.org/releases/ffmpeg-8.0.1.tar.xz"
sha256 = "05ee0b03119b45c0bdb4df654b96802e909e0a752f72e4fe3794f487229e5a41"
archive_root = "ffmpeg-8.0.1"

[[sources]]
name = "libwebp"
version = "1.6.0"
url = "https://storage.googleapis.com/downloads.webmproject.org/releases/webp/libwebp-1.6.0.tar.gz"
sha256 = "e4ab7009bf0629fd11982d4c2aa83964cf244cffba7347ecd39019a9e38c4564"
archive_root = "libwebp-1.6.0"

[[sources]]
name = "pyav"
version = "16.1.0"
url = "https://files.pythonhosted.org/packages/78/cd/3a83ffbc3cc25b39721d174487fb0d51a76582f4a1703f98e46170ce83d4/av-16.1.0.tar.gz"
sha256 = "a094b4fd87a3721dacf02794d3d2c82b8d712c85b9534437e82a8a978c175ffd"
archive_root = "av-16.1.0"

[tools]
build = "1.6.0"
setuptools = "84.0.0"
cython = "3.3.0"
wheel = "0.48.0"
delocate = "0.13.0"
delvewheel = "1.13.0"

[verification]
required_codecs = ["h264", "hevc", "libwebp_anim"]
required_formats = ["mov", "webp"]
forbidden_tokens = ["--enable-gpl", "--enable-nonfree", "libx264", "libx265", "libopenh264"]
forbidden_library_fragments = ["x264", "x265", "openh264"]
```

- [ ] **Step 4: Implement strict typed loading and identity**

Use frozen, slotted dataclasses. Require HTTPS URLs, 64 lowercase hex digest characters, exact source names once each, exact tool keys, only the two supported targets, and `cp313`. Compute the identity from raw manifest bytes plus a canonical JSON object containing the five explicit identity fields; return the first 24 hex characters of SHA-256 for readable cache paths.

```python
@dataclass(frozen=True, slots=True)
class SourceSpec:
    name: str
    version: str
    url: str
    sha256: str
    archive_root: str


def media_stack_identity(..., builder_revision: int = 1) -> str:
    payload = manifest_path.read_bytes() + b"\0" + json.dumps(
        {
            "builder_revision": builder_revision,
            "deployment_target": deployment_target,
            "machine": machine.lower(),
            "os_name": os_name,
            "python_tag": python_tag,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:24]
```

Add `.matteloop-build-cache/` to `.gitignore`; do not change or delete the existing `.matteloop-build-*` directory.

- [ ] **Step 5: Run focused tests and guardrail check**

Run:

```sh
uv run pytest tests/media_stack/test_manifest.py -q
uv run python scripts/check_guardrails.py
```

Expected: all manifest tests pass and guardrails pass.

- [ ] **Step 6: Commit the manifest slice**

```sh
git add .gitignore packaging/media-stack/manifest.toml scripts/media_stack/__init__.py scripts/media_stack/manifest.py tests/media_stack
git commit -m "build: pin the LGPL media stack inputs" -m "Record exact PyAV, FFmpeg, and libwebp sources with verified SHA-256 digests and fixed build-tool versions.

Add strict manifest validation and a platform-sensitive cache identity without changing the runtime dependency environment."
```

---

### Task 2: Stage verified sources and construct platform commands

**Files:**
- Create: `scripts/media_stack/sources.py`
- Create: `scripts/media_stack/platforms.py`
- Create: `tests/media_stack/test_sources.py`
- Create: `tests/media_stack/test_platforms.py`

**Interfaces:**
- Consumes: `SourceSpec`, `MediaStackManifest` from Task 1.
- Produces: `BuildTarget(os_name: str, machine: str, target_id: str, python_tag: str, deployment_target: str)`.
- Produces: `detect_target(...) -> BuildTarget`.
- Produces: `ensure_source(source: SourceSpec, cache_dir: Path, *, opener: UrlOpener = urllib.request.urlopen) -> Path`.
- Produces: `extract_source(archive: Path, destination: Path, expected_root: str) -> Path`.
- Produces: `libwebp_commands(target, source_dir, build_dir, prefix) -> tuple[tuple[str, ...], ...]`.
- Produces: `ffmpeg_commands(target, source_dir, build_dir, prefix) -> tuple[tuple[str, ...], ...]`.
- Produces: `pyav_build_command(target, source_dir, prefix, output_dir, python: Path) -> tuple[str, ...]`.
- Produces: `repair_wheel_command(target, wheel, prefix, output_dir, python: Path) -> tuple[str, ...]`.

- [ ] **Step 1: Write failing source-cache tests**

Cover a successful streamed download, checksum mismatch with no promoted file, reuse of an already valid archive, replacement of an invalid cached archive, and rejection of an archive whose top-level directory differs from `archive_root`. Use an injected opener returning `io.BytesIO`; no unit test reaches the network.

```python
def test_source_download_is_promoted_only_after_digest_matches(tmp_path: Path) -> None:
    payload = b"verified source"
    source = SourceSpec(
        name="fixture",
        version="1",
        url="https://example.invalid/fixture.tar.gz",
        sha256=hashlib.sha256(payload).hexdigest(),
        archive_root="fixture-1",
    )
    result = ensure_source(source, tmp_path, opener=lambda _url: closing(BytesIO(payload)))
    assert result.read_bytes() == payload
    assert not tuple(tmp_path.glob("*.part"))
```

- [ ] **Step 2: Write failing platform-command tests**

Assert:

- macOS maps only `darwin/arm64` to `macos-arm64` and Windows maps only `win32/AMD64` to `windows-x64`;
- libwebp is shared and all CLI/example targets are disabled while `WEBP_BUILD_LIBWEBPMUX=ON`;
- FFmpeg includes `--disable-static`, `--enable-shared`, `--disable-programs`, `--disable-doc`, `--disable-autodetect`, `--disable-gpl`, `--disable-nonfree`, and `--enable-libwebp`;
- none of the five forbidden tokens appears in the constructed FFmpeg command other than the two explicit disabling flags;
- Windows FFmpeg uses `--toolchain=msvc` through `msys2 -c`; macOS configures directly;
- repair uses `delocate-wheel` on macOS and `delvewheel repair --add-path <prefix>/bin` on Windows.

- [ ] **Step 3: Run focused tests and verify RED**

Run:

```sh
uv run pytest tests/media_stack/test_sources.py tests/media_stack/test_platforms.py -q
```

Expected: collection fails because `sources.py` and `platforms.py` do not exist.

- [ ] **Step 4: Implement verified download and safe extraction**

Stream into a sibling `.part` file, hash while writing, `flush()` and `os.fsync()`, then `Path.replace()` only after the digest matches. Use `tarfile.extractall(filter="data")` into an empty task-owned directory and require exactly the expected top-level directory. Honor the process trust store and `SSL_CERT_FILE`; do not create a custom unverified SSL context.

- [ ] **Step 5: Implement exact platform commands**

The libwebp CMake configure command must include:

```text
-DBUILD_SHARED_LIBS=ON
-DWEBP_LINK_STATIC=OFF
-DWEBP_BUILD_ANIM_UTILS=OFF
-DWEBP_BUILD_CWEBP=OFF
-DWEBP_BUILD_DWEBP=OFF
-DWEBP_BUILD_GIF2WEBP=OFF
-DWEBP_BUILD_IMG2WEBP=OFF
-DWEBP_BUILD_VWEBP=OFF
-DWEBP_BUILD_WEBPINFO=OFF
-DWEBP_BUILD_LIBWEBPMUX=ON
-DWEBP_BUILD_WEBPMUX=OFF
-DWEBP_BUILD_EXTRAS=OFF
```

The FFmpeg configure command must use the isolated prefix for include, lib, and `PKG_CONFIG_PATH`, retain default built-in decoders/demuxers, and enable no external library except libwebp. The PyAV command uses the pinned tool environment created later by the builder; on Windows invoke `setup.py build_ext --ffmpeg-dir=<prefix> bdist_wheel`, and on macOS use `python -m build --wheel --no-isolation` with `PKG_CONFIG_PATH=<prefix>/lib/pkgconfig`.

- [ ] **Step 6: Run focused tests**

Run:

```sh
uv run pytest tests/media_stack/test_sources.py tests/media_stack/test_platforms.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit the source and platform slice**

```sh
git add scripts/media_stack/sources.py scripts/media_stack/platforms.py tests/media_stack/test_sources.py tests/media_stack/test_platforms.py
git commit -m "build: stage LGPL media sources reproducibly" -m "Download source archives through the configured trust store, verify their pinned digests, and extract only their declared roots.

Describe shared libwebp, LGPL FFmpeg, PyAV, and wheel-repair commands for the two supported native targets."
```

---

### Task 3: Verify wheel metadata, capabilities, and native dependencies

**Files:**
- Create: `scripts/media_stack/verifier.py`
- Create: `scripts/verify_media_stack.py`
- Create: `tests/media_stack/test_verifier.py`

**Interfaces:**
- Consumes: `BuildTarget`, `MediaStackManifest`.
- Produces: `RuntimeEvidence` and `VerificationReport` frozen dataclasses.
- Produces: `configuration_errors(evidence: RuntimeEvidence, contract: VerificationContract) -> tuple[str, ...]`.
- Produces: `forbidden_bundle_entries(root: Path, fragments: tuple[str, ...]) -> tuple[Path, ...]`.
- Produces: `provenance_path(wheel: Path) -> Path`, returning `<wheel-name>.provenance.json`.
- Produces: `verify_media_wheel(wheel: Path, manifest_path: Path, target: BuildTarget, *, fixture_dir: Path | None = None) -> VerificationReport`.
- CLI: `python scripts/verify_media_stack.py WHEEL --manifest PATH --report PATH` exits nonzero on any error and writes canonical JSON only on success.

- [ ] **Step 1: Write failing pure verifier tests**

Use constructed evidence to prove that the validator accepts LGPL 2.1+ metadata with the required codecs/formats, rejects every forbidden configure token case-insensitively, rejects `GPL`/`nonfree` licence strings, reports missing H.264/HEVC/WebP capabilities together, and finds forbidden filenames recursively. Add provenance tests that reject a missing sidecar, a wrong manifest identity, a wrong target, and a wheel SHA-256 that does not match the sidecar.

```python
def test_configuration_rejects_gpl_and_x26x_components() -> None:
    evidence = RuntimeEvidence(
        ffmpeg_version="8.0.1",
        configurations=("--enable-shared --enable-gpl --enable-libx264",),
        licenses=("GPL version 3 or later",),
        codecs=("h264", "hevc", "libwebp_anim"),
        formats=("mov", "webp"),
        dependencies=("libx264.164.dylib",),
    )
    errors = configuration_errors(evidence, load_manifest(MANIFEST).verification)
    assert any("--enable-gpl" in error for error in errors)
    assert any("libx264" in error for error in errors)
    assert any("GPL version" in error for error in errors)
```

- [ ] **Step 2: Run verifier tests and verify RED**

Run:

```sh
uv run pytest tests/media_stack/test_verifier.py -q
```

Expected: collection fails because `scripts.media_stack.verifier` does not exist.

- [ ] **Step 3: Implement pure evidence validation**

Normalize metadata to lowercase only for comparisons while preserving original evidence in reports. Require all reported FFmpeg libraries to use an LGPL licence string and explicitly reject any string containing `gpl` without the `l` prefix or `nonfree`. Check dependency basenames and full dependency lines.

- [ ] **Step 4: Implement isolated wheel inspection**

Extract the wheel to a temporary directory, place that directory and repository `src/` first in `PYTHONPATH`, and run the current CPython in a subprocess that:

```python
import json
import av

assert "site-packages/av" not in av.__file__.replace("\\", "/")
print(json.dumps({
    "ffmpeg_version": av.ffmpeg_version_info,
    "library_meta": av._core.library_meta,
    "codecs": sorted(av.codecs_available),
    "formats": sorted(av.formats_available),
}, sort_keys=True))
```

Replace the illustrative path assertion with an exact check that `av.__file__` resolves below the extracted wheel root. Collect native dependencies with `delocate-listdeps --all` on macOS and `python -m delvewheel show` on Windows. Do not use `ctypes`, Win32 calls, or `/proc` parsing.

Before extraction, require the adjacent canonical JSON provenance sidecar. It contains `identity`, `manifest_sha256`, `target_id`, `python_tag`, `wheel_filename`, and `wheel_sha256`; recompute and compare every value. The sidecar is the binding between the cached binary and the manifest that authorized it.

When `fixture_dir` is provided, the subprocess must call MatteLoop's `probe_source` and `decode_frame` for both committed fixtures and call `encode_lossless_webp` plus `validate_webp` for two generated RGBA frames.

- [ ] **Step 5: Implement the thin verifier CLI**

The wrapper imports only `main` from `scripts.media_stack.verifier` and exits with its status. Errors go to stderr as one heading plus bullet lines; successful JSON uses sorted keys and a trailing newline.

- [ ] **Step 6: Run focused tests and type-check scripts**

Run:

```sh
uv run pytest tests/media_stack/test_verifier.py -q
uv run ruff check scripts/media_stack scripts/verify_media_stack.py tests/media_stack
```

Expected: all checks pass.

- [ ] **Step 7: Commit the verifier slice**

```sh
git add scripts/media_stack/verifier.py scripts/verify_media_stack.py tests/media_stack/test_verifier.py
git commit -m "build: verify LGPL PyAV wheel boundaries" -m "Inspect PyAV runtime metadata, required codec and format capabilities, and transitive native dependencies from the candidate wheel.

Reject GPL, non-free, x264, x265, and OpenH264 evidence before a wheel can enter the native application build."
```

---

### Task 4: Build, cache, and archive the media stack

**Files:**
- Create: `scripts/media_stack/compliance.py`
- Create: `scripts/media_stack/builder.py`
- Create: `scripts/build_media_stack.py`
- Create: `tests/media_stack/test_compliance.py`
- Create: `tests/media_stack/test_builder.py`
- Modify: `pyproject.toml:26-37`
- Modify: `uv.lock`

**Interfaces:**
- Consumes: all Tasks 1-3 interfaces.
- Produces: `MediaStackArtifacts(wheel: Path, provenance: Path, compliance_archive: Path, report: Path, identity: str)` frozen dataclass.
- Produces: `ensure_media_stack(root: Path, cache_dir: Path, *, force: bool = False, runner: CommandRunner = subprocess.run) -> MediaStackArtifacts`.
- Produces: `create_compliance_archive(...) -> Path`.
- CLI: `python scripts/build_media_stack.py [--force] [--cache-dir PATH] [--json]`.

- [ ] **Step 1: Reconcile existing dependency edits**

Run `git diff -- pyproject.toml uv.lock` and preserve every unrelated licensing or packaging edit. Add exact platform repair dependencies to the existing dev group:

```toml
"delocate==0.13.0; sys_platform == 'darwin'",
"delvewheel==1.13.0; sys_platform == 'win32'",
```


- [ ] **Step 2: Write failing builder and compliance tests**

Use a fake runner that records commands and creates declared outputs. Cover:

- cache miss runs libwebp, FFmpeg, PyAV, repair, verify, and compliance stages in order;
- valid cache hit skips compilation but runs verification;
- `force=True` recompiles;
- failed verification never promotes the staging wheel;
- a prior verified wheel survives a failed rebuild;
- provenance sidecar binds the manifest identity, target, Python ABI, filename, and wheel SHA-256;
- compliance archive contains the three original source archives, manifest, `changes.diff`, configure line, tool versions, licences, dependency report, and rebuild instructions;
- canonical `MediaStackArtifacts` paths are inside `<cache>/<identity>/finished/`.

```python
def test_cache_hit_is_reverified_without_recompiling(tmp_path: Path) -> None:
    first_runner = RecordingRunner(materialize=True)
    first = ensure_media_stack(ROOT, tmp_path, runner=first_runner)
    second_runner = RecordingRunner(materialize=True)
    second = ensure_media_stack(ROOT, tmp_path, runner=second_runner)
    assert second == first
    assert second_runner.stage_names == ["verify"]
```

- [ ] **Step 3: Run focused tests and verify RED**

Run:

```sh
uv run pytest tests/media_stack/test_builder.py tests/media_stack/test_compliance.py -q
```

Expected: collection fails because builder and compliance modules do not exist.

- [ ] **Step 4: Implement the isolated tool environment**

Inside the target identity directory create a task-owned tool virtualenv and install exactly:

```text
build==1.6.0
setuptools==84.0.0
Cython==3.3.0
wheel==0.48.0
delocate==0.13.0        # macOS only
delvewheel==1.13.0      # Windows only
```

Use `uv venv --python <current CPython>` and `uv pip install --python <tool-python> ...`. Keep this environment in the target cache so a wheel cache hit does not reinstall it unnecessarily. Installation failure stops the build; it never falls back to globally installed tools.

- [ ] **Step 5: Implement ordered build orchestration**

Use one explicit staging directory per invocation. Run each command with `check=False`, capture the exit status, and raise `MediaStackBuildError(stage, command, returncode)` on failure. Keep command logging readable without printing environment secrets. After wheel repair, write the provenance sidecar from the current manifest/target and the computed wheel digest, then call the verifier. Only after verification succeeds may the builder atomically replace the `finished/` wheel, sidecar, report, and compliance archive. On failure, retain the explicitly named staging directory and include its path in the error for diagnosis.

Do not add locks, journals, recovery replay, or inode binding. This is a single build process; sibling temporary output plus atomic rename is the durability ceiling.

- [ ] **Step 6: Implement target-specific compliance archives**

Create `MatteLoop-media-sources-<target>-<identity>.tar.gz` with stable member ordering and normalized metadata. Include source archives unchanged, effective build commands, target/compiler versions, empty `changes.diff` when no patch exists, PyAV/FFmpeg/libwebp/build/setuptools/Cython/wheel/delocate-or-delvewheel licence files, the provenance sidecar, verifier JSON, dependency inventory, and `REBUILD.md`. Do not include the tool virtualenv or compiled wheel.

- [ ] **Step 7: Implement the thin builder CLI**

Default cache path is `<repo>/.matteloop-build-cache/media-stack`. `--json` prints:

```json
{"compliance_archive":"...","identity":"...","provenance":"...","report":"...","wheel":"..."}
```

All values are absolute paths. Non-JSON success prints the reused/built identity and paths; errors identify the failed stage and exit nonzero.

- [ ] **Step 8: Run focused tests and lock validation**

Run:

```sh
uv lock --check
uv run pytest tests/media_stack/test_builder.py tests/media_stack/test_compliance.py -q
uv run ruff check scripts/media_stack scripts/build_media_stack.py tests/media_stack
```

Expected: all checks pass.

- [ ] **Step 9: Commit the builder slice**

Stage only the dependency lines that belong to this task plus the new media-stack files; confirm `git diff --cached --name-status` before committing.

```sh
git add pyproject.toml uv.lock scripts/media_stack/builder.py scripts/media_stack/compliance.py scripts/build_media_stack.py tests/media_stack/test_builder.py tests/media_stack/test_compliance.py
git commit -m "build: produce the LGPL media wheel" -m "Build shared libwebp and FFmpeg from pinned sources, compile and repair PyAV, and promote only a verified wheel into the local cache.

Emit a target-specific compliance archive containing exact sources, build evidence, licences, and reconstruction instructions."
```

---

### Task 5: Add codec fixtures and remove the smoke test's GPL encoder

**Files:**
- Create: `tests/fixtures/codecs/generate.py`
- Create: `tests/fixtures/codecs/README.md`
- Create: `tests/fixtures/codecs/h264-sdr.mp4`
- Create: `tests/fixtures/codecs/h265-sdr.mp4`
- Create: `tests/media_stack/test_codec_fixtures.py`
- Modify: `src/matteloop/smoke.py:244-267`
- Modify: `tests/release/test_frozen_smoke.py:26-55`

**Interfaces:**
- Consumes: existing `probe_source`, `decode_frame`, and smoke pipeline.
- Produces: `_SMOKE_VIDEO_ENCODER = "mpeg4"` and an offline smoke video that requires no x264/x265 encoder.
- Produces: committed 64x48, two-frame, 2 fps, BT.709/sRGB-tagged MP4 fixtures with codec names `h264` and `hevc`.

- [ ] **Step 1: Write failing smoke and fixture tests**

Add behavior tests asserting `_SMOKE_VIDEO_ENCODER == "mpeg4"`, `run_smoke` still reports decoded video and a two-frame alpha WebP, both fixture SHA-256 values are exact, and `probe_source` reports `h264`/`hevc` respectively.

Expected fixture hashes from two repeated deterministic generations with the locked stock PyAV wheel are:

```text
h264-sdr.mp4  0b96a9e0aaf2ebb470bac23a746d98d5b19f5e59f9592f6e1a9e2659eee83064
h265-sdr.mp4  98261fe0d518cd1de414b7b50a6c44b677d59eb99fd7046455a24d7f9c619785
```

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```sh
QT_QPA_PLATFORM=offscreen uv run pytest tests/media_stack/test_codec_fixtures.py tests/release/test_frozen_smoke.py::test_native_smoke_exercises_real_offline_runtime_boundaries -q
```

Expected: fixture files and `_SMOKE_VIDEO_ENCODER` are missing.

- [ ] **Step 3: Add the deterministic fixture generator**

The generator uses the development-only `libx264` and `libx265` encoders, 64x48 RGB frames, rate/time base `2`/`1/2`, `yuv420p`, primaries `1`, transfer `13`, matrix `1`, and limited range `1`. Pixel planes are:

```python
pixels[:, :, 0] = 32 + index * 160
pixels[:, :, 1] = 96
pixels[:, :, 2] = 208 - index * 96
```

For x265 set `x265-params=log-level=error:pools=1:frame-threads=1`. Generate twice in a temporary directory and assert byte-for-byte equality before writing the committed files. The README records the command, authored-content 0BSD licence, dimensions/timing/color metadata, exact hashes, and states that neither encoder library ships in MatteLoop.

- [ ] **Step 4: Switch packaged smoke generation to MPEG-4**

Add `_SMOKE_VIDEO_ENCODER = "mpeg4"` near `_FRAME_SIZE` and change only `container.add_stream` to use it. Keep MP4, frame content, metadata, production probe/decode, and WebP checks unchanged.

- [ ] **Step 5: Run focused tests**

Run:

```sh
QT_QPA_PLATFORM=offscreen uv run pytest tests/media_stack/test_codec_fixtures.py tests/release/test_frozen_smoke.py -q
```

Expected: tests pass without network access.

- [ ] **Step 6: Commit the fixture and smoke slice**

```sh
git add src/matteloop/smoke.py tests/release/test_frozen_smoke.py tests/fixtures/codecs tests/media_stack/test_codec_fixtures.py
git commit -m "test: make native smoke independent of GPL encoders" -m "Generate the packaged smoke source with FFmpeg's built-in MPEG-4 encoder while retaining the production decode and animated-WebP checks.

Add authored H.264 and H.265 fixtures with reproducible provenance for explicit V1 decoder qualification."
```

---

### Task 6: Feed only the verified wheel into the native app build

**Files:**
- Modify: `scripts/build.py:22-27,47-97,155-191,237-304,307-339`
- Modify: `tests/test_native_build.py:15-177`
- Modify: `tests/release/test_frozen_smoke.py:519-606`

**Interfaces:**
- Consumes: `ensure_media_stack(...) -> MediaStackArtifacts` from Task 4.
- Consumes: `verify_media_wheel(...) -> VerificationReport` from Task 3.
- Produces: `extract_wheel_package(wheel: Path, destination: Path) -> Path`, returning the extracted `av/` directory.
- Produces: `bundle_media_errors(artifact: Path, target: BuildTarget, contract: VerificationContract) -> tuple[str, ...]`.
- CLI additions: `scripts/build.py --rebuild-media-stack` and `scripts/build.py --media-wheel PATH`; both still require verification.

- [ ] **Step 1: Write failing native-build tests**

Update `_installed_versions()` in the test to omit `av`; assert `prerequisite_errors` no longer checks the development PyAV distribution. Add tests proving:

- default build preparation requests `ensure_media_stack`;
- an explicit wheel is verified and extracted but never installed into `.venv`;
- a wheel without exactly one top-level `av/` package is rejected;
- `prepare_temporary_spec` receives the extracted custom `av/` directory;
- forbidden library names in the finished bundle fail before smoke;
- the compliance archive and its `.sha256` file are copied beside the app artifact;
- no path calls `_distribution_directory("av")`.

Use dependency injection into a new `prepare_media_stack(...)` helper rather than launching compilers from unit tests.

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```sh
uv run pytest tests/test_native_build.py tests/release/test_frozen_smoke.py::test_release_workflow_has_only_manual_unsigned_native_builds -q
```

Expected: failures show that the current build still obtains `av` from the installed distribution.

- [ ] **Step 3: Remove stock PyAV from native prerequisites**

Delete only the `av` key and `_version_matches` branch from `scripts/build.py`. PyAV remains in `pyproject.toml` for source runs and tests. Native packaging readiness is now proven by the custom wheel verifier.

- [ ] **Step 4: Implement verified wheel extraction and selection**

`--media-wheel PATH` requires the adjacent `<wheel-name>.provenance.json` and calls `verify_media_wheel` directly. Default mode calls `ensure_media_stack`; `--rebuild-media-stack` passes `force=True`. Extract the wheel with `zipfile.ZipFile.extractall` into the existing task-owned temporary build directory, require `av/__init__.py` plus at least one `.so` or `.pyd`, and pass that path to the unchanged temporary-spec mechanism.

- [ ] **Step 5: Add the final bundle gate and compliance copy**

After Nuitka returns success but before smoke, recursively scan the artifact and its native dependency output for the manifest's forbidden fragments. Copy the already-created compliance archive to `dist/` with a target-specific name and write its SHA-256 as `<archive>.sha256`. Any media gate error reports all findings and returns nonzero without running smoke.

- [ ] **Step 6: Run focused tests and a spec dry run**

Run:

```sh
uv run pytest tests/test_native_build.py tests/release/test_frozen_smoke.py -q
uv run pyside6-deploy -c packaging/pysidedeploy.spec --dry-run --force
```

Expected: tests pass and the dry run accepts the unchanged nofollow seam.

- [ ] **Step 7: Commit the native packager slice**

```sh
git add scripts/build.py tests/test_native_build.py tests/release/test_frozen_smoke.py
git commit -m "build: package only the verified media wheel" -m "Replace the installed PyAV package as a native-build input with the cached or explicitly supplied LGPL candidate wheel.

Recheck the final bundle, preserve the existing Nuitka nofollow seam, and place matching compliance evidence beside the application."
```

---

### Task 7: Build and cache the stack in GitHub Actions

**Files:**
- Modify: `.github/workflows/release.yml`
- Modify: `tests/release/test_frozen_smoke.py:564-606`

**Interfaces:**
- Consumes: `scripts/build.py` automatic media-stack preparation from Task 6.
- Produces: manual two-target workflow with immutable toolchain action SHAs and a target-specific media-stack cache.

- [ ] **Step 1: Extend the workflow contract test and verify RED**

Require these immutable actions and behaviors:

```text
actions/cache@27d5ce7f107fe9357f9df03efb73ab90386fccae       # v5.0.5
ilammy/msvc-dev-cmd@a102174a2b586eec2ea151a69e6fd14404a8ce7c  # v1.13.0
msys2/setup-msys2@fb197b72ce45fb24f17bf3f807a388985654d1f2   # v2.29.0
```

Assert the cache path is `.matteloop-build-cache/media-stack`, its key includes `runner.os`, `matrix.target`, and `hashFiles('packaging/media-stack/manifest.toml', 'scripts/media_stack/**/*.py', 'scripts/build_media_stack.py', 'scripts/verify_media_stack.py')`, and no step skips verification on a cache hit. Preserve the manual trigger, read-only permissions, unsigned policy, two-target matrix, and existing upload name/path.

- [ ] **Step 2: Run the workflow test and verify RED**

Run:

```sh
uv run pytest tests/release/test_frozen_smoke.py::test_release_workflow_has_only_manual_unsigned_native_builds -q
```

Expected: missing cache and Windows toolchain steps.

- [ ] **Step 3: Add target toolchain setup**

For macOS install `cmake`, `nasm`, and `pkg-config` with Homebrew only if absent. For Windows, run the pinned MSVC environment action with `arch: amd64`, then the pinned MSYS2 action with `msystem: MSYS`, `path-type: inherit`, and packages `base-devel`, `make`, `diffutils`, `nasm`, and `pkgconf`. Keep these steps guarded with `if: runner.os == ...`.

- [ ] **Step 4: Add the media-stack cache and build evidence**

Place the pinned cache action after toolchain setup and before the native build. Do not use broad restore keys: an exact identity mismatch must compile rather than reuse a near match. Keep `uv sync --frozen --all-groups`, then run the existing `uv run --frozen --no-sync python scripts/build.py`; the script itself ensures and verifies the media stack.

The upload remains `path: dist`, which now includes the application, compliance archive, and checksum. Do not add release creation, signing, secrets, or publication permissions.

- [ ] **Step 5: Run workflow and full release-contract tests**

Run:

```sh
uv run pytest tests/release/test_frozen_smoke.py -q
```

Expected: all workflow and packaging contracts pass.

- [ ] **Step 6: Commit the workflow slice**

```sh
git add .github/workflows/release.yml tests/release/test_frozen_smoke.py
git commit -m "ci: build the LGPL stack for native artifacts" -m "Prepare the macOS and Windows source-build toolchains and cache media wheels under the complete target and recipe identity.

Keep native builds manually dispatched, unsigned, read-only, and coupled to their matching compliance archives."
```

---

### Task 8: Document, qualify on macOS, and run the complete gate

**Files:**
- Modify: `README.md`
- Modify: `docs/building.md`
- Modify: `THIRD_PARTY_NOTICES.md`
- Modify: `packaging/pysidedeploy.spec` only if the concurrent licensing edits have not already included the required notice files
- Test: all existing tests

**Interfaces:**
- Consumes: all prior tasks.
- Produces: documented local and Actions build commands, cache behavior, compliance outputs, source links, current platform qualification status, and publication boundary.

- [ ] **Step 1: Reconcile all concurrent documentation and packaging edits**

Run:

```sh
git diff -- README.md docs/building.md THIRD_PARTY_NOTICES.md packaging/pysidedeploy.spec
```

Preserve the 0BSD work, icon provenance, existing build measurements, and unrelated packaging changes. Do not overwrite entire files.

- [ ] **Step 2: Update the documented build contract**

Document:

- `scripts/build.py` automatically builds or reuses the custom media wheel;
- first cache-miss prerequisites for macOS arm64 and Windows x64;
- `--rebuild-media-stack` forces compilation and `--media-wheel PATH` remains verified;
- exact source versions and verified SHA-256 values;
- cache location and invalidation inputs;
- application, compliance archive, checksum, and verifier report outputs;
- no stock PyAV wheel is publishable as a MatteLoop native artifact;
- GitHub Actions is manual and does not publish, sign, or permanently host sources;
- actual Windows status remains unclaimed until an authorized workflow run succeeds;
- H.264/H.265 patent questions remain separate from LGPL compliance.

- [ ] **Step 3: Run the complete repository verification**

Run exactly:

```sh
uv run ruff check . && uv run mypy src && QT_QPA_PLATFORM=offscreen uv run pytest -q
```

Expected: all checks pass. If failures come from unrelated concurrent changes, report them with evidence; do not modify those areas.

- [ ] **Step 4: Build and verify the real macOS media wheel**


```sh
SSL_CERT_FILE=/opt/homebrew/etc/ca-certificates/cert.pem \
  uv run --frozen --no-sync python scripts/build_media_stack.py --force --json
```

Expected: a repaired macOS arm64 PyAV wheel, successful verification report, and matching compliance archive below `.matteloop-build-cache/media-stack/<identity>/finished/`. Run the command again without `--force`; expected output uses the same paths, skips compilation, and still verifies the wheel.

- [ ] **Step 5: Build and smoke the real macOS application**

Run:

```sh
SSL_CERT_FILE=/opt/homebrew/etc/ca-certificates/cert.pem \
  uv run --frozen --no-sync python scripts/build.py
```

Expected: `dist/MatteLoop.app`, the target-specific compliance archive, its checksum, a clean forbidden-component scan, and a passing packaged offline smoke test. Record measured duration and size in `docs/building.md`; do not estimate them.

- [ ] **Step 6: Inspect the final macOS dependency evidence**

Run the committed verifier against the cached wheel and inspect the app with the committed bundle gate. Confirm report fields contain FFmpeg 8.0.1, `h264`, `hevc`, `libwebp_anim`, `mov`, and `webp`, and contain no forbidden token or dependency. Do not substitute a manual filename-only check for the committed verifier.

- [ ] **Step 7: Commit documentation and qualification evidence**

Stage only related hunks after checking `git diff --cached --name-status`.

```sh
git add README.md docs/building.md THIRD_PARTY_NOTICES.md packaging/pysidedeploy.spec
git commit -m "docs: publish the LGPL native build contract" -m "Document reproducible local and Actions media-stack builds, cache invalidation, compliance outputs, and the stock-wheel publication boundary.

Record measured macOS qualification evidence while leaving Windows explicitly unclaimed until its manual workflow runs."
```

- [ ] **Step 8: Run the final verification after the documentation commit**

Run exactly:

```sh
uv run ruff check . && uv run mypy src && QT_QPA_PLATFORM=offscreen uv run pytest -q
```

Expected: all checks pass. Then run `git status --short` and report any pre-existing unrelated changes separately.

## Authorized follow-up, not part of this plan

Do not dispatch the GitHub Actions workflow, create a GitHub Release, upload public binaries, sign, notarize, or publish source archives without separate user authorization. Once authorized, the first Windows x64 workflow run must prove the same verifier, codec fixtures, Nuitka build, and frozen smoke gates before Windows is described as qualified.
