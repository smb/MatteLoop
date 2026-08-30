# rembgGUI Complete Implementation Plan

> **Historical (2026-08-30).** This plan is retained so older commit messages
> stay readable. Its task numbering is reference only, and its "V1 complete"
> contract has been superseded. `docs/v1-scope.md` is authoritative for scope;
> `docs/engineering-guardrails.md` is authoritative for working method.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the approved cross-platform PySide6 desktop application that previews and removes video backgrounds with `rembg`, preserves editable cut PNGs, rebuilds lossless animated WebP output, and packages without system Python or external media CLIs.

**Architecture:** `rembggui.core` contains immutable domain state and pure media algorithms, `rembggui.jobs` owns I/O, persistence, downloads, subprocess isolation, and rendering, and `rembggui.ui` renders state and emits typed events. Qt widgets and `QPixmap` remain on the GUI thread; ONNX session creation/inference runs in one spawn-based child process; every render consumes an immutable request and promotes durable cut frames before encoding.

**Tech Stack:** CPython 3.13, PySide6 6.10, PyAV, Pillow, NumPy, `rembg[cpu]` 2.0.72, ONNX Runtime CPU, pytest, pytest-qt, Ruff, mypy, uv, `pyside6-deploy`/Nuitka.

**Spec:** `docs/designs/rembggui-desktop-app.md`

## Global Constraints

- Support Windows 11 x86_64, macOS 13+ arm64 and x86_64, and Ubuntu 22.04 x86_64; builds are separate and unsigned.
- Require Python `>=3.13,<3.14` for development and packaging; end-user artifacts bundle Python and require no external `ffmpeg`, ImageMagick, `img2webp`, Bash, or Python.
- Pin `rembg[cpu]==2.0.72`; do not expose custom model paths or custom ONNX sessions. This release supports Python 3.11–3.13 and includes exactly 15 prompt-free local models.
- Select `birefnet-portrait` on first launch. Model weights live outside the application bundle and are never committed.
- Accept local 8-bit SDR MP4/MOV/WebM/MKV sources within the documented media envelope; timestamp is authoritative for VFR media.
- Output only lossless still/animated WebP in V1; final dimensions are `128..16383`, frame count `1..100000`, and RIFF output remains below 4 GiB.
- Keep at most three full-resolution RGBA frames outside the ONNX process. Timeline caches contain only scaled thumbnails: 64 MiB RAM and 256 MiB disk.
- Persist validated post-segmentation RGBA PNG cuts before encode. Rebuild reads an immutable snapshot and never reruns segmentation.
- All UI state changes flow through one pure reducer. Late worker events are ignored unless their source/job/request IDs match current state.
- Open heavy-job dialogs with asynchronous `QDialog.open()` and `Qt.ApplicationModal`, never `exec()`. Escape/close dispatch one cancel request and the dialog remains until a matching terminal acknowledgement.
- Paint, pointer/touch hit testing, keyboard focus, and `QAccessible` virtual-child rectangles use one immutable `InteractionGeometry` snapshot.
- Ordinary automated tests use synthetic public fixtures and fake model/job adapters. Real model weights, native frozen artifacts, screenreaders, and codec matrices run only in manually started release qualification.
- Do not persist active source, preview, job, or workspace selection. Persist only primitive settings, explicit output directory, window geometry, and inspector disclosure state.
- Develop locally/private by default. Creating or configuring a remote and changing repository visibility require separate user authorization.

## File Map

| Path | Responsibility |
|---|---|
| `src/rembggui/core/specs.py` | Frozen validated sampling/crop/segmentation/framing/output requests. |
| `src/rembggui/core/state.py` | Immutable app state, typed events, reducer, derived capabilities. |
| `src/rembggui/core/errors.py` | Serializable stable error codes and user-facing recovery metadata. |
| `src/rembggui/core/timebase.py` | Rational sampling and WebP delay projection. |
| `src/rembggui/core/fingerprints.py` | Targeted preview/cut/render/source fingerprints. |
| `src/rembggui/core/geometry.py` | Pure crop transforms, alpha union, framing, and UI interaction geometry. |
| `src/rembggui/core/webp.py` | Lossless encode, validation, and bounded size fitting. |
| `src/rembggui/jobs/source.py` | PyAV source probe and exact-frame decode with private containers. |
| `src/rembggui/jobs/thumbnails.py` | Cancelable target-scaled `QImage` thumbnail requests and cache metadata. |
| `src/rembggui/jobs/context.py` | Job identity, cancellation, progress, and exclusive scheduling. |
| `src/rembggui/jobs/segmentation_host.py` | Spawn-safe Pipe/shared-memory protocol and reusable rembg session. |
| `src/rembggui/jobs/models/` | Pinned 15-ID catalog and verified local downloads/cache/sessions. |
| `src/rembggui/jobs/workspace.py` | Durable cuts, manifests, validation, promotion, external-edit detection, snapshots. |
| `src/rembggui/jobs/render.py` | Preview, render, Rebuild, staging, validation, atomic output. |
| `src/rembggui/ui/` | Main window, presenters, canvases, timeline/crop, accessibility, inspector, dialogs. |
| `resources/model-manifest.json` | Pinned catalog metadata and execution classes; no weights or secrets. |
| `tests/` | Pure unit, synthetic integration, pytest-qt contracts, frozen smoke entry points. |
| `packaging/` and `.github/workflows/` | Native deployment specs, release matrix, notices, manual qualification. |

---

### Task 1: Project foundation and executable smoke surface

**Files:**
- Create: `.python-version`
- Create: `pyproject.toml`
- Create: `uv.lock`
- Create: `.gitignore`
- Create: `README.md`
- Create: `src/rembggui/__init__.py`
- Create: `src/rembggui/__main__.py`
- Create: `src/rembggui/app.py`
- Create: `tests/test_app_smoke.py`
- Create: `tests/fixtures/__init__.py`
- Create: `tests/fixtures/media_factory.py`
- Create: `.github/workflows/ci.yml`

**Interfaces:**
- Produces: `rembggui.app.main(argv: Sequence[str] | None = None) -> int`.
- Produces: `tests.fixtures.media_factory.make_video(path: Path, frames: Sequence[Image.Image], fps: Fraction, *, pts: Sequence[Fraction] | None = None, rotation: int = 0) -> Path`.
- Consumes: no earlier task.

- [ ] **Step 1: Write failing package smoke tests**

```python
def test_main_reports_version_without_opening_qt(capsys):
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip().startswith("rembgGUI ")

def test_main_smoke_test_is_headless(capsys):
    assert main(["--smoke-test"]) == 0
    assert "smoke: ok" in capsys.readouterr().out
```

- [ ] **Step 2: Bootstrap the Python 3.13 environment and verify RED**

Run: `uv python install 3.13 && uv venv --python 3.13 && uv sync --all-groups`

Run: `uv run pytest tests/test_app_smoke.py -q`

Expected: collection fails because `rembggui` and `main` do not exist.

- [ ] **Step 3: Add project metadata and minimal entry point**

Use `requires-python = ">=3.13,<3.14"`, src layout, console script `rembggui = "rembggui.app:main"`, runtime dependencies `PySide6~=6.10.1`, `av>=16,<17`, `Pillow>=12.1,<13`, `numpy>=2.3,<3`, `rembg[cpu]==2.0.72`, `platformdirs>=4.3,<5`, and dev dependencies pytest, pytest-qt, Ruff, mypy, psutil, and build tools. `main()` handles `--version` and `--smoke-test` before importing Qt-heavy modules.

- [ ] **Step 4: Add fixture generator, hygiene, README, and CI**

The fixture generator must encode tiny deterministic video through PyAV only. `.gitignore` excludes `.venv/`, model caches, `.rembggui-work/`, build outputs, local media, coverage, and `.worktrees/`, but does not ignore source fixtures generated during tests. CI installs uv, CPython 3.13, runs Ruff, mypy, and pytest with `QT_QPA_PLATFORM=offscreen` on Ubuntu.

- [ ] **Step 5: Verify GREEN and toolchain**

Run: `uv run pytest tests/test_app_smoke.py -q`

Run: `uv run ruff check . && uv run mypy src && uv run pytest -q`

Expected: all commands exit 0 with no warnings.

- [ ] **Step 6: Commit**

```bash
git add .python-version pyproject.toml uv.lock .gitignore README.md src/rembggui tests .github/workflows/ci.yml
git commit -m "feat: scaffold rembgGUI project"
```

### Task 2: Validated immutable domain contracts and structured errors

**Files:**
- Create: `src/rembggui/core/__init__.py`
- Create: `src/rembggui/core/errors.py`
- Create: `src/rembggui/core/specs.py`
- Create: `tests/core/test_specs.py`
- Create: `tests/core/test_errors.py`

**Interfaces:**
- Produces: frozen `SamplingSpec`, `CropSpec`, `SegmentationSpec`, `FramingSpec`, `OutputSpec`, `RenderRequest`.
- Produces: `ErrorCode`, serializable `AppError(Exception)`, and `ValidationError(AppError)`; both expose `code` directly.
- Exact defaults: FPS 15, model `birefnet-portrait`, Start 0, trim false, alpha threshold 2.0%, padding 0, horizontal stretch 1.0, max bytes unset.

- [ ] **Step 1: Write failing validation tests**

```python
@pytest.mark.parametrize("fps", [0, -1, 241])
def test_sampling_rejects_fps_outside_gui_guard(fps):
    with pytest.raises(ValidationError) as exc:
        SamplingSpec(start=Fraction(0), end=Fraction(1), fps=fps)
    assert exc.value.code is ErrorCode.INVALID_SAMPLING

def test_crop_must_be_positive_and_inside_oriented_source():
    with pytest.raises(ValidationError):
        CropSpec(x=90, y=0, width=20, height=10).validate_for(100, 100)

def test_render_request_is_frozen(tmp_path):
    request = valid_render_request(tmp_path)
    with pytest.raises(FrozenInstanceError):
        request.sampling = SamplingSpec(Fraction(0), Fraction(2), 15)
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/core/test_specs.py tests/core/test_errors.py -q`

Expected: import failure for missing core contracts.

- [ ] **Step 3: Implement exact dataclasses and validation**

Use `Fraction` for time, `Decimal` for MiB input conversion, `Path` for source/output, enums for edge mode and collision policy, and frozen dataclasses with validation in explicit class methods. `AppError` contains `code`, `stage`, `message_key`, `technical_detail`, `retry_action`, and optional `job_id`; it must round-trip through JSON primitives.

- [ ] **Step 4: Cover all boundaries**

Add literal tests for `[Start, End)`, crop bounds, alpha `0..100`, padding `>=0`, stretch `>0`, max size `>=0`, filename/path validation, final dimension guards, mutual exclusion of Rebuild/Regenerate, and AppError serialization.

- [ ] **Step 5: Verify GREEN and commit**

Run: `uv run pytest tests/core/test_specs.py tests/core/test_errors.py -q`

```bash
git add src/rembggui/core tests/core/test_specs.py tests/core/test_errors.py
git commit -m "feat: add immutable domain contracts"
```

### Task 3: Pure application reducer and derived capabilities

**Files:**
- Create: `src/rembggui/core/state.py`
- Create: `tests/core/test_state.py`

**Interfaces:**
- Produces: `AppState`, `SourceState`, `PreviewState`, `JobState`, `ArtifactState`, `JobKind`, typed event dataclasses, `reduce(state, event) -> AppState`, and `capabilities(state) -> Capabilities`.
- Invariant: only the reducer changes state; widgets never own independent enablement flags.

- [ ] **Step 1: Write failing transition-table tests**

```python
def test_late_job_result_is_ignored():
    state = running_preview(job_id="new")
    assert reduce(state, PreviewSucceeded(job_id="old", result=preview_result())) is state

def test_cancel_keeps_editor_locked_until_ack():
    cancelling = reduce(running_render(job_id="j1"), CancelRequested(job_id="j1"))
    assert cancelling.job.phase is JobState.CANCELLING
    assert not capabilities(cancelling).can_edit
    idle = reduce(cancelling, CancelAcknowledged(job_id="j1"))
    assert idle.job.phase is JobState.IDLE
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/core/test_state.py -q`

Expected: missing `state` module.

- [ ] **Step 3: Implement reducer, events, and capabilities**

Store active job as `ActiveJob(job_id, kind, phase, stage, initiator_focus)` so operation identity survives `CANCELLING`. Every result event carries its source/request/job identity. Include the compact `event → reducer → AppState → capability` ASCII comment required by the spec.

- [ ] **Step 4: Complete the state matrix tests**

Parameterize Empty, Loading, Source error, Ready/no preview, Model unavailable, Preview running/current/stale/error, preflight warning, Render/Rebuild running, Cancelling, Render complete, and Edited cuts. Assert capabilities and focus target, not implementation fields.

- [ ] **Step 5: Verify GREEN and commit**

Run: `uv run pytest tests/core/test_state.py -q`

```bash
git add src/rembggui/core/state.py tests/core/test_state.py
git commit -m "feat: add reducer and capabilities"
```

### Task 4: Rational sampling, WebP delays, and targeted fingerprints

**Files:**
- Create: `src/rembggui/core/timebase.py`
- Create: `src/rembggui/core/fingerprints.py`
- Create: `tests/core/test_timebase.py`
- Create: `tests/core/test_fingerprints.py`

**Interfaces:**
- Produces: `sample_times(start: Fraction, end: Fraction, fps: int) -> tuple[Fraction, ...]`.
- Produces: `webp_delays(frame_count: int, fps: int) -> tuple[int, ...]`.
- Produces: `provisional_source_fingerprint`, `complete_source_sha256`, `preview_fingerprint`, `cut_cache_key`, and `render_fingerprint`.

- [ ] **Step 1: Write failing literal tests**

```python
def test_sampling_is_half_open_and_exact():
    assert sample_times(Fraction(0), Fraction(1, 10), 30) == (
        Fraction(0), Fraction(1, 30), Fraction(1, 15)
    )

def test_sixty_fps_delays_distribute_rounding():
    delays = webp_delays(6, 60)
    assert delays == (17, 16, 17, 17, 16, 17)
    assert sum(delays) == 100

def test_output_path_does_not_stale_segmentation(tmp_path):
    assert preview_fingerprint(request_a(tmp_path)) == preview_fingerprint(request_with_new_output(tmp_path))
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/core/test_timebase.py tests/core/test_fingerprints.py -q`

Expected: missing functions.

- [ ] **Step 3: Implement pure rational algorithms and canonical hashing**

Use integer/Fraction arithmetic only. Hash canonical UTF-8 JSON with sorted keys and schema/version fields. Complete source hashing streams fixed-size chunks and compares size/mtime before and after; a change raises `SOURCE_CHANGED`.

- [ ] **Step 4: Cover VFR/high-rate/single-frame and targeted invalidation**

Test duplicated source selections at high output FPS, exact boundary equality, cumulative delay tolerance, provisional-to-complete promotion, model-weight SHA, orientation/color schema, edge options, and cut/render-only settings.

- [ ] **Step 5: Verify GREEN and commit**

Run: `uv run pytest tests/core/test_timebase.py tests/core/test_fingerprints.py -q`

```bash
git add src/rembggui/core/timebase.py src/rembggui/core/fingerprints.py tests/core
git commit -m "feat: add timebase and fingerprints"
```

### Task 5: Crop, interaction geometry, alpha union, and framing

**Files:**
- Create: `src/rembggui/core/geometry.py`
- Create: `tests/core/test_geometry.py`
- Create: `tests/core/test_framing.py`

**Interfaces:**
- Produces: pure frozen `PointF`, `SizeF`, `RectF`, `MediaTransform`, `InteractionGeometry`, `build_crop_geometry`, `build_timeline_geometry`; `rembggui.core` imports no PySide module.
- Produces: `apply_source_crop`, `alpha_bounds`, `union_alpha_bounds`, `apply_framing`, `solve_proportional_scale`.
- Geometry snapshot contains visual rectangles, pointer/touch hit regions, focus rectangles, accessible screen rectangles, and source/widget/screen converters.

- [ ] **Step 1: Write failing geometry/framing tests**

```python
def test_geometry_uses_one_rect_for_paint_hit_focus_and_accessibility():
    geometry = build_crop_geometry(state=crop_state(), viewport=SizeF(800, 450), dpr=2.0)
    assert geometry.visual["north_west"].center() == geometry.focus["north_west"].center()
    assert geometry.pointer_hit["north_west"].contains(geometry.visual["north_west"])
    assert geometry.accessible_screen["north_west"].size() == geometry.touch_hit["north_west"].size()

def test_union_trim_keeps_identical_canvas_for_every_frame():
    framed = apply_framing(two_rgba_frames(), global_bounds=QRect(2, 3, 8, 9), padding=1, stretch_x=1.0)
    assert [image.size for image in framed] == [(10, 11), (10, 11)]
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/core/test_geometry.py tests/core/test_framing.py -q`

Expected: missing geometry functions.

- [ ] **Step 3: Implement immutable transforms and framing**

Keep all core math independent of PySide; UI adapters convert `RectF` to Qt rectangles. Handle rotation, pixel aspect, letterboxing, zoom, pan, clamp, eight handles, range handles, playhead, and deterministic overlap priority. Use `floor(value + 0.5)` for horizontal stretch width.

- [ ] **Step 4: Add boundary and mutation-resistant cases**

Parameterize 100/150/200% DPR, all rotations, non-square pixels, crop edges, empty alpha, threshold 0/100, sub-128 results, padding, stretch, and max-size proportional scale. Expected rectangles and dimensions are hand-derived literals.

- [ ] **Step 5: Verify GREEN and commit**

Run: `uv run pytest tests/core/test_geometry.py tests/core/test_framing.py -q`

```bash
git add src/rembggui/core/geometry.py tests/core/test_geometry.py tests/core/test_framing.py
git commit -m "feat: add crop and framing geometry"
```

### Task 6: Lossless WebP encoding, validation, and size fitting

**Files:**
- Create: `src/rembggui/core/webp.py`
- Create: `tests/core/test_webp.py`

**Interfaces:**
- Produces: `encode_lossless_webp(frame_paths: Sequence[Path], delays_ms: Sequence[int], destination: Path) -> EncodeSummary`.
- Produces: `validate_webp(path: Path, expected_frames: int, expected_duration_ms: int) -> WebPInfo`.
- Produces: `fit_webp_to_size(source_frame_paths, delays_ms, target_bytes, work_dir, destination) -> Path` with at most 12 iterations and the approved scale step.

- [ ] **Step 1: Write failing still/animated/size tests**

```python
def test_animated_webp_is_lossless_alpha_and_has_expected_duration(tmp_path):
    output = tmp_path / "out.webp"
    encode_lossless_webp(rgba_fixture_paths(tmp_path), (67, 66, 67), output)
    info = validate_webp(output, expected_frames=3, expected_duration_ms=200)
    assert (info.frames, info.loop, info.has_alpha) == (3, 0, True)

def test_impossible_target_preserves_existing_output(tmp_path):
    existing = tmp_path / "out.webp"
    existing.write_bytes(b"known-good")
    with pytest.raises(AppError) as exc:
        fit_webp_to_size(rgba_fixture_paths(tmp_path), (100,), 1, tmp_path / "work", existing)
    assert existing.read_bytes() == b"known-good"
    assert exc.value.code is ErrorCode.IMPOSSIBLE_SIZE
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/core/test_webp.py -q`

Expected: missing encoder module.

- [ ] **Step 3: Implement disk-backed lossless encoding**

Open frame paths lazily, close every image deterministically, distribute exact delay values, write only to sibling temporary files, validate before `os.replace`, enforce dimension/frame/RIFF guards, and never read the complete RGBA sequence into a list of arrays.

- [ ] **Step 4: Implement bounded size fitting and validation cases**

Use `step = min(0.97, sqrt((target_bytes * 0.94) / current_bytes))`, always resize from unscaled post-process frames, stop after 12 iterations, and fail at the 128 px bound. Cover corrupt output, one-frame WebP, odd delay rounding, existing destination, and cleanup.

- [ ] **Step 5: Verify GREEN and commit**

Run: `uv run pytest tests/core/test_webp.py -q`

```bash
git add src/rembggui/core/webp.py tests/core/test_webp.py
git commit -m "feat: add bounded lossless WebP encoding"
```

### Task 7: Source probing, exact-frame decoding, and bounded thumbnails

**Files:**
- Create: `src/rembggui/jobs/__init__.py`
- Create: `src/rembggui/jobs/source.py`
- Create: `src/rembggui/jobs/thumbnails.py`
- Create: `src/rembggui/jobs/cache.py`
- Create: `tests/jobs/test_source.py`
- Create: `tests/jobs/test_thumbnails.py`

**Interfaces:**
- Produces: `SourceInfo`, `DecodedFrame`, `probe_source(path: Path) -> SourceInfo`, `decode_frame(path, timestamp, request_id) -> DecodedFrame`.
- Produces: `ThumbnailRequest(source_id, timestamp, logical_size, dpr, generation)` and `ThumbnailResult(request, image: QImage)`.
- Produces: `ThumbnailDiskCache(max_bytes=256*MiB)` and GUI-side `PixmapCache(max_bytes=64*MiB)` contract.

- [ ] **Step 1: Write failing source and thumbnail tests**

```python
def test_vfr_decode_selects_frame_owning_requested_interval(vfr_video):
    decoded = decode_frame(vfr_video, Fraction(7, 100), request_id=4)
    assert decoded.request_id == 4
    assert decoded.actual_pts == Fraction(1, 20)
    assert decoded.image.getpixel((0, 0)) == (0, 255, 0, 255)

def test_thumbnail_worker_returns_target_scaled_qimage(qtbot, four_k_video):
    request = ThumbnailRequest("s1", Fraction(0), QSize(100, 60), 2.0, generation=3)
    result = generate_thumbnail(four_k_video, request)
    assert result.image.size() == QSize(200, 120)
    assert result.image.width() < 3840
```

- [ ] **Step 2: Verify RED**

Run: `QT_QPA_PLATFORM=offscreen uv run pytest tests/jobs/test_source.py tests/jobs/test_thumbnails.py -q`

Expected: missing jobs modules.

- [ ] **Step 3: Implement private-container probe/decode**

Every call owns and closes its PyAV `InputContainer`. Normalize rotation/pixel aspect to sRGB RGBA, reject HDR/10-bit/audio-only/corrupt/over-limit input with structured errors, and choose the latest frame whose PTS is not after the request. No container crosses a thread.

- [ ] **Step 4: Implement scaled `QImage` thumbnails and two bounded LRUs**

Decode/scale in the worker, return `QImage`, and convert to `QPixmap` only through the GUI cache adapter. Keys include complete/provisional source fingerprint, timestamp, target physical size, generation, and pipeline version. Stale generation/source results are dropped; eviction uses actual byte cost.

- [ ] **Step 5: Cover stale requests, cancellation, cache promotion, and bounds**

Add tests for CFR/VFR, rotation, pixel aspect, Unicode paths, first/last timestamps, stale playhead IDs, resize/DPR generations, 12–48 sampling, RAM 64 MiB and disk 256 MiB eviction, corrupt cache files, and cancellation between frames.

- [ ] **Step 6: Verify GREEN and commit**

Run: `QT_QPA_PLATFORM=offscreen uv run pytest tests/jobs/test_source.py tests/jobs/test_thumbnails.py -q`

```bash
git add src/rembggui/jobs tests/jobs/test_source.py tests/jobs/test_thumbnails.py
git commit -m "feat: add source and thumbnail workers"
```

### Task 8: Exclusive jobs and spawn-safe segmentation protocol

**Files:**
- Create: `src/rembggui/jobs/context.py`
- Create: `src/rembggui/jobs/protocol.py`
- Create: `src/rembggui/jobs/segmentation_host.py`
- Create: `tests/jobs/fake_segmentation_child.py`
- Create: `tests/jobs/test_context.py`
- Create: `tests/jobs/test_segmentation_protocol.py`

**Interfaces:**
- Produces: `JobContext(job_id, kind, workspace, progress_sink, cancellation)` and `ExclusiveJobScheduler`.
- Produces: versioned `SegmentRequest`, `SegmentResponse`, `CancelRequest`, `CancelAck`, `Shutdown` dataclasses containing protocol and job IDs.
- Produces: `SegmentationClient.start()`, `.segment(image, request)`, `.cancel(job_id)`, `.replace_model(model_spec)`, `.close()`.

- [ ] **Step 1: Write failing lifecycle/protocol tests**

```python
def test_scheduler_rejects_second_heavy_job():
    scheduler = ExclusiveJobScheduler()
    with scheduler.claim(JobKind.PREVIEW, "j1"):
        with pytest.raises(AppError) as exc:
            scheduler.claim(JobKind.RENDER, "j2")
    assert exc.value.code is ErrorCode.JOB_ALREADY_RUNNING

def test_child_crash_invalidates_slot_and_restarts_only_on_retry(fake_child):
    client = fake_child(crash_on_request=True)
    with pytest.raises(AppError) as exc:
        client.segment(red_frame(), request("j1"))
    assert exc.value.code is ErrorCode.SEGMENTATION_PROCESS_CRASHED
    assert not client.is_running
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/jobs/test_context.py tests/jobs/test_segmentation_protocol.py -q`

Expected: missing context/protocol modules.

- [ ] **Step 3: Implement exclusive context and cancellation**

Cancellation is cooperative and idempotent. Progress events contain stage, completed, optional total, and detail. A context reaches terminal state exactly once and only a matching `CancelAck` unlocks the scheduler.

- [ ] **Step 4: Implement spawn child, Pipe, and one shared-memory slot**

Use `multiprocessing.get_context("spawn")`; parent creates/unlinks shared memory and validates shape/dtype/byte length before reading. The child owns exactly one rembg session and one request at a time. Include the required ASCII ownership/protocol diagram and `freeze_support()` entry path. Tests inject the deterministic fake child; ordinary tests never load ONNX.

- [ ] **Step 5: Cover crash, mismatch, cancel, cleanup, and replacement**

Test protocol-version mismatch, wrong job ID, double cancel, cancellation delayed through one fake inference, parent/child close, orphan prevention, shared-memory unlink, model replacement terminating the old child, and late responses.

- [ ] **Step 6: Verify GREEN and commit**

Run: `uv run pytest tests/jobs/test_context.py tests/jobs/test_segmentation_protocol.py -q`

```bash
git add src/rembggui/jobs tests/jobs
git commit -m "feat: isolate segmentation jobs"
```

### Task 9: Pinned model catalog, verified acquisition, and session lifecycle

**Files:**
- Create: `resources/model-manifest.json`
- Create: `src/rembggui/jobs/models/__init__.py`
- Create: `src/rembggui/jobs/models/catalog.py`
- Create: `src/rembggui/jobs/models/download.py`
- Create: `src/rembggui/jobs/models/session.py`
- Create: `tests/jobs/models/test_catalog.py`
- Create: `tests/jobs/models/test_download.py`
- Create: `tests/jobs/models/test_session.py`

**Interfaces:**
- Produces: `ExecutionClass = LOCAL` and frozen `ModelSpec`.
- Produces: `ModelCatalog.load_resource()`, `.default_id == "birefnet-portrait"`, `.get(model_id)`.
- Produces: `ModelDownloader.download(spec, destination, progress, cancelled) -> Path`.
- Produces: `ModelSessionManager.prepare(model_id, extras)`, `.remove(model_id)`, `.close()`.

- [ ] **Step 1: Write failing manifest/catalog tests**

```python
def test_manifest_contains_exact_approved_catalog_and_default():
    catalog = ModelCatalog.load_resource()
    assert catalog.default_id == "birefnet-portrait"
    assert set(catalog.ids) == {
        "u2net", "u2netp", "u2net_human_seg", "u2net_cloth_seg", "silueta",
        "isnet-general-use", "isnet-anime", "birefnet-general",
        "birefnet-general-lite", "birefnet-portrait", "birefnet-dis",
        "birefnet-hrsod", "birefnet-cod", "birefnet-massive", "bria-rmbg",
    }

def test_checksum_mismatch_never_promotes_part_file(fake_http, tmp_path):
    spec = local_spec(sha256="00" * 32)
    with pytest.raises(AppError) as exc:
        ModelDownloader(fake_http).download(spec, tmp_path, noop_progress, never_cancel)
    assert exc.value.code is ErrorCode.MODEL_CHECKSUM_MISMATCH
    assert not list(tmp_path.glob("*.part"))
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/jobs/models -q`

Expected: missing catalog modules/resources.

- [ ] **Step 3: Implement pinned manifest and trust boundary**

Record display name, upstream model ID, purpose, execution class, URLs/checksums/sizes for the 15 local models, generic requirements, render support, and license/privacy text. Unknown IDs and custom paths remain disabled. BRIA shows its weight/license warning.

- [ ] **Step 4: Implement atomic download and cache namespaces**

Stream to `<cache>/2.0.72/<model>/<file>.part`, emit byte progress only with known total, check cancellation between chunks, verify SHA-256, fsync, and atomically rename. Offline reuse requires matching hash. Use injected transport in tests; do not contact production URLs.

- [ ] **Step 5: Implement one-session replacement lifecycle**

`prepare()` routes local model changes through `SegmentationClient.replace_model`; active models cannot be removed; version namespace changes never reuse old weights.

- [ ] **Step 6: Verify GREEN and commit**

Run: `uv run pytest tests/jobs/models -q`

```bash
git add resources/model-manifest.json src/rembggui/jobs/models tests/jobs/models
git commit -m "feat: add pinned model lifecycle"
```

### Task 10: Early frozen-runtime and streaming-encoder packaging spike

**Files:**
- Create: `packaging/pysidedeploy.spec`
- Create: `packaging/smoke_child.py`
- Create: `tests/release/test_frozen_smoke.py`
- Create: `.github/workflows/release.yml`
- Modify: `src/rembggui/app.py`

**Interfaces:**
- Produces: `rembggui --smoke-test` checks Qt plugin loading, PyAV decode, two-frame alpha WebP encode/reopen, spawn child, shared-memory cleanup, and optional cached fake session without downloading weights.
- Produces: manually dispatched native matrix for Windows x86_64, macOS x86_64/arm64, Ubuntu x86_64.

- [ ] **Step 1: Write failing smoke test**

```python
def test_smoke_entry_exercises_native_boundaries(tmp_path, capsys):
    result = run_smoke(work_dir=tmp_path, use_fake_model=True)
    assert result.qt and result.pyav and result.webp and result.spawn and result.shared_memory
    assert result.peak_rgba_frames <= 3
```

- [ ] **Step 2: Verify RED**

Run: `QT_QPA_PLATFORM=offscreen uv run pytest tests/release/test_frozen_smoke.py -q`

Expected: `run_smoke` missing.

- [ ] **Step 3: Implement smoke path and deploy specification**

The smoke path uses generated media and a fake model only; it must never reach the network. Configure Nuitka/PySide plugins, package resources, multiprocessing entry point, PyAV libraries/codecs, and exclusions for tests/model weights/local media.

- [ ] **Step 4: Implement manual four-target qualification**

Provide repeatable uv/CPython 3.13 commands for matching native machines, use `pyside6-deploy`, keep model/private-media caches out of artifacts, and run smoke execution on the frozen result. Existing remote workflow configuration is optional and inert until the user separately authorizes remote use; do not sign, notarize, publish, or download the full model catalog in ordinary automated checks.

- [ ] **Step 5: Verify locally and document native gate**

Run: `QT_QPA_PLATFORM=offscreen uv run pytest tests/release/test_frozen_smoke.py -q`

Run: `uv run python -m rembggui --smoke-test`

Expected: local unfrozen smoke passes. Native bundle execution remains a manual platform-specific release gate.

- [ ] **Step 6: Commit**

```bash
git add packaging tests/release .github/workflows/release.yml src/rembggui/app.py
git commit -m "build: add native packaging smoke workflow"
```

### Task 11: Durable cut workspaces and immutable external-edit snapshots

**Files:**
- Create: `src/rembggui/jobs/workspace.py`
- Create: `tests/jobs/test_workspace.py`

**Interfaces:**
- Produces: `CutManifest`, `CutWorkspace`, `stage_cut`, `promote_cut_set`, `validate_cut_set`, `detect_external_edits`, `snapshot_for_rebuild`, `CutWorkspace.read_promoted_cut(index)`, `list_workspaces`, `delete_workspace`.
- Directory contract: `<output>/.rembggui-work/scratch/<job-id>/` disposable; `cuts/<cache-key>/` durable and never auto-deleted.

- [ ] **Step 1: Write failing promotion/edit/snapshot tests**

```python
def test_encode_failure_after_promotion_keeps_valid_cuts(tmp_path):
    workspace = completed_staged_cuts(tmp_path, count=3)
    promoted = promote_cut_set(workspace)
    simulate_encode_failure(promoted)
    assert validate_cut_set(promoted).frame_count == 3

def test_save_during_snapshot_is_rejected(tmp_path):
    cuts = promoted_cuts(tmp_path)
    with pytest.raises(AppError) as exc:
        snapshot_for_rebuild(cuts, tmp_path / "scratch", mutate_during_copy=True)
    assert exc.value.code is ErrorCode.CUTS_CHANGED_DURING_SNAPSHOT
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/jobs/test_workspace.py -q`

Expected: workspace module missing.

- [ ] **Step 3: Implement manifest, staged validation, and atomic promotion**

Manifest contains complete source hash, authoritative cache key inputs, frame sequence, RGBA dimensions, content hashes/mtimes, model/version/schema, union metadata, edited/pinned state, and last use. Promotion validates a sibling staged directory and never destroys an older valid cache before replacement succeeds.

- [ ] **Step 4: Implement edit detection and immutable Rebuild snapshot**

Rescan names/count/dimensions/PNG readability/hashes before every Rebuild. Prefer reflink/copy-on-write, fall back to copy, and compare metadata before/after. Changes after a completed snapshot affect only the next job. Durable cuts survive restart; scratch cleanup is bounded and explicit.

- [ ] **Step 5: Cover corruption, races, cleanup, and management**

Test missing/nonsequential/mismatched frames, corrupt PNG, union invalidation, previous-cache preservation, cancellation, abandoned scratch, 20 GiB warning metadata, explicit deletion, and no automatic durable-cut deletion.

- [ ] **Step 6: Verify GREEN and commit**

Run: `uv run pytest tests/jobs/test_workspace.py -q`

```bash
git add src/rembggui/jobs/workspace.py tests/jobs/test_workspace.py
git commit -m "feat: persist editable cut workspaces"
```

### Task 12: Preview, full render, and Rebuild orchestration

**Files:**
- Create: `src/rembggui/jobs/render.py`
- Create: `tests/jobs/test_preview.py`
- Create: `tests/jobs/test_render.py`
- Create: `tests/jobs/test_rebuild.py`

**Interfaces:**
- Produces: `PreviewService.preview(request, playhead, context) -> PreviewResult`.
- Produces: `RenderService.render(request, context) -> RenderArtifact`.
- Produces: `RenderService.rebuild(request, cut_workspace, context) -> RenderArtifact`.
- Dependency injection: source decoder, segmentation client, workspace, encoder, disk probe, and clock are explicit constructor dependencies.

- [ ] **Step 1: Write failing preview/render parity tests**

```python
def test_preview_matches_render_before_global_framing(services, request):
    preview = services.preview.preview(request, request.sampling.start, job("p1"))
    services.render.render(request, job("r1"))
    assert preview.pre_global_trim_rgba.tobytes() == services.workspace.read_promoted_cut(0).tobytes()

def test_rebuild_does_not_decode_or_segment(services, edited_cuts, request):
    services.render.rebuild(request, edited_cuts, job("b1"))
    assert services.decoder.calls == 0
    assert services.segmenter.calls == 0
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/jobs/test_preview.py tests/jobs/test_render.py tests/jobs/test_rebuild.py -q`

Expected: render services missing.

- [ ] **Step 3: Implement preview with exact shared pipeline**

Freeze request/playhead/fingerprint, decode/orient/crop, send the frame through the segmentation protocol, apply edge/per-frame cleanup, and return pre-global-trim RGBA plus local-bounds estimate or matching cached global bounds.

- [ ] **Step 4: Implement bounded two-pass render and early cut promotion**

Sample only `[Start, End)`, maintain no more than three RGBA frames, stage cuts and incremental union, validate/promote before encode, then read durable/private frames for union framing and lossless encode. Preflight disk/time is advisory; actual disk failure is structured and preserves old output/cuts. Include the required normal-render versus Rebuild ASCII diagram.

- [ ] **Step 5: Implement immutable Rebuild and atomic output collision handling**

Rebuild validates/snapshots cuts, never invokes decode/segmentation, recalculates invalidated union metadata, and applies framing/encode. Existing output requires an explicit collision policy and is replaced only after validated sibling output succeeds.

- [ ] **Step 6: Cover every safe boundary and failure**

Test cancel after decode/segment/cut promotion/framing/encode, child crash, empty alpha, sub-128 result, single frame, impossible max size, actual disk-full, unwritable output, failed rename, stale source, external save race, and old output/cuts preservation.

- [ ] **Step 7: Verify GREEN and commit**

Run: `uv run pytest tests/jobs/test_preview.py tests/jobs/test_render.py tests/jobs/test_rebuild.py -q`

```bash
git add src/rembggui/jobs/render.py tests/jobs
git commit -m "feat: add preview render and rebuild pipelines"
```

### Task 13: Main window, state presenter, inspector, and fixed action shelf

**Files:**
- Create: `DESIGN.md`
- Create: `src/rembggui/ui/__init__.py`
- Create: `src/rembggui/ui/theme.py`
- Create: `src/rembggui/ui/presenter.py`
- Create: `src/rembggui/ui/main_window.py`
- Create: `src/rembggui/ui/source_strip.py`
- Create: `src/rembggui/ui/preview_canvas.py`
- Create: `src/rembggui/ui/inspector.py`
- Create: `src/rembggui/ui/action_shelf.py`
- Create: `resources/fonts/IBMPlexSans-Regular.ttf`
- Create: `resources/fonts/IBMPlexSans-SemiBold.ttf`
- Create: `resources/fonts/IBMPlexMono-Regular.ttf`
- Create: `resources/fonts/OFL.txt`
- Create: `tests/ui/test_state_presentation.py`
- Create: `tests/ui/test_minimum_layout.py`
- Modify: `src/rembggui/app.py`

**Interfaces:**
- Produces: `MainWindow(store, services, settings)` and `present(state: AppState) -> PresentationModel`.
- Produces: stable widget object names and accessible names used by pytest-qt and release checklists.
- UI only dispatches typed events; presenter maps state/capabilities to text, visibility, enablement, focus target, and primary-action property.

- [ ] **Step 1: Write failing state-presentation tests**

```python
@pytest.mark.parametrize("state_name,primary,focus", [
    ("empty", None, "choose_video"),
    ("ready", "preview", "preview_action"),
    ("current", "render", "result_canvas"),
    ("stale", "preview", "preview_action"),
    ("complete", "render", "success_banner"),
])
def test_state_matrix_drives_primary_action_and_focus(qtbot, window, states, state_name, primary, focus):
    window.render_state(states[state_name])
    assert window.primary_action_name() == primary
    assert window.requested_focus_name() == focus
```

- [ ] **Step 2: Verify RED**

Run: `QT_QPA_PLATFORM=offscreen uv run pytest tests/ui/test_state_presentation.py tests/ui/test_minimum_layout.py -q`

Expected: UI modules missing.

- [ ] **Step 3: Implement theme and `DESIGN.md` from the approved contract**

Use exact colors, IBM Plex typography with packaged/fallback handling, spacing, focus ring, checkerboard, 40 px primary controls, immediate disclosure/result changes, and no decorative/card-dashboard styling. Generated mockup text is not authoritative; plan copy is.

- [ ] **Step 4: Implement one-window layout and presentation model**

Use compact source strip, shared side-by-side Original/Result stage, timeline placeholder, continuous 340–400 px scrollable inspector, and fixed 104 px action shelf. Set hard minimum 1100×720. Long paths use middle elision plus tooltip/accessible description. No application horizontal scrollbar.

- [ ] **Step 5: Cover all visible states and layout invariants**

Parameterize every approved state row. Assert visible copy, enabled actions, primary property, stale/current/error markers, focus request, editor lock, inspector width 340 at minimum, timeline reserve at least 176, side-by-side canvases, and visible action shelf. Do not assert pixels/colors through screenshots.

- [ ] **Step 6: Verify GREEN and commit**

Run: `QT_QPA_PLATFORM=offscreen uv run pytest tests/ui/test_state_presentation.py tests/ui/test_minimum_layout.py -q`

```bash
git add DESIGN.md src/rembggui/ui src/rembggui/app.py tests/ui
git commit -m "feat: add timeline-first application shell"
```

### Task 14: Visual timeline, crop editor, keyboard contexts, and accessibility tree

**Files:**
- Create: `src/rembggui/ui/geometry.py`
- Create: `src/rembggui/ui/timeline.py`
- Create: `src/rembggui/ui/crop_canvas.py`
- Create: `src/rembggui/ui/accessibility.py`
- Create: `tests/ui/test_timeline.py`
- Create: `tests/ui/test_crop_canvas.py`
- Create: `tests/ui/test_accessibility.py`
- Create: `tests/ui/test_keyboard_contexts.py`
- Modify: `src/rembggui/ui/main_window.py`

**Interfaces:**
- Produces: `TimelineWidget`, `CropCanvas`, `AccessibleEditorFactory` registered through `QAccessible.installFactory`.
- Consumes: core `InteractionGeometry`, state events, scaled thumbnail results.
- Virtual children: Export start, Export end, Preview frame playhead, Crop rectangle, and eight named crop handles.

- [ ] **Step 1: Write failing interaction/accessibility tests**

```python
def test_timeline_keys_emit_exact_editor_events(qtbot, timeline, event_spy):
    timeline.setFocus()
    qtbot.keyClick(timeline, Qt.Key_Right)
    qtbot.keyClick(timeline, Qt.Key_I)
    assert [type(event) for event in event_spy.events] == [StepFrame, SetStartToPlayhead]

def test_accessibility_tree_exposes_virtual_crop_handles(qtbot, crop_canvas):
    interface = QAccessible.queryAccessibleInterface(crop_canvas)
    names = {interface.child(i).text(QAccessible.Name) for i in range(interface.childCount())}
    assert {"Crop rectangle", "Crop north west", "Crop south east"} <= names
```

- [ ] **Step 2: Verify RED**

Run: `QT_QPA_PLATFORM=offscreen uv run pytest tests/ui/test_timeline.py tests/ui/test_crop_canvas.py tests/ui/test_accessibility.py tests/ui/test_keyboard_contexts.py -q`

Expected: widgets/accessibility missing.

- [ ] **Step 3: Implement timeline and crop from shared geometry**

Timeline shows 12–48 complete-source thumbnails, selected `[Start, End)`, distinct IN/OUT/playhead, excluded dimming, time/frame telemetry, and outside-range label. Crop supports interior drag, eight handles, numeric mirroring, 24 px pointer and 44 px touch hit regions, bounds clamp, and reset. Painting/hit/focus/accessibility never recalculate coordinates independently.

- [ ] **Step 4: Implement contextual keyboard behavior**

Left/Right one exact frame, Shift larger step, I/O set range, crop arrows one source pixel and Shift ten, hold-Space compare only with Result focus. Text fields/buttons/dialogs must retain their normal keys. Bounds/value announcements are coarse, not per mouse move.

- [ ] **Step 5: Implement `QAccessible` virtual children and events**

Expose standard roles, value/action interfaces, current value text, focus, and screen rectangles. Emit focus/value/name/description/visibility/location updates from state/geometry changes. Accessibility is an adapter over immutable state, never a second value store.

- [ ] **Step 6: Verify GREEN and commit**

Run: `QT_QPA_PLATFORM=offscreen uv run pytest tests/ui/test_timeline.py tests/ui/test_crop_canvas.py tests/ui/test_accessibility.py tests/ui/test_keyboard_contexts.py -q`

```bash
git add src/rembggui/ui tests/ui
git commit -m "feat: add accessible timeline and crop editor"
```

### Task 15: Preview integration, guarded job dialog, model manager, and workspace UI

**Files:**
- Create: `src/rembggui/ui/job_dialog.py`
- Create: `src/rembggui/ui/model_manager.py`
- Create: `src/rembggui/ui/workspace_panel.py`
- Create: `src/rembggui/ui/error_dialog.py`
- Create: `tests/ui/test_job_dialog.py`
- Create: `tests/ui/test_preview_flow.py`
- Create: `tests/ui/test_model_manager.py`
- Create: `tests/ui/test_workspace_panel.py`
- Modify: `src/rembggui/ui/main_window.py`

**Interfaces:**
- Produces: parent-owned `JobDialog`, `ModelManagerDialog`, `WorkspacePanel`, structured `ErrorDialog`.
- Consumes: job/model/workspace services through injectable facades and reducer events.

- [ ] **Step 1: Write failing async dialog tests**

```python
def test_escape_requests_cancel_once_and_waits_for_ack(qtbot, job_dialog, store):
    job_dialog.open_for(active_job("j1"))
    qtbot.keyClick(job_dialog, Qt.Key_Escape)
    qtbot.keyClick(job_dialog, Qt.Key_Escape)
    assert store.events.count(CancelRequested(job_id="j1")) == 1
    assert job_dialog.isVisible()
    store.dispatch(CancelAcknowledged(job_id="j1"))
    qtbot.waitUntil(lambda: not job_dialog.isVisible())

def test_preview_success_focuses_result(qtbot, window, fake_services):
    qtbot.mouseClick(window.preview_button, Qt.LeftButton)
    fake_services.preview.succeed(current_preview())
    qtbot.waitUntil(window.result_canvas.hasFocus)
```

- [ ] **Step 2: Verify RED**

Run: `QT_QPA_PLATFORM=offscreen uv run pytest tests/ui/test_job_dialog.py tests/ui/test_preview_flow.py tests/ui/test_model_manager.py tests/ui/test_workspace_panel.py -q`

Expected: integration widgets missing.

- [ ] **Step 3: Implement asynchronous modal lifecycle**

Set `Qt.ApplicationModal`, call `open()`, and keep a parent reference. Guard `reject()` and close events: one cancel event, disabled `Cancelling…`, no close before matching success/failure/CancelAck cleanup. Stage/detail/progress are truthful; unknown totals are indeterminate. Include the required ASCII lifecycle comment.

- [ ] **Step 4: Integrate explicit one-frame Preview and inspection**

Scrubbing updates Original with 100–150 ms decode debounce; only `Preview Frame` runs segmentation. Result supports stale overlay, linked zoom/pan/lens, hold-Space compare, current metadata, local/global-trim estimate labels, and correct focus on success/error.

- [ ] **Step 5: Implement model/workspace management UI**

Show model purpose, LOCAL execution class, size/status/license/privacy, Prepare & Preview, progress/cancel, offline cache, remove guards, and `birefnet-portrait` default. Present the 15 local models together. Workspace UI exposes Open Cut Folder, Show edited cut, Rebuild, Regenerate, size/source/last-use/edited/pinned, validation errors, and explicit deletion.

- [ ] **Step 6: Verify GREEN and commit**

Run: `QT_QPA_PLATFORM=offscreen uv run pytest tests/ui/test_job_dialog.py tests/ui/test_preview_flow.py tests/ui/test_model_manager.py tests/ui/test_workspace_panel.py -q`

```bash
git add src/rembggui/ui tests/ui
git commit -m "feat: integrate preview jobs and workspaces"
```

### Task 16: Deferred — not committed

Deferred SAM prompt exploration is outside V1 and has no delivery commitment;
historical design notes live only in `docs/future-enhancements.md`, and Task 17
is intentionally not renumbered.

### Task 17: End-to-end synthetic qualification, release docs, and final packaging contract

**Files:**
- Create: `tests/integration/test_user_journey.py`
- Create: `tests/integration/test_failure_matrix.py`
- Create: `tests/integration/test_memory_bounds.py`
- Create: `tests/release/test_packaged_resources.py`
- Create: `docs/release/gui-checklist.md`
- Create: `docs/release/native-qualification.md`
- Create: `docs/release/unsigned-artifacts.md`
- Create: `THIRD_PARTY_NOTICES.md`
- Create: `CHANGELOG.md`
- Modify: `README.md`
- Modify: `.github/workflows/ci.yml`
- Modify: `.github/workflows/release.yml`
- Modify: `packaging/pysidedeploy.spec`

**Interfaces:**
- Produces: complete local command `uv run pytest`, launch command `uv run python -m rembggui`, frozen `--smoke-test`, and documented manual release record.
- Consumes all prior public interfaces; no new product behavior.

- [ ] **Step 1: Write failing synthetic end-to-end journeys**

```python
def test_drop_preview_render_edit_rebuild_journey(app_harness, short_mp4, external_png_edit):
    app_harness.load(short_mp4)
    app_harness.set_playhead("00:00:00.400")
    app_harness.preview_with_fake_model()
    artifact = app_harness.render()
    external_png_edit(app_harness.first_cut_path)
    rebuilt = app_harness.rebuild()
    assert rebuilt.read_bytes() != artifact.read_bytes()
    assert app_harness.fake_model.render_calls == artifact.frame_count
    assert app_harness.fake_model.rebuild_calls == 0
```

- [ ] **Step 2: Verify RED**

Run: `QT_QPA_PLATFORM=offscreen uv run pytest tests/integration -q`

Expected: harness/journey gaps identify unfinished integration.

- [ ] **Step 3: Complete only integration wiring exposed by the journeys**

Wire drag/drop/picker equivalence, settings persistence/reset, output directory selection, preflight confirmation, success/open-path actions, close/cancel flow, real external-edit detection, and stable service teardown. Fix integration defects through focused failing tests; do not add new features.

- [ ] **Step 4: Add failure and memory qualification**

Cover every row of the design's Failure-Mode Coverage table with an automated or explicitly manual reference. Measure fake 60/300/3000-frame and 4K paths: no whole-video RGBA retention, at most three full-resolution frames, bounded thumbnails, child/process cleanup, and existing artifact preservation.

- [ ] **Step 5: Finalize packaging/resources/notices/docs**

Ensure fonts/resources/model manifest/Qt plugins/PyAV codecs/multiprocessing entry point are packaged without weights, private media, or generated workspaces. Generate accurate third-party notices. Document unsigned-launch steps, model licenses, local/private development, native qualification, screenreader/DPI checks, and no system CLI requirement.

- [ ] **Step 6: Run the complete verification matrix**

Run: `uv lock --check`

Run: `uv run ruff check .`

Run: `uv run mypy src`

Run: `QT_QPA_PLATFORM=offscreen uv run pytest -q`

Run: `QT_QPA_PLATFORM=offscreen uv run python -m rembggui --smoke-test`

Expected: every command exits 0 with pristine output. Record test counts and peak-memory evidence in the task report.

- [ ] **Step 7: Commit**

```bash
git add tests docs README.md THIRD_PARTY_NOTICES.md CHANGELOG.md packaging .github/workflows
git commit -m "test: qualify complete rembgGUI workflow"
```

## Completion Gate

- Every task has RED and GREEN evidence in its SDD report.
- Every task has an independent spec/quality review with no open Critical or Important finding.
- The final whole-branch review covers `1247030..HEAD` and the approved design spec.
- The plan ledger records every ruling, deferred Minor, commit range, and review verdict.
- Local CPython 3.13 lint/type/test/smoke checks pass freshly.
- Native Windows/macOS/Linux artifacts remain explicitly unclaimed until manually started qualification runs successfully. Any remote execution or publication requires separate user authorization.

## Plan Self-Review

- Spec coverage: Tasks 2–6 cover every script parameter and pure processing invariant; Tasks 7–12 cover source/model/job/cut/render lifecycles; Tasks 13–15 cover every approved UI, accessibility, and modal state; Tasks 10 and 17 cover distribution and qualification. Task 16 is explicitly deferred. No approved V1 requirement is unassigned.
- Placeholder scan: no `TBD`, `TODO`, “implement later,” vague error-handling step, or undefined “similar to” instruction remains.
- Type consistency: downstream tasks consume the exact interfaces declared by earlier tasks; `rembggui.core` remains PySide-free; Qt image/widget ownership begins only in jobs/UI adapters; render parity reads the public promoted-cut workspace API.
- Scope: all tasks produce one desktop application and share the immutable spec/job/media interfaces, so splitting into separate implementation plans would duplicate boundaries and make integration less reliable.
