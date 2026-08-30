# V1 Scope

**Authoritative.** Where this file and `docs/designs/rembggui-desktop-app.md` or
`docs/superpowers/plans/2026-08-28-rembggui-implementation.md` disagree, **this
file wins**. Read it together with `docs/engineering-guardrails.md`.

Revision: 2026-08-30, following the concept-and-implementation review.

---

## Why this file exists

The design document specifies the eventual product completely and well. It also
defines "V1 complete" as a full release contract: all 15 models, four native
signed-off artifacts on three operating systems, a bespoke accessibility tree
for the custom canvases, and a manual qualification workflow.

That contract was written before a single frame had ever been previewed in the
real application. It turned a personal tool into a multi-month programme and it
directed effort into engine depth while the application remained unable to open
a video (see `docs/engineering-guardrails.md` §1).

This file cuts V1 down to the smallest thing that is genuinely useful, so the
remaining scope can be decided with a working tool in hand instead of on paper.

---

## Definition of done for V1

> On macOS, a user launches rembgGUI, drops in a video, scrubs the source,
> selects an export range and a crop, previews a single frame with
> `birefnet-portrait`, and renders a lossless animated transparent WebP that
> opens correctly — without touching a terminal.

Nothing else gates V1. When that sentence is true end to end, V1 is done.

---

## In scope for V1

| Area | V1 commitment |
|---|---|
| Platform | **macOS 13+ arm64 only.** Run from source (`uv run rembggui`) is sufficient; a packaged `.app` is a stretch goal, not a gate. |
| Models | **4 models**: `birefnet-portrait` (default), `birefnet-general-lite`, `u2net`, `isnet-general-use`. |
| Edge treatment | `Standard` and `Decontaminate colors`. |
| Source media | Local 8-bit SDR MP4/MOV, H.264/H.265. |
| Output | Lossless animated WebP, plus the still-image single-frame case. |
| Timeline | Filmstrip, playhead, IN/OUT range handles, exact time/frame readout. |
| Crop | Visual rectangle with handles, plus numeric fields. |
| Preview | `Preview Frame` through the exact render pipeline, with the stale-result contract. |
| Jobs | Exclusive modal job dialog with truthful stage/progress and working cancellation. |
| Accessibility | Full keyboard reachability, correct tab order, accessible names and values on **standard** widgets, no colour-only status. |
| Persistence | `QSettings` primitives, window geometry, output directory. |
| Cuts | Post-segmentation cuts are still persisted (already implemented) — but see the deferral of the *Rebuild workflow* below. |

---

## Deferred past V1

Deferred means: **do not implement, do not extend, do not test against.** The
code that already exists stays as it is; it is not deleted, and it is not grown.

| Deferred item | Design ref | Rationale |
|---|---|---|
| **Rebuild-from-edited-cuts workflow and its UI** | Task 11 / 12 | The most expensive feature in the plan. Its value cannot be judged before a first real render exists. The persistence engine already exists; the *workflow* waits. |
| **Custom `QAccessible` virtual-child tree** for timeline and crop | Task 14 | Standard-widget accessibility is committed for V1. A bespoke accessible tree for custom-painted canvases is a large, untestable-on-one-machine surface. |
| **Remaining 11 models** | Design catalog | The catalog may list them as visible-but-disabled. `bria-rmbg` in particular carries a licence caveat that needs a decision, not code. |
| `ViTMatte`, alpha-matting edge mode | Design | Already outside committed scope; stays there. |
| **Windows and Linux artifacts**, four-target build matrix | Task 10 / 17 | One platform proves the product. Porting is mechanical afterwards. |
| **Manual four-artifact release qualification** | Design | Presupposes artifacts that V1 does not promise. |
| WebM / MKV / VP8 / VP9 source support | Design media table | Best-effort if PyAV handles it; not verified, not promised. |
| Model Manager UI, Workspace Manager UI | Task 15 | Download progress inside the job dialog covers the V1 need. |
| Disk-space preflight advisory and large-job confirmation | Design | Re-evaluate against real measured render times. |
| SAM prompt exploration | `docs/future-enhancements.md` | Already explicitly non-committed. |

---

## Task status against the historical plan

The plan's task numbering is kept only so older commits stay readable.

| Task | Subject | Status |
|---|---|---|
| 1–9 | Foundation, specs, reducer, timebase, geometry, WebP, source, jobs, models | Implemented |
| 10 | Packaging / streaming-encoder spike | Encoder question resolved (PyAV `libwebp_anim`, streaming). Packaging deferred. |
| 11 | Durable cut workspaces | Implemented, **frozen** — see guardrail G3 |
| 12 | Preview / render / Rebuild orchestration | Preview + render implemented; Rebuild frozen |
| 13 | Application shell | Implemented |
| **14** | Timeline, crop editor, keyboard contexts | **V1 — in progress**, minus the custom accessibility tree |
| **15** | Preview integration, job dialog | **V1 — in progress**, minus Model/Workspace manager UIs |
| 16 | (deferred in the original plan) | Deferred |
| 17 | E2E qualification, release docs, packaging | Deferred except a single macOS smoke path |

---

## Re-opening scope

Deferred items come back only when the V1 definition of done is true and the
user has actually used the tool on real footage. That order is deliberate: the
first real render will change what looks important.
