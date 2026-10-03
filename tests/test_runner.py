import json
import sys
import threading
import time
from pathlib import Path


def _run(tmp_path, source, *, selection=("test_case.py",), args=(), raw=True, **request_options):
    from mountainash_capabilities import PytestRequest, Retention, run_pytest
    suite = tmp_path / "suite"
    suite.mkdir(parents=True, exist_ok=True)
    if source is not None:
        (suite / "test_case.py").write_text(source)
    request = PytestRequest(
        python=Path(sys.executable),
        cwd=suite,
        selection=tuple(selection),
        retention=Retention(raw=raw),
        args=tuple(args),
        **request_options,
    )
    result = run_pytest(request, tmp_path / "evidence")
    assert not result.persistence_issues, result.persistence_issues
    return result


def _phases(observation, phase, nodeid=None):
    return [
        report
        for report in observation["reports"]
        if report["phase"] == phase and (nodeid is None or report.get("nodeid") == nodeid)
    ]


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield str(key)
            yield from _strings(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _strings(child)



def _dicts(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _dicts(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _dicts(child)

def _case(observation, nodeid):
    return next(case for case in observation["execution"]["cases"] if case["nodeid"] == nodeid)


def _artifact(observation, role):
    return observation["artifacts"][role]


def test_completed_pytest_failure_is_not_incomplete_execution(tmp_path):
    result = _run(tmp_path, "def test_wrong_value():\n    assert 2 + 2 == 5\n")
    observation = result.observation

    assert observation["process"]["status"] == "exited"
    assert observation["process"]["returncode"] == 1
    assert observation["execution"]["coverage"] == "complete"
    call = _phases(observation, "call")[0]
    assert (call["nodeid"], call["outcome"]) == ("test_case.py::test_wrong_value", "failed")


def test_setup_failure_has_no_call_result(tmp_path):
    result = _run(
        tmp_path,
        "import pytest\n"
        "@pytest.fixture\n"
        "def broken(): raise RuntimeError('setup broke')\n"
        "def test_uses_fixture(broken): assert True\n",
    )
    observation = result.observation
    nodeid = "test_case.py::test_uses_fixture"

    assert _phases(observation, "setup", nodeid)[0]["outcome"] == "failed"
    assert not _phases(observation, "call", nodeid)
    case = _case(observation, nodeid)
    assert case["terminal_item"] is True
    assert case["call_status"] == "not_executed"
    assert observation["execution"]["coverage"] == "complete"


def test_teardown_failure_preserves_successful_call(tmp_path):
    result = _run(
        tmp_path,
        "import pytest\n"
        "@pytest.fixture\n"
        "def resource():\n"
        "    yield\n"
        "    raise RuntimeError('teardown broke')\n"
        "def test_passes(resource): assert 2 + 2 == 4\n",
    )
    nodeid = "test_case.py::test_passes"
    assert _phases(result.observation, "call", nodeid)[0]["outcome"] == "passed"
    assert _phases(result.observation, "teardown", nodeid)[0]["outcome"] == "failed"


def test_xfail_and_xpass_preserve_pytest_representation(tmp_path):
    result = _run(
        tmp_path,
        "import pytest\n"
        "@pytest.mark.parametrize('case', ["
        "pytest.param('xfail', marks=pytest.mark.xfail(reason='expected')), "
        "pytest.param('xpass', marks=pytest.mark.xfail(reason='unexpected pass')), "
        "pytest.param('strict', marks=pytest.mark.xfail(reason='strict', strict=True)), "
        "'lookalike'])\n"
        "def test_marks(case):\n"
        "    if case == 'xfail': pytest.fail('expected failure')\n"
        "    if case == 'lookalike': pytest.fail('[XPASS(strict)] helper-shaped failure')\n",
        args=("-rxX",),
    )
    reports = {report["nodeid"].rsplit("[", 1)[-1].rstrip("]"): report
               for report in _phases(result.observation, "call")}

    assert reports["xfail"]["outcome"] == "skipped"
    assert reports["xfail"]["wasxfail_present"] is True
    assert reports["xpass"]["outcome"] == "passed"
    assert reports["xpass"]["wasxfail_present"] is True
    assert reports["strict"]["outcome"] == "failed"
    assert reports["strict"]["wasxfail_present"] is False
    assert reports["lookalike"]["outcome"] == "failed"
    assert reports["lookalike"]["wasxfail_present"] is False


def test_collection_failure_is_not_empty_success(tmp_path):
    result = _run(tmp_path, "raise RuntimeError('collection exploded')\ndef test_never_runs(): pass\n")
    observation = result.observation

    assert observation["process"]["returncode"] != 0
    assert observation["execution"]["coverage"] != "empty"
    assert any(report["phase"] == "collection" and report["outcome"] == "failed"
               for report in observation["reports"])
    assert observation["execution"]["collection_completed"] is False


def test_deselection_and_empty_selection_are_explicit(tmp_path):
    result = _run(
        tmp_path,
        "def test_alpha(): pass\ndef test_beta(): pass\n",
        args=("-k", "alpha"),
    )
    observation = result.observation
    assert "test_case.py::test_alpha" in set(_strings(observation["execution"]))
    assert "test_case.py::test_beta" in set(_strings(observation["execution"]))
    assert observation["execution"]["coverage"] == "complete"
    assert "test_case.py::test_alpha" in set(_strings(observation["execution"]["selected"]))
    assert "test_case.py::test_beta" in set(_strings(observation["execution"]["deselected"]))

    empty = _run(
        tmp_path / "empty",
        "def test_alpha(): pass\n",
        args=("-k", "does_not_match"),
    ).observation
    assert empty["execution"]["coverage"] == "empty"
    assert empty["execution"]["collection_completed"] is True


def test_maxfail_leaves_known_unexecuted_items(tmp_path):
    result = _run(
        tmp_path,
        "def test_first(): assert False\ndef test_second(): assert True\n",
        args=("-x",),
    )
    observation = result.observation
    assert _phases(observation, "call", "test_case.py::test_first")[0]["outcome"] == "failed"
    assert not _phases(observation, "call", "test_case.py::test_second")
    assert _case(observation, "test_case.py::test_second")["call_status"] == "not_executed"
    assert observation["execution"]["coverage"] == "partial"


def test_launch_and_missing_dependency_failures_remain_observations(tmp_path):
    from mountainash_capabilities import PytestRequest, Retention, run_pytest

    suite = tmp_path / "launch-suite"
    suite.mkdir()
    request = PytestRequest(
        python=suite / "missing-python", cwd=suite, selection=("test_case.py",),
        retention=Retention(raw=True),
    )
    launch = run_pytest(request, tmp_path / "launch-evidence").observation
    assert launch["process"]["status"] == "launch_failed"

    dependency = _run(
        tmp_path / "dependency",
        "import absent_backend_for_receipt_test\ndef test_unavailable(): pass\n",
    ).observation
    assert dependency["process"]["status"] == "exited"
    assert dependency["process"]["returncode"] != 0
    assert dependency["execution"]["coverage"] != "complete"


def test_empty_request_does_not_launch_pytest(tmp_path):
    from mountainash_capabilities import PytestRequest, Retention, run_pytest

    suite = tmp_path / "suite"
    suite.mkdir()
    marker = suite / "executed"
    (suite / "test_case.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('yes')\ndef test_write(): pass\n"
    )
    for selection in ((), ("",)):
        request = PytestRequest(
            python=Path(sys.executable), cwd=suite, selection=selection,
            retention=Retention(raw=True),
        )
        try:
            run_pytest(request, tmp_path / ("evidence-" + str(len(selection))))
        except ValueError:
            pass
        else:
            raise AssertionError("empty or blank selection must raise ValueError")
    assert not marker.exists()

    explicit = _run(tmp_path / "explicit", "def test_valid(): pass\n", selection=(".",))
    assert explicit.observation["execution"]["coverage"] == "complete"


def test_programmatic_invocation_preserves_argv_selection(tmp_path):
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "conftest.py").write_text(
        "import sys\n"
        "def pytest_addoption(parser): parser.addoption('--variant')\n"
        "def pytest_generate_tests(metafunc):\n"
        "    if 'variant' in metafunc.fixturenames:\n"
        "        value = next(arg.split('=', 1)[1] for arg in sys.argv if arg.startswith('--variant='))\n"
        "        metafunc.parametrize('variant', [value])\n"
    )
    (suite / "test_case.py").write_text("def test_variant(variant): assert variant == 'chosen'\n")
    result = _run(tmp_path, None, args=("--variant=chosen",))
    call = _phases(result.observation, "call")[0]
    assert call["nodeid"] == "test_case.py::test_variant[chosen]"
    assert call["outcome"] == "passed"


def test_interrupted_collection_is_unknown_not_empty(tmp_path):
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "conftest.py").write_text(
        "def pytest_collection_modifyitems(items):\n"
        "    assert any('candidate' in item.nodeid for item in items)\n"
        "    raise KeyboardInterrupt()\n"
    )
    result = _run(tmp_path, "def test_candidate(): pass\n")
    observation = result.observation
    assert observation["execution"]["coverage"] == "unknown"
    assert observation["execution"]["collection_completed"] is False
    assert observation["process"]["status"] in {"exited", "interrupted"}
    assert "test_case.py::test_candidate" in observation["execution"]["candidates"]


def test_context_failure_does_not_block_call(tmp_path):
    result = _run(
        tmp_path,
        "def test_passes(): assert True\n",
        packages=("absent_distribution_for_receipt_test",),
    )
    observation = result.observation
    assert _phases(observation, "call")[0]["outcome"] == "passed"
    assert any(
        entry.get("status") in {"unknown", "unavailable"}
        and "absent_distribution_for_receipt_test" in " ".join(_strings(entry))
        for entry in _dicts(observation["context"])
    )


def test_target_context_is_not_parent_context(tmp_path):
    import mountainash

    suite = tmp_path / "suite"
    suite.mkdir()
    local = suite / "mountainash"
    local.mkdir()
    (local / "__init__.py").write_text("__version__ = '97.0-target-test'\n")
    (suite / "test_case.py").write_text(
        "import mountainash\n"
        "def test_target_import():\n"
        "    assert mountainash.__version__ == '97.0-target-test'\n"
        "    assert 'suite/mountainash' in mountainash.__file__.replace('\\\\', '/')\n"
    )
    result = _run(tmp_path, None)
    observation = result.observation
    assert _phases(observation, "call")[0]["outcome"] == "passed"
    context = observation["context"]
    assert context["status"] in {"recorded", "partial"}
    assert str(suite / "mountainash") in " ".join(_strings(context))
    assert "97.0-target-test" in set(_strings(context))
    assert Path(mountainash.__file__).resolve() != (local / "__init__.py").resolve()


def test_timeout_keeps_flushed_events_and_unfinished_call(tmp_path):
    ready = tmp_path / "ready"
    result = _run(
        tmp_path,
        "from pathlib import Path\nimport time\n"
        f"def test_waits():\n    Path({str(ready)!r}).write_text('ready')\n    time.sleep(30)\n",
        timeout_seconds=3,
        terminate_grace_seconds=0.1,
    )
    observation = result.observation
    assert ready.exists()
    assert observation["process"]["status"] == "timed_out"
    case = _case(observation, "test_case.py::test_waits")
    assert "call" in case["attempted_phases"]
    assert case["call_status"] == "unfinished"
    assert not _phases(observation, "call", "test_case.py::test_waits")
    assert observation["execution"]["coverage"] == "partial"


def test_cancellation_waits_for_readiness_and_cleans_up_thread(tmp_path):
    from mountainash_capabilities import PytestRequest, Retention, run_pytest

    suite = tmp_path / "suite"
    suite.mkdir()
    ready = tmp_path / "ready"
    (suite / "test_case.py").write_text(
        "from pathlib import Path\nimport time\n"
        f"def test_waits():\n    Path({str(ready)!r}).write_text('ready')\n    time.sleep(30)\n"
    )
    cancel = threading.Event()
    outcome = []
    failure = []
    request = PytestRequest(
        python=Path(sys.executable), cwd=suite, selection=("test_case.py",),
        retention=Retention(raw=True), terminate_grace_seconds=0.1,
    )

    def execute():
        try:
            outcome.append(run_pytest(request, tmp_path / "evidence", cancel=cancel))
        except BaseException as error:
            failure.append(error)

    thread = threading.Thread(target=execute)
    thread.start()
    deadline = time.monotonic() + 10
    while not ready.exists() and time.monotonic() < deadline:
        if not thread.is_alive():
            break
        time.sleep(0.01)
    try:
        assert ready.exists(), "target did not reach its readiness point"
        cancel.set()
    finally:
        if thread.is_alive():
            cancel.set()
        thread.join(timeout=10)
    assert not thread.is_alive(), "runner did not finish after cancellation"
    assert not failure
    observation = outcome[0].observation
    assert observation["process"]["status"] == "cancelled"
    case = _case(observation, "test_case.py::test_waits")
    assert "call" in case["attempted_phases"]
    assert case["call_status"] == "unfinished"
    assert not _phases(observation, "call", "test_case.py::test_waits")
    assert observation["execution"]["coverage"] == "partial"


def test_truncated_event_keeps_prior_results(tmp_path):
    from mountainash_capabilities.runner import decode_events

    result = _run(tmp_path, "def test_recorded(): assert True\n")
    artifact = _artifact(result.observation, "events")
    events_path = result.record_dir / artifact["path"]
    with events_path.open("ab") as stream:
        stream.write(b'{"sequence":')
    events, issues = decode_events(events_path)

    assert events
    assert any("test_case.py::test_recorded" in set(_strings(event)) for event in events)
    assert issues


def test_omitted_raw_output_leaves_no_diagnostic_copy(tmp_path):
    from mountainash_capabilities import export_json

    marker = "SENSITIVE-OUTPUT-MARKER-7a61c0"
    result = _run(
        tmp_path,
        f"def test_sensitive():\n    print({marker!r})\n    assert False, {marker!r}\n",
        raw=False,
    )
    observation = result.observation

    assert marker not in export_json(result)
    for artifact in observation["artifacts"].values():
        if artifact["availability"] == "retained":
            data = (result.record_dir / artifact["path"]).read_bytes()
            assert marker.encode() not in data
    assert _phases(observation, "call")[0]["outcome"] == "failed"
    assert _artifact(observation, "stdout")["availability"] == "omitted"
    assert _artifact(observation, "stderr")["availability"] == "omitted"
    assert marker not in json.dumps(observation)
    assert marker not in result.record_dir.joinpath("record.json").read_text()


def test_duplicate_nodeids_do_not_overwrite_reports(tmp_path):
    result = _run(
        tmp_path,
        "def test_twice(): assert True\n",
        selection=("test_case.py", "test_case.py"),
        args=("--keep-duplicates",),
    )
    reports = _phases(result.observation, "call", "test_case.py::test_twice")

    assert len(reports) == 2
    assert reports[0]["occurrence"] != reports[1]["occurrence"]
    cases = [case for case in result.observation["execution"]["cases"]
             if case["nodeid"] == "test_case.py::test_twice"]
    assert len(cases) == 2
    assert any("ambiguous" in value.lower() for value in _strings(result.observation))


def test_collection_only_and_setup_only_are_not_complete_execution(tmp_path):
    collected = _run(
        tmp_path,
        "def test_one(): assert True\n",
        args=("--collect-only",),
    ).observation
    assert collected["execution"]["collection_completed"] is True
    assert collected["execution"]["coverage"] == "partial"
    assert not _phases(collected, "call")

    setup_only = _run(
        tmp_path,
        "def test_one(): assert True\n",
        args=("--setup-only",),
    ).observation
    assert setup_only["execution"]["coverage"] == "partial"
    assert not _phases(setup_only, "call")


def test_malformed_event_stream_keeps_valid_events(tmp_path):
    from mountainash_capabilities.runner import decode_events

    result = _run(tmp_path, "def test_valid_event(): assert True\n")
    artifact = _artifact(result.observation, "events")
    path = result.record_dir / artifact["path"]
    original = path.read_bytes()
    valid_lines = original.splitlines(keepends=True)
    assert len(valid_lines) > 1
    path.write_bytes(valid_lines[0] + b'{"broken":\n' + b"\n".join(valid_lines[1:]))
    events, issues = decode_events(path)

    assert events
    assert issues
    assert any("test_case.py::test_valid_event" in set(_strings(event)) for event in events)


def test_setup_show_does_not_omit_executed_calls(tmp_path):
    result = _run(tmp_path, "def test_visible(): assert True\n", args=("--setup-show",))
    assert _phases(result.observation, "call")[0]["outcome"] == "passed"
    assert result.observation["execution"]["coverage"] == "complete"


def test_target_receipt_records_import_path_and_adapter_version(tmp_path):
    result = _run(tmp_path, "def test_receipt(): assert True\n")
    context = result.observation["context"]
    assert str(tmp_path / "suite") in context["target_start"]["receipt"]["python"]["sys_path"]
    assert context["adapter"]["pytest_version"] == "8.3.5"
    assert context["adapter"]["adapter_compatibility"] == "qualified"


def test_authorized_diagnostic_is_visible_in_rendered_report(tmp_path):
    from markdown_it import MarkdownIt
    from mountainash_capabilities import render_markdown
    result = _run(tmp_path, "def test_bad(): assert False, 'specific-failure-marker'\n")
    rendered = MarkdownIt().render(render_markdown(result))
    assert "specific-failure-marker" in rendered


def test_event_loss_and_numeric_overflow_are_explicit(tmp_path):
    from mountainash_capabilities.runner import decode_events
    result = _run(tmp_path, "def test_receipt(): assert True\n")
    path = result.record_dir / _artifact(result.observation, "events")["path"]
    lines = path.read_bytes().splitlines(keepends=True)
    path.write_bytes(b"".join(lines[:1] + lines[2:]) + b'{"value":1e999}\n')
    events, problems = decode_events(path)
    assert any(x["code"] == "event_sequence_gap" for x in problems)
    assert any(x["code"] == "malformed_event" for x in problems)
    assert any("test_case.py::test_receipt" in set(_strings(event)) for event in events)
