import json

import pytest


@pytest.mark.parametrize("missing", [
    "occurrence", "duration_seconds", "report_position", "diagnostic", "sections",
])
def test_missing_report_attribution_is_rejected(tmp_path, valid_record, missing):
    from mountainash_capabilities import read_observation
    from mountainash_capabilities.store import reserve
    entry = reserve(tmp_path)
    record = valid_record(identity=entry.name)
    del record["reports"][0][missing]
    (entry / "record.json").write_text(json.dumps(record))
    loaded = read_observation(tmp_path, entry.name)
    assert loaded.record is None
    assert any(x["code"] == "invalid_record" for x in loaded.issues)


def test_unknown_malformed_artifact_cannot_escape_read_api(tmp_path, valid_record):
    from mountainash_capabilities import read_observation
    from mountainash_capabilities.store import reserve
    entry = reserve(tmp_path)
    record = valid_record(identity=entry.name)
    record["artifacts"]["unexpected"] = None
    (entry / "record.json").write_text(json.dumps(record))
    result = read_observation(tmp_path, entry.name)
    assert result.record is None
    assert any(x["code"] == "invalid_record" for x in result.issues)


@pytest.mark.parametrize("group,field", [
    ("producer", "protocol_version"), ("request", "python"),
    ("context", "environment"), ("process", "elapsed_seconds"),
])
def test_required_observation_facts_cannot_disappear(tmp_path, valid_record, group, field):
    from mountainash_capabilities.store import reserve, publish
    entry = reserve(tmp_path)
    record = valid_record(identity=entry.name)
    del record[group][field]
    result = publish(entry, record, {})
    assert result.record is None
    assert any(x["code"] == "invalid_record" for x in result.issues)
