# Task 11 report — durable cut workspaces and immutable rebuild snapshots

## Outcome

Task 11 adds a local-only durable cut workspace service with strict immutable
manifests, streamed RGBA PNG staging, validated promotion, external-edit
reconciliation, immutable rebuild snapshots, bounded scratch cleanup, workspace
inventory metadata, pin guards, and explicit durable deletion.

The public contract is implemented in `src/rembggui/jobs/workspace.py`:
`CutManifest`, `CutWorkspace`, `stage_cut`, `promote_cut_set`,
`validate_cut_set`, `detect_external_edits`, `snapshot_for_rebuild`,
`CutWorkspace.read_promoted_cut`, `list_workspaces`, and `delete_workspace`.
`cleanup_scratch` is the immediate successful-job/cancellation cleanup surface;
`cleanup_abandoned_scratch` remains the bounded age-gated cleanup surface.

User-owned `.agents/`, `AGENTS.md`, and `emoteScript` were not edited or staged.
No remote, cloud, provider, GUI, model, decode, segmentation, or encode behavior
was added.

## TDD evidence

### Initial RED

After adding only `tests/jobs/test_workspace.py`:

```text
.venv/bin/pytest tests/jobs/test_workspace.py -q
ERROR tests/jobs/test_workspace.py
ImportError: cannot import name 'workspace' from 'rembggui.jobs'
1 error in 0.34s
```

This was the intended missing-module failure. No Task 11 production module or
new error codes existed at that point.

### First GREEN and defect-driven cycles

The first implementation run exposed a live promotion journal being consumed by
the observer path, metadata-only snapshot mutation being misclassified, and
single-frame reads not reconciling valid external edits. After those corrections:

```text
.venv/bin/pytest tests/jobs/test_workspace.py -q
32 passed in 0.76s
```

A second RED deliberately exercised four unproven safety boundaries:

- invalid staged sets were not yet removed after validation failure;
- the journal fallback could lose the old cache when candidate activation failed;
- pre-existing corrupt cuts were reported as a concurrent snapshot race;
- the descriptor-copy fallback opened its destination write-only before hashing.

All four tests failed for those exact reasons. The fixes remove invalid stages,
recover backup directories by validating arbitrary sibling paths, establish the
snapshot baseline before scratch allocation, and use a read/write bound copy
descriptor. A final name-gap and promotion-journal symlink RED also proved and
fixed strict staging sequence and no-follow recovery behavior.

Final focused GREEN:

```text
.venv/bin/pytest tests/jobs/test_workspace.py -q
38 passed in 0.97s
```

### Review fix round 1 RED/GREEN

The initial implementation review identified one Critical and eight Important
gaps. Focused RED tests reproduced reachable mutable manifest state, lost-update
and timestamp-regression races, observer recovery during a live fallback
exchange, unstructured journal creation failure, an ancestor-swap binding race,
post-open descriptor leakage, unenforceable filesystem-policy decisions,
post-materialization namespace limits, and missing immediate/corrupt cleanup
contracts. The first RED stopped at collection because `cleanup_scratch` did not
exist; after exposing that API, the remaining tests failed at their specific
boundaries before the fixes were applied.

Final review-fix focused GREEN, including the reused Windows native handle
layer's tests:

```text
.venv/bin/pytest -q tests/jobs/models/test_cache_fs.py tests/jobs/test_workspace.py
81 passed in 1.35s
```

### Review fix round 2 RED/GREEN

The second review found that several Windows operations still fell back to a
lexical path after a directory handle had been bound, and identified four
related contract gaps. Focused RED tests first proved that a redirected lexical
path could receive Windows directory creation, enumeration, and recursive
deletion; that `FrozenJsonMap._items` could be reassigned or deleted; that one
failed handle close prevented later closes; that an `os.fdopen` failure leaked
its transferred descriptor; that snapshot manifests remained writable through
the pin/edit APIs; and that unknown and arbitrary FUSE Linux filesystems were
admitted. Native-API unit tests also established the handle-relative Windows
creation, enumeration, flush, and failed-flush cleanup contract.

The initial focused RED was:

```text
.venv/bin/pytest -q tests/jobs/test_workspace.py -k \
  'slot_reassignment or windows_bound_mkdir or windows_bound_enumeration or windows_close_attempts or mountinfo_uses or snapshot_rejects_every_manifest_mutator or fdopen_failure'
15 failed, 51 deselected
```

Final review-fix focused GREEN:

```text
.venv/bin/pytest -q tests/jobs/models/test_cache_fs.py tests/jobs/test_workspace.py
100 passed in 1.72s
```

## Architecture and safety decisions

- `CutManifest`, `CutFrame`, `CutUnionMetadata`, workspace summaries, listings,
  and cleanup results are frozen. Cache-key JSON is recursively frozen through
  a frozen, slotted `FrozenJsonMap` whose only stored collection is an immutable
  tuple; nested mutation and ordinary slot reassignment/deletion are impossible.
- Manifest JSON is UTF-8, canonical, sorted, compact, newline-terminated,
  deterministic, versioned, duplicate-key rejecting, non-finite rejecting,
  exact-key strict, and atomically fsync/replace persisted. Parsing is capped at
  64 MiB, 100,000 frames, bounded nesting, key counts, string lengths, integer
  ranges, paths, dimensions, pixels, and frame byte sizes.
- The authoritative cache-key input object exactly matches Task 4's
  `cut_cache_key` payload: complete source SHA-256, rational sampling, crop,
  model ID and weight SHA-256, pinned rembg version, pipeline schema,
  orientation/color version, and edge settings. Provisional source identities
  are rejected as unknown manifest inputs. A direct integration test proves the
  two public cache-key calculations agree.
- Every frame is named `frame-NNNNNN.png`, is an independently readable one-frame
  8-bit RGBA PNG, has uniform bounded dimensions, and records byte size, mtime,
  and complete SHA-256. Validation rejects missing, extra, nonsequential,
  corrupt, wrong-mode, mismatched-dimension, linked, or concurrently replaced
  entries.
- Workspace roots are bound anchor-to-leaf beneath the selected output
  directory. POSIX walks every component with no-follow directory descriptors;
  Windows reuses the native handle-relative, no-reparse binding from the model
  cache and keeps ancestor handles open without delete/write sharing. Creation,
  enumeration, file I/O, promotion rename, and recursive deletion are relative
  to a bound parent. Windows directory enumeration consumes the bound handle,
  and directory creation uses a relative native create followed by strict parent
  durability and handle-relative cleanup on failure. Traversal and
  symlink/junction/reparse redirection are rejected. Every exceptional bind path
  closes all acquired descriptors/handles; multi-handle close attempts all
  owners, retains any failed ownership for retry, and reports one structured
  combined failure. Descriptor-to-file-object transfers close the descriptor
  exactly once if `os.fdopen` fails.
- Local-filesystem admission is descriptor-bound: macOS uses native `fstatfs`
  `MNT_LOCAL`; Linux binds `st_dev` to bounded `/proc/self/mountinfo` parsing and
  admits only an explicit reviewed set of local, memory, image, and container
  filesystem types; and Windows rejects remote/unknown drive types while
  accepting removable local volumes. The Linux policy deliberately fails closed
  for unknown local filesystems and arbitrary FUSE subtypes until they are
  reviewed and added. Workspace directory flushes on Windows are strict:
  inability to confirm durability is a structured failure instead of an
  unreported best-effort success.
- Staging directories are siblings of `cuts/<cache-key>`. Invalid stages are
  removed. First promotion is one atomic rename. Replacement prefers native
  directory exchange (`renamex_np(RENAME_SWAP)` on macOS and
  `renameat2(RENAME_EXCHANGE)` on Linux), eliminating an absent-target window.
  Other platforms use an fsynced bounded journal plus old-directory backup; every
  observer recovers the candidate or restores the prior validated cache before
  listing/opening it. Validation and injected rename/cleanup failures preserve
  the previous valid cache.
- External edit detection rescans the entire namespace and every frame's
  readability, dimensions, metadata, and hash. Valid content or metadata changes
  atomically update frame records, set `edited`, invalidate union metadata, and
  retain `pinned`. Corrupt edits remain file-specific structured failures. All
  manifest-mutating APIs require a promoted workspace; staging and private
  snapshot manifests are read-only through the public contract.
- Manifest read/scan/compare-and-swap writes, pin updates, promotion recovery,
  open, listing, and promotion share the same per-output/cache-key reentrant
  lock. Last-use timestamps are monotone, pin state is freshly read, changed
  union metadata cannot be restored by a stale writer, and a manifest identity
  mismatch aborts rather than overwriting an external update.
- Rebuild snapshotting establishes a fully validated source baseline, prefers
  descriptor-bound proven reflink/COW (`FICLONE`/`fclonefileat`), and otherwise
  streams through descriptor-bound copies. It compares source identity,
  metadata, and hash before and after every copy and rescans the full source set
  after the operation. Any post-baseline mutation raises
  `CUTS_CHANGED_DURING_SNAPSHOT`; cancellation and all handled failures remove
  incomplete scratch. A completed snapshot is a private set, so later edits only
  affect the next job.
- Durable `cuts/<cache-key>` directories are never automatically deleted.
  Inventory exposes source, last use, edited/pinned state, exact validated size,
  aggregate size, and the 20 GiB warning decision. Deletion is explicit and a
  pinned cache requires an explicit override. A cache with readable unpinned
  metadata can be explicitly removed even when its frames are corrupt; an
  unreadable pin state requires `allow_pinned=True`. Scratch cleanup supports
  exact immediate job cleanup and age-gated abandonment cleanup. Namespace,
  tree-size, listing, recovery, and deletion walks enforce bounds while
  iterating, before materializing attacker-controlled collections.
- `read_promoted_cut(index)` reconciles valid external edits, validates the one
  requested record, returns a distinct owned Pillow RGBA image, and optionally
  registers it with the existing `RgbaOwnershipTracker`. It never materializes or
  retains the full animation. Repeated validation/read tests show no descriptor
  growth.

## Structured errors

Task 11 adds stable codes for invalid manifests/sets, staging, promotion,
snapshot I/O, unsafe workspace namespaces, pinned deletion, and deletion
failure. The pre-existing `CUTS_CHANGED_DURING_SNAPSHOT` and `JOB_CANCELLED`
codes remain the snapshot race and cancellation contracts.

## Verification

- Focused Task 11 plus native handle suite: `100 passed in 1.72s`.
- Permitted full suite: `789 passed in 48.62s`.
- `.venv/bin/ruff check .` — passed.
- `.venv/bin/mypy src` — passed for 28 source files.
- Ruff format check over Task 11 Python files — passed.
- `git diff --check` — passed.

## Residual platform qualification

Native directory exchange, descriptor walking, mount admission, and
descriptor-bound reflink were exercised on the current macOS host; portable
descriptor copy and the journaled two-rename fallback were forced by tests.
Windows handle acquisition, handle-relative creation/enumeration/rename/deletion,
reparse rejection, handle cleanup, removable/remote policy, and strict durability
failure paths have deterministic tests, but a Windows-native run remains release
qualification and is not claimed by this macOS task. The Linux allowlist may
need an explicit reviewed addition for an uncommon but legitimate local
filesystem; this is the intended fail-closed tradeoff.
