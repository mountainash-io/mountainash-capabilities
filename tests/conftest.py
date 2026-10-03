import sys

import pytest

@pytest.fixture
def valid_record():
    """Return a factory for a complete, minimal format-1 observation."""

    def make_record(identity="0123456789abcdef0123456789abcdef"):
        return {
            "format_version": 1,
            "kind": "pytest_execution",
            "id": identity,
            "producer": {"name": "mountainash-capabilities", "version": "0.1.0", "protocol_version": 1},
            "captured_at": "2026-10-03T12:00:00Z",
            "ended_at": "2026-10-03T12:00:01Z",
            "elapsed_seconds": 1.0,
            "request": {
                "python": sys.executable,
                "cwd": "/tmp/witness-project",
                "selection": ["tests/test_example.py::test_expected_failure"],
                "args": [],
                "timeout_seconds": None,
                "terminate_grace_seconds": 2.0,
                "packages": [],
                "source_checkout": None,
                "retention": {"raw": True},
                "supplied_metadata": {"purpose": "store regression"},
            },
            "source": {
                "checkout": {"value": None, "status": "not_applicable"},
                "revision": {"value": None, "status": "unknown"},
                "dirty": {"value": None, "status": "unknown"},
                "acquisition_issues": [],
            },
            "context": {
                "companion": {"name": "mountainash-capabilities", "version": "0.1.0"},
                "target_start": {"value": "2026-10-03T12:00:00Z", "status": "recorded"},
                "target_end": {"value": "2026-10-03T12:00:01Z", "status": "recorded"},
                "status": "recorded",
                "environment": {"coordinates": []},
                "supplied_facts": {"purpose": "store regression"},
            },
            "process": {
                "status": "exited",
                "returncode": 1,
                "signal": None,
                "launch_error": None,
                "elapsed_seconds": 1.0,
                "termination": {"requested": False, "escalated": False},
                "cleanup": {"reaped": True},
            },
            "execution": {
                "coverage": "complete",
                "collection_completed": True,
                "candidates": ["tests/test_example.py::test_expected_failure"],
                "selected": ["tests/test_example.py::test_expected_failure"],
                "deselected": [],
                "attempted": ["tests/test_example.py::test_expected_failure"],
                "finished": ["tests/test_example.py::test_expected_failure"],
                "unexecuted": [],
                "pytest_exit_status": 1,
                "cases": [
                    {
                        "nodeid": "tests/test_example.py::test_expected_failure",
                        "occurrence": 0,
                        "attempted_phases": ["setup", "call", "teardown"],
                        "reported_phases": ["setup", "call", "teardown"],
                        "terminal_item": True,
                        "call_status": "reported",
                    }
                ],
            },
            "reports": [
                {
                    "nodeid": "tests/test_example.py::test_expected_failure",
                    "occurrence": 0,
                    "phase": "call",
                    "outcome": "failed",
                    "duration_seconds": 0.01,
                    "wasxfail_present": False,
                    "diagnostic": {"availability": "omitted"},
                    "sections": {"availability": "omitted"},
                    "report_position": 1,
                }
            ],
            "artifacts": {
                role: {
                    "availability": "omitted",
                    "content": "original",
                    "reason": "not_supplied",
                }
                for role in ("stdout", "stderr", "events")
            },
            "issues": [],
        }

    return make_record
