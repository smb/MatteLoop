# V1 Scope

**Authoritative.** Where this file and `docs/designs/matteloop-desktop-app.md` or
`docs/superpowers/plans/2026-08-28-matteloop-implementation.md` disagree, **this
file wins**. Read it together with `docs/engineering-guardrails.md`.

Revision: 2026-09-01, following native dependency qualification.

Product rename, 2026-08-31: `rembgGUI` is now **MatteLoop**. New installs use
the `matteloop` package, command, settings identity, cache, and workspace
names. Every existing subdirectory of `~/Library/Caches/rembggui/` — model
weights, compiled provider caches and thumbnails — and
`<output-directory>/.rembggui-work/` data remain readable, with new locations
preferred when both exist.

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

> On macOS, a user launches MatteLoop, drops in a video, scrubs the source,
> selects an export range and a crop, previews a single frame with
> `birefnet-portrait`, and renders a lossless animated transparent WebP that
> opens correctly — without touching a terminal.

Nothing else gates V1. When that sentence is true end to end, V1 is done.

---

## In scope for V1

| Area | V1 commitment |
|---|---|
| Platform | **macOS 15+ arm64 and Windows x64 packaged artifacts.** The macOS floor follows the measured minimum of the bundled PySide6 6.10.3 bindings; the custom media dylibs retain their separate 13.0 deployment target. Run from source (`uv run matteloop`) remains supported; Linux stays deferred. |
| Models | **13 models**: `birefnet-portrait` (default), `u2net`, `u2netp`, `u2net_human_seg`, `silueta`, `isnet-general-use`, `isnet-anime`, `birefnet-general`, `birefnet-general-lite`, `birefnet-dis`, `birefnet-hrsod`, `birefnet-cod`, `birefnet-massive`. Excluded: `bria-rmbg` (model-specific licence requires its own consent flow) and `u2net_cloth_seg` (needs a clothing-category input the UI cannot provide). |
| Edge treatment | `Standard` and `Decontaminate colors`. |
| Source media | Local 8-bit SDR MP4/MOV, H.264/H.265. |
| Output | Lossless animated WebP, plus the still-image single-frame case. |
| Timeline | Filmstrip, playhead, IN/OUT range handles, exact time/frame readout. |
| Crop | Visual rectangle with handles, plus numeric fields. |
| Preview | `Preview Frame` through the exact render pipeline, with the stale-result contract. |
| Jobs | Exclusive modal job dialog with truthful stage/progress and working cancellation. |
| Accessibility | Full keyboard reachability, correct tab order, accessible names and values on **standard** widgets, no colour-only status. |
| Persistence | `QSettings` primitives, window geometry, output directory. |
| Cuts | Post-segmentation cuts are persisted, editable, selectable from a promoted cut-set picker, and reusable through Rebuild. **Scope reopened 2026-08-31:** re-rendering an edited clip otherwise repeats segmentation, including a 927 MiB model and 39 inferences, merely to re-encode. |

### Recorded design deviations

- **Render completion summary, 2026-08-31:** a successful Render or Rebuild now
  leaves the job dialog open in a Complete state with the output summary and
  Open output, Open folder, and Close actions. The design document specified
  automatic close and focus on the success banner; this changed because a
  render may run unattended for minutes, and the user needs to see what was
  produced when they return. The existing success banner remains for later
  reference.
- **Windows packaging, 2026-08-31:** an unsigned Windows x64 artifact is back
  in V1 scope for publication preparation. The repository will be public, so
  its Actions build has no paid-minute constraint; this adds the second
  committed packaging target without reopening Linux artifacts or any other
  deferred item.
- **Model Manager outdated-weight cleanup, 2026-09-04:** although the Model
  Manager UI remains deferred, it gained the outdated-weight notice and
  `Delete outdated` as the agreed handling for a rembg namespace move (issue
  #22). Bulk re-download returned in #29 with confirmation, cancellation, batch
  progress and per-model deletion after verification.

---

## Deferred past V1

Deferred means: **do not implement, do not extend, do not test against.** The
code that already exists stays as it is; it is not deleted, and it is not grown.

| Deferred item | Design ref | Rationale |
|---|---|---|
| **Custom `QAccessible` virtual-child tree** for timeline and crop | Task 14 | Standard-widget accessibility is committed for V1. A bespoke accessible tree for custom-painted canvases is a large, untestable-on-one-machine surface. |
| **`bria-rmbg` and `u2net_cloth_seg`** | Design catalog | `bria-rmbg` requires a model-specific licence consent flow; `u2net_cloth_seg` needs a clothing-category input the UI cannot provide. |
| `ViTMatte`, alpha-matting edge mode | Design | Already outside committed scope; stays there. |
| **Linux artifacts and the remaining four-target build matrix** | Task 10 / 17 | macOS arm64 and Windows x64 are the committed packaging targets; Linux and additional target variants stay deferred. |
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
| 10 | Packaging / streaming-encoder spike | Encoder question resolved (PyAV `libwebp_anim`, streaming). macOS arm64 and Windows x64 packaging is in progress; Linux remains deferred. |
| 11 | Durable cut workspaces | Implemented, **frozen** — see guardrail G3 |
| 12 | Preview / render / Rebuild orchestration | Preview + render + Rebuild implemented |
| 13 | Application shell | Implemented |
| **14** | Timeline, crop editor, keyboard contexts | **V1 — in progress**, minus the custom accessibility tree |
| **15** | Preview integration, job dialog | **V1 — in progress**, minus Model/Workspace manager UIs |
| 16 | (deferred in the original plan) | Deferred |
| 17 | E2E qualification, release docs, packaging | Packaging preparation is in progress for macOS arm64 and Windows x64; broader qualification remains deferred |

---

## Re-opening scope

Deferred items come back only when the V1 definition of done is true and the
user has actually used the tool on real footage. That order is deliberate: the
first real render will change what looks important.
