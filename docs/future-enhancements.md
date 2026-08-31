# Future enhancements

> **Exploration only — non-committed, outside V1, and no roadmap or delivery promise.**

## Deferred SAM prompt exploration

The following notes preserve an earlier exploration for a possible future
Segment Anything Model (SAM) prompt workflow. They are not a V1 requirement,
do not add a runtime model ID, and must not be read as a commitment to ship.

### Product and interaction idea

- A selected preview frame could offer point-prompted extraction with positive
  and negative points over the oriented, cropped image.
- Points would be stored as normalized `SamPoint(x: float, y: float, label:
  Positive | Negative)` coordinates in `[0, 1]`; at least one positive point
  would be required before a request.
- Source replacement, orientation/crop changes, or another stale-preview
  condition would invalidate points and prevent reuse against different image
  content.
- The initial boundary would be preview-only. Static points do not track a
  moving subject, so full-video rendering remains unsuitable until a separate
  temporal-tracking design exists.
- Switching to a future prompt session may need to evict and close an active
  local model session according to the existing cleanup contract; switching
  back would use normal local session replacement.

### Possible adapter boundary

- The historical catalog sketch used the ID `sam` and a separate
  `ExecutionClass.SAM_PREVIEW` capability rather than a downloadable local
  artifact. That ID and execution class are not part of the current catalog.
- A future `src/matteloop/jobs/models/sam.py` could provide `SamPoint` and
  `build_sam_extras(points, oriented_crop_size)` while keeping the session
  boundary responsible for lifecycle, errors, and cancellation.
- The adapter would map normalized `(x, y)` into crop pixels and pass rembg's
  expected `[y, x]` point order. For example, a point `(0.25, 0.5)` on a
  `400 × 200` crop would become `[100, 100]` as `[y, x]`.
- Positive/negative labels, coordinate bounds, a missing-positive error, and
  stale-source/crop invalidation would be explicit validation rules, not UI
  assumptions.

### Privacy, licensing, and operational questions

- The intended direction was local-only processing and no media upload, but
  any future adapter must prove that property end-to-end.
- Before use, review the applicable Meta license and model terms for the exact
  model/weight combination. Do not infer permission from this exploratory
  note.
- Open decisions include the exact SAM or `sam_vit` variant, weight source and
  acquisition mechanism, checksum/provenance, license review result, rembg
  adapter API, accessibility for adding/removing and distinguishing points,
  temporal tracking semantics, session eviction and memory budget, and
  packaging/distribution of any weights.

### Evidence that a future proposal would need

- Unit tests for positive/negative normalized points, `[y, x]` conversion,
  missing-positive rejection, stale point invalidation, and adapter extras.
- Session tests for preview-only behavior, local-session eviction/cleanup, and
  no side effects for invalid prompts.
- Manual checks for keyboard and screen-reader access, visible point state,
  crop/orientation invalidation, local/privacy behavior, memory pressure, and
  the explicit full-render restriction unless temporal tracking is designed.
- No ordinary test should download a real SAM weight or call a live service.

### Historical planned locations

The earlier draft mentioned `src/matteloop/jobs/models/sam.py`, prompt UI work
under `src/matteloop/ui/`, corresponding model/session tests, and a dedicated
release qualification check. Those files and tasks are historical planning
notes only; they are not current V1 work.
