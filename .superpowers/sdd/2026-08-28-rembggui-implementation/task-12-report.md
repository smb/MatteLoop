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

Focused GREEN for every changed contract and service suite before the final
cleanup audit:

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

A final cleanup audit then added two focused REDs: failure to remove the
hard-link no-clobber candidate after a successful commit was silently ignored,
and failure to remove a framed-PNG temporary after a primary write error was
also swallowed. The publisher now returns the first condition through artifact
notes, while the second is attached to the primary exception. The primary
publication/write outcome is never replaced. Final service GREEN was 40 passed;
the subsequent full suite was 888 passed.

An independent boundary review then drove a second strict RED/GREEN hardening
round. Six deterministic public reproductions established the gaps before any
implementation change:

- identical Preview fingerprints for distinct prepared model-weight SHA/runtime
  bindings;
- cross-output Rebuild snapshots leaked under the cut workspace's scratch root;
- a candidate-path swap after WebP validation could publish attacker bytes under
  both replace and no-clobber policies;
- auto-fit snapshot/resize loops copied all frames after cancellation was
  requested on the first frame;
- unbounded alpha-matting erosion reached the native/provider boundary; and
- Rebuild falsely reported requested sample times as decoded VFR PTS.

The corresponding GREEN contracts now bind Preview to every content-affecting
prepared identity, track and deduplicate every actual scratch owner, acknowledge
cancellation per auto-fit frame/copy/resize, enforce a public 255 erosion maximum
at domain/wire/child boundaries, and report Rebuild `actual_pts=None` honestly.
The encoder now returns an immutable, non-publicly-constructible
`ValidatedCandidate`: WebP validation runs through a private hard link proven to
name a held file descriptor, with stable file identity, byte size, and SHA-256.
Publication rechecks that descriptor immediately before commit and verifies the
final entry afterward. Replace publication keeps an fsynced sibling rollback
copy and atomically restores it on identity/content mismatch; no-clobber removes
the link it created on mismatch. Candidate handles remain held through the
commit decision and are closed before failure cleanup, including Windows handles
opened with read/write/delete sharing.

The hardened changed-contract batch is **493 passed**; the final repository-wide
suite is **913 passed**.

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
- The encoder never receives the final path. It returns only a descriptor-bound,
  SHA-verified `ValidatedCandidate`. `REPLACE` uses atomic replacement followed
  by descriptor/byte verification and atomic rollback; `CHOOSE_ANOTHER_NAME`
  and `CANCEL` use atomic hard-link no-clobber publication followed by the same
  verification. Actual disk-full, quota, permission, read-only, candidate-swap,
  collision, and publication failures preserve the prior output byte-for-byte.
- `JobContext.commit_if_not_cancelled()` serializes cancellation against the
  one final publish linearization. Artifact identity and cleanup complete before
  that commit, and there is no cancellation checkpoint afterward. Cleanup errors
  are diagnostic notes and never replace the primary error or cancellation.

## Verification

- `uv run pytest -q` — **913 passed in 48.25s** outside the restricted
  shared-memory sandbox.
- Hardened changed contract and orchestration suites — **493 passed in 44.85s**;
  focused Publisher and service gates — **10 passed** and **45 passed**.
- `uv run ruff check .` — passed.
- `uv run ruff format --check` over all 14 fix-round Python files — passed.
- `uv run mypy src` — passed for 29 source files.
- `git diff --check` — passed.

## Residual risk

Ordinary tests deliberately use injected deterministic fakes and never download
models or invoke live ONNX/rembg inference. The exact documented rembg kwargs are
unit-tested at the child boundary, and the real local lossless WebP path is
integration-tested; a manual release qualification with already-cached model
weights remains appropriate before shipping the GUI integration.
