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
`ValidatedCandidate`, with stable file identity, byte size, and SHA-256.
Candidate handles remain held through the commit decision and are closed before
failure cleanup, including Windows handles opened with read/write/delete sharing.

The hardened changed-contract batch is **493 passed**; the final repository-wide
suite is **913 passed**.

A follow-up publication-race audit proved three remaining pathname gaps with
direct RED tests. A private validation hard link could be swapped between its
identity check and `validate_webp`; a closed rollback pathname could be replaced
before rollback; and no-clobber mismatch cleanup could unlink a concurrent
writer's replacement output. The failures were observed respectively as an
invalid candidate being accepted, attacker bytes replacing the known-good old
output, and the concurrent output disappearing.

The final GREEN boundary is descriptor-based end to end:

- `validate_webp` accepts either a local `Path` or a caller-owned stable binary
  file. RIFF parsing and Pillow decode use a duplicate of that exact descriptor;
  an open caller is neither closed nor repositioned.
- Replace retains the previous output as an open identity/SHA-bound descriptor
  for the whole transaction. Rollback recreates old bytes into a fresh exclusive
  file, fsyncs, reopens it with delete sharing, atomically replaces, and verifies
  the final descriptor identity and SHA. Transient and repeated rollback-path
  substitutions are retried within a fixed bound; terminal interference leaves
  an exact recovery snapshot and a structured `publish-rollback` error.
- No-clobber first copies the validated descriptor into a private, fsynced,
  identity-bound stage and atomically hard-links that stage. If the final entry
  changes afterward, publication fails but never unlinks the now-foreign target.
  Private candidates and stages are removed only when their identities still
  match; ambiguous foreign paths are retained with diagnostics.

The direct race/ownership suite is **9 passed**, the complete WebP and render
orchestration gate is **117 passed**, and the final full suite is **918 passed**.

A third publication review then targeted the remaining file-identity
linearization gaps. Each finding first received a deterministic RED reproducer:

- a caller-owned BinaryIO could return one descriptor during inspection and a
  different descriptor when validation duplicated it;
- REPLACE, no-clobber, and rollback could accept a destination swap occurring
  inside the held-descriptor SHA read because the pathname was checked only
  before that read;
- a failed REPLACE with no previous output blindly unlinked whatever pathname
  occupied the destination during rollback;
- rollback allocated its only durable recovery after destructive commit, so
  ENOSPC or EACCES during recovery could lose the old bytes and return the wrong
  stage/action;
- successful no-clobber publication used hard-link-plus-unlink, leaving a
  check-to-unlink race for the private stage; and
- candidate cleanup had the same impossible-to-make-portable conditional
  pathname deletion.

The GREEN implementation captures and validates `fileno()` exactly once,
immediately duplicates that exact non-Boolean non-negative integer, and performs
all RIFF and Pillow work on the owned duplicate. Every long publication content
check now has the same ordering: held identity plus path identity before SHA,
SHA through the held descriptor, then both identities again. The second path
check is the publication verification linearization point.

REPLACE now creates and fsyncs a durable recovery before its first destructive
rename. A same-filesystem hard link is preferred and a fully copied/fsynced
descriptor snapshot is the fallback. Recovery lives in a mode-0700 private
namespace with two fixed names per destination. The secondary hard-link name
survives interference with the primary name; both are reused when already exact
and replaced by the next successful transaction. Restore attempts copy only
from the held descriptor and never depend on allocating a recovery after failure.
Exhausted restore attempts return `publish-rollback` /
`recover-output` and the exact verified recovery path; hostile replacement of
that path is detected and explicitly reported without claiming that the path
still contains old bytes.

No-clobber publication now consumes a private fsynced stage with the platform's
native exclusive rename: `renamex_np(RENAME_EXCL)` on macOS,
`renameat2(RENAME_NOREPLACE)` on Linux, and Windows
`FILE_RENAME_INFORMATION` with `ReplaceIfExists=false` against an explicitly
bound destination-directory handle. A concurrent target wins without being
modified. Private
publication and recovery slots are fixed per destination, so collisions and
subsequent jobs reuse bounded storage. On POSIX there is no portable
identity-conditional unlink; ambiguous or still-addressable candidate/temp
paths are therefore retained with an exact diagnostic instead of risking
deletion of foreign bytes. Successful native publication consumes its stage,
and REPLACE consumes its candidate.

This round's complete jobs contract gate is **526 passed** and the complete
WebP contract gate is **72 passed**.

A fourth review focused on crash durability and recovery-namespace ownership.
Four deterministic RED reproducers established that an unexpected `tell()`
exception leaked the WebP validation duplicate, a hard-linked recovery name was
not itself file-fsynced, a recovery-directory fsync failure left an unbounded
random pending name, and replacing the lexical recovery directory could redirect
later recovery publication into a foreign namespace.

The GREEN recovery transaction now reuses Task 11's handle-bound directory
infrastructure through a narrow public `RecoveryDirectory` adapter. Parent and
child directory handles remain open for the complete prepare/verify/rollback
critical section. Every recovery lstat, open, create, link, replace, unlink, and
directory sync is relative to those handles; `path_for()` exists only to report
a retained recovery location. POSIX hard links use source and destination
`dir_fd`s. Windows deliberately has no hard-link pathname fallback: it creates a
descriptor-bound copy through the Windows directory-handle API, uses
handle-relative replace, and requires the Task 11 strict directory flush.

Recovery and shadow pending entries are fixed per destination rather than
random, so a failed directory flush leaves at most one bounded entry that the
next transaction reuses handle-relatively. Both hard-linked and copied recovery
files are `fsync`ed through their held regular-file descriptors before their
directory entry is made durable and before the output's destructive commit.
Finally, every exception after `os.dup()` now closes that owned descriptor while
preserving the original exception and attaching any cleanup failure as a note.
Recovery-descriptor ownership transfers to the returned artifact only after
shadow preparation succeeds, so that branch also closes every held descriptor
on failure.

A final output-publication review found that the copied Windows recovery file
used Task 11's intentionally restrictive cache-sharing contract, and that the
outer output parent was still reparsed for candidate commit, no-clobber commit,
and rollback. The review's deterministic RED fakes modelled Windows sharing in
both directions: a held writable pending descriptor rejected subsequent stat,
read, or delete access without reciprocal sharing. POSIX parent-swap REDs then
showed a lexical REPLACE consuming a same-named foreign candidate and a lexical
no-clobber rename consuming a foreign private stage. A redirected private
namespace also surfaced the internal `CUT_WORKSPACE_UNSAFE` error domain.

The final GREEN publisher opens one `PublicationDirectory` before its first
candidate check and keeps that exact parent handle through existing-output
snapshot, recovery-child creation, candidate revalidation, commit, post-commit
verification, no-clobber, rollback, and cleanup. Recovery and publication
children are opened from that already-bound parent; they never reopen the
parent path. POSIX operations use source and destination `dir_fd`s. Windows
publication files use a separate, narrowly scoped READ|WRITE|DELETE sharing
contract, including the held pending descriptor, stable read handles, lstat,
and delete-access rename handles. Existing cache file methods retain their
READ-only sharing and lock guarantees. Windows rename supplies both bound
directory handles to native `FILE_RENAME_INFORMATION`, including the
same-directory REPLACE case; no close-before-rename workaround exists.

Every operational candidate, destination, stage, recovery, and rollback access
inside the transaction is handle-relative. Lexical paths remain only for API
validation and diagnostics. If the parent name is exchanged during a POSIX
transaction, publication either aborts before commit or operates exclusively in
the originally bound directory and detects the renamed parent at postverify;
rollback then restores the original directory without touching the foreign
replacement. Unsafe private-output namespaces are translated at the public
publisher boundary to `INVALID_OUTPUT` / `output` with `retry-output`.

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
  SHA-verified `ValidatedCandidate`. WebP parsing/decoding never reopens its
  pathname. `REPLACE` first persists its bounded recovery anchors, then atomically
  replaces and descriptor-verifies; `CHOOSE_ANOTHER_NAME` and `CANCEL` consume
  a bounded private copy through native exclusive rename. Actual disk-full,
  quota, permission, read-only, candidate/stage/SHA swaps, collision, and
  publication failures cannot report attacker or concurrent bytes as a
  successful output.
- `JobContext.commit_if_not_cancelled()` serializes cancellation against the
  one final publish linearization. Artifact identity and cleanup complete before
  that commit, and there is no cancellation checkpoint afterward. Cleanup errors
  are diagnostic notes and never replace the primary error or cancellation.

## Verification

- `.venv/bin/python -m pytest -q` — **950 passed in 48.31s** outside the restricted
  shared-memory sandbox.
- Focused WebP, render, workspace, and native Windows cache-filesystem contract
  gate — **263 passed in 35.41s**, including parent-namespace swaps and the
  bidirectional Windows-sharing fakes.
- `ruff check .` — passed.
- `ruff format --check` over all 6 changed Python files — passed.
- `mypy src` — passed for 29 source files.
- `git diff --check` — passed.

## Residual risk

Ordinary tests deliberately use injected deterministic fakes and never download
models or invoke live ONNX/rembg inference. The exact documented rembg kwargs are
unit-tested at the child boundary, and the real local lossless WebP path is
integration-tested; a manual release qualification with already-cached model
weights remains appropriate before shipping the GUI integration.
On failed/ambiguous cleanup, a hidden candidate or private temporary may remain
with its diagnostic rather than risk deleting bytes installed by another
actor. Publication/recovery slots are bounded per destination and reused;
unverified encoder candidates are bounded per failed job and can accumulate
until an explicit maintenance pass. This is the deliberate portable safety
tradeoff because POSIX has no atomic conditional unlink by held inode identity.

The Windows `CreateFileW` sharing branch and `MoveFileExW` no-replace flags are
type-checked and covered through injected platform contracts, but this
race-hardening round executed real filesystem mutations on macOS rather than a
Windows or Linux host.
