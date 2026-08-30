# Task 12 report — preview, render, and source-free Rebuild

## Outcome

Task 12 adds the complete local Preview, normal Render, and Rebuild
orchestration. Preview and normal Render share one private cut-frame pipeline;
normal Render validates and durably promotes editable PNG cuts before framing or
encoding; Rebuild validates and snapshots those cuts and performs no source
probe, hash, decode, or segmentation I/O. Both render modes converge on the same
bounded second pass and publish only a validated sibling WebP candidate.

The public services are `PreviewService.preview(request, playhead, context)`,
`RenderService.render(request, context)`, and
`RenderService.rebuild(request, cut_workspace, context)`. Source, prepared local
segmentation, workspace, encoder, disk probe, clock, and output publisher are
explicit injected contracts. Result and artifact records are frozen; Preview
RGBA storage is immutable `bytes`, never a live mutable Pillow owner.

User-owned `.agents/`, `AGENTS.md`, and `emoteScript` were not edited or staged.
No SAM, cloud, remote, model-download, or live-inference scope was introduced.

## TDD evidence

The first service RED failed at collection because `rembggui.jobs.render` and
its services did not exist. Contract RED cycles then exercised matting defaults,
edge-mode transport, canonical cut-key inputs, cancellation linearization,
private promotion snapshots, union CAS, auto-fit cancellation, and ownership.
The animated WebP cycle also reproduced an opaque FFmpeg `ANIM` background around
transparent cropped frames; canonical transparent animation metadata fixed the
real pixel-parity failure.

The service cycles covered preview/render pre-global parity; playhead and VFR
PTS; local versus exact range trim; model/edge rejection before inference;
deterministic local decontamination; half-open sampling and rational delays;
one, two, and many frames; empty/sub-128/impossible framing; ownership; all
cancellation boundaries; child crash pass-through; disk and publish failures;
collision races; immutable Rebuild snapshots; external edits; union invalidation
and CAS loss; and source functions that hard-fail if Rebuild touches them.

Final focused GREEN for every changed contract and service suite:

```text
439 passed in 43.09s
```

The first full-suite run found one stale private helper caller:
`tests/jobs/models/test_session.py` still invoked `_run_rembg` without the new
strict `SegmentOptions`. This was the intended API migration, not a compatibility
default: the test now explicitly selects `standard`. Session, protocol, and
orchestration regression GREEN:

```text
170 passed in 10.91s
```

## Architecture and safety decisions

- `cut_cache_key_inputs()` is the only authoritative cut-input mapping. The
  exact recursively frozen values are stored in `CutManifest`; selected catalog
  edge mode, all pinned rembg 2.0.72 matting defaults, model/weight identity,
  rembg version, and local pipeline versions remain auditable.
- Protocol v2 transports an exact `SegmentOptions` object and rejects unknown
  keys, modes, Boolean integers, and invalid matting ranges at the wire boundary.
  Standard and decontaminate call documented rembg kwargs with alpha matting
  disabled; alpha matting sends the three documented parameters.
- Decontamination is an explicit deterministic local RGBA postprocess in the
  shared pipeline: transparent RGB becomes black and partially transparent,
  black-premultiplied RGB is converted to straight RGB with saturating integer
  half-up rounding. Alpha is unchanged; `post_process_mask` is never substituted.
- Normal Render stages PNGs while incrementally accumulating the union, then
  validates, makes a job-private immutable snapshot, and atomically promotes the
  durable set before framing. Its encoder inputs are private scratch paths only.
- Rebuild derives source/model identity from the manifest, detects edits, makes
  an immutable snapshot, and reads only that snapshot. A stale union is recomputed
  framewise; durable metadata is updated only by hash-guarded CAS, and CAS loss is
  a non-fatal note.
- At most one private cut and one framed image are live during the second pass.
  The ownership tracker also follows encoder validation and auto-fit resizes;
  production integration records peak at most three and current zero.
- The encoder never receives the final path. `REPLACE` uses atomic replacement;
  `CHOOSE_ANOTHER_NAME` and `CANCEL` use atomic hard-link no-clobber publication
  or fail structurally where that portable primitive is unavailable. Actual
  disk-full, quota, permission, read-only, and publication failures preserve the
  prior output byte-for-byte.
- `JobContext.commit_if_not_cancelled()` serializes cancellation against the
  one final publish linearization. Artifact identity and cleanup complete before
  that commit, and there is no cancellation checkpoint afterward. Cleanup errors
  are diagnostic notes and never replace the primary error or cancellation.

## Verification

- `uv run pytest -q` — **886 passed in 45.96s** outside the restricted
  shared-memory sandbox.
- All changed contract and orchestration suites — **439 passed in 43.09s**.
- `uv run ruff check .` — passed.
- `uv run ruff format --check` over all 22 changed Python files — passed.
- `uv run mypy src` — passed for 29 source files.
- `git diff --check` — passed.

## Residual risk

Ordinary tests deliberately use injected deterministic fakes and never download
models or invoke live ONNX/rembg inference. The exact documented rembg kwargs are
unit-tested at the child boundary, and the real local lossless WebP path is
integration-tested; a manual release qualification with already-cached model
weights remains appropriate before shipping the GUI integration.
