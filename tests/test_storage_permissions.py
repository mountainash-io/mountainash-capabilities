import json


def test_unreadable_record_directory_returns_issue(tmp_path, valid_record):
    from mountainash_capabilities import read_observation
    from mountainash_capabilities.store import publish, reserve

    root = tmp_path / "restricted"
    entry = reserve(root)
    assert not publish(entry, valid_record(identity=entry.name), {}).issues
    root.chmod(0)
    try:
        result = read_observation(root, entry.name)
        assert result.record is None
        assert any(x["code"] == "unreadable_record" for x in result.issues)
    finally:
        root.chmod(0o700)


def test_unreadable_artifact_directory_returns_issue(tmp_path, valid_record):
    from mountainash_capabilities import read_observation
    from mountainash_capabilities.store import publish, reserve

    entry = reserve(tmp_path)
    source = tmp_path / "source"
    source.write_bytes(b"evidence")
    result = publish(entry, valid_record(identity=entry.name), {"stdout": source})
    nested = entry / "restricted"
    nested.mkdir()
    (entry / "stdout.bin").rename(nested / "stdout.bin")
    result.record["artifacts"]["stdout"]["path"] = "restricted/stdout.bin"
    (entry / "record.json").write_text(json.dumps(result.record))
    nested.chmod(0)
    try:
        loaded = read_observation(tmp_path, entry.name)
        assert loaded.record is not None
        assert any(x["code"] == "artifact_unavailable" for x in loaded.issues)
    finally:
        nested.chmod(0o700)
