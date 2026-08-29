# Task 10 report — native packaging and offline smoke

## Outcome

Task 10 adds a typed, immutable native-runtime smoke result; a real offline smoke path; portable frozen-resource discovery; a `pyside6-deploy`/Nuitka specification; and a manual-only unsigned four-target release workflow.

The source-level and packaging-contract gates pass. A local macOS arm64 bundle was not completed: the bounded build reached native SCons compilation but exceeded the five-minute local spike budget. No frozen executable existed, so the frozen-executable smoke remains a CI/native-host gate rather than a locally claimed success.

## Audited scope

- `pyproject.toml` and `uv.lock` pin Nuitka 2.8.10 and provide `pip`, which the installed `pyside6-deploy` invokes while validating its configured packages.
- `src/rembggui/app.py` calls `multiprocessing.freeze_support()` and exposes `--smoke-test` as stable JSON with a nonzero structured failure result.
- `src/rembggui/smoke.py` exercises the installed Qt platform and PNG/WebP plugins, creates H.264 media with PyAV, decodes through the production source path, encodes and reopens a real two-frame alpha WebP through the production path, performs an actual `spawn` plus shared-memory round trip, verifies unlinking, optionally uses a deterministic local fake session, and measures at most three simultaneous full-resolution RGBA owners.
- `src/rembggui/smoke_child.py` is an importable spawn target. It attaches to parent-owned shared memory, blocks sockets, optionally mutates bytes through the local fake session, and returns JSON-safe evidence.
- `src/rembggui/core/webp.py` adds only an optional ownership observer to the existing production encoder/validator so the smoke measures the actual media path rather than a duplicate implementation.
- `src/rembggui/resources.py` and the small `ModelCatalog` change resolve the Task 9 manifest and provenance from source, standalone executable, or macOS `Contents/Resources` roots.
- `packaging/entrypoint.py`, `packaging/pysidedeploy.spec`, and `packaging/smoke_child.py` define the native entry point, required Qt/image/native packages and resources, exclusions for tests/model weights/tokens/media, and frozen smoke launcher.
- `.github/workflows/release.yml` is `workflow_dispatch` only and contains exactly Windows x64, macOS Intel, macOS arm64, and Ubuntu x64. It uses Python 3.13, pinned uv, frozen/no-cache dependency installation, unsigned builds, frozen smoke, and artifact upload. It contains no push, publish, signing, secret, or model-download step.
- `tests/test_app_smoke.py` and `tests/release/test_frozen_smoke.py` cover CLI JSON/nonzero behavior, immutable result shape, real runtime boundaries, Qt PNG/WebP round trips, frozen resources, deploy-spec parsing/dry-run, bundle lookup, and workflow policy.

No Task 10 diff adds cloud or withoutBG behavior. The pre-existing catalog cloud declarations remain unchanged; the catalog diff is limited to packaged manifest/provenance lookup. User-owned `.agents/`, `AGENTS.md`, and `emoteScript` were neither edited nor staged.

## TDD and recovery evidence

The inherited recovery tree already contained most Task 10 implementation, so the original missing-feature RED could not honestly be reconstructed. Recoverable RED/GREEN evidence was preserved instead:

1. The first focused run failed because PySide6 6.10 runtime rejects the bytes format token advertised by its stubs; using the runtime-supported string token changed the release suite from 1 failure / 6 passes to 7 passes.
2. New contract tests then failed for the brief-level result accessors, real Qt PNG/WebP saves, and exact no-cache workflow command; implementation changed those 3 failures to a 9-pass release suite.
3. A stricter native-package/resource specification test failed before recursive ONNX Runtime and explicit Pillow PNG/WebP plugin inclusion, then passed together with the installed `pyside6-deploy --dry-run` test.
4. The first full-suite recovery run exposed a leaked `QGuiApplication` that made later `QApplication` tests fail. The smoke now creates `QApplication`; a direct smoke-then-thumbnail regression sequence leaves the downstream GUI cache test passing. The final sandbox full run improved from 612 passes / 40 failures to 618 passes / 34 failures, with every remaining failure caused by the controller sandbox denying POSIX shared-memory creation.

## Verification

- `UV_CACHE_DIR=/private/tmp/rembggui-task10-uv-cache uv lock --check` — passed, 54 packages resolved from the lock.
- `QT_QPA_PLATFORM=offscreen .venv/bin/pytest -q` — 618 passed; 34 environment-blocked failures, all `PermissionError: Operation not permitted` from `multiprocessing.shared_memory` under the sandbox.
- Focused Task 10 tests in the same sandbox — 11 passed; the 2 real spawn/shared-memory cases hit the same sandbox denial.
- Before the final Qt lifecycle hardening, the controller-permitted shared-memory run had all 13 focused tests passing and the real CLI returned `ok: true`, two alpha WebP frames, a spawn/shared-memory round trip with unlink confirmation, fake local session use, and `peak_full_res_rgba_owners: 3`.
- Final direct CLI in the restricted sandbox — correctly emitted structured JSON and exited nonzero at the shared-memory permission boundary.
- `.venv/bin/ruff check .` — passed.
- `.venv/bin/mypy src` — passed for 26 source files.
- Ruff format check over all 10 Task 10 Python files — passed. The whole repository still has 12 pre-existing formatting differences outside Task 10; they were intentionally not rewritten.
- `git diff --check` — passed.
- Installed `pyside6-deploy --dry-run --force` against the restored portable spec — passed.

## Native bundle spike

Two explicitly bounded, offline local probes were attempted; neither invoked GitHub Actions or any remote workflow.

1. The first probe completed Nuitka analysis pass 1 and then the sandbox denied a write to `~/Library/Caches/Nuitka`.
2. Local Nuitka source confirmed `NUITKA_CACHE_DIR` as its supported override. The second probe used `NUITKA_CACHE_DIR=/private/tmp/rembggui-nuitka-cache` and `PIP_NO_INDEX=1`, passed the former cache boundary, and reached native SCons compilation. Nuitka displayed an optional ccache download prompt; it was never accepted and no download was performed. At the five-minute boundary the agent-created deployment output was removed, terminating the bounded build without a bundle.

No native-build success is claimed. The deployment directory, distribution directory, crash report, Task 10 bytecode caches, and the exact temporary Nuitka cache were removed. The deploy tool's environment-specific spec rewrite was also restored to the portable checked-in form.
