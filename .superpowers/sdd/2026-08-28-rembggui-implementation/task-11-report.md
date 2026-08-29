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
`cleanup_abandoned_scratch` is the explicit bounded cleanup surface required by
the workspace lifecycle contract.

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

## Architecture and safety decisions

- `CutManifest`, `CutFrame`, `CutUnionMetadata`, workspace summaries, listings,
  and cleanup results are frozen. Cache-key JSON is recursively frozen through
  `FrozenJsonMap`; nested mutation is impossible.
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
- Workspace roots are canonicalized beneath the selected existing output
  directory. Traversal, symlink/junction/reparse-style redirection, UNC/network
  syntax on Windows, and non-local filesystems where the host exposes a local
  mount flag are rejected. POSIX file access is no-follow and descriptor-relative
  to a bound directory, with named/open identity checks before release.
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
  retain `pinned`. Corrupt edits remain file-specific structured failures.
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
  pinned cache requires an explicit override. Scratch cleanup is age gated at
  more than 24 hours and count bounded.
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

- Focused Task 11 suite: `38 passed in 0.97s`.
- Sandboxed full suite: `723 passed`, plus exactly 34 existing POSIX
  `SharedMemory` failures caused by sandbox `PermissionError`.
- Controller-permitted identical full suite: `757 passed in 45.51s`.
- `.venv/bin/ruff check .` — passed.
- `.venv/bin/mypy src` — passed for 28 source files.
- Ruff format check over Task 11 Python files — passed.
- `git diff --check` — passed.

## Residual platform qualification

Native directory exchange and descriptor-bound reflink were exercised on the
current macOS host, and the portable descriptor-copy and two-rename journal
fallbacks were forced by tests. Windows reparse and rename behavior is coded
defensively but remains part of the planned native Windows release
qualification; no Windows-native execution is claimed by this local task.
