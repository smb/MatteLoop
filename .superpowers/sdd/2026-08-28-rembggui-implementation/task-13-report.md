# Task 13 report — timeline-first application shell

## Outcome

Task 13 adds the initial one-window desktop shell: a pure immutable-state
presenter, narrow UI ports, compact source identity/recovery surfaces, shared
Original/Result stage, timeline reserve, continuous inspector, and fixed action
shelf. `MainWindow(store, services, settings, parent=None)` renders the initial
snapshot and owns exactly one store subscription, which it removes on close.

The presenter imports only `rembggui.core.state`. Qt widgets only translate
clicks into frozen UI commands; no Qt shell module imports jobs, model/session,
workspace, rembg, or ONNX modules. The normal app launch is lazy behind
`_run_gui()`, preserving the pre-Qt version and smoke paths.

State visibility and primary emphasis come from reducer-built `AppState` plus
`capabilities(state)`: active/non-ready states have no primary action, a current
preview promotes Render, and valid artifacts alone do not. Preview retry failure
keeps the older result stale. The inspector persists only five strict-boolean
disclosure keys; the window persists only geometry.

IBM Plex Sans Regular/SemiBold and IBM Plex Mono Regular, with OFL-1.1, were
obtained from IBM's official Plex repository. The runtime loader anchors through
the existing direct `model-manifest.json` resource lookup, rejects symlinks, and
falls back truthfully when the packaged set is unavailable.

No user-owned untracked `.agents/`, `AGENTS.md`, or `emoteScript` files were
edited or staged. No SAM, cloud, remote, `withoutBg`, or unrelated job work was
introduced.

## TDD evidence

The initial RED command failed at collection exactly because `rembggui.ui` did
not exist. Production modules were then implemented minimally against those
tests. A later RED test proved that the normal `main([])` path did not delegate
to the lazy GUI seam; `_run_gui()` made that test green. Contract tests cover
the reducer-built visible-state matrix, stale retry, command translation,
subscription cleanup, empty/minimum layout, long-path accessibility, strict
settings fallback, packaged-font fallback, and AST import boundaries.

## Verification

```text
QT_QPA_PLATFORM=offscreen .venv/bin/pytest -q tests/ui tests/test_app_smoke.py tests/test_resources.py
63 passed

.venv/bin/ruff check src/rembggui/ui src/rembggui/app.py tests/ui tests/test_app_smoke.py
All checks passed

.venv/bin/ruff format --check src/rembggui/ui src/rembggui/app.py tests/ui tests/test_app_smoke.py
14 files already formatted

.venv/bin/mypy src
Success: no issues found in 38 source files

QT_QPA_PLATFORM=offscreen .venv/bin/python -m rembggui --smoke-test
ok: true (outside sandbox; POSIX shared memory is sandbox-blocked)
```

The initial Task 13 handoff left full-suite controller verification pending
because its terminal stream detached before returning a final status. The
review follow-up reran that suite outside the sandbox; the result is recorded
below with the required shared-memory qualification.

## Task 13 review follow-up

The follow-up closes the application-shell review findings without widening
the processing scope. The reducer presentation now gives preview errors
precedence over unavailable-model copy, names stale categories visibly, marks
the result with dynamic `status`/checkerboard properties, and preserves the
exact edited-cut copy `Model preview — rebuild uses edited cut frames`.

The fixed 104 px shelf contains only `Preview Frame` and `Render Video`.
Render completion is a separate banner immediately above it with the exact
`Render complete` announcement, middle-elided artifact path, full tooltip and
accessible description, plus `Open output` and `Open folder`. Rebuild and its
workspace recovery controls live in the inspector scroll content; an edited-cut
validation error opens the workspace and focuses a visible recovery button.

The source target now accepts exactly one existing local video URL (`.mp4`,
`.mov`, `.webm`, or `.mkv`) and dispatches a typed `VideoDropped(Path)` command;
picker replacement dispatches `ChooseVideoRequested(replace=True)`. Invalid,
remote, multiple, directory, and unsupported drops are ignored. Source file
size display uses immutable probe metadata (`metadata.revision.size`) rather
than a UI-thread filesystem stat.

Original/result/error/success/recovery targets are keyboard focusable with the
two-pixel focus style and explicit tab order. Required accessible names and
dynamic Prepare & Preview naming are covered by pytest-qt tests. Wheel force
includes and native deployment data entries now carry the one canonical font
directory and OFL notice alongside the existing model resources; a wheel build
verified all six packaged resource paths.

### Follow-up TDD and verification

The review regression suite was first run RED before implementation:

```text
QT_QPA_PLATFORM=offscreen .venv/bin/pytest -q tests/ui/test_task13_review_fixes.py
11 failed, 3 passed
```

After the fixes:

```text
QT_QPA_PLATFORM=offscreen .venv/bin/pytest -q tests/ui/test_task13_review_fixes.py
36 passed

QT_QPA_PLATFORM=offscreen .venv/bin/pytest -q tests/ui tests/test_app_smoke.py tests/test_resources.py
101 passed

.venv/bin/ruff check src/rembggui/ui src/rembggui/app.py src/rembggui/resources.py tests/ui tests/test_resources.py
All checks passed

.venv/bin/ruff format --check src/rembggui/ui src/rembggui/app.py src/rembggui/resources.py tests/ui tests/test_resources.py
16 files already formatted

.venv/bin/mypy src
Success: no issues found in 38 source files

.venv/bin/python -m build --wheel --outdir /tmp/rembggui-task13-wheel
Successfully built rembggui-0.1.0-py3-none-any.whl

QT_QPA_PLATFORM=offscreen .venv/bin/pytest -q  # outside sandbox for shared memory
1076 passed, 15 warnings

QT_QPA_PLATFORM=offscreen .venv/bin/python -m rembggui --smoke-test  # outside sandbox
ok: true (spawn/shared-memory roundtrip, video decode, WebP alpha)
```

Native platform builds were not executed. No SAM, cloud, remote, or
`withoutBg` scope was introduced.
