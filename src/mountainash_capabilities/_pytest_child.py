"""Small stdlib-only pytest subprocess adapter; no companion imports in target Python."""

from __future__ import annotations

import importlib.metadata
import json
import math
import os
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_PROTOCOL_VERSION = 1
_PHASES = ("setup", "call", "teardown")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def load_request(config_path: str) -> dict[str, Any]:
    with open(config_path, encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError("private request must be a JSON object")
    return value


class Reporter:
    """Emit flushed, ordered JSONL receipts from pytest's public hooks."""

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.sink = open(config["events_path"], "w", encoding="utf-8", newline="\n")
        self.sequence = 0
        self.report_position = 0
        self.collection_failed = False
        self.candidates: list[dict[str, Any]] = []
        self.selected: list[dict[str, Any]] = []
        self.deselected: list[str] = []
        self._candidate_occurrences: dict[str, int] = defaultdict(int)
        self._selected_occurrences: dict[str, list[int]] = defaultdict(list)
        self._started_occurrences: dict[str, int] = defaultdict(int)
        self.active: dict[str, int] = {}
        self.active_phases: set[tuple[str, int, str]] = set()

    @staticmethod
    def _loaded_mountainash() -> dict[str, Any]:
        module = sys.modules.get("mountainash")
        if module is None:
            return {"status": "unknown", "value": None}
        result: dict[str, Any] = {"status": "recorded"}
        try:
            version = getattr(module, "__version__", None)
            if version is None or type(version) is str:
                result["version"] = version
            else:
                result["version_status"] = "unavailable"
                result["version_error_type"] = type(version).__name__
        except Exception as exc:
            result["version_status"] = "unavailable"
            result["version_error_type"] = type(exc).__name__
        try:
            path = getattr(module, "__file__", None)
            if path is None or type(path) is str:
                result["path"] = path
            else:
                result["path_status"] = "unavailable"
                result["path_error_type"] = type(path).__name__
        except Exception as exc:
            result["path_status"] = "unavailable"
            result["path_error_type"] = type(exc).__name__
        return result

    def emit(self, event_type: str, **fields: Any) -> None:
        event = {
            "protocol_version": _PROTOCOL_VERSION,
            "sequence": self.sequence,
            "type": event_type,
            **fields,
        }
        try:
            line = json.dumps(event, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError, OverflowError, RecursionError) as exc:
            line = json.dumps(
                {
                    "protocol_version": _PROTOCOL_VERSION,
                    "sequence": self.sequence,
                    "type": "serialization_error",
                    "source_type": event_type,
                    "error_type": type(exc).__name__,
                },
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
            )
        self.sink.write(line + "\n")
        self.sink.flush()
        self.sequence += 1

    def startup(self) -> None:
        packages = []
        for name in self.config.get("packages", []):
            try:
                version = importlib.metadata.version(name)
                packages.append({"name": name, "version": version, "status": "recorded"})
            except Exception as exc:
                packages.append({
                    "name": name,
                    "version": None,
                    "status": "unavailable",
                    "error_type": type(exc).__name__,
                })
        self.emit(
            "startup",
            captured_at=_now(),
            python={
                "executable": sys.executable,
                "version": sys.version,
                "cwd": os.getcwd(),
                "sys_path": list(sys.path),
            },
            packages=packages,
            mountainash=self._loaded_mountainash(),
        )

    def end_context(self) -> None:
        self.emit("end_context", captured_at=_now(), mountainash=self._loaded_mountainash())

    def close(self) -> None:
        self.sink.close()

    def _nodeid(self, item: Any) -> str:
        return str(item.nodeid)

    def _active_occurrence(self, nodeid: str) -> int:
        if nodeid in self.active:
            return self.active[nodeid]
        selected = self._selected_occurrences.get(nodeid, [])
        index = self._started_occurrences[nodeid]
        if index < len(selected):
            return selected[index]
        candidates = [row["occurrence"] for row in self.candidates if row["nodeid"] == nodeid]
        return candidates[index] if index < len(candidates) else index

    def _report_text(self, report: Any) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        if not self.config.get("raw", False):
            return (
                {"availability": "omitted", "reason": "raw_retention_disabled"},
                {"availability": "omitted", "reason": "raw_retention_disabled"},
                {},
            )

        diagnostic: dict[str, Any]
        diagnostic_text: dict[str, Any] = {}
        try:
            longrepr = getattr(report, "longrepr", None)
            if longrepr is None:
                diagnostic = {"availability": "omitted", "reason": "not_present"}
            else:
                text = str(longrepr)
                diagnostic = {"availability": "retained", "text": text}
        except Exception as exc:
            diagnostic = {"availability": "unavailable", "error_type": type(exc).__name__}

        sections: dict[str, Any]
        try:
            raw_sections = getattr(report, "sections", [])
            values = []
            for name, content in raw_sections:
                values.append({"name": str(name), "text": str(content)})
            sections = {"availability": "retained", "items": values}
        except Exception as exc:
            sections = {"availability": "unavailable", "error_type": type(exc).__name__}
        return diagnostic, sections, diagnostic_text

    def pytest_itemcollected(self, item: Any) -> None:
        nodeid = self._nodeid(item)
        occurrence = self._candidate_occurrences[nodeid]
        self._candidate_occurrences[nodeid] += 1
        row = {"nodeid": nodeid, "occurrence": occurrence}
        self.candidates.append(row)
        self.emit("candidate", **row)

    def pytest_deselected(self, items: list[Any]) -> None:
        nodeids = [self._nodeid(item) for item in items]
        self.deselected.extend(nodeids)
        self.emit("deselected", nodeids=nodeids)

    def pytest_collectreport(self, report: Any) -> None:
        outcome = getattr(report, "outcome", None)
        nodeid = getattr(report, "nodeid", "")
        if outcome == "failed":
            self.collection_failed = True
        diagnostic, sections, text = self._report_text(report)
        duration = getattr(report, "duration", None)
        if type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0:
            duration = None
        event = {
            "phase": "collection",
            "nodeid": nodeid if type(nodeid) is str else str(nodeid),
            "occurrence": 0,
            "outcome": outcome,
            "duration_seconds": duration,
            "wasxfail_present": hasattr(report, "wasxfail"),
            "diagnostic": diagnostic,
            "sections": sections,
            "report_position": self.report_position,
            "target_context": self._loaded_mountainash(),
        }
        if self.config.get("raw", False) and hasattr(report, "wasxfail"):
            try:
                text["wasxfail"] = str(report.wasxfail)
            except Exception as exc:
                event["wasxfail_error_type"] = type(exc).__name__
        event.update(text)
        self.report_position += 1
        self.emit("report", **event)

    def pytest_collection_finish(self, session: Any) -> None:
        counts: dict[str, int] = defaultdict(int)
        for item in session.items:
            nodeid = self._nodeid(item)
            index = counts[nodeid]
            counts[nodeid] += 1
            candidates = [row["occurrence"] for row in self.candidates if row["nodeid"] == nodeid]
            occurrence = candidates[index] if index < len(candidates) else index
            row = {"nodeid": nodeid, "occurrence": occurrence}
            self.selected.append(row)
            self._selected_occurrences[nodeid].append(occurrence)
        self.emit("selected", items=self.selected)

    def pytest_collection(self, session: Any):
        try:
            outcome = yield
        except BaseException:
            self.emit(
                "collection_receipt",
                completed=False,
                failed_reports=self.collection_failed,
                exception=True,
            )
            raise
        completed = outcome.excinfo is None and not self.collection_failed
        self.emit(
            "collection_receipt",
            completed=completed,
            failed_reports=self.collection_failed,
        )

    def pytest_configure(self, config: Any) -> None:
        option = getattr(config, "option", None)
        collect_only = bool(getattr(option, "collectonly", False))
        setup_only = bool(getattr(option, "setuponly", False))
        xdist_active = False
        try:
            plugin = config.pluginmanager.hasplugin("xdist")
            workers = getattr(option, "numprocesses", 0)
            xdist_active = bool(plugin and workers not in (None, 0, "0"))
        except Exception:
            xdist_active = False
        self.emit(
            "execution_mode",
            pytest_version=getattr(sys.modules.get("pytest"), "__version__", None),
            collect_only=collect_only,
            setup_only=setup_only,
            xdist_active=xdist_active,
        )

    def pytest_runtest_logstart(self, nodeid: str, location: Any) -> None:
        index = self._started_occurrences[nodeid]
        self._started_occurrences[nodeid] += 1
        selected = self._selected_occurrences.get(nodeid, [])
        candidates = [row["occurrence"] for row in self.candidates if row["nodeid"] == nodeid]
        occurrence = selected[index] if index < len(selected) else (
            candidates[index] if index < len(candidates) else index
        )
        self.active[nodeid] = occurrence
        self.emit("item_attempt", nodeid=nodeid, occurrence=occurrence)

    def _phase_attempt(self, phase: str, item: Any) -> None:
        nodeid = self._nodeid(item)
        occurrence = self._active_occurrence(nodeid)
        self.active_phases.add((nodeid, occurrence, phase))
        self.emit("phase_attempt", nodeid=nodeid, occurrence=occurrence, phase=phase)

    def pytest_runtest_setup(self, item: Any) -> None:
        self._phase_attempt("setup", item)

    def pytest_runtest_call(self, item: Any) -> None:
        self._phase_attempt("call", item)

    def pytest_runtest_teardown(self, item: Any, nextitem: Any) -> None:
        self._phase_attempt("teardown", item)

    def pytest_runtest_logreport(self, report: Any) -> None:
        phase = getattr(report, "when", None)
        if phase not in _PHASES:
            self.emit("adapter_issue", code="unknown_report_phase", phase=str(phase))
            return
        nodeid = getattr(report, "nodeid", "")
        if type(nodeid) is not str:
            nodeid = str(nodeid)
        occurrence = self.active.get(nodeid, self._active_occurrence(nodeid))
        outcome = getattr(report, "outcome", None)
        duration = getattr(report, "duration", None)
        if type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0:
            duration = None
        diagnostic, sections, text = self._report_text(report)
        event = {
            "phase": phase,
            "nodeid": nodeid,
            "occurrence": occurrence,
            "outcome": outcome,
            "duration_seconds": duration,
            "wasxfail_present": hasattr(report, "wasxfail"),
            "diagnostic": diagnostic,
            "sections": sections,
            "report_position": self.report_position,
            "target_context": self._loaded_mountainash(),
        }
        if self.config.get("raw", False) and hasattr(report, "wasxfail"):
            try:
                text["wasxfail"] = str(report.wasxfail)
            except Exception as exc:
                event["wasxfail_error_type"] = type(exc).__name__
        event.update(text)
        self.report_position += 1
        self.emit("report", **event)

    def pytest_runtest_logfinish(self, nodeid: str, location: Any) -> None:
        occurrence = self.active.get(nodeid, self._active_occurrence(nodeid))
        self.emit("item_finished", nodeid=nodeid, occurrence=occurrence)
        self.active.pop(nodeid, None)

    def pytest_sessionfinish(self, session: Any, exitstatus: int) -> None:
        try:
            status = int(exitstatus)
        except (TypeError, ValueError):
            status = None
        self.emit("session_finish", exit_status=status, mountainash=self._loaded_mountainash())


def register_reporter_hooks(pytest: Any, reporter_type: type[Reporter]) -> None:
    """Apply pytest decorators only after importing the target interpreter's pytest."""
    reporter_type.pytest_collection = pytest.hookimpl(hookwrapper=True)(reporter_type.pytest_collection)
    for name in (
        "pytest_runtest_setup",
        "pytest_runtest_call",
        "pytest_runtest_teardown",
    ):
        setattr(reporter_type, name, pytest.hookimpl(tryfirst=True)(getattr(reporter_type, name)))


def main(config_path: str) -> int:
    config = load_request(config_path)
    cwd = str(config["cwd"])
    if sys.path:
        sys.path[0] = cwd
    else:
        sys.path.insert(0, cwd)
    sys.argv = ["pytest", *config["pytest_args"]]
    reporter = Reporter(config)
    reporter.startup()
    try:
        import pytest

        register_reporter_hooks(pytest, Reporter)
        return int(pytest.main(config["pytest_args"], plugins=[reporter]))
    finally:
        try:
            reporter.end_context()
        finally:
            reporter.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
