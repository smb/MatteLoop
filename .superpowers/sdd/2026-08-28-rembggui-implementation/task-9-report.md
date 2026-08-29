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
  version, and model are opened hierarchically with `FILE_SHARE_READ` only:
  neither write nor delete sharing is admitted. The anchor is the only
  full-path `CreateFileW` operation. Every descendant directory is opened or
  created with `NtCreateFile` and `OBJECT_ATTRIBUTES.RootDirectory` set to its
  already-validated parent handle.
  Immediately after every anchor or handle-relative directory open,
  `GetFileInformationByHandleEx(FileAttributeTagInfo)` proves that the opened
  object has `FILE_ATTRIBUTE_DIRECTORY` and not
  `FILE_ATTRIBUTE_REPARSE_POINT`; all validated handles remain held while the
  next handle-relative child component is opened/created and until the file
  operation finishes. There is no pre-binding `resolve`, `lstat`, or recursive
  directory creation window. Missing-component early returns and all exceptions
  close every already-opened handle exactly once in reverse order.
- On Windows, model-file stat/open/create/unlink/atomic-replace/directory-flush
  and identity checks are also handle-relative. Reads and new files use
  `NtCreateFile` from the bound model handle. Deletion uses
  `SetFileInformationByHandle`; replacement uses `NtSetInformationFile` on the
  bound-opened source with a null root and validated same-directory simple
  name. A later in-place reparse mutation of a lexical directory name can be
  detected but cannot redirect these operations to the new target.
- Streams only bounded native chunks, enforces manifest size on both overrun
  and EOF, emits progress only when a valid known total exists, and checks
  cancellation before/between/after reads and before promotion.
- Verifies SHA-256, re-proves the promoted entry is the same written inode,
  mandatorily flushes and fsyncs the artifact file, then attempts a directory
  flush where supported. On Windows, `ERROR_ACCESS_DENIED` from
  `FlushFileBuffers` on the read-only bound directory handle is an explicit
  best-effort limitation; the handle is never reopened with `GENERIC_WRITE`.
  The downloader closes the transport before promotion and exposes cleanup
  failures instead of hiding them.
- Reuses a cache entry only after a fresh descriptor-bound size/SHA proof;
  invalid cache entries are never reused. A process-wide path lock provides
  same-target single flight across downloader instances. Its key is an
  absolute, dot-normalized, platform-case-normalized lexical path; it never
  calls `Path.resolve` or follows a symlink/junction before the cache namespace
  is handle-bound and rejected fail-closed.
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

### Fix round 4

RED evidence:

```text
uv run pytest \
  tests/jobs/models/test_cache_fs.py::test_windows_binding_holds_anchor_through_every_cache_ancestor \
  tests/jobs/models/test_cache_fs.py::test_windows_binding_fails_when_foreign_writer_already_owns_component \
  tests/jobs/models/test_cache_fs.py::test_windows_bound_directory_denies_in_place_reparse_write_handles \
  tests/jobs/models/test_cache_fs.py::test_windows_binding_creates_each_missing_segment_under_held_ancestors \
  tests/jobs/models/test_cache_fs.py::test_windows_native_anchor_open_uses_read_only_share_and_reparse_flags -q
5 failed: directory handles still admitted FILE_SHARE_WRITE

uv run pytest \
  tests/jobs/models/test_download.py::test_single_flight_key_never_calls_path_resolve \
  tests/jobs/models/test_download.py::test_single_flight_lock_identity_survives_symlink_namespace_swap \
  tests/jobs/models/test_download.py::test_single_flight_key_normalizes_lexical_dotdot_and_windows_case -q
3 failed: lock identity followed the filesystem and changed after a symlink swap
```

That first GREEN attempt was deliberately rejected before commit. The
`CreateFileW` documentation caveat below means `FILE_SHARE_READ` alone cannot
be treated as proof against a `FILE_WRITE_ATTRIBUTES` handle. Corrected TDD
evidence was then captured:

```text
uv run pytest \
  tests/jobs/models/test_cache_fs.py::test_windows_binding_holds_anchor_through_every_cache_ancestor \
  tests/jobs/models/test_cache_fs.py::test_windows_bound_directory_allows_attribute_mutation_but_not_data_writer \
  tests/jobs/models/test_cache_fs.py::test_windows_bound_file_lifecycle_stays_relative_to_original_handle -q
2 failed, 1 passed: descendant directories and cache files still used full paths

uv run pytest tests/jobs/models/test_session.py::test_child_launch_binds_cache_without_resolving_filesystem -q
1 failed: child verification still called Path.resolve and opened a full path

uv run pytest \
  tests/jobs/models/test_cache_fs.py::test_windows_native_replace_uses_bound_source_handle_and_simple_name \
  tests/jobs/models/test_cache_fs.py::test_windows_native_read_transfers_handle_ownership_to_crt_fd -q
1 failed, 1 passed: the initial Win32 rename route had no proven relative-root contract

uv run pytest tests/jobs/models/test_cache_fs.py::test_windows_native_replace_uses_bound_source_handle_and_simple_name -q
1 failed: the native variable-length rename buffer was 40 bytes instead of the required 44-byte structure-plus-name allocation
```

The corrected deterministic Windows API seam checks the anchor call for
`GENERIC_READ`, `FILE_SHARE_READ` only, `FILE_FLAG_BACKUP_SEMANTICS`, and
`FILE_FLAG_OPEN_REPARSE_POINT`, then checks each descendant call carries only
a parent handle and one validated component. It models an already-open foreign
writer as a sharing violation, but correctly permits the documented
`FILE_WRITE_ATTRIBUTES` in-place mutation. After that mutation, stat/read/
create/replace/unlink/flush stay rooted at the original model handle and an
outside sentinel remains unchanged; the handle-identity check reports the
mutation fail-closed. The spawned child now rebinds the same cache hierarchy
and reads/hashes through `BoundModelDirectory`, without `Path.resolve` or a
full-path artifact open. These are API-contract tests on macOS, not evidence of
an actual Windows filesystem run.

The native boundary uses fixed-width Win32 structure fields in its ctypes
buffers. `NtCreateFile` receives a validated single-component UTF-16 name and
an already-bound `OBJECT_ATTRIBUTES.RootDirectory`; atomic promotion uses
`NtSetInformationFile(FileRenameInformation)` on the source handle that was
opened relative to the bound model. The
[`FILE_RENAME_INFORMATION` contract](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntifs/ns-ntifs-_file_rename_information)
defines a null root plus a simple filename as an in-place rename in that file's
existing directory. This avoids both a full path and the network restriction
on nonzero rename roots. A seam test decodes the null root, information class,
and UTF-16 simple destination from the outgoing buffer. Successful
`msvcrt.open_osfhandle` calls transfer a native file handle to the returned CRT
descriptor; a separate ownership test proves that the wrapper closes the
native handle only when that transfer fails.

Microsoft's [`CreateFileW` documentation](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew)
defines share compatibility per opened file/device and says share modes remain
effective for the handle lifetime. It also notes that attribute/extended-
attribute access requests are not affected by the share flag. The
[`FSCTL_SET_REPARSE_POINT` documentation](https://learn.microsoft.com/en-us/windows/win32/api/winioctl/ni-winioctl-fsctl_set_reparse_point)
confirms that mutation operates on a file/directory handle. Because the
automated seam cannot settle filesystem-specific behavior or the documented
attribute-access caveat, Task 17 must run native Windows qualification on
NTFS, ReFS where available, and supported SMB: verify a pre-existing writer
causes binding failure; attempt new `FILE_WRITE_DATA` and
`FILE_WRITE_ATTRIBUTES` handles plus `FSCTL_SET_REPARSE_POINT` against every
bound level; and exercise relative child create/open, download, promotion,
reuse, child byte verification, and remove while all read-only-share ancestor
handles are live. Whether mutation is rejected or permitted, every operation
must remain on the original handle identity and outside sentinels must remain
unchanged; any deviation is release-blocking.

The single-flight key now uses only lexical `abspath` + `normpath` +
platform `normcase`. Regression tests prohibit `Path.resolve`, prove exact lock
object identity before/after a symlink namespace swap, normalize equivalent
dot/dot-dot forms, exercise Windows case normalization through `ntpath`, and
retain the existing two-thread one-transport-open behavior.

GREEN evidence:

```text
uv run pytest tests/jobs/models -q
120 passed in 0.69s

uv run pytest -q
639 passed in 39.72s (exit 0, outside the filesystem sandbox so POSIX shared memory is available)

uv run ruff check .
All checks passed!

uv run mypy src
Success: no issues found in 23 source files

git diff --check
clean
```

### Fix round 5

RED evidence:

```text
uv run pytest \
  tests/jobs/models/test_cache_fs.py::test_open_new_fdopen_failure_closes_transferred_fd_and_allows_windows_unlink \
  tests/jobs/models/test_cache_fs.py::test_open_new_fdopen_and_fd_close_failure_preserves_both_once \
  tests/jobs/models/test_cache_fs.py::test_windows_native_open_new_validation_failure_closes_before_transfer \
  tests/jobs/models/test_cache_fs.py::test_open_new_success_transfers_fd_ownership_to_file_object -q
2 failed, 2 passed: os.fdopen failure leaked the transferred descriptor and no combined cleanup error existed
```

`BoundModelDirectory.open_new` now treats the raw descriptor as its property
until `os.fdopen` succeeds. Any `BaseException` from that transfer closes the
descriptor exactly once before re-raising the original error. If descriptor
close also fails, `FileDescriptorCloseError` retains both `primary_error` and
`close_error` and chains directly from the primary error. As an `OSError`, it
also follows the downloader's existing structured storage-error mapping. The
regression uses a real OS descriptor behind the Windows cache abstraction: it
proves `EBADF` after wrapper failure, then removes the `.part` entry through the
bound handle. Separate ownership cases cover validation failure before CRT
transfer and a successful file object close.

The one test that lazily imports pinned ORT now changes into pytest's temporary
directory first. This contains ORT's macOS `:memory:.ses` telemetry marker in
test-owned storage; both the focused and full gates leave the workspace clean.

The artifact descriptor's `flush` plus `fsync` remains mandatory. Directory
flush remains a best-effort durability enhancement on the read-only Windows
directory handle; `ERROR_ACCESS_DENIED` is treated as unsupported rather than
requesting `GENERIC_WRITE`. Task 17 must qualify actual artifact and directory
durability across supported Windows filesystems and power-loss boundaries.

GREEN evidence:

```text
uv run pytest tests/jobs/models -q
124 passed in 0.70s

uv run pytest -q
643 passed in 40.09s (exit 0, outside the filesystem sandbox so POSIX shared memory is available)

uv run ruff check .
All checks passed!

uv run mypy src
Success: no issues found in 23 source files

uv run ruff format --check \
  src/rembggui/jobs/models/cache_fs.py tests/jobs/models/test_cache_fs.py \
  tests/jobs/models/test_session.py
3 files already formatted

git diff --check
clean
```

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
