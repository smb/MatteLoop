# Local model scope cleanup report

## Later scope-decision addendum

This report's RED/GREEN evidence below records the state when it was produced
and is intentionally not rewritten. A later V1 decision deferred SAM prompt
support completely: the current product contract has exactly 15 approved,
prompt-free LOCAL models, all with verified local artifacts and preview/full
render support. `birefnet-portrait` remains the default.

The current runtime trust boundary has one execution class, `LOCAL`. The
manifest and catalog reject unknown IDs before extras, cleanup checks,
downloads, or child/client actions; this includes the retired SAM ID. Generic
`ModelSpec` metadata and the local lifecycle/cleanup guards remain intact.

The binding design and executable plan carry only explicit deferred markers;
the complete earlier prompt exploration is preserved in
`docs/future-enhancements.md` as non-committed material outside V1 with no
delivery promise. Task 17 was not renumbered. Development remains local/private
by default; configuring a remote, changing visibility, or publishing requires
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
retired model field/error code, former-ID session routing, and stale
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

Review fix round 1 added direct-construction coverage for a former upstream
alias, custom and wrong release URLs, malformed artifact scalar fields,
non-canonical specification/default/edge types, and malformed catalog ID
elements. It also proved unknown-ID lookup must precede both extras and pending
cleanup checks:

```text
uv run pytest tests/jobs/models/test_catalog.py -k 'direct_catalog_construction' -q
20 failed, 3 passed

uv run pytest tests/jobs/models/test_session.py -k 'unknown_former_model_id' -q
2 failed

uv run pytest tests/jobs/models/test_catalog.py::test_direct_catalog_construction_maps_a_malformed_id_element -q
1 failed
```

Focused GREEN for the complete catalog/session/error/scope boundary:

```text
uv run pytest tests/jobs/models/test_catalog.py tests/jobs/models/test_session.py tests/core/test_errors.py tests/test_scope_contract.py -q
104 passed in 0.72s
```

No test downloaded model weights, started a live network request, or ran real
ONNX inference.

## Verification

```text
uv run pytest -q
719 passed in 45.48s

uv run ruff check .
All checks passed!

uv run mypy src
Success: no issues found in 27 source files

uv run ruff format --check <changed Python files>
4 files already formatted

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
