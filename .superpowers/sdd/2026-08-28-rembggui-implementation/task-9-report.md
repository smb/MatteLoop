# Task 9 implementation report

## Outcome

Implemented the pinned model catalog, verified model acquisition, one-session
replacement lifecycle, and the manifest-bound Task 8 child launch boundary.

- The catalog exposes exactly the approved 17 public IDs and defaults to
  `birefnet-portrait`.
- Fifteen prompt-free local entries carry exact upstream release URLs, runtime
  filenames, expected byte sizes, app-pinned SHA-256 values, and the checksum
  declared by pinned `rembg` 2.0.72 source as provenance.
- `sam` is capability-routed and preview-only with no Task 9 artifact.
- `withoutbg` is capability-routed with the 20 MiB upload limit/privacy
  metadata and no artifact, credential, token, endpoint, or transport.
- BRIA visibly records its 1,024,331,469-byte weight, model-specific license
  caveat, and commercial-use warning.

## Trust boundary and lifecycle

### Manifest

- Strict UTF-8 JSON with duplicate-key and non-finite-number rejection.
- Exact root/model/artifact schemas, exact approved ID set, exact default and
  pinned `rembg` version.
- Deeply immutable `ModelCatalog`/`ModelSpec`/`ModelArtifact`/inference-default
  values (frozen/slots plus tuple and mapping-proxy collections), with no
  custom/unknown model lookup.
- Local/SAM/cloud invariants are validated rather than inferred at call sites.
- Artifact URLs must be unambiguous HTTPS GitHub v0.0.0 release paths without
  credentials, ports, queries, fragments, percent-encoding, backslashes, or
  nested paths; runtime filenames are canonical `<model-id>.onnx` names.

### Downloader

- Requires an injected transport; ordinary tests never use network.
- Binds the passed spec back to the active catalog before opening transport.
- Uses the exact namespace
  `<cache>/2.0.72/<model>/<runtime-filename>.part` and same-directory atomic
  promotion.
- Rejects symlink/reparse/non-directory namespace components and non-regular
  cache files. On POSIX, every cache operation is relative to a bound directory
  descriptor opened with `O_DIRECTORY|O_NOFOLLOW`. On Windows, root/version/
  model directory handles are held without delete sharing, preventing parent
  rename/junction replacement while the full-path APIs run.
- Streams only bounded native chunks, enforces manifest size on both overrun
  and EOF, emits progress only when a valid known total exists, and checks
  cancellation before/between/after reads and before promotion.
- Verifies SHA-256, re-proves the promoted entry is the same written inode,
  fsyncs the file and directory where supported, closes the
  transport before promotion, and exposes cleanup failures instead of hiding
  them.
- Reuses a cache entry only after a fresh descriptor-bound size/SHA proof;
  invalid cache entries are never reused. A process-wide path lock provides
  same-target single flight across downloader instances.
- Maps HTTP, TLS, proxy, general network, permission, disk, size, checksum,
  unsafe-cache, and cancellation failures to structured `AppError` codes.

### Session and Task 8 projection

- `ModelSessionManager` has explicit catalog/downloader/client factory/cache/
  progress/cancellation dependencies.
- A changed local model is fully downloaded and verified before
  `replace_model`; same-active preparation is idempotent. Failed download keeps
  the old session, while failed replacement clears active state truthfully.
- The child launch payload has one exact schema and only includes the built-in
  ID, upstream ID, pinned version, manifest-bound cache home/filename, size,
  SHA-256, and typed catalog-owned inference defaults. Extras, custom paths,
  prompts, and tokens are rejected in Task 9.
- `u2net_cloth_seg` carries the input-free canonical `cloth_category=full`
  default from the pinned rembg semantics. It yields exactly one mask and keeps
  result height/width equal to the source; the GUI receives no free Task 9
  cloth-category option.
- The spawned child reloads the catalog, opens the manifest-bound regular file
  once without following links, reads immutable bytes through that same file
  descriptor, and hashes those exact bytes. The very same `bytes` object is
  passed directly to `onnxruntime.InferenceSession`; ORT never receives a model
  path. The pinned rembg session object is allocated without invoking its
  constructor/downloader. No global rembg/ORT monkeypatch, `U2NET_HOME`, or
  parent/global environment mutation is used.
- SAM/cloud return capability results and never start a local Task 9 child.
  Switching away from local closes the local client once.
- Removal rejects the active model, unknown IDs, symlink traversal, and
  non-regular targets; it removes only the exact version/model artifact.
- A failed close/replacement cleanup retains the exact client handle and a
  conservative pending-ID guard for retry. Active state becomes truthfully
  empty, but neither the prior nor attempted artifact can be removed until a
  later close succeeds. Manager close remains idempotent after success.

## Provenance

- URLs and upstream checksum strings were transcribed from the installed,
  project-pinned `rembg==2.0.72` sources under
  `.venv/lib/python3.13/site-packages/rembg/sessions`.
- For assets where those sources declare only MD5, the manifest retains that
  MD5 explicitly as upstream provenance while enforcing the separately indexed
  app-pinned SHA-256 supplied by the approved Task 9 brief.
- Expected byte sizes and SHA-256 values are app pins from the approved Task 9
  brief; they are not claimed as SHA-256 values published by GitHub or as live
  byte qualification. BRIA's checksum literal in pinned rembg source matches
  the app pin, but was likewise not independently live-qualified here.
- `resources/model-provenance.json` records, per local ID, the exact URL, size,
  app SHA-256, pin method/status, upstream checksum, exact pinned source file,
  and checksum-status wording. It dates the record and assigns live artifact
  qualification explicitly to Task 17.
- Automated tests use synthetic bytes and an injected transport. No live model
  was downloaded and this report does not claim live release-asset
  qualification; Task 17/manual release qualification must acquire every real
  local artifact and run inference on each target architecture.

## TDD evidence

Initial RED:

```text
uv run pytest tests/jobs/models -q
3 collection errors: ModuleNotFoundError: rembggui.jobs.models
```

Initial-review fix RED proofs observed before fixes:

- transport close raised raw `OSError` after promotion;
- part cleanup permission failure was hidden by the checksum error;
- an ambiguous backslash GitHub path was accepted;
- cloth segmentation had no canonical category and could concatenate three
  masks vertically;
- cache paths could be redirected between path checks and promotion/removal;
- ORT consumed a path after hashing rather than the hashed content itself;
- cleanup failure discarded the client handle and removal guard;
- `ModelCatalog` attributes remained assignable;
- output-file close raised an unstructured `OSError`;
- provenance wording overstated what upstream had published/what was live
  qualified.

All were then made GREEN with the production boundary changes described above.

Fix-round focused evidence (synthetic transport/bytes; no network or real ONNX):

```text
uv run pytest tests/jobs/models -q
86 passed in 0.65s

uv run pytest -q
605 passed in 39.51s

uv run ruff check .
All checks passed!

uv run mypy src
Success: no issues found in 23 source files
```

`ruff format` reports all nine changed Python files already formatted, and
`git diff --check` is part of the final pre-commit gate.

## Deliberate tradeoffs / remaining qualification

- Cached local weights are rehashed on offline reuse. Child startup additionally
  reads the complete artifact into immutable bytes and hashes exactly that
  object before giving it to ORT. For the 1,024,331,469-byte BRIA model this
  adds roughly 1.024 GB of Python-side resident content during session setup
  (and may temporarily overlap ORT-native model memory). The cost is deliberate:
  it removes the path-after-hash trust gap for all supported platforms.
- ONNX Runtime construction remains a native, non-interruptible unit as
  designed; cancellation is honored at the surrounding safe boundaries.
- The cache root remains an explicit platform/app dependency rather than a
  hard-coded global path. The child binds its relative
  `<version>/<model>/<filename>` shape and exact content.
- The root `resources/model-manifest.json` is the planned Task 9 resource.
  Task 10 owns frozen-runtime resource inclusion and packaged-path discovery.
- Real artifact availability, native ONNX loading, licenses/notices, and all
  target-platform model runs remain manual release qualification gates; no
  network or multi-gigabyte weight is admitted into ordinary tests.
