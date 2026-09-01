# Qt/PySide LGPL Source Companion Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make every successful unsigned native build produce and bundle the exact Qt/PySide LGPL installation information and publish the app only beside verified media and Qt source/checksum pairs.

**Architecture:** Add a strict, focused `scripts.qt_source` companion builder that reuses the existing pinned source-download primitive and creates a deterministic archive from exact installed-package inventory plus committed legal/relink evidence. Integrate its frozen result into the existing native build flow without locks, journals, cross-artifact rollback, or recovery state; keep the separate media repair-evidence correction bounded to `scripts/media_stack` and a dedicated guarded fix commit.

**Tech Stack:** Python 3.13, stdlib `tomllib`/`tarfile`/`gzip`/`hashlib`, pytest, PySide6 6.10.3, Nuitka/pyside6-deploy, existing media source/cache primitives.

---

### Task 1: Define and strictly load the exact Qt source manifest

**Files:**
- Create: `packaging/qt-source/manifest.toml`
- Create: `scripts/qt_source.py`
- Create: `tests/qt_source/test_manifest.py`

**Step 1: Write failing behavior tests**

Cover the exact three source names, versions, archive roots, official HTTPS URLs, and SHA-256 digests. Prove missing/extra keys, malformed digests, wrong roots, floating versions, and non-HTTPS URLs fail closed. Prove raw manifest byte changes invalidate identity.

**Step 2: Run the focused tests to verify RED**

Run: `uv run pytest -q tests/qt_source/test_manifest.py`

**Step 3: Implement the smallest typed loader and identity model**

Use frozen dataclasses and strict key equality. Keep recipe revision explicit. Identity must include raw manifest bytes, the exact four-distribution inventory, and every evidence path/byte pair.

**Step 4: Run focused tests to GREEN and commit**

Run: `uv run pytest -q tests/qt_source/test_manifest.py`

Commit the manifest/loader behavior with a substantive non-`fix:` body.

### Task 2: Build and validate the deterministic source companion

**Files:**
- Modify: `scripts/qt_source.py`
- Create: `tests/qt_source/test_companion.py`
- Create: `legal/GPL-3.0.txt`
- Create: `legal/LGPL-3.0.txt`
- Create: `legal/QT-PYSIDE-LGPL-NOTICE.md`
- Create: `legal/RELINK.md`
- Create: `legal/patches/README.md`

**Step 1: Write failing archive/content/cache tests**

With injected source fetches, prove the builder passes exact manifest specs to the existing `ensure_source`, stores original archive bytes, writes canonical source checksums/provenance and exact four-package inventory, includes only approved project evidence, and produces byte-identical normalized gzip/tar output. Prove a corrupted cached companion/checksum is rejected and rebuilt, and a missing/mismatched source fails.

**Step 2: Run focused tests to verify RED**

Run: `uv run pytest -q tests/qt_source/test_companion.py`

**Step 3: Add exact license and installation evidence**

Commit complete GPL-3.0 and LGPL-3.0 texts matching the official Qt Base 6.10.3 archive. Write a prominent notice and practical `RELINK.md` for unsigned macOS and Windows: build from included Qt Base/Image Formats/PySide sources, replace the documented dynamic-library/binding/plugin paths with ABI-compatible 6.10.3 outputs, run the packaged smoke, and distinguish local ad-hoc re-signing from signing/notarization support.

**Step 4: Implement bounded deterministic construction**

Reuse `scripts.media_stack.sources.ensure_source`; use only temporary files, fsync, atomic replace, and existing cleanup. Normalize archive metadata exactly, write the canonical adjacent checksum, and validate cached output before reuse. Add no locks, journals, rollback framework, or recovery state.

**Step 5: Run focused tests to GREEN and commit**

Run: `uv run pytest -q tests/qt_source/test_manifest.py tests/qt_source/test_companion.py`

Commit the companion and legal evidence with a substantive non-`fix:` body.

### Task 3: Require legal files and the exact four Qt/PySide distributions

**Files:**
- Modify: `packaging/pysidedeploy.spec`
- Modify: `scripts/build.py`
- Modify: `tests/test_native_build.py`
- Modify: `tests/release/test_frozen_smoke.py`

**Step 1: Write failing behavior tests**

Require `PySide6`, `PySide6_Essentials`, `PySide6_Addons`, and `shiboken6` to equal 6.10.3 exactly; retain exact Nuitka and ONNX Runtime pins. Prove each missing/wrong Qt distribution reports an error. Prove the packaging spec declares `GPL-3.0.txt`, `LGPL-3.0.txt`, `QT-PYSIDE-LGPL-NOTICE.md`, and `RELINK.md` in the app bundle.

**Step 2: Run focused tests to verify RED**

Run: `uv run pytest -q tests/test_native_build.py tests/release/test_frozen_smoke.py`

**Step 3: Implement exact prerequisites and packaging declarations**

Keep `scripts/build.py` under G6 by delegating companion construction and pair publication helpers to the focused module. Preserve the project 0BSD license declarations.

**Step 4: Run focused tests to GREEN and commit**

Run: `uv run pytest -q tests/test_native_build.py tests/release/test_frozen_smoke.py`

Commit exact prerequisites and in-bundle compliance files with a substantive non-`fix:` body.

### Task 4: Make the Qt companion mandatory in the native build result

**Files:**
- Modify: `scripts/build.py`
- Modify: `scripts/qt_source.py`
- Modify: `tests/test_native_build.py`
- Modify: `tests/qt_source/test_companion.py`

**Step 1: Write failing build/publication behavior tests**

Prove native preparation calls the companion builder with exact installed inventory, malformed/missing companion data fails before Nuitka, failed bundle/smoke validation publishes neither new pair, successful completion writes both canonical source/checksum pairs beside the app, and final success requires all five distribution deliverables.

**Step 2: Run focused tests to verify RED**

Run: `uv run pytest -q tests/test_native_build.py tests/qt_source/test_companion.py`

**Step 3: Integrate the frozen companion result**

Prepare/verify the companion before Nuitka. After existing bundle and smoke gates, publish each independently verified archive/checksum pair with bounded temporary-file/fsync/replace behavior. Do not add cross-pair atomicity, locks, journals, recovery state, upload, or publication.

**Step 4: Run focused tests to GREEN and commit**

Run: `uv run pytest -q tests/test_native_build.py tests/qt_source/test_companion.py tests/release/test_frozen_smoke.py`

Commit native integration with a substantive non-`fix:` body.

### Task 5: Serialize only allowlisted repair environment evidence

**Files:**
- Modify: `scripts/media_stack/builder.py`
- Modify: `scripts/media_stack/manifest.py`
- Modify: `tests/media_stack/test_builder.py`
- Modify: `tests/media_stack/test_manifest.py`
- Modify: `tests/media_stack/test_compliance.py`

**Step 1: Write failing behavior tests**

Prove macOS repair command evidence begins with exactly `env DYLD_LIBRARY_PATH=${STAGING}/prefix/lib MACOSX_DEPLOYMENT_TARGET=13.0`, retains the pinned tool-Python/delocate command and existing flags, and contains neither an inherited DYLD tail nor a sentinel environment secret. Prove the real subprocess environment still preserves inherited values and prepends the staging library. Prove Windows evidence is unchanged and recipe revision invalidates prior identities.

**Step 2: Run focused tests to verify RED**

Run: `uv run pytest -q tests/media_stack/test_builder.py tests/media_stack/test_manifest.py tests/media_stack/test_compliance.py`

**Step 3: Implement the bounded correction**

Allow `_run_command` to accept a separate sanitized recorded command or equivalent focused value without logging the actual inherited environment. Increment the explicit builder recipe revision. Do not change subprocess behavior, repair flags, Windows construction, verifier, or compliance gates.

**Step 4: Run focused and required guardrails**

Run:

```sh
uv run pytest -q tests/media_stack/test_builder.py tests/media_stack/test_manifest.py tests/media_stack/test_compliance.py tests/media_stack/test_platforms.py tests/media_stack/test_verifier.py tests/test_native_build.py
uv run ruff check .
uv run mypy src
uv run python scripts/check_guardrails.py
```

**Step 5: Commit the authorized fix separately**

Use `fix: record sanitized delocate environment` with a substantive body and a `Trigger:` line naming the failing regression test and review reproduction. If another subsequent change to this file is needed, stop for controller review.

### Task 6: Update documentation without widening qualification claims

**Files:**
- Modify: `README.md`
- Modify: `THIRD_PARTY_NOTICES.md`
- Modify: `docs/building.md`
- Modify: `.superpowers/sdd/2026-08-31-lgpl-media-stack/task-8-report.md`
- Modify if required: `pyproject.toml`

**Step 1: Add publication-boundary assertions where suitable**

Extend existing release/native tests to prove documentation names both source/checksum pairs and forbids claims of Windows qualification, actual-host macOS 15 launch, signing, or notarization where a stable assertion is valuable.

**Step 2: Document the exact contract**

Explain the automatic Qt companion, exact source URLs/digests, CA-bundle
invocation, cache identity/location, four exact installed packages, all five
inseparable deliverables, verification commands, bundle legal/relink files, and
manual nonpublishing Actions boundary. State that the macOS 26 host built media
dylibs for 13.0 but the complete app requires macOS 15 because of measured
bundled PySide6 bindings, was not launched on an actual macOS 15 host, leaves
Windows unqualified, and treats patents separately. Do not add measurements
until qualification runs.

**Step 3: Run documentation/release tests and commit**

Run: `uv run pytest -q tests/release/test_frozen_smoke.py tests/test_native_build.py tests/qt_source`

Commit docs with a substantive body.

### Task 7: Perform exact repository and native qualification

**Files:**
- Modify from actual evidence only: `README.md`, `docs/building.md`, `THIRD_PARTY_NOTICES.md`, `.superpowers/sdd/2026-08-31-lgpl-media-stack/task-8-report.md`

**Step 1: Run the exact full repository gate**

Run: `uv run ruff check . && uv run mypy src && QT_QPA_PLATFORM=offscreen uv run pytest -q`

Also run the committed engineering guardrail/G6 checker. Record exact counts/durations/status.

**Step 2: Force a fresh media build**

Run the documented exact `scripts/build_media_stack.py --force` JSON contract,
setting `SSL_CERT_FILE` to the environment's CA bundle if required; measure wall time. Inspect `build/commands.txt` for the exact sanitized repair env, verify checksums, wheel tag, committed verifier result/fixture output, and Mach-O minos evidence. Stop before any new code correction if a new defect appears.

**Step 3: Prove the media cache hit**

Run the same builder without `--force`; measure time and confirm identical identity/paths, no compilation, full re-verification.

**Step 4: Build the real app and Qt companion**

Run `scripts/build.py` with the verified media cache; measure time. Verify app,
media archive/checksum, Qt companion/checksum, exact original source hashes,
normalized inventory/provenance, legal/relink files inside companion and app,
bundle gate, packaged offline smoke, and direct Mach-O target evidence.

**Step 5: Record only measured results and rerun final verification**

Update docs/report with actual durations, byte sizes, identities, output names, checks, and status. Run the exact full gate again, `git diff --check`, and `git status --short`.

**Step 6: Commit qualification evidence and report**

Stage only Task 8 files (never `.venv` or unrelated dirty files), commit with a substantive docs body, and report commits, exact evidence, artifact sizes, qualification boundaries, and adjacent issues. Do not upload, publish, release, sign, or notarize.
