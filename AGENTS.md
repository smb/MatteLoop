<!-- BEGIN GSTACK-CODEX MANAGED BLOCK -->
## gstack — AI Engineering Workflow

This block is managed by `gstack-codex`. Do not edit inside this block.

Skills live in `.agents/skills`. Invoke them by name, e.g. `/office-hours`.
Refresh with `npx gstack-codex init --project`.
This repo currently has the `full` pack installed.

## Available skills

| Skill | What it does |
|-------|-------------|
| `/office-hours` | YC Office Hours — two modes. Startup mode: six forcing questions that expose demand reality, status quo, desperate specificity, narrowest wedge, observation, and future-fit. |
| `/plan-ceo-review` | CEO/founder-mode plan review. Rethink the problem, find the 10-star product, challenge premises, expand scope when it creates a better product. |
| `/plan-eng-review` | Eng manager-mode plan review. Lock in the execution plan — architecture, data flow, diagrams, edge cases, test coverage, performance. |
| `/plan-design-review` | Designer's eye plan review — interactive, like CEO and Eng review. |
| `/design-consultation` | Design consultation: understands your product, researches the landscape, proposes a complete design system (aesthetic, typography, color, layout, spacing, motion), and generates font+color preview pages. |
| `/review` | Pre-landing PR review. Analyzes diff against the base branch for SQL safety, LLM trust boundary violations, conditional side effects, and other structural issues. |
| `/investigate` | Systematic debugging with root cause investigation. Four phases: investigate, analyze, hypothesize, implement. |
| `/design-review` | Designer's eye QA: finds visual inconsistency, spacing issues, hierarchy problems, AI slop patterns, and slow interactions — then fixes them. |
| `/qa` | Systematically QA test a web application and fix bugs found. |
| `/qa-only` | Report-only QA testing. Systematically tests a web application and produces a structured report with health score, screenshots, and repro steps — but never fixes anything. |
| `/ship` | Ship workflow: detect + merge base branch, run tests, review diff, bump VERSION, update CHANGELOG, commit, push, create PR. |
| `/document-release` | Post-ship documentation update. Reads all project docs, cross-references the diff, builds a Diataxis coverage map (reference/how-to/tutorial/explanation), updates README/ARCHITECTURE/CONTRIBUTING/CLAUDE.md to match what shipped, detects architecture diagram drift, polishes CHANGELOG voice with a sell-test rubric, cleans up TODOS, and optionally bumps VERSION. |
| `/retro` | Weekly engineering retrospective. Analyzes commit history, work patterns, and code quality metrics with persistent history and trend tracking. |
| `/browse` | Fast headless browser for QA testing and site dogfooding. Navigate any URL, interact with elements, verify page state, diff before/after actions, take annotated screenshots, check responsive layouts, test forms and uploads, handle dialogs, and assert element states. |
| `/setup-browser-cookies` | Import cookies from your real Chromium browser into the headless browse session. |
| `/careful` | Safety guardrails for destructive commands. Warns before rm -rf, DROP TABLE, force-push, git reset --hard, kubectl delete, and similar destructive operations. |
| `/freeze` | Restrict file edits to a specific directory for the session. |
| `/guard` | Full safety mode: destructive command warnings + directory-scoped edits. |
| `/unfreeze` | Clear the freeze boundary set by /freeze, allowing edits to all directories again. |
| `/gstack-upgrade` | Upgrade gstack to the latest version. Detects global vs vendored install, runs the upgrade, and shows what's new. |
| `/autoplan` | Auto-review pipeline — reads the full CEO, design, eng, and DX review skills from disk and runs them sequentially with auto-decisions using 6 decision principles. |
| `/benchmark` | Performance regression detection using the browse daemon. Establishes baselines for page load times, Core Web Vitals, and resource sizes. |
| `/benchmark-models` | Cross-model benchmark for gstack skills. Runs the same prompt through Claude, GPT (via Codex CLI), and Gemini side-by-side — compares latency, tokens, cost, and optionally quality via LLM judge. |
| `/canary` | Post-deploy canary monitoring. Watches the live app for console errors, performance regressions, and page failures using the browse daemon. |
| `/claude` | Claude Code CLI wrapper for non-Claude hosts - three modes. Review: independent diff review via claude -p. |
| `/context-restore` | Restore working context saved earlier by /context-save. Loads the most recent saved state (across all branches by default) so you can pick up where you left off — even across Conductor workspace handoffs. |
| `/context-save` | Save working context. Captures git state, decisions made, and remaining work so any future session can pick up without losing a beat. |
| `/cso` | Chief Security Officer mode. Infrastructure-first security audit: secrets archaeology, dependency supply chain, CI/CD pipeline security, LLM/AI security, skill supply chain scanning, plus OWASP Top 10, STRIDE threat modeling, and active verification. |
| `/design-html` | Design finalization: generates production-quality Pretext-native HTML/CSS. |
| `/design-shotgun` | Design shotgun: generate multiple AI design variants, open a comparison board, collect structured feedback, and iterate. |
| `/devex-review` | Live developer experience audit. Uses the browse tool to actually TEST the developer experience: navigates docs, tries the getting started flow, times TTHW, screenshots error messages, evaluates CLI help text. |
| `/diagram` | Turn an English description (or mermaid source) into a diagram triplet: the source, an editable .excalidraw file you can open on excalidraw.com, and rendered SVG + PNG (clean mermaid style; the .excalidraw carries the hand-drawn aesthetic). |
| `/document-generate` | Generate missing documentation from scratch for a feature, module, or entire project. |
| `/health` | Code quality dashboard. Wraps existing project tools (type checker, linter, test runner, dead code detector, shell linter), computes a weighted composite 0-10 score, and tracks trends over time. |
| `/ios-clean` | Remove the DebugBridge SPM package and all #if DEBUG wiring from an iOS app. |
| `/ios-design-review` | Visual design audit for iOS apps on real hardware. Connects to a real iPhone via the same StateServer as /ios-qa, screenshots every screen, evaluates against Apple HIG, DESIGN.md, and design best practices. |
| `/ios-fix` | Autonomous iOS bug fixer. Takes a bug found by /ios-qa, reads the source, writes the fix, rebuilds, redeploys, and verifies the fix on the real device. |
| `/ios-qa` | Live-device iOS QA for SwiftUI apps. Connects to a real iPhone via USB CoreDevice IPv6 tunnel, reads Swift source to understand every screen, then runs a vision-driven agent loop: screenshot → analyze → decide → act → verify → repeat. |
| `/ios-sync` | Regenerate the iOS debug bridge against the latest upstream gstack templates. |
| `/land-and-deploy` | Land and deploy workflow. Merges the PR, waits for CI and deploy, verifies production health via canary checks. |
| `/landing-report` | Read-only queue dashboard for workspace-aware ship. Shows which VERSION slots are currently claimed by open PRs, which sibling Conductor workspaces have WIP work likely to ship soon, and what slot /ship would pick next. |
| `/learn` | Manage project learnings. Review, search, prune, and export what gstack has learned across sessions. |
| `/make-pdf` | Turn any markdown file into a publication-quality PDF. Proper 1in margins, intelligent page breaks, page numbers, cover pages, running headers, curly quotes and em dashes, clickable TOC, diagonal DRAFT watermark. |
| `/open-gstack-browser` | Launch GStack Browser — AI-controlled Chromium with the sidebar extension baked in. |
| `/pair-agent` | Pair a remote AI agent with your browser. One command generates a setup key and prints instructions the other agent can follow to connect. |
| `/plan-devex-review` | Interactive developer experience plan review. Explores developer personas, benchmarks against competitors, designs magical moments, and traces friction points before scoring. |
| `/plan-tune` | Self-tuning question sensitivity + developer psychographic for gstack (v1: observational). |
| `/scrape` | Pull data from a web page. First call on a new intent prototypes the flow via $B primitives and returns JSON. |
| `/setup-deploy` | Configure deployment settings for /land-and-deploy. Detects your deploy platform (Fly.io, Render, Vercel, Netlify, Heroku, GitHub Actions, custom), production URL, health check endpoints, and deploy status commands. |
| `/setup-gbrain` | Set up gbrain for this coding agent: install the CLI, initialize a local PGLite or Supabase brain, register MCP, capture per-remote trust policy. |
| `/skillify` | Codify the most recent successful /scrape flow into a permanent browser-skill on disk. |
| `/spec` | Turn vague intent into a precise, executable spec in five phases. |
| `/sync-gbrain` | Keep gbrain current with this repo's code and refresh agent search guidance in CLAUDE.md. |

Repo installs include the full generated skill pack. Heavy browser/runtime binaries stay machine-local in v1.
Installed release: `0.2.8`
<!-- END GSTACK-CODEX MANAGED BLOCK -->

## Engineering guardrails (REQUIRED — read before any code work)

**Read `docs/engineering-guardrails.md` and `docs/v1-scope.md` first**, before
the design document and before the implementation plan.

They record measured failures from this repository's own history. The headline
one: `jobs/workspace.py` landed at 2 434 lines and doubled to 5 041 across 14
`fix:` commits — 9 of them consecutive — adding locks, journals, crash recovery
and inode bindings to a single-writer desktop application, while the GUI still
could not open a video. Green tests and clean types the whole way down.

Authority order when documents disagree:

1. `docs/engineering-guardrails.md` — how to work
2. `docs/v1-scope.md` — what is in scope right now
3. `docs/designs/matteloop-desktop-app.md` — the eventual product
4. `docs/superpowers/plans/2026-08-28-matteloop-implementation.md` — historical

Most-broken rules:

- **G1** — a `fix:` commit needs a `Trigger:` line naming a repro or a
  previously-failing test. Third consecutive `fix:` on one file: stop and ask.
- **G2** — vertical slice before depth. Do not deepen `jobs/` code that the
  running GUI cannot reach yet.
- **G3** — no locks, journals, or crash recovery on single-writer paths. The
  durability ceiling is temp file → fsync → atomic rename → fsync directory.
- **Commit bodies are substantive.** Short summary line, blank line, then what
  actually changed. Never a one-liner, never test results in the body.
- **G4** — degrade, never refuse.
- **G6** — 800 lines per module, 60 per function.
- **G7** — tests named after behaviour, never after a task or review round.

Finish the assignment and stop. Report adjacent problems you notice in a
sentence; do not fix them unprompted.

Verification for every change:

```sh
uv run ruff check . && uv run mypy src && QT_QPA_PLATFORM=offscreen uv run pytest -q
```
