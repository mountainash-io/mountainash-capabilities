"""A wheel must work outside source roots, including its child adapter."""

import json
import os
import subprocess
from pathlib import Path


def test_installed_wheel_runs_and_reads_named_outcomes(tmp_path):
    python = os.environ.get("MA_COMPANION_INSTALLED_PYTHON")
    assert python and Path(python).is_file(), (
        "Set MA_COMPANION_INSTALLED_PYTHON to the development interpreter with the built wheel"
    )
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "test_values.py").write_text(
        "def test_good():\n    assert 2 + 2 == 4\n\ndef test_bad():\n    assert 2 + 2 == 5\n",
        encoding="utf-8",
    )
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    run_script = """
import json, sys
from pathlib import Path
from mountainash_capabilities import PytestRequest, Retention, run_pytest
result = run_pytest(
    PytestRequest(Path(sys.executable), Path(sys.argv[1]),
                  ("test_values.py",), Retention(raw=False)),
    Path(sys.argv[2]),
)
assert not result.persistence_issues, result.persistence_issues
assert result.observation["process"]["returncode"] == 1
assert result.observation["execution"]["coverage"] == "complete"
print(json.dumps({"id": result.observation["id"], "reports": result.observation["reports"]}))
"""
    run = subprocess.run(
        [python, "-c", run_script, str(suite), str(tmp_path / "evidence")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    initial = json.loads(run.stdout)
    read_script = """
import json, sys
from pathlib import Path
from mountainash_capabilities import read_observation, render_markdown, export_json
result = read_observation(Path(sys.argv[1]), sys.argv[2])
assert not result.issues, result.issues
assert "test_bad" in render_markdown(result)
json.loads(export_json(result))
print(json.dumps(result.record["reports"]))
"""
    read = subprocess.run(
        [python, "-c", read_script, str(tmp_path / "evidence"), initial["id"]],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    reports = json.loads(read.stdout)
    assert reports == initial["reports"]
    calls = {r["nodeid"]: r["outcome"] for r in reports if r["phase"] == "call"}
    assert calls == {
        "test_values.py::test_good": "passed",
        "test_values.py::test_bad": "failed",
    }
