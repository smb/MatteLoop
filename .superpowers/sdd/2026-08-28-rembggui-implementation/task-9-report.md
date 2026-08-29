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
  descriptor opened with `O_DIRECTORY|O_NOFOLLOW`. On Windows, the lexical
  drive-volume or UNC-share anchor and every component through cache root,
  version, and model are opened hierarchically without delete sharing.
  Immediately after each `CreateFileW`,
  `GetFileInformationByHandleEx(FileAttributeTagInfo)` proves that the opened
  object has `FILE_ATTRIBUTE_DIRECTORY` and not
  `FILE_ATTRIBUTE_REPARSE_POINT`; all validated handles remain held while the
  next full-path component is opened/created and until the file operation
  finishes. There is no pre-binding `resolve`, `lstat`, or recursive directory
  creation window. Missing-component early returns and all exceptions close
  every already-opened handle exactly once in reverse order.
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
- A directory-handle close failure after download, verified cache reuse, failed
  download cleanup, or removal is mapped to the existing structured storage
  error. When cleanup fails while another operation is already failing, the
  cleanup error remains visible and the original failure is retained as the
  direct cause.

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
  MD5 explicitly as upstream provenance while enforcing the separately pinned
  application SHA-256.
- Each app SHA-256/size has a secondary large-file-metadata witness at a fixed
  commit: the seven classic U²-Net/IS-Net/Silueta assets use
  `tomjackson2023/rembg@cd3a3d6767a7859efea31ef0f2f373582cf06d82`; seven
  BiRefNet assets use
  `EmmaJohnson311/TensorRT-ONNX-collect@43d1d62b06bac8b7d3886a209771f6d7ca10d899`;
  BRIA uses
  `ChuuniZ/comfyui-image-models@302f8bb8c9606587dae63532702ef3b72208cce7`.
  These secondary witnesses are not official GitHub release checksums and do
  not establish byte identity with the upstream release asset.
- BRIA's checksum literal in pinned rembg source matches the app pin, but was
  likewise not independently live-qualified here.
- `resources/model-provenance.json` records, per local ID, the exact URL, size,
  app SHA-256, commit-bound witness URL/SHA/size/trust status, upstream
  checksum, exact pinned source file, and checksum-status wording. It dates the
  record and assigns live artifact qualification explicitly to Task 17.
- Automated tests use synthetic bytes and an injected transport. No live model
  was downloaded and this report does not claim live release-asset
  qualification; Task 17/manual release qualification must download each
  official GitHub asset, verify the app SHA-256 and the pinned rembg upstream
  checksum, and run inference on each target architecture.

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

### Fix round 2

RED evidence:

```text
uv run pytest tests/jobs/models/test_cache_fs.py -q
3 failed: _bind_windows did not accept the post-open API seam

uv run pytest tests/jobs/models/test_cache_fs.py::test_windows_binding_rejects_open_handle_without_directory_identity -q
1 failed: missing DIRECTORY handle identity was accepted

uv run pytest tests/jobs/models/test_cache_fs.py::test_windows_early_return_attempts_every_close_after_one_close_failure -q
1 failed: one CloseHandle error prevented remaining handle cleanup

uv run pytest tests/jobs/models/test_catalog.py::test_each_local_pin_has_honest_auditable_provenance -q
1 failed: provenance still named an unsupported internal source label
```

GREEN evidence:

```text
uv run pytest tests/jobs/models -q
91 passed in 0.66s

uv run pytest -q
610 passed in 39.60s

uv run ruff check .
All checks passed!

uv run mypy src
Success: no issues found in 23 source files

git diff --check
clean
```

No network transport, official release asset, or real ONNX inference was used
by the automated tests. The BRIA secondary witness revision was resolved to
`302f8bb8c9606587dae63532702ef3b72208cce7`; its metadata exposes the same
SHA-256 as the app pin, without being treated as official-release byte proof.

### Fix round 3

RED evidence:

```text
uv run pytest \
  tests/jobs/models/test_cache_fs.py::test_windows_binding_holds_anchor_through_every_cache_ancestor \
  tests/jobs/models/test_cache_fs.py::test_windows_binding_blocks_redirect_of_ancestor_above_cache_root \
  tests/jobs/models/test_cache_fs.py::test_windows_path_chain_supports_drive_and_unc_anchors \
  tests/jobs/models/test_cache_fs.py::test_windows_path_chain_rejects_unanchored_or_drive_relative_roots -q
6 failed: binding began at cache_root and exposed the cache-root ancestor

uv run pytest \
  tests/jobs/models/test_download.py::test_bound_directory_close_oserror_is_structured_for_download_and_reuse \
  tests/jobs/models/test_download.py::test_bound_close_cleanup_failure_preserves_primary_download_error \
  tests/jobs/models/test_session.py::test_remove_maps_bound_directory_close_oserror_after_visible_cleanup -q
4 failed: raw OSError escaped download, reuse, failure cleanup, and remove

uv run pytest tests/jobs/models/test_cache_fs.py::test_windows_path_chain_rejects_traversal_and_device_namespaces -q
3 failed, 3 passed: non-canonical UNC traversal shares and a non-ASCII drive were still accepted
```

The deterministic Windows seam attempted to rename/junction an ancestor above
`cache_root` exactly before opening `cache_root`. The old implementation had no
handle for that ancestor; the redirect succeeded and the later root lookup was
redirected. The corrected implementation already holds the ancestor without
delete sharing, so the redirect is blocked. Separate assertions prove
anchor-to-leaf opening order,
post-open identity queries, held ancestors during each one-level
`CreateDirectoryW`, and reverse exactly-once cleanup after missing components,
midway native-open failure, identity failure, and `CloseHandle` failure.

GREEN evidence:

```text
uv run pytest tests/jobs/models -q
110 passed in 0.67s

uv run pytest -q
629 passed in 39.57s

uv run ruff check .
All checks passed!

uv run mypy src
Success: no issues found in 23 source files

git diff --check
clean
```

The Windows path parser accepts canonical absolute drive and UNC-share roots,
rejects relative, drive-relative, rooted-without-drive, traversal, and device
namespace forms, and performs no filesystem resolution before the native
handle chain begins. Tests remain synthetic and cross-platform: no network,
real ONNX, or Windows-only runtime is required to exercise the native API seam.

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
