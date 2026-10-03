# mountainash-capabilities

Explicit Mountainash witness execution, immutable local evidence, independent queries and factual reports.

This companion depends on Mountainash for its native environment representation. Mountainash does not depend on this package. Existing tests retain ownership of their assertions and expected failures.

## Execute → retain → read → report

Python 3.12 or newer is required. Supply an existing target Python with pytest and the dependencies required by your witnesses. The reporting adapter is launched by file path; the target does not need the companion installed.

```python
from pathlib import Path
from mountainash_capabilities import (
    PytestRequest, Retention, Query, run_pytest,
    read_observation, query_observations, render_markdown, export_json,
)

result = run_pytest(
    PytestRequest(
        python=Path("/path/to/target/bin/python"),
        cwd=Path("/path/to/mountainash"),
        selection=(
            "tests/expressions/cross_backend/test_null_nan_clip.py"
            "::TestClip::test_clip_both_bounds",
        ),
        args=("--ma-backend-scope=full",),
        packages=("mountainash", "polars", "narwhals", "ibis-framework"),
        source_checkout=Path("/path/to/mountainash"),
        timeout_seconds=120,
        retention=Retention(raw=False),
    ),
    destination=Path("./evidence"),
)
print(render_markdown(result))
print(export_json(result))
```

Selection must be nonempty. Select `"."` explicitly if you intend to run the working directory's suite. The API preserves supplied pytest arguments, configuration and plugins. It does not choose tests or infer expectations from their error messages.

A failed test is a normal recorded outcome, not an API exception. Inspect independently:

- `result.observation["process"]`: exit, signal, launch failure, timeout or cancellation.
- `result.observation["execution"]`: known scope, phase attempts, completion and missing calls.
- `result.observation["reports"]`: native collection/setup/call/teardown outcomes and xfail metadata.
- `result.persistence_issues`: problems retaining the observation.
- `result.record_dir`: reserved location, including incomplete publication when applicable.

Invalid request types, empty selection and nonpositive/nonfinite limits raise `ValueError` before launch. If reservation of the requested destination fails, execution does not start. Otherwise launch/dependency failures remain observations, with missing scope explicit.

In a separate Python process:

```python
from pathlib import Path
from mountainash_capabilities import (
    Query, read_observation, query_observations, render_markdown, export_json,
)

store = Path("./evidence")
loaded = read_observation(store, "replace_with_the_record_uuid_hex")
print(render_markdown(loaded))

found = query_observations(
    store,
    Query(
        witness="tests/test_example.py::test_case",
        captured_from="2026-10-01T00:00:00Z",
        captured_before="2026-11-01T00:00:00Z",
        process_status="exited",
        coverage_status="complete",
    ),
)
print(export_json(found))
```

Queries combine filters with AND. Witness matching is exact against recorded selected, deselected, attempted or reported nodeids; a file path is not a wildcard. Time bounds are UTC and half-open: `[captured_from, captured_before)`. Filters, destination, enumeration time and incomplete query coverage remain in exports even when nothing matches.

Reading, querying and rendering never execute witnesses. They do not need the original target environment.

## Coverage is not success

- `complete`: all selected items reached terminal pytest results and reporting has no detected gaps.
- `partial`: known scope contains unfinished items, missing reports or omitted execution phases.
- `unknown`: final scope is not trustworthy or the execution topology is unsupported.
- `empty`: successful collection selected no items.

A setup-failed item can have complete item execution while its call remains `not_executed`. A passing call followed by teardown failure preserves both outcomes. Collection interruption is not empty success. Strict XPASS and helper-generated failures retain the native pytest representation; no diagnostic text is parsed to invent a semantic outcome.

Duplicate nodeids retain separate occurrences and an ambiguity issue. Distributed/xdist completeness is not supported; available reports remain facts, with unknown coverage.

## Context and support

The demonstrated host/target is Python 3.12.14 with pytest 8.3.5. Other pytest versions are allowed to run but have `adapter_compatibility="unverified"`; this is not a version certification matrix.

The target records its Python executable/version/import path, requested distribution versions, and Mountainash's version/module location when observed loaded. The companion context is separate. Missing metadata does not block executable work.

`source_checkout` opts into read-only revision/dirty-state acquisition. It describes a live checkout, not frozen content or proof of reproducibility. A dirty tree is not represented as immutable HEAD content. No package inventory, dependency installation, checkout repair or recursive fingerprinting occurs.

POSIX process-group cancellation is exercised on Linux. The non-POSIX path terminates the direct child; descendant isolation is not claimed.

## Retention and cancellation

`Retention(raw=...)` is an explicit choice:

- `True`: stdout/stderr bytes and authorized diagnostics/sections/xfail reasons are retained. They may contain sensitive data.
- `False`: stdout/stderr go to the null device; diagnostic-bearing fields are omitted. Structural events and native outcomes remain, labelled redacted.

Known credential-bearing fields in request metadata and authenticated URLs are sanitized independently of raw retention. Executable inputs are unchanged. This is bounded known-field sanitization, not a secret scanner for arbitrary output, nodeids or positional arguments.

Pass a `threading.Event` as `cancel=` to request cancellation. Timeout, cancellation and caught caller `KeyboardInterrupt` terminate the launched process group on POSIX, escalate after `terminate_grace_seconds`, reap the child and retain partial facts. Caught `KeyboardInterrupt` returns status `interrupted` rather than re-raising. Killing the parent outright cannot guarantee publication; pending entries remain visible.

Output capture ends at a recorded cutoff after the direct child exits. Published files are bounded snapshots; inherited descriptors cannot mutate those published files. Later descendant output is not claimed captured. This executes trusted local Python, including fixtures and plugins, and is **not a sandbox**.

## Storage contract

Keep the evidence root dedicated to observations. Each run reserves its own UUID directory. Final `record.json` publication is atomic and non-overwriting; artifacts carry size and SHA-256. Filesystems without the required hard-link publication operation return a publication issue rather than using an unsafe overwrite fallback.

If publication fails, authorized working output is retained and its location is returned in an `unpublished_output_retained` persistence issue. Private executable request configuration is removed separately; a cleanup failure is explicit. Unpublished working files are recovery material, not integrity-verified published evidence.

Readers reject escaping/symlink artifact paths and report missing/corrupt bytes. Incomplete, unreadable or unsupported records remain query coverage issues even if filters would otherwise return nothing. Enumeration is not a transactional snapshot. No repair, database, index or garbage collector is provided.

## Development

The unpublished package pins an immutable Mountainash revision because the `0.1.0` version alone does not identify its capture API. There is no PyPI release or publishing workflow.

Install development dependencies in a **development** environment; the execution API never installs into a target:

```sh
python -m pip install -e '.[dev]'
python -m build --wheel --no-isolation
python -m pip install --no-deps --force-reinstall dist/mountainash_capabilities-0.1.0-py3-none-any.whl
MA_COMPANION_INSTALLED_PYTHON=/absolute/path/to/python python -m pytest tests -q
```

The installed-package test requires the supplied interpreter to have the built wheel and pytest. It executes real passing/failing witnesses outside the source checkout and reads them back in another process. It never installs or builds during the test run.

## Scope

Phase 1 provides existing-witness execution and durable evidence consumers. No CLI, service, scheduler, agents, disposable experiment API, interpretation model, comparator, upstream client, claim inspection, automatic baseline or support inference is included.
