# Task 10 report — native packaging and offline smoke

## Outcome

Task 10 adds a typed, immutable native-runtime smoke result; a real offline smoke path; portable frozen-resource discovery; a `pyside6-deploy`/Nuitka specification; and a manual-only unsigned four-target release workflow.

The source-level and packaging-contract gates pass. A local macOS arm64 bundle was not completed: the bounded build reached native SCons compilation but exceeded the five-minute local spike budget. No frozen executable existed, so the frozen-executable smoke remains a CI/native-host gate rather than a locally claimed success.

## Audited scope

- `pyproject.toml` and `uv.lock` pin Nuitka 2.8.10, `pip`, and the exact Linux-only patch-tool package required by the installed deploy helper.
- `src/rembggui/app.py` calls `multiprocessing.freeze_support()` and exposes `--smoke-test` as stable JSON with a nonzero structured failure result.
- `src/rembggui/smoke.py` exercises the installed Qt platform and PNG/WebP plugins, creates H.264 media with PyAV, decodes through the production source path, encodes and reopens a real two-frame alpha WebP through the production path, performs an actual `spawn` plus shared-memory round trip, verifies unlinking, optionally uses a deterministic local fake session, and measures at most three simultaneous full-resolution RGBA owners.
- `src/rembggui/smoke_child.py` is an importable spawn target. It attaches to parent-owned shared memory, blocks sockets, optionally mutates bytes through the local fake session, and returns JSON-safe evidence.
- `src/rembggui/core/rgba.py`, `src/rembggui/jobs/source.py`, and `src/rembggui/core/webp.py` provide an optional lifetime ownership ledger across production decode normalization, encode, and pixel validation. Weak-referenceable owners are identity-deduplicated; non-weak PyAV frames remain strongly registered until an exact CPython 3.13 refcount checkpoint proves no external Python owner remains. Codec-native internal buffers remain opaque to Python and are not claimed as measured.
- `src/rembggui/resources.py` and the small `ModelCatalog` change resolve the Task 9 manifest and provenance from source, standalone executable, or macOS `Contents/Resources` roots.
- `packaging/entrypoint.py`, `packaging/pysidedeploy.spec`, and `packaging/smoke_child.py` define the native entry point, required Qt/image/native packages and resources, exclusions for tests/model weights/tokens/media, and frozen smoke launcher.
- `.github/workflows/release.yml` is `workflow_dispatch` only and contains exactly Windows x64, macOS Intel, macOS arm64, and Ubuntu x64. It uses Python 3.13, pinned uv, frozen/no-cache dependency installation, unsigned builds, frozen smoke, and artifact upload. It contains no push, publish, signing, secret, or model-download step.
- `tests/test_app_smoke.py` and `tests/release/test_frozen_smoke.py` cover CLI JSON/nonzero behavior, immutable result shape, real runtime boundaries, Qt PNG/WebP round trips, frozen resources, deploy-spec parsing/dry-run, bundle lookup, and workflow policy.

User-owned `.agents/`, `AGENTS.md`, and `emoteScript` were neither edited nor staged.

## Fix-round evidence

- A six- and twelve-frame scaling test proves the normal production encode/validation path remains constant at three live full-resolution RGBA owners and releases all measured owners afterward.
- A deliberately retaining ledger keeps owners from six frames alive and drives the observed peak above three, proving the measurement is based on actual object lifetime rather than observer argument count.
- Production decode passes the same ledger through PyAV reformat, Pillow transfer, color conversion, pixel-aspect resize, rotation, and the returned frame. A wrapper around the real reformatter proves an externally retained RGBA `VideoFrame` remains counted after its local handle disappears and is pruned only after the external reference is released.
- Shared-memory construction through child completion now has one cleanup scope. Injected pipe, start, send, receive, child, and timeout failures prove endpoints and processes are boundedly cleaned while the created segment is closed and unlinked once; injected close and unlink failures remain attached to the primary error without masking it.
- Resource discovery rejects non-canonical and device-like names, fails when missing, rejects symlinked files/directories, and fails closed when standalone and macOS bundle copies are ambiguous. Production manifest reads are descriptor-bound and verify file and directory identity; returned paths are documented for trusted, read-only packaged resources.
- The manual workflow verifies the exact Linux patch-tool version and executable before invoking the deploy helper with frozen, no-sync resolution.

## TDD and recovery evidence

The inherited recovery tree already contained most Task 10 implementation, so the original missing-feature RED could not honestly be reconstructed. Recoverable RED/GREEN evidence was preserved instead:

1. The first focused run failed because PySide6 6.10 runtime rejects the bytes format token advertised by its stubs; using the runtime-supported string token changed the release suite from 1 failure / 6 passes to 7 passes.
2. New contract tests then failed for the brief-level result accessors, real Qt PNG/WebP saves, and exact no-cache workflow command; implementation changed those 3 failures to a 9-pass release suite.
3. A stricter native-package/resource specification test failed before recursive ONNX Runtime and explicit Pillow PNG/WebP plugin inclusion, then passed together with the installed `pyside6-deploy --dry-run` test.
4. The first full-suite recovery run exposed a leaked `QGuiApplication` that made later `QApplication` tests fail. The smoke now creates `QApplication`; a direct smoke-then-thumbnail regression sequence leaves the downstream GUI cache test passing.
5. The fix round began with 4 ownership-ledger failures, 2 shared-memory cleanup failures, 11 resource-resolution failures, and 2 dependency/workflow failures. A later symlink-escape test also failed before descriptor-bound resource hardening. The focused final run passed all 131 sandbox-compatible release/core/resource/catalog tests; deliberate retention, construction and post-start cleanup failures, and missing/ambiguous/traversal resource cases are included.
6. The decoder follow-up began with 3 focused failures because `decode_frame` and `_normalized_image` did not accept the taskwide ledger. The same tests now prove normal decode peak 3/current 1 then 0 after returned-image drop, retained real PyAV reformat current 2 then 1 then 0, and all 6 RGB-transfer/SAR/rotation owners under deliberate retention.

## Verification

- `UV_CACHE_DIR=/private/tmp/rembggui-task10-fix-uv-cache uv lock --check` — passed, 55 packages resolved from the lock.
- `QT_QPA_PLATFORM=offscreen .venv/bin/pytest -q` after decoder lifetime hardening — 657 passed; 34 environment-blocked failures, all `PermissionError: Operation not permitted` from `multiprocessing.shared_memory` under the sandbox.
- Focused source/ownership/release tests in the same sandbox — 73 passed; the 2 real spawn/shared-memory cases were separately exercised by the full run and hit the same sandbox denial.
- An earlier controller-permitted run established the real Qt/PyAV/WebP/spawn/shared-memory path. The final weak-lifetime ledger and cleanup changes still require the controller's permitted CLI/full rerun; no post-fix native success is claimed here.
- Final direct CLI after decoder lifetime hardening in the restricted sandbox — completed the measured media path, emitted structured JSON, and exited nonzero at the shared-memory permission boundary.
- `.venv/bin/ruff check .` — passed.
- `.venv/bin/mypy src` — passed for 27 source files.
- Ruff format check over the changed Python scope — passed. Pre-existing formatting differences outside Task 10 were intentionally not rewritten.
- `git diff --check` — passed.
- Installed `pyside6-deploy --dry-run --force` against the restored portable spec — passed.

## Native bundle spike

Two explicitly bounded, offline local probes were attempted; neither invoked GitHub Actions or any remote workflow.

1. The first probe completed Nuitka analysis pass 1 and then the sandbox denied a write to `~/Library/Caches/Nuitka`.
2. Local Nuitka source confirmed `NUITKA_CACHE_DIR` as its supported override. The second probe used `NUITKA_CACHE_DIR=/private/tmp/rembggui-nuitka-cache` and `PIP_NO_INDEX=1`, passed the former cache boundary, and reached native SCons compilation. Nuitka displayed an optional ccache download prompt; it was never accepted and no download was performed. At the five-minute boundary the agent-created deployment output was removed, terminating the bounded build without a bundle.

No native-build success is claimed. The deployment directory, distribution directory, crash report, Task 10 bytecode caches, and the exact temporary Nuitka cache were removed. The deploy tool's environment-specific spec rewrite was also restored to the portable checked-in form.
