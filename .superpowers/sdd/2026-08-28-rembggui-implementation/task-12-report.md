# Task 12 report — preview, render, and source-free Rebuild

## Outcome

Task 12 adds the complete local Preview, normal Render, and Rebuild
orchestration. Preview and normal Render share one private cut-frame pipeline;
normal Render validates and durably promotes editable PNG cuts before framing or
encoding; Rebuild validates and snapshots those cuts and performs no source
probe, hash, decode, or segmentation I/O. Both render modes converge on the same
bounded second pass and publish only a descriptor-validated WebP candidate
owned by that job's private scratch directory.

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
`FILE_RENAME_INFORMATION_EX` (information class 65) with POSIX rename semantics
and no replace flag against an explicitly bound destination-directory handle. A concurrent target wins without being
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
directory handles to native `FILE_RENAME_INFORMATION_EX`, including the
same-directory REPLACE case; no close-before-rename workaround exists.

The release-hardening review then found one final cluster at the native output
boundary. The previous Windows final-parent handle combined generic read/write
access with cache-style READ-only sharing, and publication still used the older
rename information class while held file handles remained open. Final output
rename and rollback also omitted the post-rename source/target directory sync;
native workspace errors could cross the public publisher boundary; a full
deferred-close registry could lose its retry owner during cleanup aggregation;
and a normal job left its complete UUID encoder candidate beside the output.

Each item received a deterministic RED before the implementation change. The
Windows fakes enforce sharing in both directions, inspect the native structure,
information class, RootDirectory, and flags, and keep read/write descriptors
open through stat/hash/rename. Durability tests record the exact commit → source
private-directory sync → output-parent sync order for both policies and the
corresponding rollback order after an injected target-sync EIO. Error tests
inject reparse and bound-close failures. Registry-capacity-zero tests force a
persistent CloseHandle failure, collect garbage, and then retry the exact
retained owner. Render and Rebuild failure tests verify that partial candidates
are removed with their job scratch rather than accumulating in the output
parent.

The GREEN design now has three deliberately separate Windows directory-open
contracts. Cache/cut ancestors retain their restrictive READ sharing. Only the
final `PublicationDirectory` parent and its private publication/recovery
children use granular directory rights (`FILE_LIST_DIRECTORY`,
`FILE_ADD_FILE`, `FILE_ADD_SUBDIRECTORY`, `FILE_TRAVERSE`,
`FILE_DELETE_CHILD`, `FILE_READ_ATTRIBUTES`, and `SYNCHRONIZE`) with
READ|WRITE|DELETE sharing. Publication rename uses
`FILE_RENAME_INFORMATION_EX` / class 65 with
`FILE_RENAME_POSIX_SEMANTICS`; REPLACE additionally sets
`FILE_RENAME_REPLACE_IF_EXISTS`, while no-clobber does not. This follows
Microsoft's documented bidirectional CreateFile sharing contract, relative
rename target access, and FileRenameInformationEx flags:

- <https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew>
- <https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-fsa/87f86c9b-6c2a-4803-84b7-131a74a434fa>
- <https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-fscc/4217551b-d2c0-42cb-9dc1-69a716cf6d0c>

Both collision policies copy from the held candidate descriptor into the same
handle-bound `.rembggui-publish` namespace. Every cross-directory final rename
then strictly syncs the consumed source directory and output parent before
success; rollback does the same after each restore rename. A target sync error
after REPLACE enters the existing descriptor-bound rollback and restores the
known-good old bytes. `UnsafeCacheError`, `BoundDirectoryCloseError`, and
workspace-domain `AppError`s are translated to `INVALID_OUTPUT` / `output` at
the public publisher boundary.

`PublicationDirectory.close(primary)` and `RecoveryDirectory.close(primary)`
now carry the active primary through `ExitStack`, so a registry-full deferred
owner attaches to the original error. Boundary translation explicitly transfers
such owners. A final ownership audit also covered partial Windows parent binding:
if a later component open fails and ancestor `CloseHandle` calls fail, the exact
failed handles are retained as one retry owner on the opening error rather than
being lost during stack construction. With no primary, a retention-bearing cleanup failure is a
structured output failure rather than a note-only success; all other resources
are still given a close attempt. Finally, `candidate_path` requires the job's
explicit private work directory. The candidate descriptor closes before the
workspace removes that job tree, and publication treats the candidate pathname
as caller-owned diagnostic state rather than attempting an unsafe conditional
unlink.

Every operational candidate, destination, stage, recovery, and rollback access
inside the transaction is handle-relative. Lexical paths remain only for API
validation and diagnostics. If the parent name is exchanged during a POSIX
transaction, publication either aborts before commit or operates exclusively in
the originally bound directory and detects the renamed parent at postverify;
rollback then restores the original directory without touching the foreign
replacement. Unsafe private-output namespaces are translated at the public
publisher boundary to `INVALID_OUTPUT` / `output` with `retry-output`.

A release-hardening follow-up reproduced a cooperative cross-process race that
the descriptor binding alone did not serialize. Publication, recovery,
recovery-shadow, and rollback-restore files deliberately use fixed bounded
names; without a transaction lock, a second render for the same destination
could enter `open_fixed_pending()`, unlink the first render's still-live pending
inode, and replace its installed publication stage. The final target's identity
checks prevented false success, but the second job still mutated another live
transaction and could force it to fail.

The publisher now acquires one destination-scoped lock before the first private
stage or durable-recovery mutation and releases it only after publication,
rollback, staged-file inspection, and recovery cleanup. The lock is a fixed
`<target-key>.transaction.lock` regular file inside the handle-bound, mode-0700
`.rembggui-publish` directory. It is opened no-follow and identity-checked,
locked non-blockingly with `flock(LOCK_EX|LOCK_NB)` on POSIX or the Windows CRT
one-byte non-blocking file-lock contract, and never unlinked. Process death
therefore releases ownership in the kernel while leaving a harmless, reusable
bounded name. A short-lived per-target in-process guard also covers a
platform whose native byte-range lock permits same-process re-entry; its map
entry is removed on release. Different destination names use different lock
entries and do not serialize globally. Contention is returned before snapshot,
recovery, publication-stage, shadow, or restore mutation as structured
`INVALID_OUTPUT` / `output` with `retry-output`.

The regression tests pause an actual forked publisher with a live fixed entry,
then run an independent contender against the same target. They cover REPLACE,
native no-clobber, recovery-shadow pending, and rollback-restore pending. In
each case the contender fails busy without changing the held inode or bytes and
the owning transaction subsequently completes or restores the old output. The
workspace tests additionally prove stale-name reuse after abrupt owner exit,
independent target locks, the in-process guard under a deliberately re-entrant
platform adapter, and the Windows non-blocking lock/unlock call contract.

A final adversarial review showed that the first target-lock key still depended
on the caller's path spelling and that the lock entry itself was not continuously
bound. A real fork using `exports/../exports/output.webp` entered beside the
canonical spelling. Replacing the live lock inode let a second process lock the
replacement, and renaming/recreating `.rembggui-publish` let it create a new lock
and overwrite the first process's fixed stage. The same bypass reached recovery
shadow and rollback-restore pending slots. Each case was captured as a RED fork
test that pauses the first publisher with a live inode before starting the second.

The corrected transaction derives one key from the validated destination
component in the already-bound `PublicationDirectory`; it never hashes the raw
caller path. Windows and macOS name domains normalize to NFC and case-fold, while
POSIX retains case distinctions. A fixed per-target anchor is created directly,
handle-relatively, in the bound output parent—not inside either replaceable
private directory. Its exact held descriptor binds the expected publication
directory identity and lock-file identity. Acquisition opens and validates that
anchor before opening an existing lock, so a replaced lock or private directory
fails closed before the contender creates, truncates, links, replaces, or unlinks
any private entry.

The anchor, publication directory, and lock identities are revalidated from held
descriptors before and after every publication, recovery, shadow, and restore
stage mutation. Fixed pending files are created with `O_EXCL`; a pre-existing
slot is reused only while the unchanged parent anchor and exclusive kernel lock
prove that no other transaction owns it. Reuse truncates the exact held stale
inode rather than unlinking its pathname. Ambiguous or unanchored entries fail
closed and remain byte-for-byte untouched. Fixed-slot replace also verifies the
held source inode around the handle-relative rename. The parent anchor and lock
are never deleted, so storage remains bounded at two coordination entries per
destination and process death remains reusable through kernel lock release.

These ownership rules preserve the earlier parent-namespace guarantee: the
critical section validates and operates on the originally held handles. If the
outer parent name is exchanged, rollback can restore the original bound parent
without touching the foreign replacement; the lexical binding check still makes
the overall publication fail rather than falsely reporting success.

A final fixed-slot review then replaced the parent anchor itself while the first
publisher was paused with a live stage. Because the first process retained its
old anchor descriptor, a second process could create a new, internally
consistent anchor and transaction lock for the same target. Before the fix,
both REPLACE and native no-clobber contenders entered the fixed publication slot
and overwrote the first process's live inode. Equivalent REDs covered a live
recovery-shadow pending and rollback-restore pending. An anchor-payload rewrite
was added separately to distinguish same-inode content interference from a
name/inode replacement.

The authoritative ownership boundary now includes every fixed private file
inode. `LockedSlotFile` retains the exact regular-file descriptor, its
device/inode identity, a nonblocking OS advisory lock, and an in-process inode
guard. An existing publication, recovery, shadow, or restore pending/final slot
is opened no-follow, bracketed by descriptor/entry identity checks, and locked
before it can be inspected for reuse, truncated, copied, or used as a replace
source/destination. The lock transfers with the inode across pending-to-final
renames and remains held until the publication/recovery holder cleanup closes.
Thus replacing the coordinator anchor and lock no longer grants a contender the
right to mutate the first process's live fixed slot: it receives structured
`INVALID_OUTPUT` / `output` / `retry-output` contention instead.

New fixed files are claimed with exclusive creation (or an atomic hard link)
and are never written before their inode lock is acquired. Stale slots are
reusable only after process death releases their kernel lock. A slot that aliases
its copy source, or any multiply linked stale copy target, is never truncated.
If source and destination fixed names already identify the same locked inode,
the bounded alias is retained: there is no portable unlink-if-inode operation,
so the implementation deliberately avoids a pathname check followed by blind
unlink. Close failures retain the exact slot owner for retry through the same
bounded cleanup-owner mechanism as the surrounding directory handles.

The POSIX fork suite now covers coordinator replacement for REPLACE,
no-clobber, recovery shadow, and rollback restore, plus anchor-payload rewrite,
crash-released slot reuse, hard-link no-truncate, and same-inode no-unlink.
Windows uses the same publication-shared read/write descriptor and the existing
CRT one-byte nonblocking adapter; Microsoft's `_locking` contract explicitly
permits locking past end of file, so an empty exclusively-created slot needs no
sentinel write before ownership is established:
<https://learn.microsoft.com/en-us/cpp/c-runtime-library/reference/locking>.

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
  never replace a primary error or cancellation; a retry-owner-bearing failure
  after an otherwise successful publish is surfaced as a structured output error.

## Verification

- `.venv/bin/pytest -q --tb=short` — **997 passed in 50.89s** outside the
  restricted shared-memory sandbox. The 13 warnings are Python 3.13's macOS warning for
  the deliberately forked cross-process lock repros in an already
  multi-threaded pytest process; no test failed or hung.
- Focused native cache-filesystem, workspace, and render gate — **237 passed in
  5.11s**. The slower WebP and Rebuild gate is separately **81 passed in
  33.03s**. Together they include canonical alias serialization,
  lock/private-directory/anchor replacement, fixed-slot inode ownership,
  crash-released stale-slot reuse, parent-namespace swaps, durable sync ordering,
  retry-owner retention, and bidirectional Windows-sharing fakes.
- `ruff check .` — passed.
- `ruff format --check` over all 4 changed Python files — passed.
- `mypy src` — passed for 29 source files.
- `git diff --check` — passed.

## Residual risk

Ordinary tests deliberately use injected deterministic fakes and never download
models or invoke live ONNX/rembg inference. The exact documented rembg kwargs are
unit-tested at the child boundary, and the real local lossless WebP path is
integration-tested; a manual release qualification with already-cached model
weights remains appropriate before shipping the GUI integration.
On failed/ambiguous cleanup, a fixed private publication or recovery entry may
remain with its diagnostic rather than risk deleting bytes installed by another
actor. Publication/recovery slots are bounded per destination and reused.
Encoder candidates instead live in the already-bounded job scratch tree and are
removed by its existing handle-safe cleanup after their held descriptor closes.
This is the deliberate portable safety tradeoff because POSIX has no atomic
conditional unlink by held inode identity.

The Windows `CreateFileW` sharing branch and class-65 native rename structure,
RootDirectory, and flags are type-checked and covered through injected platform
contracts, but this race-hardening round executed real filesystem mutations on
macOS rather than a Windows or Linux host. A release run on both native platforms
remains appropriate for filesystem-specific durability behavior.
