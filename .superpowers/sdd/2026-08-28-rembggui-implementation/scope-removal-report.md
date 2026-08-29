# Local model scope cleanup report

## Outcome

The current product contract now contains exactly 16 model IDs: 15
prompt-free local models plus `sam`. The default remains
`birefnet-portrait`. Local models support preview and full render; SAM is
preview-only and requires point prompts.

The runtime model trust boundary now has only two execution classes:
`LOCAL` and `SAM_PREVIEW`. The strict manifest schema and `ModelSpec` no longer
contain fields that existed only for the retired remote execution path. Both
manifest parsing and direct `ModelCatalog` construction reject missing,
duplicate, unknown, or hidden IDs.

`ModelSessionManager` now routes only verified local sessions and the SAM
preview capability. A former catalog ID fails with `MODEL_NOT_FOUND` before a
download or client action, preserves an already-active local session, and
cannot be used as an alias or custom catalog entry. The execution-specific error
code was removed from the serializable error contract.

The binding design, executable plan, Task 9 report, and current local SDD
context now describe the same 16-ID product. Task 16 is SAM-only. Task 17 has
no retired execution-path qualification. Development is local/private by
default; configuring a remote, changing visibility, or publishing requires
separate user authorization.

The 15-entry local model provenance record was unchanged and remains exact.
The generic packaging exclusion for filenames containing `token` remains as
defense in depth and is not tied to a product execution path.

## TDD evidence

Initial desired-state run:

```text
uv run pytest tests/jobs/models/test_catalog.py tests/jobs/models/test_session.py tests/core/test_errors.py tests/test_scope_contract.py -q
9 failed, 70 passed
```

The failures proved the old 17-ID catalog, third execution class,
provider-only model field/error code, former-ID session routing, and stale
binding documentation were observable before implementation.

A second RED case proved direct `ModelCatalog` construction could still admit
a hidden former ID:

```text
uv run pytest tests/jobs/models/test_catalog.py::test_direct_catalog_construction_rejects_a_hidden_former_id -q
1 failed
```

Focused GREEN after the fail-closed constructor hardening:

```text
uv run pytest tests/jobs/models/test_catalog.py::test_direct_catalog_construction_rejects_a_hidden_former_id tests/jobs/models/test_catalog.py tests/jobs/models/test_session.py tests/core/test_errors.py tests/test_scope_contract.py -q
82 passed in 1.24s
```

No test downloaded model weights, started a live network request, or ran real
ONNX inference.

## Verification

```text
uv run pytest -q
696 passed in 44.66s

uv run ruff check .
All checks passed!

uv run mypy src
Success: no issues found in 27 source files

uv run ruff format --check <changed Python files>
7 files already formatted

git diff --check
clean
```

The complete pytest run was intentionally executed with filesystem-sandbox
escalation because the multiprocessing suite uses POSIX shared memory that is
not available in the restricted sandbox. It ran sequentially on the host and
made no live network calls.

## Audit boundary

The semantic search covered product source, resources, tests, binding docs,
and current SDD context. It excluded `.git/`, `.agents/`, `AGENTS.md`,
`emoteScript`, machine-local environments/caches, and historical
`.superpowers/sdd/**/review-*.diff` audit artifacts as required. The only exact
former-ID references left in the current tree are negative regression tests
that prove fail-closed lookup/session behavior.
