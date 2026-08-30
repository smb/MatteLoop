# Engineering Guardrails

**Required reading for every AI agent and human working in this repository —
Claude, Codex, and any other assistant. Read this before the design document,
before the implementation plan, and before writing a single line of code.**

These rules are not style preferences. Each one is a direct response to a
measured failure that already happened in this repository. The evidence is in
the git history and is cited inline so the rules can be argued with on facts.

---

## 0. The one-sentence version

Build the thinnest path a user can actually walk, all the way through, before
making any part of it deeper — and never harden code against a failure you
imagined rather than observed.

---

## 1. What went wrong here (the evidence)

A review on 2026-08-30 measured the state of the repository after 20 commits:

| Measure | Value |
|---|---|
| Source lines | 22 485 |
| Test lines | 23 357 |
| Tests passing | 1 120 (ruff clean, mypy strict clean) |
| **User-reachable functionality** | **none — `app.py` wired `NoOpServices`** |
| `jobs/workspace.py` | 5 041 lines, 231 functions, one module |
| `jobs/render.py` | 2 848 lines |
| entire `ui/` package | ~1 100 lines |

`jobs/workspace.py` landed at 2 434 lines in `331ec0b` and **doubled to 5 041
lines across 14 subsequent `fix:` commits, 9 of them consecutive**:

```
331ec0b feat: persist editable cut workspaces      <- 2434 lines, legitimate
420bcff fix: harden cut workspace persistence
481f4d3 fix: seal cut workspace filesystem boundaries
14dca33 fix: close cut workspace trust boundaries
98efc23 fix: preserve cut workspace cleanup errors
fb62a62 fix: harden Windows cut promotion lifecycle
997b8ba feat: close render pipeline contracts
0b0fc78 fix: bind durable recovery workspace       <- 9 consecutive fix: commits
5021830 fix: bind atomic output publication           begin here; none of them
c98d5eb fix: harden native output publication         added user capability
94d67b5 fix: serialize output transactions
260059c fix: bind output lock ownership
6947f91 fix: bind fixed output slot inodes
a85ddf2 fix: hold output locks through close
122e1e9 fix: consume ambiguous output closes
63fe2b0 fix: close raced output anchors
```

Not one of those commits made the application able to do anything it could not
do before. Every one of them added locks, journals, inode bindings, crash
recovery, or platform syscalls to defend a **single-user desktop application
that, by its own design (`core/state.py`), can only ever run one exclusive job
at a time.**

This is the failure mode to avoid. It is cheap to enter, it produces green
tests and clean type checks the whole way down, and it feels like diligence.

---

## 2. The rules

### G1 — A `fix:` commit needs an observed trigger

**Rule.** Every `fix:` commit must cite, in its commit body, one of:

- `Trigger: repro` — concrete steps a user or test can follow to see the wrong
  behaviour, or
- `Trigger: test <name>` — a test that failed *before* the fix and passes after.

If you cannot write that line, you do not have a fix. You have speculation.
Speculation goes in `docs/future-enhancements.md`, not in the codebase.

**Forbidden reasoning.** "While reading this I noticed a race that could
theoretically…", "this is not robust against…", "to be safe we should also…".
That reasoning wrote 2 600 lines here and delivered nothing.

**Hard stop.** If you are about to write the **third consecutive `fix:` commit
touching the same file**, stop. Do not write it. Report to the user what you
were about to harden and why, and ask whether it is worth it. Three in a row is
the documented entry point into the spiral above.

### G1a — Commit messages carry a compact body

**Rule.** A short conventional summary line, a blank line, then **one to two
lines per changed point**. A commit touching four things has roughly four short
paragraphs.

A one-line commit is not acceptable. Neither is an essay — no narrative, no
background, no restating a point twice. The body is a scannable list of what
changed, readable in fifteen seconds.

**Never put verification status in the body.** No "all tests pass", no pass
counts, no ruff/mypy/CI status. Git history records *what changed*; verification
is ephemeral and belongs in the report to the user.

```
fix: accept untagged SDR colour metadata

Untagged containers report AVCOL_*_UNSPECIFIED (2) and an unspecified range,
which _color_profile treated as fatal — ordinary 8-bit SDR clips were rejected
as SOURCE_HDR_UNSUPPORTED.

Unspecified values now resolve to SDR defaults before validation: BT.709, or
BT.601 below 720p, with limited range for YUV. The profile records what it
assumed.

Genuine HDR is still rejected and now named: PQ and HLG transfers, BT.2020
primaries and matrices.

Trigger: repro — an untagged 1280x720 H.264 clip that PyAV decodes cleanly
failed with "color primaries 2 cannot be proven as BT.709/sRGB".
```

### G2 — Vertical slice before depth

**Rule.** No module may be built deeper than the currently reachable user path
requires. Before adding capability to anything under `jobs/` or `core/`, the
end-to-end path that uses it must already be reachable from the running GUI.

"Reachable" means: a person launches `uv run rembggui`, clicks, and the code
executes. Not "a test calls it". Not "it will be wired in Task 15".

**Why.** 22 485 lines of engine were written and validated against imagined
requirements before a single frame was ever previewed in the real application.
Everything learned about this product will be learned *after* the first real
render, not before it.

**Check before deepening `jobs/`:** is the feature I am about to extend
currently invocable through the GUI? If no — wire it first, extend it second.

### G3 — Concurrency hardening is out of scope for single-writer paths

**Rule.** This application runs **one exclusive job at a time**, enforced by the
reducer (`JobState` in `core/state.py`). On any path a single process reaches
serially, the accepted durability ceiling is:

> write to a sibling temporary file → `fsync` the file → atomic `rename` →
> `fsync` the parent directory.

That is it. Do **not** add, on those paths:

- advisory or OS file locks
- write-ahead journals or crash-recovery replay
- inode / file-identity binding and re-verification
- directory file descriptors held open to defend against path swaps
- transaction objects that wrap the above

**Exception.** Two *different* processes genuinely writing the same path — for
example the user's external image editor saving into `cuts/` while a Rebuild
reads it. That case is real and is already designed for via an immutable
snapshot. Multi-writer defence is permitted **only** there, and the commit must
name the second writer explicitly.

**Exception request.** If you believe a path genuinely needs more than the
ceiling above, do not implement it. Write the case in the commit-less form of a
one-paragraph note to the user and let them decide.

### G4 — Degrade, never refuse

**Rule.** A precondition the user cannot fix from within the UI must never hard
-fail a job. It must degrade to a working fallback and, at most, inform.

**The case that motivated this.** `_assert_local_filesystem` in
`jobs/workspace.py` proved via hand-written `ctypes` `statfs` structs (macOS),
`/proc/self/mountinfo` parsing (Linux) and `GetDriveTypeW` (Windows) that the
work directory was on local storage — and aborted the entire render otherwise.
Because the workspace lives at `<output-directory>/.rembggui-work/`, a user
whose output folder sits on a NAS or SMB mount could not render at all. The
design document excluded network *sources*, never network *outputs*.

Correct shape: detect, fall back to a local work directory, tell the user
where the intermediates went. Never: "outside the contract", job aborted.

### G5 — No bespoke platform internals

**Rule.** Hand-written `ctypes` structs, syscall numbers, `/proc` parsing, and
Win32 API calls require a written justification in the module docstring **and** a
pure-stdlib fallback path that keeps the feature working.

Prefer, in order: stdlib (`os`, `shutil`, `pathlib`) → a pinned dependency
already in `pyproject.toml` → nothing at all (drop the feature).

Bespoke platform internals cannot be reviewed on the machine that wrote them,
cannot be tested on the platforms they target, and silently misbehave when a
struct layout changes. The Darwin `statfs` struct in this repository was
verifiable by nobody.

### G6 — Module and function budgets

| Unit | Budget | Action when exceeded |
|---|---|---|
| Source module | **800 lines** | split into a package with a documented seam |
| Function | **60 lines** | extract named helpers |
| Package public surface | keep `__init__.py` re-exporting the old names | so splits stay non-breaking |

A 5 041-line module with 231 functions is not reviewable by a human or an agent
with a bounded context window. Budgets are checked in review, not by a linter —
exceeding them is a discussion, not an error, but the discussion must happen.

### G7 — Tests are named after behaviour

**Rule.** Test module and function names describe **what the software does**,
never the process that produced them.

- Forbidden: `test_task13_review_fixes.py`, `test_round2.py`,
  `test_review_followup.py`, `test_fixes_from_feedback.py`.
- Correct: `test_shell_state_matrix.py`, `test_focus_routing.py`,
  `test_output_publication.py`.

Process-named tests are unreadable three months later and tell a future reader
nothing about which behaviour broke.

### G8 — Scope is a ceiling, not a target

**Rule.** The design document (`docs/designs/rembggui-desktop-app.md`) describes
the *eventual* product. `docs/v1-scope.md` describes what is being built **now**.
When they disagree, `docs/v1-scope.md` wins. Do not implement a design-document
requirement that the V1 scope defers, however well specified it is.

A completely specified requirement is not the same as a currently needed one.

---

## 3. Pre-commit checklist

Run through this before every commit. Any "no" stops the commit.

1. Does this change move a **user-reachable** capability forward, or fix an
   **observed** defect? (G1, G2)
2. If it is a `fix:` — is there a `Trigger:` line naming a repro or a
   previously-failing test? (G1)
3. Is this the third consecutive `fix:` on the same file? If yes — **stop and
   ask the user.** (G1)
4. Does the commit have a real body listing what changed — not a one-liner,
   and with no test or lint results in it? (G1a)
5. Did I add a lock, journal, recovery path, or identity binding to a
   single-writer path? (G3)
6. Does any new failure path abort a job for a condition the user cannot fix
   from the UI? (G4)
7. Did I add `ctypes`, syscalls, `/proc`, or Win32 calls without a justification
   and a stdlib fallback? (G5)
8. Is any module now over 800 lines, or any function over 60? (G6)
9. Are the new tests named after behaviour? (G7)
10. Is this requirement inside the current `docs/v1-scope.md`? (G8)

---

## 3a. The ratchet (mechanical enforcement)

`scripts/check_guardrails.py` runs in CI before ruff. It is a **ratchet**, not a
rewrite mandate:

- Every `src/` module already over the 800-line budget is recorded with its
  current size in `scripts/guardrails-baseline.json`.
- The check fails when a recorded module **grows**, when a **new** module goes
  over budget, or when a test module is named after a task or review round.
- It never asks you to shrink what is already there. Frozen means frozen, not
  rewritten.

This is what "stop hardening" looks like in a form a machine can enforce:
`jobs/render.py`, `jobs/segmentation_host.py`, `jobs/source.py`, `core/webp.py`
and friends may be read and called, but they may not get bigger.

If a change genuinely justifies growth — a real feature landing in a frozen
module — say so to the user, get agreement, then run:

```sh
uv run python scripts/check_guardrails.py --update
```

Updating the baseline silently, to make the check go green, is the same failure
this document exists to prevent.

---

## 4. Notes for Codex specifically

The implementation model for this repository is
`codex exec -m gpt-5.6-luna -c model_reasoning_effort="xhigh"`. When invoked:

- **Read this file and `docs/v1-scope.md` first.** They override the design
  document and the implementation plan wherever they disagree.
- The implementation plan `docs/superpowers/plans/2026-08-28-rembggui-implementation.md`
  is historical. Task numbering there is retained for reference only; the
  deferral table in `docs/v1-scope.md` is authoritative.
- Finish the assignment given and stop. Do not continue into adjacent
  robustness work you notice along the way. Report what you noticed instead —
  a sentence in the final message is worth more than 300 lines of defence.
- Verification for every change is exactly:
  `uv run ruff check . && uv run mypy src && QT_QPA_PLATFORM=offscreen uv run pytest -q`.
  Green is required. Adding tests is welcome; adding *machinery to make new
  tests pass* is the spiral.
