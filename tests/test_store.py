import json
import os
import subprocess
import sys
import textwrap
from copy import deepcopy
from pathlib import Path

import pytest




def issue_codes(result):
    return {issue["code"] for issue in result.issues}


def _subprocess_env():
    env = os.environ.copy()
    # Keep the caller's PYTHONPATH and add this checkout's source tree so a
    # fresh interpreter can import the package without an editable install.
    src = str(Path(__file__).resolve().parents[1] / "src")
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (src, env.get("PYTHONPATH"))))
    return env


def test_publication_cannot_replace_prior_observation(tmp_path, valid_record):
    from mountainash_capabilities import read_observation
    from mountainash_capabilities.store import publish, reserve

    entry = reserve(tmp_path)
    record = valid_record(identity=entry.name)
    original = tmp_path / "original.bin"
    original.write_bytes(b"original diagnostic")
    first = publish(entry, record, {"stdout": original})
    replacement = tmp_path / "replacement.bin"
    replacement.write_bytes(b"replacement diagnostic")
    changed = {**record, "reports": []}
    second = publish(entry, changed, {"stdout": replacement})
    loaded = read_observation(tmp_path, entry.name)

    assert first.issues == ()
    assert "already_published" in issue_codes(second)
    assert loaded.record["reports"][0]["outcome"] == "failed"
    assert (entry / "stdout.bin").read_bytes() == b"original diagnostic"


def test_two_writer_processes_publish_independent_reservations(tmp_path, valid_record):
    from mountainash_capabilities.store import reserve

    entries = [reserve(tmp_path), reserve(tmp_path)]
    records = [valid_record(identity=entry.name) for entry in entries]
    payloads = [b"writer-one", b"writer-two"]
    procs = []
    for entry, record, payload in zip(entries, records, payloads):
        source = tmp_path / f"{entry.name}.source"
        source.write_bytes(payload)
        code = textwrap.dedent(
            """
            import json, sys
            from pathlib import Path
            from mountainash_capabilities.store import publish
            entry, record_path, source = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
            result = publish(entry, json.loads(record_path.read_text()), {"stdout": source})
            print(json.dumps({"record": result.record, "issues": result.issues}))
            """
        )
        record_path = tmp_path / f"{entry.name}.json"
        record_path.write_text(json.dumps(record))
        procs.append(
            subprocess.Popen(
                [sys.executable, "-c", code, str(entry), str(record_path), str(source)],
                cwd=Path(__file__).resolve().parents[1],
                env=_subprocess_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        )

    results = [proc.communicate(timeout=15) for proc in procs]
    assert all(proc.returncode == 0 for proc in procs), [stderr for _, stderr in results]
    for entry, payload, (stdout, _) in zip(entries, payloads, results):
        assert json.loads(stdout)["issues"] == []
        from mountainash_capabilities import read_observation

        loaded = read_observation(tmp_path, entry.name)
        assert loaded.record["reports"][0]["nodeid"] == records[entries.index(entry)]["reports"][0]["nodeid"]
        assert (entry / "stdout.bin").read_bytes() == payload


def test_competing_process_publishers_keep_winning_record_and_bytes(tmp_path, valid_record):
    from mountainash_capabilities.store import reserve

    entry = reserve(tmp_path)
    gate = tmp_path / "start-publishers"
    specs = []
    procs = []
    for number in range(2):
        payload = f"publisher-{number}".encode()
        source = tmp_path / f"source-{number}.bin"
        source.write_bytes(payload)
        record = valid_record(identity=entry.name)
        record["reports"][0]["nodeid"] = f"tests/test_{number}.py::test_failure_{number}"
        record_path = tmp_path / f"record-{number}.json"
        record_path.write_text(json.dumps(record))
        specs.append((record, payload))
        code = textwrap.dedent(
            """
            import json, sys, time
            from pathlib import Path
            from mountainash_capabilities.store import publish
            entry, gate, record_path, source = map(Path, sys.argv[1:])
            while not gate.exists():
                time.sleep(0.002)
            result = publish(entry, json.loads(record_path.read_text()), {"stdout": source})
            print(json.dumps({"record": result.record, "issues": result.issues}))
            """
        )
        procs.append(
            subprocess.Popen(
                [sys.executable, "-c", code, str(entry), str(gate), str(record_path), str(source)],
                cwd=Path(__file__).resolve().parents[1],
                env=_subprocess_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        )
    gate.touch()
    outputs = [proc.communicate(timeout=15) for proc in procs]
    assert all(proc.returncode == 0 for proc in procs), [stderr for _, stderr in outputs]
    results = [json.loads(stdout) for stdout, _ in outputs]
    assert sum(not result["issues"] for result in results) == 1
    from mountainash_capabilities import read_observation

    loaded = read_observation(tmp_path, entry.name)
    winner = next(record for record, _ in specs if record["reports"][0]["nodeid"] == loaded.record["reports"][0]["nodeid"])
    expected_bytes = next(payload for record, payload in specs if record == winner)
    assert loaded.record["reports"][0]["nodeid"] == winner["reports"][0]["nodeid"]
    assert (entry / "stdout.bin").read_bytes() == expected_bytes
    assert any(
        {issue["code"] for issue in result["issues"]}
        & {"already_published", "publication_in_progress"}
        for result in results
        if result["issues"]
    )

def test_publication_to_symlinked_record_directory_is_rejected(tmp_path, valid_record):
    from mountainash_capabilities.store import publish, reserve

    entry = reserve(tmp_path)
    target = tmp_path / "actual-entry"
    target.mkdir()
    link = tmp_path / "linked-entry"
    link.symlink_to(target, target_is_directory=True)
    record = valid_record(identity=link.name)

    result = publish(link, record, {})

    assert issue_codes(result) & {"invalid_record_directory", "symlink_record_directory", "publication_failed"}
    assert not (target / "record.json").exists()


def test_fresh_process_reads_published_failure(tmp_path, valid_record):
    from mountainash_capabilities.store import publish, reserve

    entry = reserve(tmp_path)
    assert publish(entry, valid_record(identity=entry.name), {}).issues == ()
    code = textwrap.dedent(
        """
        import json, sys
        from pathlib import Path
        from mountainash_capabilities import read_observation
        result = read_observation(Path(sys.argv[1]), sys.argv[2])
        print(json.dumps({
            "outcome": result.record["reports"][0]["outcome"],
            "nodeid": result.record["reports"][0]["nodeid"],
            "issues": result.issues,
        }))
        """
    )

    completed = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), entry.name],
        cwd=Path(__file__).resolve().parents[1],
        env=_subprocess_env(),
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    observed = json.loads(completed.stdout)

    assert observed["outcome"] == "failed"
    assert observed["nodeid"] == "tests/test_example.py::test_expected_failure"
    assert observed["issues"] == []



def test_incomplete_reservation_is_reported_without_a_record(tmp_path):
    from mountainash_capabilities import read_observation
    from mountainash_capabilities.store import reserve

    entry = reserve(tmp_path)
    result = read_observation(tmp_path, entry.name)

    assert result.record is None
    assert "incomplete_entry" in issue_codes(result)


def test_unknown_format_is_reported_and_not_returned(tmp_path, valid_record):
    from mountainash_capabilities import read_observation
    from mountainash_capabilities.store import publish, reserve

    entry = reserve(tmp_path)
    record = valid_record(identity=entry.name)
    record["format_version"] = 99
    result = publish(entry, record, {})
    assert result.record is None or result.record["format_version"] != 99
    assert issue_codes(result) & {"invalid_record", "unsupported_format", "invalid_format"}
    # A rejected publication leaves an incomplete reservation. Independently
    # exercise reading an unsupported record copied into the directory.
    (entry / "record.json").write_text(json.dumps(record))
    loaded = read_observation(tmp_path, entry.name)
    assert loaded.record is None
    assert issue_codes(loaded) & {"invalid_record", "unsupported_format", "invalid_format"}


def test_invalid_json_is_reported_as_an_issue(tmp_path, valid_record):
    from mountainash_capabilities import read_observation
    from mountainash_capabilities.store import publish, reserve

    entry = reserve(tmp_path)
    assert publish(entry, valid_record(identity=entry.name), {}).issues == ()
    (entry / "record.json").write_text("{not valid json")

    result = read_observation(tmp_path, entry.name)

    assert result.record is None
    assert "invalid_record" in issue_codes(result)


def test_missing_retained_artifact_is_reported(tmp_path, valid_record):
    from mountainash_capabilities import read_observation
    from mountainash_capabilities.store import publish, reserve

    entry = reserve(tmp_path)
    source = tmp_path / "diagnostic.bin"
    source.write_bytes(b"failure detail")
    assert publish(entry, valid_record(identity=entry.name), {"stdout": source}).issues == ()
    (entry / "stdout.bin").unlink()

    result = read_observation(tmp_path, entry.name)

    assert result.record is not None
    assert any(code in issue_codes(result) for code in ("missing_artifact", "artifact_unavailable", "artifact_integrity"))


def test_hash_mismatched_artifact_is_reported(tmp_path, valid_record):
    from mountainash_capabilities import read_observation
    from mountainash_capabilities.store import publish, reserve

    entry = reserve(tmp_path)
    source = tmp_path / "diagnostic.bin"
    source.write_bytes(b"original evidence")
    assert publish(entry, valid_record(identity=entry.name), {"stdout": source}).issues == ()
    (entry / "stdout.bin").write_bytes(b"altered evidence")

    result = read_observation(tmp_path, entry.name)

    assert result.record is not None
    assert any(code in issue_codes(result) for code in ("artifact_hash_mismatch", "artifact_integrity", "corrupt_artifact"))


def test_invalid_identity_is_rejected_before_publication(tmp_path, valid_record):
    from mountainash_capabilities.store import publish, reserve

    entry = reserve(tmp_path)
    record = valid_record(identity="../outside")
    result = publish(entry, record, {})

    assert result.record is None
    assert issue_codes(result) & {"invalid_identity", "invalid_record", "identity_mismatch"}
    assert not (entry / "record.json").exists()


def test_parent_traversal_artifact_reference_is_not_read_outside_entry(tmp_path, valid_record):
    from mountainash_capabilities import read_observation
    from mountainash_capabilities.store import publish, reserve

    entry = reserve(tmp_path)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"must not be recovered")
    record = valid_record(identity=entry.name)
    record["artifacts"]["stdout"] = {
        "availability": "retained",
        "content": "original",
        "path": "../outside.bin",
        "size": len(outside.read_bytes()),
        "sha256": "0" * 64,
        "media_type": "application/octet-stream",
        "encoding": "binary",
    }
    # Inject a record as if it came from an untrusted copied evidence directory.
    (entry / "record.json").write_text(json.dumps(record))

    result = read_observation(tmp_path, entry.name)

    assert result.record is not None
    assert "invalid_artifact_path" in issue_codes(result)
    assert outside.read_bytes() == b"must not be recovered"


def test_artifact_symlink_is_not_followed(tmp_path, valid_record):
    from mountainash_capabilities import read_observation
    from mountainash_capabilities.store import reserve

    entry = reserve(tmp_path)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"secret outside observation")
    record = valid_record(identity=entry.name)
    record["artifacts"]["stdout"] = {
        "availability": "retained",
        "content": "original",
        "path": "stdout.bin",
        "size": outside.stat().st_size,
        "sha256": "0" * 64,
        "media_type": "application/octet-stream",
        "encoding": "binary",
    }
    (entry / "stdout.bin").symlink_to(outside)
    (entry / "record.json").write_text(json.dumps(record))

    result = read_observation(tmp_path, entry.name)

    assert result.record is not None
    assert any(code in issue_codes(result) for code in ("artifact_symlink", "invalid_artifact_path", "artifact_integrity"))
    assert outside.read_bytes() == b"secret outside observation"


def test_strict_record_encoding_rejects_nonfinite_json(valid_record):
    from mountainash_capabilities.records import encode_record

    record = valid_record()
    record["process"]["elapsed_seconds"] = float("nan")

    with pytest.raises(ValueError):
        encode_record(record)


def test_raw_omitted_report_preserves_outcome_without_diagnostic_copy(tmp_path, valid_record):
    from mountainash_capabilities import read_observation
    from mountainash_capabilities.store import publish, reserve

    entry = reserve(tmp_path)
    record = valid_record(identity=entry.name)
    record["request"]["retention"]["raw"] = False
    record["reports"][0].update(
        wasxfail_present=True,
        diagnostic={"availability": "omitted"},
        sections={"availability": "omitted"},
    )
    record["artifacts"]["stdout"] = {
        "availability": "omitted",
        "content": "redacted",
        "reason": "raw_retention_disabled",
    }
    assert publish(entry, record, {}).issues == ()

    loaded = read_observation(tmp_path, entry.name)

    assert loaded.issues == ()
    assert loaded.record["reports"][0]["outcome"] == "failed"
    assert loaded.record["reports"][0]["phase"] == "call"
    assert loaded.record["reports"][0]["wasxfail_present"] is True
    assert loaded.record["reports"][0]["diagnostic"]["availability"] == "omitted"
    assert loaded.record["reports"][0]["sections"]["availability"] == "omitted"
    assert loaded.record["artifacts"]["stdout"]["availability"] == "omitted"
    assert "diagnostic_text" not in loaded.record["reports"][0]
    assert "wasxfail" not in loaded.record["reports"][0]


@pytest.mark.parametrize("raw", [True, False])
def test_sanitize_request_redacts_nested_credentials_and_authenticated_urls(valid_record, raw):
    from mountainash_capabilities.records import PytestRequest, Retention, sanitize_request

    metadata = {
        "ordinary": "keep this fact",
        "nested": {
            "Access-Token": "metadata-secret",
            "authorization": "Bearer metadata-auth",
            "url": "https://user:password@host.example/path?token=query-secret&keep=visible",
        },
    }
    args = ("--endpoint=https://user:password@host.example/path?api_key=argument-secret&keep=yes",)
    request = PytestRequest(
        python=Path(sys.executable),
        cwd=Path("/tmp/witness-project"),
        selection=("tests/test_example.py::test_expected_failure",),
        args=args,
        retention=Retention(raw=raw),
        supplied_metadata=metadata,
    )
    original_args = request.args
    original_metadata = deepcopy(request.supplied_metadata)

    sanitized, issues = sanitize_request(request)
    encoded = json.dumps(sanitized)

    assert request.args == original_args
    assert request.supplied_metadata == original_metadata
    assert "metadata-secret" not in encoded
    assert "metadata-auth" not in encoded
    assert "password" not in encoded
    assert "query-secret" not in encoded
    assert "argument-secret" not in encoded
    assert "keep this fact" in encoded
    assert "visible" in encoded and "yes" in encoded
    assert issues
    assert all("secret" not in json.dumps(issue).lower() for issue in issues)
