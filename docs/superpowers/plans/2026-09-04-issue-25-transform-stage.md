# Transform Stage on a Finished Cut — Implementation Plan (issue #25)

> Executes the decided spec in `ISSUE-25-SPEC.md` under the hard constraints in
> `CONSTRAINTS.md`. Where the two collide, the constraints win; every such
> override is listed in "Overrides of the spec's file table" at the end.
> This plan contains contracts, decisions and edge cases — not code. A task
> whose implementation is obvious from its contract is meant to be written by
> the implementer, not copied from here.

**Branch:** `feat/issue-25-transform-stage` (off `main`). Never push to `main`,
never merge, never close the PR. PR text: `Closes #25`.

**Definition of done for every stage:** `uv run ruff check . && uv run mypy src
&& QT_QPA_PLATFORM=offscreen uv run pytest -q` green, plus
`uv run python scripts/check_guardrails.py` passing with the baseline
untouched, plus `mypy src` reporting exactly the 9 pre-existing errors in the
files named in `CONSTRAINTS.md` §B and no new file in that list.

---

## 1. Intent

After this lands, a user who has rendered (or picked) a cut can, without
leaving MatteLoop and without re-running the model:

- shorten the loop to a frame range,
- crop the output to a free or fixed-ratio rectangle drawn on the result
  canvas,
- resize to explicit pixel dimensions (or a 7TV preset), choosing what happens
  when the aspect ratio does not match (keep / stretch / cover / pad),
- watch the result loop at output fps before deciding,

and then re-encode through the existing Rebuild path. The stored PNGs are never
modified; the transform is remembered per cut and restored when the cut is
reopened.

---

## 2. Verified facts that shape the design

Line numbers are from the branch point (`c0b6e7f`).

| Fact | Evidence |
|---|---|
| A cut directory may contain **only** `manifest.json` plus the canonical frame names; anything else invalidates the set. | `jobs/workspace/_scan.py:60-79` builds `expected_names` and raises `_set_error("... unexpected ...")` on any extra entry. |
| That invalidation is not recoverable in the picker: `list_workspaces` retries with `detect_external_edits`, which rescans and raises again. | `jobs/workspace/_snapshot_ops.py:295-300`, `_cut_ops.py:366-373`. |
| The manifest rejects unknown keys **and** any other schema version, and all three manifest modules are frozen. | `_manifest.py:325-343` (`_exact_keys`), `_manifest.py:160-167` (schema gate); baseline `scripts/guardrails-baseline.json`. |
| Dot-prefixed entries in `cuts_root` are ignored by every scan that touches that directory. | `_snapshot_ops.py:264-265` (`list_workspaces` skips `name.startswith(".")`), `_models.py:53` (`_existing_promoted_path`), `_manifest_io.py:167-170` (recovery matches only `_MARKER_RE`), `_manifest_io.py:255-287` (recovery touches only the target/stage/backup/marker names it derives), `_common.py:85-90` (the three reserved dot-name regexes), `_platform.py:166-173` (`_assert_safe_directory` only opens the directory). `delete_workspace` removes only `workspace.path` (`_snapshot_ops.py:347`). |
| `webp_delays` distributes cumulative rounding — delays are **not** uniform (3 fps → 333/334/333; four frames → 333/334/333/333). | `core/timebase.py:41-46`: `rhu((i+1)·1000/fps) − rhu(i·1000/fps)`. |
| The cut cache key, the render fingerprint and the union fingerprint enumerate their inputs explicitly; none reads a whole `RenderRequest`. `render_fingerprint` hashes only `cut_key`, `framing`, `max_bytes` ("the framing and size settings used by encoding"); nothing in `ui/` or `core/state.py` reads `RenderArtifact.fingerprint` today. `cut_cache_key_inputs` names the cut directory and must not change. | `core/fingerprints.py:187-221`, `:235-246`, `:249-259`; grep `.fingerprint` over `src/matteloop/ui`, `core/state.py`. |
| `_encode_snapshot` bakes `tuple(notes)` into the `RenderArtifact` it returns, so a note appended *after* the call never reaches the artifact. Its parameter for the promoted workspace is named `durable`. | `jobs/render.py:1325`, `:1458-1472`. |
| `main_window` connects `command_requested` for exactly three widgets (timeline, original canvas, inspector); a new canvas signal is not wired automatically. | `ui/main_window.py:193-194`. |
| `CropCanvas.keyPressEvent` emits `CropChanged` directly, not through `_emit_drag`; `PreviewStage` puts the result canvas in cover mode, which crops the displayed pixmap to fill the canvas. | `ui/crop_canvas.py:210`, `ui/preview_canvas.py:219-227`, `:238`. |
| `apply_framing` is also used by the preview path and `errno` by the rollback path; only `tempfile` is exclusive to `_persist_framed_png`. `_map_output_os_error`/`_output_error` have 43 call sites in render.py (2 defs + 43 uses), none elsewhere. | `jobs/render.py:906`, `:920`, `:2008`, `:1647`; grep. |
| `ImageOps.contain`/`fit`/`pad` exist in Pillow but use Python `round()` and float crop boxes and do not premultiply; they cannot reproduce the house rounding or the pixel assertions of AC 4. | Pillow `ImageOps`; `core/webp.py:1226-1236`. |
| The house resize idiom is RGBA → `RGBa` (premultiplied) → `LANCZOS` → RGBA, PNG saved with `optimize=False`. | `core/webp.py:1226-1244` (`_resize_from_sources`). |
| Framing rounds with `_round_positive_fraction` (floor of x + ½). | `core/geometry.py:221-224`, `:1131-1133`. |
| `rebuild()` is 1155–1271; the per-frame framing loop lives in `_encode_snapshot` 1339–1385; the encoder is called at 1408–1416; `manifest.frame_count` is used as the encoded frame count at 1395 and 1460. | `jobs/render.py`. |
| `_persist_framed_png` (1643–1674) and the two output-error helpers (2906–2938) are the only render.py symbols the framing loop needs; the helpers have 43 call sites in render.py and none in tests; `_persist_framed_png` is patched by `tests/jobs/test_render.py:1550-1556` and called at `:2968`. | grep over `src`/`tests`. |
| `core/state.py` (880), `ui/presenter.py` (179) and `ui/main_window.py` (380) are in the baseline and must not grow. `ParameterEvent` is routed by `state.py:410-411` to `parameters.reduce_parameters`, so a new event added to the union in `core/parameters.py` needs no change in `state.py`. | baseline; `core/state.py:361,410-411`. |
| `ui/inspector.py` is at 798/800; `ui/render_controller.py` at 793/800. | `wc -l`. |
| `show_workspace_attention` (`inspector.py:771-773`) has no callers in `src` or `tests`; `_read_bool` (`:775-777`) has one caller (`:601`). | grep. |
| Reading a promoted cut through `read_promoted_cut` rescans and rehashes the whole directory on every call. | `_models.py:240` (`detect_external_edits(self)` per read). |
| The preview and full render must call the same crop/cleanup functions. | `docs/designs/matteloop-desktop-app.md:33`. |
| Every new function must be ≤ 60 lines; every new module ≤ 800 lines. | `scripts/check_guardrails.py`, `CONSTRAINTS.md` §A. |

---

## 3. Architecture decisions

### D1 — Persistence: a dot-prefixed sidecar **beside** the cut directory, not in it, not in the manifest

**Chosen.** `cuts_root / f".transform-{cache_key}.json"`, written by the job
layer after a successful publish, read by the UI when a cut is opened.

**Why not the manifest (spec's first choice).** `_manifest.py:325-343` rejects
any key it does not know and `:160-167` rejects any other `schema_version`. The
three manifest modules are frozen. Even with a read-compat shim in *new* code,
a manifest written with a new key or version is unreadable by the *previous*
release: for cache-key-named directories `validate_cut_set` raises
`CUT_MANIFEST_INVALID`, which `list_workspaces` does not catch
(`_snapshot_ops.py:296-300` only catches `CUT_SET_INVALID`), so one touched cut
would blank the whole picker after a rollback. The rollback plan in the spec
("must stay loadable by the previous code") cannot be met by a manifest field.

**Why not a sidecar inside the cut directory.** `_scan.py:60-79` computes the
exact expected name set and raises on any extra entry; `list_workspaces` then
retries through `detect_external_edits`, which raises again. A single sidecar
inside a cut directory would make that cut — and, via the uncaught error,
every cut in the output directory — invisible to the picker. This is the
"strand or invalidate user data" outcome the constraints forbid.

**Why the beside-the-directory sidecar is safe.** Every routine that lists or
recovers `cuts_root` either skips dot-names outright (`_snapshot_ops.py:264`,
`_models.py:53`) or matches one of three reserved patterns
(`_common.py:88-90`: `.stage-`, `.replace-…json`, `.backup-`), none of which
`.transform-<64 hex>.json` matches. Recovery (`_manifest_io.py:255-287`) only
ever removes the stage/backup/marker names it derives from the key.
`delete_workspace` removes only the cut directory, so the sidecar becomes an
orphan of a few hundred bytes; the UI delete path removes it explicitly
(Task B6). Old code never reads the file, so rollback strands nothing.

Two paths that *see* the file and are harmless: `list_workspaces` counts every
entry (dot-names included) against `MAX_WORKSPACE_ENTRIES * 3 = 30 000`
(`_snapshot_ops.py:262`, `_common.py:76`) — at most one sidecar per cut and at
most 10 000 cuts, so the bound cannot trip. The picker's size column is summed
from the manifest (`_snapshot_ops.py:301-302`), so the sidecar's bytes are not
reported; accepted. `cleanup_scratch` (`_snapshot_ops.py:402`) and
`_bounded_tree_size` enumerate `scratch`, never `cuts`. `stage_cut`
(`_cut_ops.py:67-77`) enumerates a staging directory, never `cuts_root`.

**Recorded deviation from AC 8.** AC 8 says "restores the last transform from
the manifest". The behaviour AC 8 describes — reopening a cut restores the
last applied transform, and a cut without a stored transform loads as identity
— is satisfied by the sidecar: absent file → identity; unreadable or
mismatched file → identity plus a logged note (degrade, never refuse). The
manifest itself is unchanged, which is what keeps every existing cut readable
by both old and new code.

**Write discipline.** Temp file in `cuts_root`, fsync, `os.replace`
(guardrail G3 ceiling). Written only when the applied transform is
non-identity; an identity rebuild *removes* the sidecar so "reopen" then means
identity. Failures to write are appended to the artifact `notes`, never raised.
The one call lives **inside** `_encode_snapshot`, after the `if published:`
scratch cleanup and before `assert summary is not None` (render.py:1451-1458):
`store_transform(durable, request.transform, notes)`. Placing it after the
call in `render()`/`rebuild()` would lose the notes (`tuple(notes)` is baked
into the artifact at :1471) and would write the sidecar even when publication
did not happen. Existing tests that assert an empty or dot-free `cuts_root`
(`test_render.py:1526`, `:3006`) are unaffected because they run identity
transforms.

**Rejected as secondary:** QSettings keyed by cache key — does not travel with
the output directory, leaks on delete, and V1 scope limits QSettings to
primitives.

### D2 — Pixel operations live in a new `core/transform.py`; the encoder and the player call the same two functions

`core/webp.py` and `core/geometry.py` are frozen. The new module owns
`resolve_resize` (pure integer arithmetic), `transformed_size` and
`apply_transform` (crop then resize on a Pillow RGBA image). Both the encoder
(via `jobs/transform_stage.py`) and the result player call, per frame,
`geometry.apply_framing(cut, plan)` followed by `transform.apply_transform(framed,
spec)`. That is the function pair the design document's rule at line 33
refers to. The player additionally downsizes the *result* for display; the
encoder persists it.

`apply_transform` returns the **same object** for an identity spec (no crop,
no convert, no resize, no copy). Byte identity (AC 1) therefore follows from
the fact that the extracted loop, with an identity spec, executes exactly the
statements it executes today.

### D3 — `render.py` shrinks by extracting the framing loop plus its helpers into `jobs/transform_stage.py`

The block `_encode_snapshot` lines 1339–1385 (trim-union guard, plan
construction, framed-directory creation, per-frame read → `apply_framing` →
persist loop, progress reporting) becomes one call into the new module. The
persistence primitive `_persist_framed_png` (1643–1674) moves with it because
it is called only from that loop. The two error helpers it needs
(`_map_output_os_error` 2906–2928, `_output_error` 2931–2938) move to
`jobs/encoding.py` (93 lines, "helpers kept separate from the frozen render
orchestration", already imported by render.py, imports nothing from render.py
— no cycle) and are imported back into render.py under the same names so its
43 call sites are untouched.

Line accounting for render.py (must end ≤ 2938):

| Change | Lines |
|---|---|
| Replace 1339–1385 with a plan+stage call | −47 + ~8 |
| Move `_persist_framed_png` (+2 blank) | −34 |
| Move the two error helpers (+2 blank) | −35 |
| Extend the `jobs.encoding` import, add `transform_stage` and `transform_store` imports | +5 (worst case after isort) |
| `store_transform(durable, request.transform, notes)` inside `_encode_snapshot` (D1) | +1 |
| `import tempfile` drops (its only use was :1647). `apply_framing` (:906, :920) and `errno` (:2008) stay. | −1 |
| **Net** | **≈ −103** |

`jobs/encoding.py` gains `import errno` for `_map_output_os_error`; it already
imports `AppError` and `ErrorCode`.

The extraction is behaviour-preserving and mechanical: the loop body is moved
verbatim, its free variables become parameters, and the only semantic change is
that the loop iterates the kept range and returns the kept delays. The
`tests/jobs/test_render.py:1550-1556` and `:2968` seams follow the moved
function (they patch/call `transform_stage._persist_framed_png` instead of
`render_module._persist_framed_png`; two one-line test edits).

The `manifest.frame_count` uses at 1395 and 1460 become `len(framed_paths)`
(same line count). `RenderArtifact.frame_count`/`delays_ms` describe the
encoded output; `requested_timestamps`/`actual_pts` keep describing the cut
grid (unchanged, documented in the dataclass docstring — no field added).

The trim/union guard and `FramingPlan` construction move too
(`framing_plan(...)` in the stage module), so the player builds its plan with
the same function as the encoder.

**`render_fingerprint` stays as it is.** Adding the transform to the hash was
proposed and then dropped — see §4.11 for the decision and the evidence that
`RenderArtifact.fingerprint` has no consumer. `cut_cache_key_inputs` and
`union_fingerprint` are likewise untouched: the cut and its union do not depend
on the transform, and changing the cache key would orphan every promoted cut.

### D4 — The transform is reducer state inside `ParameterState`; the event is a `ParameterEvent`

`core/state.py` is frozen, so `AppState` gains no field. `ParameterState`
(`core/parameters.py:46-86`, 448 lines, room) gains `transform: TransformSpec`
with an identity default. `TransformChanged` joins the `ParameterEvent` union;
`state.py:410-411` already routes every `ParameterEvent` to
`reduce_parameters`, which gains one branch. The reducer does **not**
invalidate the preview (the single-frame preview is unaffected by a
post-framing transform), so changing a transform does not re-trigger the
"Preview recommended" preflight — Render → "Matching cut set found" → Rebuild
is the same path framing changes use today.

QSettings persistence is untouched: `preferences.py` writes only its `_KEYS`;
`parameters_from_values` leaves the new field at its default. The transform is
per cut (D1), not a preference.

### D5 — The UI needs a "current cut" that state cannot hold: a `CutSession` owned by a new controller

Nothing in `AppState` names a cut workspace, and `ArtifactResult` (frozen
`state.py`) cannot gain one. The `RenderArtifact` returned by the worker does
carry `cut_workspace` and `manifest`. So:

- `RenderWorker` gains `artifact_ready = Signal(object)` emitted with the raw
  `RenderArtifact` before `RenderSucceeded`.
- `RenderController` re-emits it as its own `artifact_ready` signal (+2 lines)
  and gains a public `transform_restore: Callable[[CutWorkspace], None] | None`
  attribute consulted in `_use_workspace` before the request is built (+3
  lines), so "Use this set" restores the cut's stored transform *before* the
  rebuild request is assembled. Total +5 within the 7-line headroom. Fallback if
  the count overruns: move `_WorkspaceProbeWorker` (`render_controller.py:56-83`,
  no test references) to `ui/workspace_controller.py`.
- A new `ui/transform_stage.py` holds `TransformStageController(QObject)`: it
  subscribes to the store, owns the `CutSession`, owns the frame-loading
  worker, and drives the `TransformGroup` (Stage B) and the
  `ResultPlayerCanvas` (Stage C). It is created by `SourceController` and
  attached to the window's widgets by one new method called from `app.py`
  (`_run_gui` stays under 60 lines). `main_window.py` is untouched — which
  means `attach` must itself connect `canvas.command_requested` to
  `store.dispatch`; `main_window.py:193-194` wires only the timeline, the
  original canvas and the inspector.

The session records the *applied* transform on artifact success (the request's
transform, which the encoder just used), so a render that used a transform does
not reset the inspector to identity afterwards.

### D6 — The inspector gains the Transform group as a separate module and two mechanical removals pay for the wiring

`ui/inspector.py` has 2 lines of headroom. The group is a self-contained
`QWidget` in `ui/transform_group.py`; the inspector only constructs it, mounts
it in a new `("transform", "Transform", False)` disclosure and lists it in
`tab_widgets()`:

| Inspector change | Lines |
|---|---|
| `from matteloop.ui.transform_group import TransformGroup` | +1 |
| `_DISCLOSURES` row | +1 |
| construct `self.transform_group` in `__init__` **before** the `_DISCLOSURES` loop (`:144`), passing `self.command_requested.emit` as its emitter | +1 |
| `_section`: `if key == "transform": …addWidget(self.transform_group)` | +2 |
| `tab_widgets()`: the disclosure button and `*self.transform_group.tab_widgets()` | +2 |
| remove `show_workspace_attention` (`:771-773`, no callers in `src`, `tests`, `resources`) | −4 |
| move `_read_bool` (`:775-777`) to `inspector_disclosure.read_bool(settings, name, default)`; `:601` becomes `read_bool(self._settings, …)`; the import line `from matteloop.ui.inspector_disclosure import configure_disclosure, read_bool` is 77 chars (the name `read_bool_setting` would make it 85 — still under ruff's 88, but leave no margin for isort to wrap it into four lines) | −4 |
| **Net** | **−1 → 797** |

State reaches the group through the existing view path: `crop_view.render_source_editor`
(+1 line) calls `inspector.transform_group.apply(model.parameters, editable)`;
`ParameterPresentation` gains `transform` and `artifact` fields so the readout
can show the last artifact's actual size and file size (`present_parameters`
already receives the whole state; `presenter.py` is not touched). Cut facts
(frame count, framed size, fps, delays) reach the group from the
`TransformStageController` via `set_cut(...)`; the group is enabled only when
both a transform and cut facts are present.

### D7 — The result player is a `CropCanvas` subclass with two frame caches and one timer

`crop_canvas.py` (312 lines) is reused, not forked: its `__init__` gains
keyword parameters `title`, `object_name`, `runtime_root` (defaults preserve
today's behaviour) and **both** emit sites — `_emit_drag` (`:245`) and
`keyPressEvent` (`:210`) — route the new rectangle through a
`_constrain(crop, target)` hook (default identity) and emit via a
`_crop_event` factory (default `CropChanged`). `ResultPlayerCanvas` overrides
the hook (aspect lock) and the factory (`TransformChanged` with the new crop).
Missing the keyboard site would make an arrow-key nudge on the result canvas
dispatch a *source* crop change, invalidate the preview and force
re-segmentation on the next render.

Two display modes on one canvas, chosen by the group's "Edit crop" toggle:
crop-edit mode plays the **framed** frames with the crop overlay and handles;
otherwise it plays the **transformed** frames (what the file will contain).
While a session is loaded the canvas displays in fit mode
(`set_cover_frame(False)`): the overlay geometry from `build_crop_geometry`
assumes a fitted pixmap, and cover mode (`preview_canvas.py:219-227`) would
crop a `pad` result's transparent columns out of view. The stage's cover
setting is restored when the session closes.

Caching, bounded and reload-free for the common edits:

- The loader (a `QThread` worker) reads **all** stored frames of the cut
  directly by manifest filename (never through `read_promoted_cut`, which
  rescans the directory per call — `_models.py:240`), applies `apply_framing`
  and `apply_transform` at full resolution, and keeps two display-sized
  caches: *framed*, keyed by (cut, framing, display size), and *transformed*,
  keyed additionally by (crop, resize). Trim never reloads: playback slices
  `kept_range` over the caches and the sliced `webp_delays` — the same numbers
  the encoder receives. The framed cache survives crop/resize edits.
- Crop/resize edits arrive per mouse move. The transformed rebuild is
  debounced (one restarted single-shot timer, 250 ms) and skipped entirely
  while crop-edit mode is on (the canvas shows the framed cache then); it runs
  once when crop-edit is toggled off. A generation counter drops stale results.
- Memory is capped by `PLAYER_CACHE_BUDGET_BYTES = 128 MiB` for both caches
  together. The display size shrinks (aspect kept) until
  `2 × frames × w × h × 4` fits, never below 64 px on the short side; if the
  floor still overflows (thousands of frames), the caches hold the first N
  stored frames that fit and the canvas shows the status marker "Previewing
  the first N of M frames". Degrade, never refuse; `MAX_OUTPUT_FRAMES` is
  100 000 (`timebase.py`), so an unbounded cache is not hypothetical.

The canvas keeps its public `PreviewCanvas` surface (`set_presented_frame`,
`set_status_marker`, `status_label`, properties) because `main_window._render`
calls them on every state change; while a session is playing it ignores a
repeated placeholder call but yields to a *new* preview image (identity
comparison), pausing playback — Preview Frame still shows its result.

---

## 4. Contracts

Signatures are the contract. Field lists are complete; defaults are as shown.

### 4.1 `src/matteloop/core/specs.py` (additions; stays well under 800)

```
class MismatchMode(StrEnum):
    KEEP = "keep"; STRETCH = "stretch"; COVER = "cover"; PAD = "pad"

@dataclass(frozen=True)
class ResizeSpec:
    width: int | None = None
    height: int | None = None
    mismatch: MismatchMode = MismatchMode.KEEP
    def __post_init__(self) -> None            # calls validate()
    def validate(self) -> None                 # ValidationError(INVALID_TRANSFORM, "transform", …)

@dataclass(frozen=True)
class TransformSpec:
    first_frame: int = 0
    last_frame: int | None = None
    crop: CropSpec | None = None
    resize: ResizeSpec | None = None
    def __post_init__(self) -> None            # calls validate()
    def validate(self) -> None                 # structural only
    @property
    def is_identity(self) -> bool              # all four fields at their defaults
    def kept_range(self, frame_count: int) -> range
    def select_kept[T](self, items: tuple[T, ...]) -> tuple[T, ...]
    def validate_for(self, frame_count: int, framed_size: tuple[int, int]) -> tuple[int, int]
        # bounds against the cut; returns the final (width, height) before auto-fit

@dataclass(frozen=True)
class SizePreset:
    platform: str; label: str; width: int; height: int

PLATFORM_SIZE_PRESETS: tuple[SizePreset, ...]   # ("7TV", "128x128", 128, 128), ("7TV", "256x128", 256, 128), ("7TV", "384x128", 384, 128)
```

`RenderRequest` gains `transform: TransformSpec = field(default_factory=TransformSpec)`
(last field, so positional construction in tests keeps working) and
`validate()` calls `self.transform.validate()`. `validate_for_source` is
unchanged: the transform is validated against the *cut*, not the source.

`MismatchMode` is a `StrEnum` with exactly the spec's four literal values —
the house idiom (`EdgeMode`, `CollisionPolicy`), serialising identically. The
spec's `validate(frame_count, framed_size)` is split into `validate()` /
`validate_for(...)` to mirror `CropSpec.validate()` / `CropSpec.validate_for()`
(`specs.py:119-153`); behaviour is the spec's.

`core/errors.py` gains `ErrorCode.INVALID_TRANSFORM = "invalid_transform"`
(one line; `tests/core/test_errors.py:138` only asserts a retired code is
absent).

**Validation rules (`validate`):**
- `first_frame` is a non-bool int ≥ 0; `last_frame` is `None` or a non-bool
  int ≥ `first_frame`; `crop` is `None` or a `CropSpec`; `resize` is `None`
  or a `ResizeSpec`.
- `ResizeSpec`: at least one of width/height given; each given value is a
  non-bool int in `[MIN_FINAL_DIMENSION, MAX_FINAL_DIMENSION]`; `mismatch` is a
  `MismatchMode`.

**Validation rules (`validate_for(frame_count, framed_size)`),** each message
names the violated bound and the actual value, and **every** failure is a
`ValidationError(INVALID_TRANSFORM, "transform", …)` — never the framing
code, which would point the user at the wrong group:
- `0 <= first_frame <= last_frame_or_default < frame_count` ("last_frame 9
  exceeds the last stored frame 7").
- `crop` fully inside `framed_size` via `CropSpec.validate_for` (re-raised as
  `INVALID_TRANSFORM` with the framed size in the message).
- The bounded dimensions are the **final canvas** — `ResizePlan.canvas` when
  a resize is set, else the cropped size, else the framed size. `scaled` (the
  intermediate of `cover`/`pad`) is bounded only by the allocation budget.
  Each axis must lie in `[128, 16383]` ("derived height 64 is below the 128 px
  minimum"; "crop height 100 is below the 128 px minimum"). This is the same
  check `FramingSpec().validate_final_dimensions` performs, re-raised under the
  transform code; the final `(width, height)` is returned.
- Split into `_validate_range`, `_validate_crop`, `_validate_size` so no
  function nears 60 lines.

The `TransformGroup` readout calls `validate_for` with the cut facts and shows
a failing message inline (cf. `inspector.py:356-362`) before Render is ever
clicked; the job repeats the check in `stage_encoder_frames` and raises the
same message. Validation stays in one function.

### 4.2 `src/matteloop/core/transform.py` (new, Qt-free, ≤ 300 lines)

```
@dataclass(frozen=True)
class ResizePlan:
    scaled: tuple[int, int]        # size after the proportional/non-proportional resample
    canvas: tuple[int, int]        # final output size
    offset: tuple[int, int]        # paste offset (pad) — (0, 0) otherwise
    crop_box: tuple[int, int, int, int] | None   # cover only: box cut from `scaled`
    resample: bool                 # False when scaled == source (no resample at all)

def resolve_resize(source_size: tuple[int, int], resize: ResizeSpec) -> ResizePlan
def transformed_size(framed_size: tuple[int, int], spec: TransformSpec) -> tuple[int, int]
def apply_transform(image: Image.Image, spec: TransformSpec) -> Image.Image
    # identity → returns `image` itself; otherwise crop (if any) then resize (if any).
    # Never mutates its input. Always returns mode "RGBA".
```

**Exact rounding rules for `resolve_resize`** — `w×h` is the (cropped) source,
`W`/`H` the requested values, `rhu(x)` is `geometry._round_positive_fraction`
(floor of x + ½ on an exact `Fraction`), all ratios exact `Fraction`s:

| Case | scaled | canvas | placement |
|---|---|---|---|
| only `W` given | `(W, rhu(h·W/w))` | = scaled | none; mismatch ignored |
| only `H` given | `(rhu(w·H/h), H)` | = scaled | none; mismatch ignored |
| both given, `w·H == h·W` | `(W, H)` | `(W, H)` | none; mismatch ignored |
| `keep` | `s = min(W/w, H/h)`; `(rhu(w·s), rhu(h·s))` | = scaled | none — the limiting axis is exactly `W` or `H` |
| `stretch` | `(W, H)` non-proportional | `(W, H)` | none |
| `cover` | `s = max(W/w, H/h)`; `(rhu(w·s), rhu(h·s))` (≥ `(W, H)` on both axes) | `(W, H)` | `crop_box = (l, t, l+W, t+H)` with `l = (scaled_w − W) // 2`, `t = (scaled_h − H) // 2`; the odd pixel goes right/bottom |
| `pad` | `s = min(W/w, H/h)`; `(rhu(w·s), rhu(h·s))` (≤ `(W, H)`) | `(W, H)` transparent `(0,0,0,0)` | `offset = ((W − scaled_w) // 2, (H − scaled_h) // 2)`; the odd pixel goes right/bottom |

`resample` is `False` exactly when `scaled == (w, h)`; then no `RGBa`
conversion and no `resize` call happen — `pad` pastes the untouched frame and
`keep`/`cover` return it unchanged. When `resample` is `True`, the resample is
`convert("RGBa")` → `resize(scaled, LANCZOS)` → `convert("RGBA")`, i.e. the
`_resize_from_sources` idiom at `webp.py:1226-1236`. `cover` resamples once to
`scaled` and then crops losslessly; it never resamples a source sub-rectangle.

Checks against AC 4 on a 128×128 frame with 256×128: `keep` → `s = 1` →
128×128, no resample; `stretch` → 256×128 non-uniform; `cover` → `s = 2` →
scaled 256×256, `crop_box = (0, 64, 256, 192)` = the middle half; `pad` →
`s = 1` → scaled 128×128, `offset = (64, 0)` → columns 0–63 and 192–255 fully
transparent, columns 64–191 byte-identical to the frame. AC 5 on a 256×128
frame with width 256 → `(256, rhu(128·256/256)) = (256, 128)` for every mode.

The intermediate `scaled` allocation is bounded with
`geometry._validate_allocation_budget(scaled, 4, "transform resample")` (the
same budget framing uses). `transformed_size` is pure arithmetic (crop size,
then `resolve_resize(...).canvas`) and performs no bounds check — the bounds
live in `validate_for` only. Implement `resolve_resize` as one small helper per
mode (`_single_axis`, `_keep`, `_cover`, `_pad`) dispatched from a ≤ 20-line
body, and `apply_transform` as `_crop` / `_resample` / `_place`.

Pillow's `ImageOps.contain`/`fit`/`pad` were considered [Layer 1] and
rejected: they round with Python `round()` and resample a float crop box,
neither premultiplies, and AC 4's byte-identical `pad` columns and lossless
`cover` crop need exact integer placement.

### 4.3 `src/matteloop/jobs/transform_stage.py` (new, ≤ 250 lines)

```
def framing_plan(source_size: tuple[int, int], union: PixelBounds | None, framing: FramingSpec) -> FramingPlan
    # moves render.py:1339-1350 verbatim: raises INVALID_FRAMING when trim is on and union is None

def stage_encoder_frames(
    read_cut: Callable[[int, RgbaOwnershipTracker], Image.Image],
    frame_count: int,
    plan: FramingPlan,
    transform: TransformSpec,
    delays: tuple[int, ...],
    framed_directory: Path,
    tracker: RgbaOwnershipTracker,
    context: JobContext,
) -> tuple[tuple[Path, ...], tuple[int, ...]]
    # 1. transform.validate_for(frame_count, plan.output_size) — before any file is created
    # 2. tracker.include_size(plan.output_size) and the transformed size
    # 3. mkdir framed_directory (exist_ok=False; OSError → _map_output_os_error as today)
    # 4. for index in transform.kept_range(frame_count): the moved loop body, with
    #    apply_transform(framed, transform) between apply_framing and persist,
    #    progress totals = kept count, output names frame-{position:06d}.png (0-based over kept frames)
    # 5. return (paths, transform.select_kept(delays))

def _persist_framed_png(path: Path, image: Image.Image) -> None   # moved verbatim from render.py:1643-1674
```

`render.py` call shape (illustrative, not code): `plan = framing_plan(...)`;
`framed_paths, delays = stage_encoder_frames(partial(self._workspace.read_cut,
private), manifest.frame_count, plan, request.transform, delays, scratch /
"framed-inputs", tracker, context)`. Per-frame `tracker.register(...)` and
`close()`/`del` discipline is kept exactly as today for every intermediate
(cut, framed, transformed).

The signature alone is ten lines and the moved loop body is ~25; put the
per-frame work (read → frame → transform → persist, with the `try/finally`
pairs) in `_stage_frame(read_cut, index, position, plan, transform,
framed_directory, tracker) -> Path` so `stage_encoder_frames` stays a
≤ 40-line driver. The module is new, so a single 61-line function fails CI.

`jobs/encoding.py` gains `_map_output_os_error` and `_output_error` moved
verbatim from `render.py:2906-2938` (plus `import errno`); render.py imports
them back by name. `transform_stage.py` imports from `jobs.encoding`,
`core.geometry`, `core.transform`, `core.specs`, `core.rgba`, `jobs.context`
— never from `jobs.render`, so no cycle.

### 4.4 `src/matteloop/jobs/transform_store.py` (new, Qt-free, ≤ 200 lines)

```
TRANSFORM_SIDECAR_SCHEMA = "matteloop-cut-transform"
TRANSFORM_SIDECAR_VERSION = 1

def transform_sidecar_path(workspace: CutWorkspace) -> Path   # cuts_root / f".transform-{cache_key}.json"
def transform_to_payload(spec: TransformSpec) -> dict[str, object]
def transform_from_payload(payload: object) -> TransformSpec     # ValueError on any deviation
def load_transform(workspace: CutWorkspace, notes: list[str] | None = None) -> TransformSpec
    # missing file → identity; unreadable/mismatched schema, version or cache_key → identity + note
def store_transform(workspace: CutWorkspace, spec: TransformSpec, notes: list[str]) -> None
    # identity → remove the sidecar if present; else temp file + fsync + os.replace;
    # any OSError → note appended, nothing raised
def discard_transform(workspace: CutWorkspace) -> None          # best effort, never raises
```

Payload: `{"schema", "schema_version", "cache_key", "transform": {"first_frame",
"last_frame", "crop": {x,y,width,height} | null, "resize": {width, height,
"mismatch"} | null}}`, canonical JSON (sorted keys, no NaN) like the manifest.
`store_transform` is called from exactly one place, `_encode_snapshot` (D1);
`discard_transform` from `WorkspacePickerController._delete_selected` after
`delete_workspace` succeeds (`workspace_controller.py:63`, `:73`).

### 4.11 `src/matteloop/core/fingerprints.py` — DROPPED (orchestrator decision, 2026-09-04)

**Do not touch `core/fingerprints.py` in this ticket.** The maintainer's
standing instruction for this branch is that the rembg pin, the model manifest
and `fingerprints.py` stay untouched, because that file is one of the gates in
the "Bumping rembg" checklist in `CLAUDE.md` and a stray edit there is
expensive to spot.

Verified independently before dropping it: `render_fingerprint` has exactly one
producer (`render.py:1389`) and its value becomes `RenderArtifact.fingerprint`
(`render.py:1457`, dataclass at `render.py:293-296`), which is in-memory only —
never persisted, never compared against a stored value, and not consumed
anywhere in `ui/` or `core/state.py`. Omitting the transform from the hash
therefore cannot cause a stale artifact to be reused or a cache to be missed.
The change was correctness hygiene with no reachable behaviour, i.e. exactly
what guardrail G1 tells us not to write.

Task T4 and edge case E34 are struck for the same reason. Record the decision
in the PR description.

### 4.5 `src/matteloop/core/parameters.py` (additions)

```
ParameterState.transform: TransformSpec = field(default_factory=TransformSpec)   # last field

@dataclass(frozen=True, slots=True)
class TransformChanged:
    transform: TransformSpec

ParameterEvent = (... | TransformChanged)
def _reduce_transform(state: AppState, event: TransformChanged) -> AppState
    # no-op when equal or not a TransformSpec; replaces parameters.transform; does NOT invalidate the preview
```

### 4.6 `src/matteloop/core/crop.py` (addition)

```
def fit_crop_aspect(crop: CropSpec, ratio: Fraction, target: str, *, source_width: int, source_height: int) -> CropSpec
    # Re-fit `crop` to width:height == ratio after a handle/body move:
    #   corner handles keep the dragged corner's opposite corner fixed and adjust the axis that grew less;
    #   edge handles adjust the perpendicular axis around the crop's centre; "crop" (move) is unchanged.
    #   Result is clamped by clamp_crop and has width, height >= 1; rounding is rhu on exact Fractions.
def centered_crop_for_aspect(ratio: Fraction, *, source_width: int, source_height: int) -> CropSpec
    # the largest centred rectangle of that ratio inside the source (used when a preset is chosen)
```

### 4.7 `src/matteloop/ui/parameter_presentation.py` (additions)

`ParameterPresentation` gains `transform: TransformSpec =
field(default_factory=TransformSpec)` and `artifact: ArtifactResult | None =
None` as its last two fields; `present_parameters` fills them from
`state.parameters.transform` and `state.artifact_result`. Defaults keep the
existing keyword construction in `tests/ui/test_parameter_presentation.py`
valid, so the only test that changes is the one asserting the new values.

### 4.8 `src/matteloop/ui/transform_group.py` (new, ≤ 600 lines)

```
@dataclass(frozen=True)
class CutFacts:
    cache_key: str
    frame_count: int
    framed_size: tuple[int, int]
    fps: int
    delays_ms: tuple[int, ...]

class TransformGroup(QWidget):
    aspect_lock_changed = Signal(object)     # Fraction | None
    crop_edit_toggled = Signal(bool)
    use_playhead_requested = Signal(str)     # "first" | "last"
    def __init__(self, emit: Callable[[object], None], parent: QWidget | None = None, *,
                 presets: tuple[SizePreset, ...] = PLATFORM_SIZE_PRESETS) -> None
    def apply(self, presentation: ParameterPresentation, editable: bool) -> None
    def set_cut(self, facts: CutFacts | None) -> None
    def set_playhead_frame(self, index: int | None) -> None
    def tab_widgets(self) -> tuple[QWidget, ...]
```

Widgets (object names in parentheses; all with accessible names): first/last
frame spinboxes (`transform_first_frame`, `transform_last_frame`) with "Use
playhead" buttons (`transform_first_playhead`, `transform_last_playhead`);
"Edit crop" checkbox (`transform_crop_edit`); aspect preset combo
(`transform_aspect`: Free, 1:1, 2:1, 3:1, 4:3, 16:9); crop x/y/width/height
spinboxes (`transform_crop_x` …); width/height spinboxes
(`transform_width`, `transform_height`) with range
`[MIN_FINAL_DIMENSION − 1, MAX_FINAL_DIMENSION]` and special value text
"Auto" at the minimum; percentage spinbox (`transform_percent`); mismatch
combo (`transform_mismatch`: Keep original aspect ratio / Stretch to fit /
Center and crop to fit / Add transparent padding); size preset combo
(`transform_size_preset`: "Custom" then `presets` grouped by platform via
separator + platform header rows that are not selectable); read-only readout
label (`transform_readout`).

Behaviour contract: every edit emits `TransformChanged` with a *structurally
valid* spec (the group never emits a `ResizeSpec` with both axes Auto — it
shows the filename-style inline error instead, cf. `inspector.py:356-362`);
choosing a size preset fills width and height and leaves the mismatch combo
untouched (AC 10); the percentage field fills width and height from the cropped
framed size and clears itself when width or height is edited by hand; choosing
a non-Free aspect preset replaces the crop with `centered_crop_for_aspect` and
emits `aspect_lock_changed`; the readout shows "N of M frames · D.DDD s ·
W×H px" from facts + transform, and appends "· rendered W'×H', S" from the
last artifact when present (AC 6/10). Playhead buttons are enabled only after
`set_playhead_frame` with a non-`None` index.

### 4.9 `src/matteloop/ui/transform_stage.py` (new, ≤ 600 lines)

```
@dataclass(frozen=True)
class CutSession:
    workspace: CutWorkspace
    manifest: CutManifest
    fps: int                 # manifest.cache_key_inputs["sampling"]["fps"] — the cut's grid, not the inspector's current fps

class TransformStageController(QObject):
    facts_changed = Signal(object)          # CutFacts | None
    def __init__(self, store: StateStore, *, frame_reader: FrameReader | None = None, parent: QObject | None = None) -> None
    def attach(self, group: TransformGroup, canvas: ResultPlayerCanvas | None) -> None   # canvas is None until Stage C
    def open_artifact(self, artifact: object) -> None      # duck-typed: needs .cut_workspace and .manifest, else ignored
    def restore_for(self, workspace: CutWorkspace) -> None # load_transform → clamp crop to framed size → dispatch TransformChanged
    def close_session(self) -> None
    def shutdown(self) -> None
    @property
    def session(self) -> CutSession | None
    @property
    def facts(self) -> CutFacts | None
```

`FrameReader` is a small Protocol (`read(workspace, frame: CutFrame) ->
Image.Image`) defaulting to a direct `Image.open` of `workspace.path /
frame.filename`; tests inject fakes. The session holds no `FramingSpec`: facts
follow the **current** `state.parameters` (trim, alpha_threshold, padding,
stretch_x) and are recomputed on a `QThread` worker (`_CutFactsWorker`) when
any of the four changes — union from `manifest.union_metadata` when its
`alpha_threshold` text equals the current threshold (`detect_external_edits`
drops the metadata on edited frames, `_cut_ops.py:381`, so a stale union
cannot survive an edit), otherwise `geometry.union_alpha_bounds` over the
stored frames; framed size from `transform_stage.framing_plan(...)`; delays
from `webp_delays(frame_count, fps)`. The worker follows the
`moveToThread` / `finished → quit → deleteLater` idiom of
`ui/preview_controller/controller.py:192-202`. A generation counter discards
stale results. On facts change the controller clamps
`state.parameters.transform.crop` to the new framed size with `clamp_crop`
and dispatches the clamped spec if it differs.

`SourceController` gains `transform_stage` (property) and
`attach_transform_stage(group, canvas)`; it connects
`render_controller.artifact_ready → transform_stage.open_artifact` and sets
`render_controller.transform_restore = transform_stage.restore_for`;
`shutdown()` calls `transform_stage.shutdown()`. `attach` connects
`canvas.command_requested → store.dispatch` (nothing else does, D5) and the
group's three signals to the controller. `app.py` calls
`controller.attach_transform_stage(window.inspector.transform_group,
window.result_canvas)` after `set_dialog_parent`. `restore_for` dispatches
synchronously (`ReducerStore.dispatch`, `store.py:21-24`), so a
`_current_request()` built after it already carries the restored transform —
that ordering is E17.

### 4.10 `src/matteloop/ui/result_player.py` (new, Stage C, ≤ 500 lines)

```
PLAYER_CACHE_BUDGET_BYTES = 128 * 1024 * 1024
PLAYER_MIN_DISPLAY_SIDE = 64

@dataclass(frozen=True)
class PlayerFrames:
    key: object                          # (cache_key, framing, crop, resize, display size) identity
    framed: tuple[QImage, ...]           # stored frames 0..cached-1, framed only, display-scaled
    transformed: tuple[QImage, ...] | None   # same frames, framed + transformed; None while deferred (crop-edit on)
    delays_ms: tuple[int, ...]           # full grid from webp_delays; the canvas slices
    cached: int                          # frames held (== frame_count unless the budget truncated)
    frame_count: int

class ResultPlayerCanvas(CropCanvas):
    playhead_changed = Signal(int)       # stored-cut frame index
    def __init__(self, parent=None, *, runtime_root: Path | None = None) -> None   # title "Result", object_name "result_canvas"
    def set_frames(self, frames: PlayerFrames | None) -> None    # None restores the stage's cover mode
    def set_kept_range(self, kept: range) -> None                # trim: re-slices, never reloads
    def set_crop_edit(self, enabled: bool, presentation: CropPresentation | None) -> None
    def set_aspect_lock(self, ratio: Fraction | None) -> None
    def play(self) -> None; def pause(self) -> None
    @property
    def playing(self) -> bool
    @property
    def current_frame(self) -> int | None
    play_button: QPushButton             # object name "result_play", hidden without frames
```

The `CropPresentation` for crop-edit mode is built from the facts: `width =
coded_width = framed_w`, `height = coded_height = framed_h`, `rotation = 0`,
`pixel_aspect = 1.0`, `source_id = cache_key`, `crop = transform.crop or the
full framed rectangle`.

`_FrameLoadWorker(QObject)` reads stored frames via the session's
`FrameReader`, applies `apply_framing` then `apply_transform`, scales both
results to the budgeted display size with
`Qt.AspectRatioMode.KeepAspectRatio`, and emits one `PlayerFrames`. When only
the transform changed and the framed cache's key still matches, the worker
receives the existing framed images and rebuilds the transformed cache alone.
Runs on a `QThread` owned by `TransformStageController`, which also owns the
250 ms debounce timer (D7).

---

## 5. Edge cases (each mapped to the criterion or test that covers it)

| # | Edge case | Behaviour | Covered by |
|---|---|---|---|
| E1 | Default `TransformSpec` on rebuild | Encoder receives the same PNG paths, bytes and delays as before; `apply_transform` returns its input object | AC 1 — `test_rebuild.py::test_identity_transform_rebuild_is_byte_identical` |
| E2 | Trim `2..5` on an 8-frame cut | 4 frames, delays = `delays[2:6]` of the full grid (not recomputed — `webp_delays` is non-uniform). At 3 fps the full grid is `333,334,333,333,334,333,333,334`, so the kept sequence is `333,333,334,333`; a recomputed `webp_delays(4, 3)` would be `333,334,333,333` — same sum, different order, so the test must compare the **sequence**, not the sum | AC 2 — `test_rebuild.py::test_trim_keeps_exactly_the_selected_frames_and_their_delays` (fps 3; assert `artifact.delays_ms == full[2:6]` and the encoder's recorded delays) |
| E3 | `last_frame >= frame_count`, or `first_frame > last_frame` | `ValidationError(INVALID_TRANSFORM)` naming the bound; raised in `stage_encoder_frames` before the framed directory exists; no output written; old output untouched | AC 3 — `test_rebuild.py::test_trim_outside_the_cut_is_rejected_before_any_file_is_written` |
| E4 | Trim to a single frame | Still WebP path (`animated = count > 1` in webp.py) — allowed | `test_rebuild.py` trim test parametrised with `first == last` |
| E5 | Four mismatch modes on 1:1 → 256×128 | dims and pixels per §4.2 table | AC 4 — `tests/core/test_transform.py` (4 tests + pad columns byte-equal to the source) |
| E6 | Width only on 2:1 | 256×128 for every mode | AC 5 — `test_transform.py` parametrised over modes |
| E7 | Both axes `None`, or derived dimension < 128, or given dimension > 16383 | `ResizeSpec`/`validate_for` raise `INVALID_TRANSFORM` at validation, never at encode | AC 5 — `tests/core/test_specs.py` |
| E8 | `resize` target equals the cropped size | `resample=False`; no `RGBa` round trip | `test_transform.py::test_resize_to_the_same_size_does_not_resample` (pixel equality on a semi-transparent frame) |
| E9 | `max_bytes` set and the resized output exceeds it | encode succeeds; `EncodeSummary` reports the auto-fit size; artifact width/height smaller than requested; the completion dialog and the readout show them | AC 6 — `test_rebuild.py::test_auto_fit_shrinks_a_resized_output_and_reports_actual_size` |
| E10 | Any number of transforms and rebuilds | Stored PNG sha256s in the manifest unchanged; `validate_cut_set` still passes; no new entries in the cut directory | AC 7 — `test_rebuild.py::test_transforms_never_touch_stored_cut_frames` |
| E11 | Non-identity rebuild then reopen | Sidecar written beside the cut directory; `load_transform` returns the same spec; `list_workspaces` still lists the cut; old-code readers ignore the file | AC 8 — `tests/jobs/test_transform_store.py`, `test_rebuild.py::test_rebuild_records_the_applied_transform_beside_the_cut` |
| E12 | Identity rebuild after a non-identity one | Sidecar removed; reopen → identity | `test_transform_store.py::test_identity_store_removes_the_sidecar` |
| E13 | Sidecar corrupt / wrong cache key / wrong version / unwritable directory | identity + note; job never fails because of the sidecar | `test_transform_store.py` (3 cases) + degrade test with a read-only `cuts_root` skipped on Windows |
| E14 | Framing changed (padding/trim/stretch) after a crop was set | framed size changes → controller clamps the crop and dispatches; a stale crop that still escapes is rejected by `validate_for` with the framed size in the message | `test_specs.py` (validate_for), `tests/ui/test_transform_stage.py::test_facts_change_clamps_the_crop` |
| E15 | Framing trim on with the transform | union computed over **all** stored frames, not the kept ones — the framed size and crop space are independent of the trim | `test_rebuild.py` trim test asserts the framed size equals the untrimmed rebuild's |
| E16 | `cover` with odd overflow / `pad` with odd remainder | extra pixel right/bottom | `test_transform.py::test_cover_and_pad_place_the_odd_pixel_right_and_bottom` (129×128 → 128×128 cover; 128×128 → 257×128 pad) |
| E17 | Reopen via "Use this set" | transform restored **before** the rebuild request is built; the rebuild uses it | `tests/ui/test_render_controller.py::test_use_this_set_restores_the_stored_transform_before_rebuilding` |
| E18 | Render (not rebuild) with a transform set | applied by the shared stage; sidecar written for the new cut; inspector keeps the transform | `test_render.py::test_render_applies_the_request_transform` |
| E19 | Transform edits while a job runs | ignored by the reducer (`can_edit` false) | `tests/core/test_parameters.py` |
| E20 | Transform edits and the preview | preview stays CURRENT (no invalidation); Render skips the preflight | `test_parameters.py::test_transform_change_does_not_invalidate_the_preview` |
| E21 | Fake preset row | appears in the combo under its platform with no other change | AC 10 — `tests/ui/test_transform_group.py` (inject `presets=`) |
| E22 | Preset chosen | width/height filled, mismatch untouched, readout updated | AC 10 |
| E23 | Player reads a frame that fails (`OSError`, `UnidentifiedImageError`) | worker reports; canvas shows "Cut frames could not be read"; no traceback on the GUI thread | `tests/ui/test_result_player.py` |
| E24 | Transform changed while a load is in flight | generation counter drops the stale `PlayerFrames` | `test_transform_stage.py` |
| E25 | Player loop | cycles exactly `kept_range` at the kept delays; Play/Pause toggles; zero segmentation/encoder calls | AC 9 — `test_result_player.py` with `FakeSegmenter`/`FakeEncoder` from `tests/jobs/render_support.py` |
| E26 | New preview image arrives while playing | canvas pauses and shows the preview; Play resumes the loop | `test_result_player.py` |
| E27 | Externally edited cut frames | the player reads current bytes (what the next rebuild uses); nothing else changes | covered by E23 style test with a rewritten PNG |
| E28 | Delete cut set from the picker | sidecar removed with it | `tests/ui/test_workspace_dialog.py` (or the controller test) |
| E29 | Tab order with the new disclosure | `tab_widgets()` includes the group; the existing tab-order test's expected list gains the transform disclosure between "Crop & Cleanup" and "Output" | `test_main_window_state_and_source_drop.py::test_actual_tab_order...` (expected list updated) |
| E30 | Arrow-key nudge on the result canvas in crop-edit mode | emits `TransformChanged` with the nudged (and aspect-constrained) crop; **never** `CropChanged` (`crop_canvas.py:210` routes through the factory) | **regression guard** — `test_result_player.py::test_keyboard_nudge_never_emits_a_source_crop_change` |
| E31 | `pad` result shown in the player | full 256×128 visible including the transparent columns (fit mode, not cover) | `test_result_player.py::test_session_displays_in_fit_mode_and_restores_cover` |
| E32 | Long cut (frames × display size over `PLAYER_CACHE_BUDGET_BYTES`) | display size shrinks to fit; below the 64 px floor only the first N frames are cached and the status marker names N of M; no exception, no refusal | `test_result_player.py::test_cache_budget_shrinks_then_truncates` (budget injected small) |
| E33 | Ten `TransformChanged` crop edits within 250 ms | one transformed rebuild after the timer; zero rebuilds while crop-edit is on; the framed cache is reused (same key) | `test_transform_stage.py::test_crop_edits_are_debounced_and_reuse_the_framed_cache` |
| E35 | Crop-only transform whose crop is 100×100 | `validate_for` raises `INVALID_TRANSFORM` ("crop height 100 is below the 128 px minimum"), not `INVALID_FINAL_DIMENSIONS` | `test_specs.py` |

---

## 6. Task order

Three stages; each ends mergeable and green. Test first, always: the test is
written, run and seen to fail for the stated reason before the implementation.
Every new function ≤ 60 lines — the split points named in §4 (`_stage_frame`,
`_validate_range/_crop/_size`, one helper per resize mode, `_build_*` rows in
the group) are part of the contract, not a suggestion; re-check
`check_guardrails.py` after each task.

### Stage A — Core (AC 1–8)

**A1. Specs and error code.**
- Test first: `tests/core/test_specs.py` — `TransformSpec()` is identity;
  `ResizeSpec()` with both axes `None` raises `INVALID_TRANSFORM`; width 127
  and 16384 rejected; `validate_for` rejects `last_frame >= frame_count`
  (message contains the bound) and a crop outside the framed size; `validate_for`
  returns the final size; `RenderRequest` default carries identity and
  `replace(request, transform=...)` validates. Fails: `ImportError`.
- Files: `core/errors.py` (+1), `core/specs.py` (+~150), `core/parameters.py`
  (field only, +2). **`core/fingerprints.py` is not touched** (§4.11).
- Budget: specs.py ≈ 650/800; no frozen file.

**A2. Pixel operations.**
- Test first: `tests/core/test_transform.py` — the four AC 4 cases with pixel
  assertions on a synthetic 128×128 frame (left half red, right half blue,
  opaque; plus one semi-transparent pixel to catch premultiply drift), AC 5,
  E8, E16, identity returns the same object, crop-only output size and pixels,
  output mode is RGBA. Fails: `ImportError`.
- Files: `core/transform.py` (new).
- Budget: new module ≤ 300; functions ≤ 60 (split `apply_transform` into
  `_crop`, `_resample`, `_place`).

**A3. Move the two output-error helpers to `jobs/encoding.py`** (mechanical).
- Test first: none needed beyond the existing suite; run `tests/jobs` before
  and after — identical results. `ruff` proves nothing else moved.
- Files: `jobs/encoding.py` (+35 plus `import errno`), `jobs/render.py` (−35,
  import extended; `import errno` stays — `:2008`).
- Budget: render.py ≤ 2938 — check.

**A4. Extract the framing loop into `jobs/transform_stage.py`** (mechanical, identity only).
- Test first: `tests/jobs/test_transform_stage.py` — `stage_encoder_frames`
  with an identity spec on a 3-frame fake reader writes `frame-00000{0,1,2}.png`
  with the framed pixels and returns the delays unchanged; `framing_plan` raises
  `INVALID_FRAMING` for trim without union. Fails: `ImportError`.
  Then update the two seams in `tests/jobs/test_render.py:1550-1556, 2968` to
  `transform_stage` and confirm they still pass.
- Files: `jobs/transform_stage.py` (new; `stage_encoder_frames` +
  `_stage_frame`), `jobs/render.py` (−47 +8 for the loop, −34 for
  `_persist_framed_png`, import lines, `import tempfile` removed —
  `apply_framing` stays, it serves the preview at `:906`/`:920`).
- Budget: render.py must be strictly below 2938 after this task; record the
  count in the commit body. `_encode_snapshot` becomes shorter (still counted
  as long; the count of long functions must not rise).

**A5. Apply the transform in the stage.**
- Test first: `tests/jobs/test_rebuild.py` — AC 1 byte identity (rebuild
  twice, once with `transform=TransformSpec()` explicitly, once via
  `request(...)` default, compare `output.webp` bytes and the encoder's
  recorded paths/delays); AC 2 trim with fps 3 (`validate_webp` frame count
  and `delays_ms` sum); AC 3 (`pytest.raises(ValidationError)`, output
  unchanged, no `framed-inputs` directory left under scratch); crop+resize
  dimensions via `validate_webp`; E15 framed size independence; AC 7 sha256
  check via `FilesystemWorkspacePort().validate(...)`. Fails: transform
  ignored / attribute errors.
- Files: `jobs/transform_stage.py`, `jobs/render.py` (frame-count substitutions
  at 1395/1460 — same line count).
- Budget: no growth in render.py.

**A6. Auto-fit interplay (AC 6).**
- Test first: `test_rebuild.py` — resize to 512×256 with `max_bytes` small
  enough to force a fit on a noisy synthetic cut; assert the artifact's
  width/height are below 512×256 and the job succeeded. Use the real
  `PillowWebPEncoder` through `FakeEncoder`-style wiring only if the fake cannot
  fit; otherwise a `FittingFakeEncoder` that calls `auto_fit_webp`. Fails:
  attribute mismatch or no shrink.
- Files: tests only, unless the stage needs `tracker.include_size` for the fit
  sizes (then `transform_stage.py`).

**A7. Sidecar store.**
- Test first: `tests/jobs/test_transform_store.py` — round trip; missing →
  identity; corrupt JSON / wrong schema / wrong cache key → identity with a
  note; identity store removes; `list_workspaces` still lists the cut and
  `validate_cut_set` passes with the sidecar present; `discard_transform` on a
  missing file is silent. Fails: `ImportError`.
- Files: `jobs/transform_store.py` (new).

**A8. Record the applied transform in the job layer.**
- Test first: `test_rebuild.py` — after a rebuild with a trim,
  `load_transform(artifact.cut_workspace)` equals the request's transform;
  after a subsequent identity rebuild the sidecar is gone; a render with a
  transform records it too (E18); an unwritable `cuts_root` (POSIX only) adds a
  note and the artifact still publishes. Fails: identity returned.
- Files: `jobs/render.py` (+1 line inside `_encode_snapshot`, between the
  `if published:` cleanup and `assert summary is not None` — see D1 for why
  not after the call; both `render()` and `rebuild()` pass through here).
- Budget: render.py still below 2938 (A4 bought ≈ 100 lines).

**A9. Docs.** `docs/v1-scope.md`: add a "Transform" row to the "In scope"
table under "Cuts" with the 2026-09-04 scope-reopen note and "#25". No other
doc changes.

**Stage A verification.** Full gate; `check_guardrails.py` passes with the
baseline file unchanged (`git diff --exit-code scripts/guardrails-baseline.json`);
`mypy src` reports exactly the 9 pre-existing errors; render.py line count
recorded in the PR description. The branch is mergeable here: the feature is
reachable (any request carries the transform) even though the UI does not yet
expose it, and every existing path is byte-identical.

### Stage B — Inspector (AC 10)

**B1. Reducer event.**
- Test first: `tests/core/test_parameters.py` — `TransformChanged` replaces
  the transform, leaves `preview` untouched (E20), is ignored while a job runs
  (E19) and when unchanged; `parameters_from_values` yields identity;
  `persist_parameters` writes no transform key. Fails: `ImportError`.
- Files: `core/parameters.py` (+~16).

**B2. Request mapping.**
- Test first: `tests/ui/test_parameter_requests.py` — extend the existing
  "share every inspector parameter" test with a non-identity transform;
  `tests/ui/test_workspace_dialog.py` or a new `test_workspace_presentation.py`
  — `request_for_workspace` forwards `base.transform`. Fails: attribute
  absent / identity.
- Files: `ui/request_builder.py` (+1), `ui/workspace_presentation.py` (+1).

**B3. Presentation.**
- Test first: `tests/ui/test_parameter_presentation.py` — `present_parameters`
  exposes `transform` and `artifact`. Fails: `AttributeError` (the new fields
  have defaults, §4.7, so existing constructions keep working).
- Files: `ui/parameter_presentation.py` (+5).

**B4. `TransformGroup` widget.**
- Test first: `tests/ui/test_transform_group.py` — AC 10 preset fills
  width/height and leaves mismatch; fake preset row appears under a fake
  platform header (E21); each control emits a valid `TransformChanged`; both
  axes Auto shows the inline error and emits nothing; percentage fills and
  clears; aspect preset emits a centred crop and `aspect_lock_changed`;
  `apply` with `editable=False` disables everything; `set_cut(None)` disables;
  readout wording with and without an artifact; `tab_widgets()` order. Fails:
  `ImportError`.
- Files: `ui/transform_group.py` (new), `core/crop.py` (+~50 for
  `fit_crop_aspect` / `centered_crop_for_aspect`, tested in
  `tests/core/test_crop.py` first).

**B5. Mount in the inspector; free the lines.**
- Test first: `tests/ui/test_parameter_inspector.py` — the "transform"
  disclosure exists with title "Transform" and is collapsed by default; the
  group is inside its body; `tab_widgets()` contains the disclosure and the
  group's widgets in order. Update the expected list in
  `test_actual_tab_order_skips_hidden_widgets...` (E29). Fails: `KeyError`.
- Files: `ui/inspector.py` (net −1 → 797 per D6), `ui/inspector_disclosure.py`
  (+~5 for `read_bool`), `ui/crop_view.py` (+1).
- Budget: inspector.py ≤ 800 — record the count in the commit body. If the
  count still lands above 800 (an import wrapped by isort, say), the next
  mechanical extraction is `_set_provider_options` (`:779-797`, pure combo
  bookkeeping) into a module-level helper beside `build_provider_options` —
  only then, never speculatively.

**B6. Session controller and wiring.**
- Test first: `tests/ui/test_transform_stage.py` — `open_artifact` with a
  duck-typed artifact lacking a workspace is ignored; with a real cut (rendered
  through `tests/jobs/render_support` fakes into `tmp_path`) facts arrive with
  the manifest frame count, the framed size from the same `framing_plan`, the
  cut's fps and `webp_delays`; `restore_for` dispatches the stored transform
  (and identity when none); a padding change re-computes facts and clamps the
  crop (E14); `shutdown` joins the worker. `tests/ui/test_render_controller.py`
  — `artifact_ready` fires with the worker's artifact; E17 ordering. Picker
  delete removes the sidecar (E28). Fails: `ImportError`/no signal.
- Files: `ui/transform_stage.py` (new; `attach` wires
  `canvas.command_requested → store.dispatch` when a canvas is given),
  `ui/render_worker.py` (+~4), `ui/render_controller.py` (+5, ≤ 800 — record
  the count: `artifact_ready` signal, its connect in `_spawn_worker`, the
  `transform_restore` attribute, two lines in `_use_workspace` *before*
  `_current_request()`), `ui/controller.py` (+~15),
  `ui/workspace_controller.py` (+2: `discard_transform` after each successful
  `delete_workspace`, `:63` and `:73`), `app.py` (+1; `_run_gui` ≤ 60).

**Stage B verification.** Full gate + guardrails; `uv run matteloop` shows the
Transform disclosure, enabled after a render, and Render → "Matching cut set
found" → Rebuild produces the transformed file (manual check noted in the PR,
not a gate).

### Stage C — Result player (AC 9)

**C1. `CropCanvas` reuse hooks.**
- Test first: `tests/ui/test_crop_canvas.py` — a subclass overriding
  `_constrain` and `_crop_event` receives **both** the drag and an arrow-key
  nudge (E30; the base class still emits `CropChanged` for both); default
  behaviour of the existing tests unchanged; `CropCanvas(title="Result",
  object_name="x")` sets those. Fails: `TypeError` on the kwargs.
- Files: `ui/crop_canvas.py` (+~12: kwargs, the two hooks, `:210` and `:245`
  routed through them).

**C2. `ResultPlayerCanvas` and the frame loader.**
- Test first: `tests/ui/test_result_player.py` — with a `PlayerFrames` of 4
  frames and delays `(333, 334, 333, 333)` (= `webp_delays(4, 3)`) and
  `set_kept_range(range(1, 4))` the canvas shows frame 1, advances on the
  timer (use `qtbot.waitUntil` on `current_frame`), wraps to frame 1,
  `pause()` stops advancing, `play()` resumes; a new `set_kept_range` re-slices
  without a new `PlayerFrames`; the play button is hidden without frames and
  accessible with them; crop-edit mode shows the framed cache with the overlay
  and emits `TransformChanged` on drag **and** on an arrow key (E30) with the
  aspect lock applied; E26; E31 (fit mode while loaded, cover restored on
  `set_frames(None)`); E32 with an injected 1 MiB budget; loader on a real
  temp cut produces both caches at display size and AC 9's zero-call
  assertion on `FakeSegmenter`/`FakeEncoder`. Fails: `ImportError`.
- Files: `ui/result_player.py` (new), `ui/preview_canvas.py` (`PreviewStage`
  constructs `ResultPlayerCanvas`; `set_cover_frame` semantics unchanged),
  `ui/transform_stage.py` (owns the loader thread and the debounce timer,
  E33; `attach` with a canvas).
- Existing UI tests that assert `result_canvas` object/accessible names, status
  label, properties and tab position must pass unchanged.

**C3. Playhead coupling.**
- Test first: `tests/ui/test_transform_stage.py` — `use_playhead_requested("last")`
  with the player at stored index 5 emits `TransformChanged(last_frame=5)`;
  buttons disabled when the player has no frames. Fails: no emission.
- Files: `ui/transform_stage.py`.

**Stage C verification.** Full gate + guardrails; the tab-order test still
passes with the play button hidden in a session-less window; a manual loop
check in `uv run matteloop` noted in the PR.

---

## 7. Verification (per stage, identical commands)

```
uv run ruff check .
uv run mypy src                      # exactly the 9 baseline errors, same files
QT_QPA_PLATFORM=offscreen uv run pytest -q
uv run python scripts/check_guardrails.py
git diff --exit-code scripts/guardrails-baseline.json
wc -l src/matteloop/jobs/render.py src/matteloop/ui/inspector.py src/matteloop/ui/render_controller.py
```

The three `wc -l` numbers go into each stage's PR comment. Commits: summary
line, blank line, one to two lines per changed point; `fix:` only with a
`Trigger:` line; third consecutive `fix:` on one file stops and reports.

---

## 8. Overrides of the spec's file table (forced by the line budgets)

| Spec said | Plan does | Why |
|---|---|---|
| `render.py:1155-1257` gains frame selection, crop, resize | `jobs/transform_stage.py` (new) gains them; render.py loses ≈ 100 lines (D3) | render.py frozen at 2938 |
| `_manifest.py:141-159` persists the transform | `jobs/transform_store.py` sidecar beside the cut directory (D1) | manifest modules frozen; exact-key + version gates would strand data on rollback; in-directory sidecar invalidates the cut |
| `inspector.py:648-669` gains the Transform group | `ui/transform_group.py` (new); inspector mounts it with 6 lines and gives back 8 (D6) | inspector.py at 798/800 |
| `preview_canvas.py:115-200` gains the player | `ui/result_player.py` (new); `PreviewStage` swaps the class (D7) | keep the player reviewable; preview_canvas stays a base |
| crop/resize in `core/webp.py` next to `_resize_from_sources` | `core/transform.py` (new) with the same premultiply idiom (D2) | webp.py frozen |
| `TransformSpec.validate(frame_count, framed_size)` | `validate()` + `validate_for(frame_count, framed_size)` | mirrors `CropSpec`; same behaviour |
| `ResizeSpec.mismatch: Literal[...]` | `MismatchMode(StrEnum)` with the same four values | house idiom; identical serialisation |
| AC 8 "from the manifest" | from the sidecar; recorded deviation | see D1 |
| "Delays are computed for the kept frames only" | the encoder's full-grid delays are **sliced** to the kept range | `webp_delays` is non-uniform (`timebase.py:29-47`); slicing is what "the summed delays of exactly those frames" (AC 2) means |
| Player "transformed lazily at canvas resolution" | full-resolution `apply_framing` + `apply_transform` per stored frame on a worker, cached at display size under a 128 MiB budget, two caches (framed / transformed), transformed rebuild debounced | the design rule at `matteloop-desktop-app.md:33` requires the encoder's functions on the encoder's inputs; the budget keeps memory bounded for any frame count |
| "The player uses the same per-frame delays the encoder writes (… they are uniform)" | same delays, but they are **not** uniform — sliced from the full grid | `timebase.py:41-46` |
| `TransformSpec.validate(...)` "called from `rebuild()`" | called from `stage_encoder_frames`, which both `render()` and `rebuild()` reach through `_encode_snapshot`; the inspector readout calls the same function early for the inline message | one validation function, two callers, one message |

Everything else in the spec — presets as data, no rotate/flip/thinning, crop
in framed-frame space, centre-only cover, transparent-only padding, byte
identity for the WebP only — is implemented as written.

---

## 9. Review outcome (eng review, 2026-09-04)

### NOT in scope

- Rotate, flip, frame thinning, fps change, non-transparent padding, cover
  anchors, 7TV upload or limits — the spec's own exclusions (G8).
- A transform for the *source* crop or range — the frames do not exist.
- Growing any frozen module or touching `scripts/guardrails-baseline.json` —
  the constraints file forbids it; every budget overrun in §3 has a named
  fallback that extracts instead.
- Migrating or reading a transform from the manifest — D1 rejects it with
  evidence; the sidecar is the persistence.
- Distribution: no new artifact type; the existing macOS/Windows packaging is
  untouched.

### What already exists (reused, not rebuilt)

| Sub-problem | Existing | Plan |
|---|---|---|
| Premultiplied LANCZOS resize | `webp.py:1226-1236` | idiom copied exactly into `core/transform.py` (webp.py frozen) |
| Rounding | `geometry._round_positive_fraction` | reused for every derived dimension |
| Crop handles, drag, nudge, clamp | `crop_canvas.py`, `core/crop.py` | subclassed / extended; not forked |
| Frame reading, framing plan | `apply_framing`, `FramingPlan` | called from the shared stage by encoder and player |
| Delays | `webp_delays` | sliced, never recomputed |
| Atomic file write | `_persist_framed_png` pattern | reused for the sidecar (G3 ceiling) |
| Worker thread idiom | `preview_controller/controller.py:192-202` | copied for facts and frame loaders |
| Pillow `ImageOps.contain/fit/pad` | stdlib-adjacent [Layer 1] | rejected (rounding, float box, no premultiply — §4.2) |

### Failure modes (per new codepath)

| Path | Realistic failure | Test | Handling | User sees |
|---|---|---|---|---|
| `stage_encoder_frames` | `validate_for` rejects a stale crop after a framing change | E14, E3 | `INVALID_TRANSFORM` before any file | inline message in the group; the job dialog names the bound |
| `apply_transform` | allocation budget exceeded by `cover`'s `scaled` | E7-adjacent (`_validate_allocation_budget`) | `ValidationError` | error dialog, no partial output |
| `store_transform` | read-only `cuts_root` | E13 | note, never raised | rebuild succeeds; note in the completion summary |
| `load_transform` | corrupt / foreign sidecar | E13 | identity + note | cut opens as identity |
| `_FrameLoadWorker` | unreadable PNG | E23 | worker reports | "Cut frames could not be read" |
| `_FrameLoadWorker` | thousands of frames | E32 | budget + truncation | "Previewing the first N of M frames" |
| `_CutFactsWorker` | stale result after a fast framing change | E24 | generation counter | none |
| `ResultPlayerCanvas.keyPressEvent` | source crop dispatched by mistake | E30 | factory hook | none — the regression guard exists for this |
| `_use_workspace` restore | `TransformChanged` ignored because a job is running | E19 | reducer no-op | picker closes; identity used — acceptable, a job is running |

No path is silent, untested and unhandled: **0 critical gaps**.

### Worktree parallelization

| Step | Modules | Depends on |
|---|---|---|
| A1–A2 specs, transform | `core/` | — |
| A3–A6 extraction + stage | `jobs/`, `tests/jobs` | A1–A2 |
| A7–A8 sidecar store | `jobs/` | A4 (placement inside `_encode_snapshot`) |
| B1–B3 reducer, request mapping, presentation | `core/parameters.py`, `ui/` (small files) | A1 |
| B4 `TransformGroup` + `core/crop.py` helpers | `ui/transform_group.py`, `core/crop.py` | A1, B3 |
| B5–B6 inspector mount, controller, wiring | `ui/inspector.py`, `ui/render_controller.py`, `ui/controller.py` | B4, A7 |
| C1–C3 player | `ui/crop_canvas.py`, `ui/result_player.py`, `ui/transform_stage.py` | B6 |

Lane A: A1 → A2 → A3 → A4 → A5 → A6 → A7 → A8 (sequential, shared `jobs/render.py`).
Lane B: B1 → B2 → B3 → B4 (after A1 only; independent of A3–A8).
Then B5 → B6 (needs both lanes), then C1 → C2 → C3.
Launch A3+ and B1–B4 in parallel worktrees after A1–A2 merge; conflict flag:
both lanes touch `core/parameters.py` (A1 adds the field, B1 the event) —
keep A1's two-line field addition and B1's event in separate commits.

## Implementation Tasks
Synthesized from this review's findings. Each task derives from a specific
finding above. Run with Claude Code or Codex; checkbox as you ship.

- [ ] **T1 (P1, human: ~1h / CC: ~5min)** — `jobs/render.py` — Call `store_transform` inside `_encode_snapshot`, not after it
  - Surfaced by: Architecture — `tuple(notes)` baked at `render.py:1471`; notes appended later are lost
  - Files: `src/matteloop/jobs/render.py`, `tests/jobs/test_rebuild.py`
  - Verify: A8's "unwritable cuts_root adds a note" test sees the note on the artifact
- [ ] **T2 (P1, human: ~1h / CC: ~10min)** — `ui/crop_canvas.py` — Route `keyPressEvent` through `_crop_event`/`_constrain`
  - Surfaced by: Architecture — `crop_canvas.py:210` emits `CropChanged` directly
  - Files: `src/matteloop/ui/crop_canvas.py`, `tests/ui/test_crop_canvas.py`, `tests/ui/test_result_player.py`
  - Verify: E30 regression guard
- [ ] **T3 (P1, human: ~2h / CC: ~15min)** — `ui/result_player.py` — Bound the caches (128 MiB), debounce transformed rebuilds, fit mode while loaded
  - Surfaced by: Performance — unbounded per-frame caches; reload per mouse move; cover mode crops `pad` output
  - Files: `src/matteloop/ui/result_player.py`, `src/matteloop/ui/transform_stage.py`
  - Verify: E31, E32, E33
- [x] ~~**T4** — `core/fingerprints.py` — add the transform to `render_fingerprint`~~ **DROPPED**
  - `fingerprints.py` is off-limits on this branch (rembg gate). The fingerprint
    is in-memory only and has no consumer, so the omission changes no behaviour.
    See §4.11. E34 is struck with it.
- [ ] **T5 (P2, human: ~30min / CC: ~5min)** — `core/specs.py` — Every `validate_for` failure is `INVALID_TRANSFORM`; bounds apply to the final canvas
  - Surfaced by: Code quality — crop-only final < 128 would surface as a framing error
  - Files: `src/matteloop/core/specs.py`, `tests/core/test_specs.py`
  - Verify: E35
- [ ] **T6 (P2, human: ~30min / CC: ~5min)** — `ui/transform_stage.py` — `attach` connects `canvas.command_requested`; drop `CutSession.framing`
  - Surfaced by: Architecture — `main_window.py:193-194` wires three widgets only; E14 needs current framing
  - Files: `src/matteloop/ui/transform_stage.py`, `tests/ui/test_transform_stage.py`
  - Verify: B6/C2 tests; a drag on the result canvas reaches the store
- [ ] **T7 (P2, human: ~15min / CC: ~2min)** — tests — Compare the delay *sequence* in the AC 2 test
  - Surfaced by: Test review — sum equality cannot distinguish slicing from recomputation
  - Files: `tests/jobs/test_rebuild.py`
  - Verify: E2
- [ ] **T8 (P2, human: ~15min / CC: ~2min)** — `ui/inspector.py` — Recount after B5 with the import line included (target 797)
  - Surfaced by: Code quality — the D6 table omitted the `TransformGroup` import
  - Files: `src/matteloop/ui/inspector.py`
  - Verify: `wc -l` in the commit body; `check_guardrails.py`

_No new tasks from Step 0 (scope accepted as-is)._

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | — |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | — | outside voice abandoned by the coordinator; a Sol review runs on the finished diff |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR (PLAN) | 17 issues, 0 critical gaps, all folded into §2–§9 |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | — |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

- **AUTO-DECIDED (spawned session):** scope accepted as-is despite the >8-file smell (the spec's AC 1–10 and the frozen-module budgets force the module count); every finding applied with its recommended option; no baseline, source or test file touched.
- **VERDICT:** ENG CLEARED — ready to implement.

NO UNRESOLVED DECISIONS
