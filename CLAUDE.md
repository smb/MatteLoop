## gstack (REQUIRED — global install)

**Before doing ANY work, verify gstack is installed:**

```bash
_GS=""
for _D in "${GSTACK_ROOT:-}" "$HOME/.claude/skills/gstack" "$HOME/.codex/skills/gstack" "$HOME/.factory/skills/gstack" "$HOME/.kiro/skills/gstack" "$HOME/.config/opencode/skills/gstack" "$HOME/.slate/skills/gstack" "$HOME/.cursor/skills/gstack" "$HOME/.openclaw/skills/gstack" "$HOME/.hermes/skills/gstack" "$HOME/.gbrain/skills/gstack" "$HOME/.gstack/repos/gstack"; do
  [ -z "$_GS" ] && [ -n "$_D" ] && [ -d "$_D/bin" ] && _GS="$_D"
done
[ -n "$_GS" ] && echo "GSTACK_OK: $_GS" || echo "GSTACK_MISSING"
```

If GSTACK_MISSING: STOP. Do not proceed. Tell the user:

> gstack is required for all AI-assisted work in this repo.
> Install it:
> ```bash
> git clone --depth 1 https://github.com/garrytan/gstack.git ~/.claude/skills/gstack
> cd ~/.claude/skills/gstack && ./setup --team
> ```
> Then restart your AI coding tool.

Do not skip skills, ignore gstack errors, or work around missing gstack.

Using gstack skills: After install, skills like /qa, /ship, /review, /investigate,
and /browse are available. Use /browse for all web browsing.
Use the resolved install path above for gstack file paths
(default: ~/.claude/skills/gstack).

## Engineering guardrails (REQUIRED — read before any code work)

**Read `docs/engineering-guardrails.md` and `docs/v1-scope.md` before writing
code, before the design document, and before the implementation plan.**

They are not style guides. They record measured failures from this repository's
own history — most importantly a hardening spiral that doubled
`jobs/workspace.py` to 5 041 lines across 14 `fix:` commits while the
application still could not open a video.

Authority order when documents disagree:

1. `docs/engineering-guardrails.md` — how to work
2. `docs/v1-scope.md` — what is in scope right now
3. `docs/designs/matteloop-desktop-app.md` — the eventual product
4. `docs/superpowers/plans/2026-08-28-matteloop-implementation.md` — historical

The three rules that get broken most often:

- **A `fix:` commit needs a `Trigger:` line** naming a repro or a
  previously-failing test. No observed trigger, no fix.
- **Third consecutive `fix:` on the same file — stop and ask the user.**
- **Commit bodies are substantive.** Short summary line, blank line, then what
  actually changed. Never a one-liner, never test results in the body.
- **Degrade, never refuse.** A precondition the user cannot fix from the UI must
  fall back, not abort the job.

Verification for every change:

```sh
uv run ruff check . && uv run mypy src && QT_QPA_PLATFORM=offscreen uv run pytest -q
```

## Bumping `rembg` (REQUIRED — all of it, or segmentation dies silently)

The pinned rembg version is not one number. It names the model cache namespace
and is compared at three independent gates, so a partial bump leaves the app
installable, green in CI, and unable to segment a single frame. That is not
hypothetical: #18 moved the runtime 2.0.72 → 2.0.75 and nothing else, and every
preview and render on `main` failed with a bare `EOFError` until #24 — the cause
reached neither the UI nor the log (issue #22).

Change all of these together:

1. `pyproject.toml` — `rembg[cpu]==<new>`, then `uv lock`.
2. `resources/model-manifest.json` — `rembg_version`.
3. `resources/model-manifest.json` — append the **old** version to
   `obsolete_rembg_versions`. This is the step that gets forgotten. The cache
   directory is named after the pin, so every bump orphans every downloaded
   weight (up to 972 MiB each, 15 models), and that list is the only record
   those directories exist: without it the model manager cannot show, re-fetch
   or delete them, and the disk cost is invisible and permanent.

   **Append — never replace.** The list is cumulative over every version this
   tool has ever shipped, because a user may be upgrading from any of them. A
   bump to 2.0.80 makes it `["2.0.72", "2.0.75"]`; dropping an entry strands the
   weights of everyone who skipped a release, with no way back to them from the
   UI. Never migrate or rename weight directories — the namespace is the
   integrity boundary.
4. `resources/model-provenance.json` — `rembg_version` and every
   `upstream_checksum_source` path, after re-deriving each `upstream_checksum`
   from the new `rembg/sessions/*.py`. Only paths and version strings may
   change. A checksum that moved upstream is a supply-chain event, not a
   rename: stop and report it rather than committing the new value.
5. `src/matteloop/core/fingerprints.py` — `REMBG_VERSION`. `jobs/render.py`
   compares this at every render and raises `INVALID_SEGMENTATION` on drift.
6. `src/matteloop/jobs/models/catalog.py` — `_PINNED_REMBG_VERSION`.
7. `docs/building.md` — the locked-build version list.

The coupling test in `tests/jobs/models/test_catalog.py` fails when the
installed runtime and the manifest pin disagree. It is a floor, not a
substitute for this list: it cannot see steps 3, 4 or 7.

## Working on issues (REQUIRED)

Work that answers a GitHub issue goes onto its own branch and into a pull
request that references the issue. Never push it to `main` directly.

- **Check the assignee before you start, then assign yourself.** An assignee is
  the only signal other people have that the work is taken; without one, two
  contributors can spend an evening on the same defect. If the issue is already
  assigned to somebody else, ask before you begin rather than working in
  parallel with them. Unassign yourself if you stop.
- Reference the issue in the pull request: `Closes #N` when the change fully
  resolves it, `Refs #N` otherwise.
- **Never merge a branch and never close a pull request.** Whether the work
  is done, and whether it lands, is the maintainer's call — not something to
  infer from green CI.
- `main` is protected; a direct push is rejected, and that is intentional.
- **Never force-push a branch that has been pushed, without being told to.**
  Amending, rebasing or squashing published commits is the maintainer's call,
  the same as merging is. `--force-with-lease` only protects against
  destroying somebody else's commits; it does not grant permission to rewrite
  history a reviewer may already have fetched, a comment may already cite, or
  a pull request already lists. Amend freely before the first push; after it,
  add a commit, or ask.

The repository is public and takes reports from other people, so the trail
from a report to the change answering it has to stay visible and reviewable.

## Delegation

Implementation is delegated to `codex exec -m gpt-5.6-luna -c model_reasoning_effort="xhigh"`.
Second review on complex changes uses `-m gpt-5.6-sol`. Short model aliases
(`luna`, `sol`) are unavailable with a ChatGPT login and fail silently with
exit 137 — always use the full model name.

## Skill routing

When the user's request matches an available skill, invoke it via the Skill tool. When in doubt, invoke the skill.

Key routing rules:

- Product ideas/brainstorming → invoke /office-hours
- Strategy/scope → invoke /plan-ceo-review
- Architecture → invoke /plan-eng-review
- Design system/plan review → invoke /design-consultation or /plan-design-review
- Full review pipeline → invoke /autoplan
- Bugs/errors → invoke /investigate
- QA/testing site behavior → invoke /qa or /qa-only
- Code review/diff check → invoke /review; complex changes additionally get a Codex pass with gpt-5.6-sol (see Delegation)
- Visual polish → invoke /design-review
- Ship/PR → invoke /ship (not /land-and-deploy: merging is the maintainer's call, see Working on issues)
- Save progress → invoke /context-save
- Resume context → invoke /context-restore
- Author a backlog-ready spec/issue → invoke /spec
