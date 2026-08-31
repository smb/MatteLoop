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

## Delegation

Implementation is delegated to `codex exec -m gpt-5.6-luna -c model_reasoning_effort="xhigh"`.
Second review on complex changes uses `-m gpt-5.6-sol`. Short model aliases
(`luna`, `sol`) are unavailable with a ChatGPT login and fail silently with
exit 137 — always use the full model name.
