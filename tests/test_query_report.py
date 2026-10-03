import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from markdown_it import MarkdownIt


def retained(tmp_path, valid_record, **changes):
    from mountainash_capabilities.store import publish, reserve
    entry = reserve(tmp_path)
    record = valid_record(identity=entry.name)
    record.update(changes)
    result = publish(entry, record, {})
    assert not result.issues, result.issues
    return entry, record


def test_filtered_query_keeps_unknown_coverage(tmp_path, valid_record):
    from mountainash_capabilities import Query, query_observations
    from mountainash_capabilities.store import reserve
    retained(tmp_path, valid_record)
    broken = reserve(tmp_path)
    (broken / "record.json").write_text("{broken")
    result = query_observations(tmp_path, Query(witness="does_not_match::test_case"))
    assert result.records == ()
    assert result.complete is False
    assert any(i["code"] == "invalid_record" for i in result.issues)


def test_exact_witness_roles_and_filter_conjunction(tmp_path, valid_record):
    from mountainash_capabilities import Query, query_observations, render_markdown
    from mountainash_capabilities.store import publish, reserve
    entry = reserve(tmp_path)
    record = valid_record(identity=entry.name)
    record["execution"]["deselected"] = ["test_excluded.py::test_other"]
    publish(entry, record, {})
    nodeid = record["reports"][0]["nodeid"]
    matching = query_observations(tmp_path, Query(
        witness=nodeid, process_status="exited", coverage_status="complete", context_status="recorded"
    ))
    assert [x.record["id"] for x in matching.records] == [entry.name]
    assert not query_observations(tmp_path, Query(witness="tests/test_example.py")).records
    assert not query_observations(tmp_path, Query(witness=nodeid, coverage_status="partial")).records
    deselected = query_observations(tmp_path, Query(witness="test_excluded.py::test_other"))
    assert [x.record["id"] for x in deselected.records] == [entry.name]
    markdown = render_markdown(deselected)
    assert "deselected" in markdown.lower()
    assert "test_other" in MarkdownIt().render(markdown)


def test_time_filter_half_open_and_ordered(tmp_path, valid_record):
    from mountainash_capabilities import Query, query_observations
    later, _ = retained(tmp_path, valid_record, captured_at="2026-10-03T12:00:01Z")
    earlier, _ = retained(tmp_path, valid_record, captured_at="2026-10-03T12:00:00Z")
    result = query_observations(tmp_path, Query(
        captured_from="2026-10-03T12:00:00+00:00", captured_before="2026-10-03T12:00:01Z"
    ))
    assert [x.record["id"] for x in result.records] == [earlier.name]
    assert [x.record["id"] for x in query_observations(tmp_path, Query()).records] == [
        earlier.name, later.name
    ]


@pytest.mark.parametrize("kwargs", [
    {"process_status": "success"}, {"coverage_status": "passed"}, {"context_status": "known"},
    {"captured_from": "yesterday"}, {"captured_before": "2026-10-03T12:00:00"},
    {"captured_from": "2026-10-04T00:00:00Z", "captured_before": "2026-10-03T00:00:00Z"},
])
def test_invalid_filters_are_errors(tmp_path, kwargs):
    from mountainash_capabilities import Query, query_observations
    with pytest.raises(ValueError):
        query_observations(tmp_path, Query(**kwargs))


def test_missing_destination_is_not_complete_empty_search(tmp_path):
    from mountainash_capabilities import Query, query_observations
    result = query_observations(tmp_path / "missing", Query())
    assert result.records == ()
    assert not result.complete
    assert result.issues


def test_empty_query_exports_retain_provenance(tmp_path):
    from mountainash_capabilities import Query, export_json, query_observations
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    a = query_observations(first, Query(witness="test_a"))
    b = query_observations(second, Query(witness="test_b"))
    encoded_a, encoded_b = export_json(a), export_json(b)
    assert a.complete and b.complete
    assert "test_a" in encoded_a and str(first) in encoded_a and a.enumerated_at in encoded_a
    assert "test_b" in encoded_b and str(second) in encoded_b and b.enumerated_at in encoded_b
    assert json.loads(encoded_a) != json.loads(encoded_b)


def test_failure_and_missing_artifact_visible_without_markup_injection(tmp_path, valid_record):
    from mountainash_capabilities import read_observation, render_markdown, export_json
    from mountainash_capabilities.store import publish, reserve
    entry = reserve(tmp_path)
    record = valid_record(identity=entry.name)
    record["reports"][0]["diagnostic"] = {
        "availability": "retained", "text": "<script>alert(1)</script> | ``` [bad](https://bad)"
    }
    source = tmp_path / "raw"
    source.write_bytes(b"diagnostic")
    assert not publish(entry, record, {"stdout": source}).issues
    (entry / "stdout.bin").unlink()
    result = read_observation(tmp_path, entry.name)
    markdown = render_markdown(result)
    encoded = export_json(result)
    assert "test_expected_failure" in markdown and "failed" in markdown
    assert "artifact_unavailable" in markdown and "artifact_unavailable" in encoded
    rendered = MarkdownIt("commonmark", {"html": True}).render(markdown)
    assert "<script>" not in rendered
    assert '<a href="https://bad"' not in rendered
    assert "alert" in rendered
    assert json.loads(encoded)["record"]["reports"][0]["outcome"] == "failed"


def test_fresh_process_can_render_without_execution(tmp_path, valid_record):
    entry, _ = retained(tmp_path / "store", valid_record)
    script = """
import json, sys
from pathlib import Path
from mountainash_capabilities import read_observation, render_markdown, export_json
r = read_observation(Path(sys.argv[1]), sys.argv[2])
assert not r.issues
assert "test_expected_failure" in render_markdown(r)
print(export_json(r))
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    run = subprocess.run(
        [sys.executable, "-c", script, str(entry.parent), entry.name],
        cwd=tmp_path, env=env, text=True, capture_output=True, check=True, timeout=15,
    )
    assert "test_expected_failure" in run.stdout
    assert json.loads(run.stdout)
