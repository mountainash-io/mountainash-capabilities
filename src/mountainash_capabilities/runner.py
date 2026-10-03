"""Run an explicit pytest target and retain its factual execution observation."""

from __future__ import annotations

import errno
import json
import math
import os
import shutil
import signal
import stat
import subprocess
import time
from pathlib import Path
from threading import Event
from typing import Any
from uuid import uuid4

from . import store
from .records import (
    PytestRequest,
    Record,
    RunResult,
    issue,
    sanitize_request,
    serialize_environment,
    utc_now,
    parse_time,
    validate_request,
)

_PROTOCOL_VERSION = 1
_PHASES = {"setup", "call", "teardown"}
_OUTCOMES = {"passed", "failed", "skipped"}


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"nonfinite JSON number: {value}")


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("nonfinite JSON number")
    return parsed


def decode_events(path: Path) -> tuple[list[Record], list[Record]]:
    """Decode the measured JSONL prefix and expose loss without dropping prior facts."""
    events: list[Record] = []
    problems: list[Record] = []
    expected = 0
    try:
        with store._open_regular(Path(path)) as stream:
            remaining = os.fstat(stream.fileno()).st_size
            line_number = 0
            while remaining:
                raw = stream.readline(remaining)
                if not raw:
                    problems.append(issue("event_stream_short_read", "decode"))
                    break
                remaining -= len(raw)
                line_number += 1
                if not raw.endswith(b"\n"):
                    problems.append(issue("truncated_event_line", "decode", line=line_number))
                try:
                    event = json.loads(
                        raw.decode("utf-8"), object_pairs_hook=_strict_object,
                        parse_constant=_reject_constant, parse_float=_finite_float,
                    )
                    if not isinstance(event, dict):
                        raise ValueError("event must be an object")
                    sequence = event.get("sequence")
                    if type(sequence) is not int or sequence < 0:
                        raise ValueError("missing event sequence")
                except (UnicodeError, ValueError, TypeError, RecursionError) as exc:
                    problems.append(issue("malformed_event", "decode", line=line_number,
                                          error_type=type(exc).__name__))
                    continue
                if sequence != expected:
                    problems.append(issue("event_sequence_gap", "decode", line=line_number,
                                          expected=expected, observed=sequence))
                expected = sequence + 1
                events.append(event)
    except (OSError, ValueError) as exc:
        problems.append(issue("event_stream_unavailable", "decode",
                              error_type=type(exc).__name__, errno=getattr(exc, "errno", None)))
    return events, problems


def _companion_version() -> str:
    try:
        from importlib.metadata import version

        return version("mountainash-capabilities")
    except Exception:
        return "0.1.0"


def _base_record(request: PytestRequest, retained_request: Record, identity: str) -> Record:
    now = utc_now()
    raw = request.retention.raw
    return {
        "format_version": 1,
        "kind": "pytest_execution",
        "id": identity,
        "producer": {
            "name": "mountainash-capabilities",
            "version": _companion_version(),
            "protocol_version": _PROTOCOL_VERSION,
        },
        "captured_at": now,
        "ended_at": now,
        "elapsed_seconds": 0.0,
        "request": retained_request,
        "source": {
            "checkout": {"value": None, "status": "not_applicable"},
            "revision": {"value": None, "status": "unknown"},
            "dirty": {"value": None, "status": "unknown"},
            "acquisition_issues": [],
        },
        "context": {
            "companion": {"name": "mountainash-capabilities", "version": _companion_version()},
            "target_start": {"value": None, "status": "unknown"},
            "target_end": {"value": None, "status": "unknown"},
            "environment": {"coordinates": []},
            "supplied_facts": retained_request["supplied_metadata"],
            "status": "unavailable",
            "acquisitions": [],
        },
        "process": {
            "status": "launch_failed",
            "returncode": None,
            "signal": None,
            "launch_error": None,
            "elapsed_seconds": 0.0,
            "termination": {"requested": False, "escalated": False},
            "cleanup": {"reaped": True},
        },
        "execution": {
            "coverage": "unknown",
            "collection_completed": False,
            "candidates": [],
            "selected": [],
            "deselected": [],
            "attempted": [],
            "finished": [],
            "unexecuted": [],
            "pytest_exit_status": None,
            "cases": [],
        },
        "reports": [],
        "artifacts": {
            "stdout": {
                "availability": "omitted" if not raw else "unavailable",
                "content": "redacted" if not raw else "original",
                "reason": "raw_retention_disabled" if not raw else "not_captured",
            },
            "stderr": {
                "availability": "omitted" if not raw else "unavailable",
                "content": "redacted" if not raw else "original",
                "reason": "raw_retention_disabled" if not raw else "not_captured",
            },
            "events": {
                "availability": "unavailable",
                "content": "redacted" if not raw else "original",
                "reason": "not_captured",
            },
        },
        "issues": [],
    }


def _source_facts(request: PytestRequest) -> tuple[Record, list[Record]]:
    if request.source_checkout is None:
        return (
            {
                "checkout": {"value": None, "status": "not_applicable"},
                "revision": {"value": None, "status": "unknown"},
                "dirty": {"value": None, "status": "unknown"},
                "acquisition_issues": [],
            },
            [],
        )

    checkout = request.source_checkout.absolute()
    facts: Record = {
        "checkout": {"value": str(checkout), "status": "recorded"},
        "revision": {"value": None, "status": "unavailable"},
        "dirty": {"value": None, "status": "unavailable"},
        "acquisition_issues": [],
    }
    problems: list[Record] = []
    try:
        revision = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "--verify", "HEAD"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        if revision.returncode == 0:
            value = revision.stdout.decode("ascii", errors="strict").strip()
            if value:
                facts["revision"] = {"value": value, "status": "recorded"}
            else:
                raise ValueError("git returned an empty revision")
        else:
            problem = issue("source_revision_unavailable", "source", returncode=revision.returncode)
            problems.append(problem)
        status = subprocess.run(
            ["git", "-C", str(checkout), "status", "--porcelain=v1", "--untracked-files=normal"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        if status.returncode == 0:
            facts["dirty"] = {"value": bool(status.stdout.strip()), "status": "recorded"}
        else:
            problems.append(issue("source_status_unavailable", "source", returncode=status.returncode))
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError, ValueError) as exc:
        problems.append(issue(
            "source_acquisition_unavailable",
            "source",
            error_type=type(exc).__name__,
            errno=getattr(exc, "errno", None),
        ))
    facts["acquisition_issues"] = problems
    return facts, problems


def _make_work_file(path: Path, *, exclusive: bool = True) -> None:
    flags = os.O_WRONLY | os.O_CREAT
    if exclusive:
        flags |= os.O_EXCL
    fd = os.open(path, flags, 0o600)
    os.close(fd)
    os.chmod(path, 0o600)


def _cutoff_size(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError(errno.EINVAL, "capture source is not a regular file")
        return metadata.st_size
    finally:
        os.close(fd)


def _snapshot(source: Path, destination: Path, size_limit: int) -> int:
    """Copy only the regular-file prefix measured at the capture cutoff."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(source, flags)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError(errno.EINVAL, "capture source is not a regular file")
        if metadata.st_size < size_limit:
            raise OSError(errno.EIO, "capture source shortened after the cutoff")
        remaining = size_limit
        copied = 0
        out_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            while remaining:
                block = os.read(fd, min(1024 * 1024, remaining))
                if not block:
                    raise OSError(errno.EIO, "capture source shortened during snapshot")
                view = memoryview(block)
                while view:
                    written = os.write(out_fd, view)
                    view = view[written:]
                copied += len(block)
                remaining -= len(block)
            os.fsync(out_fd)
        finally:
            os.close(out_fd)
        os.chmod(destination, 0o600)
        return copied
    finally:
        os.close(fd)


def _group_exists(process_id: int) -> bool:
    if os.name != "posix":
        return False
    try:
        os.killpg(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _terminate(process: subprocess.Popen[bytes], grace_seconds: float) -> tuple[int | None, bool]:
    """Terminate the owned process group, then escalate after finite grace."""
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    else:
        try:
            process.terminate()
        except OSError:
            pass
    escalated = False
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        escalated = True
    if os.name == "posix" and _group_exists(process.pid):
        escalated = True
    if escalated:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            try:
                process.kill()
            except OSError:
                pass
    if process.poll() is None:
        process.wait()
    return process.returncode, escalated


def _case_key(nodeid: str, occurrence: int) -> tuple[str, int]:
    return nodeid, occurrence


def _account(events: list[Record], problems: list[Record]) -> tuple[Record, list[Record], list[Record]]:
    execution: Record = {
        "coverage": "unknown",
        "collection_completed": False,
        "candidates": [],
        "selected": [],
        "deselected": [],
        "attempted": [],
        "finished": [],
        "unexecuted": [],
        "pytest_exit_status": None,
        "cases": [],
    }
    reports: list[Record] = []
    issues = list(problems)
    selected_keys: list[tuple[str, int]] = []
    cases: dict[tuple[str, int], Record] = {}
    case_order: list[tuple[str, int]] = []
    terminal: set[tuple[str, int]] = set()
    attempted_keys: set[tuple[str, int]] = set()
    collection_receipt_seen = False
    selected_receipt_seen = False
    accounting_loss = bool(problems)
    topology_unsupported = False
    collect_only = False
    setup_only = False

    def get_case(nodeid: str, occurrence: int) -> Record:
        key = _case_key(nodeid, occurrence)
        if key not in cases:
            cases[key] = {
                "nodeid": nodeid,
                "occurrence": occurrence,
                "attempted_phases": [],
                "reported_phases": [],
                "terminal_item": False,
                "call_status": "unknown",
            }
            case_order.append(key)
        return cases[key]

    for event in events:
        if event.get("protocol_version") != _PROTOCOL_VERSION:
            issues.append(issue("unsupported_event_protocol", "decode"))
            accounting_loss = True
            continue
        kind = event.get("type")
        if kind == "startup":
            continue
        if kind == "end_context":
            continue
        if kind == "candidate":
            nodeid, occurrence = event.get("nodeid"), event.get("occurrence")
            if type(nodeid) is not str or type(occurrence) is not int or occurrence < 0:
                issues.append(issue("malformed_candidate", "accounting"))
                accounting_loss = True
                continue
            execution["candidates"].append(nodeid)
        elif kind == "selected":
            selected_receipt_seen = True
            items = event.get("items")
            if not isinstance(items, list):
                issues.append(issue("malformed_selected_receipt", "accounting"))
                accounting_loss = True
                continue
            for item in items:
                if not isinstance(item, dict):
                    issues.append(issue("malformed_selected_item", "accounting"))
                    accounting_loss = True
                    continue
                nodeid, occurrence = item.get("nodeid"), item.get("occurrence")
                if type(nodeid) is not str or type(occurrence) is not int or occurrence < 0:
                    issues.append(issue("malformed_selected_item", "accounting"))
                    accounting_loss = True
                    continue
                key = _case_key(nodeid, occurrence)
                selected_keys.append(key)
                execution["selected"].append(nodeid)
                get_case(nodeid, occurrence)
        elif kind == "deselected":
            nodeids = event.get("nodeids")
            if not isinstance(nodeids, list) or any(type(nodeid) is not str for nodeid in nodeids):
                issues.append(issue("malformed_deselected_receipt", "accounting"))
                accounting_loss = True
                continue
            execution["deselected"].extend(nodeids)
        elif kind == "collection_receipt":
            collection_receipt_seen = True
            completed = event.get("completed")
            if type(completed) is not bool:
                issues.append(issue("malformed_collection_receipt", "accounting"))
                accounting_loss = True
            else:
                execution["collection_completed"] = completed
        elif kind == "item_attempt":
            nodeid, occurrence = event.get("nodeid"), event.get("occurrence")
            if type(nodeid) is not str or type(occurrence) is not int or occurrence < 0:
                issues.append(issue("malformed_item_attempt", "accounting"))
                accounting_loss = True
                continue
            key = _case_key(nodeid, occurrence)
            execution["attempted"].append(nodeid)
            attempted_keys.add(key)
            get_case(nodeid, occurrence)
        elif kind == "phase_attempt":
            nodeid, occurrence, phase = (
                event.get("nodeid"), event.get("occurrence"), event.get("phase")
            )
            if (type(nodeid) is not str or type(occurrence) is not int or occurrence < 0
                    or type(phase) is not str or phase not in _PHASES):
                issues.append(issue("malformed_phase_attempt", "accounting"))
                accounting_loss = True
                continue
            case = get_case(nodeid, occurrence)
            if phase not in case["attempted_phases"]:
                case["attempted_phases"].append(phase)
        elif kind == "report":
            phase, nodeid, occurrence = (
                event.get("phase"), event.get("nodeid"), event.get("occurrence")
            )
            outcome = event.get("outcome")
            position = event.get("report_position")
            duration = event.get("duration_seconds")
            diagnostic, sections = event.get("diagnostic"), event.get("sections")
            if (
                type(phase) is not str
                or phase not in (_PHASES | {"collection"})
                or type(nodeid) is not str
                or type(occurrence) is not int
                or occurrence < 0
                or type(outcome) is not str
                or outcome not in _OUTCOMES
                or type(event.get("wasxfail_present")) is not bool
                or type(position) is not int
                or position < 0
                or (duration is not None and (type(duration) not in (int, float) or duration < 0))
                or not isinstance(diagnostic, dict)
                or type(diagnostic.get("availability")) is not str
                or diagnostic.get("availability") not in {"retained", "omitted", "unavailable"}
                or not isinstance(sections, dict)
                or type(sections.get("availability")) is not str
                or sections.get("availability") not in {"retained", "omitted", "unavailable"}
            ):
                issues.append(issue("malformed_report_event", "accounting"))
                accounting_loss = True
                continue
            report = {
                "nodeid": nodeid,
                "occurrence": occurrence,
                "phase": phase,
                "outcome": outcome,
                "duration_seconds": duration,
                "wasxfail_present": event["wasxfail_present"],
                "diagnostic": diagnostic,
                "sections": sections,
                "report_position": position,
            }
            if "wasxfail" in event:
                report["wasxfail"] = event["wasxfail"]
            reports.append(report)
            if phase in _PHASES:
                case = get_case(nodeid, occurrence)
                if phase not in case["reported_phases"]:
                    case["reported_phases"].append(phase)
        elif kind == "item_finished":
            nodeid, occurrence = event.get("nodeid"), event.get("occurrence")
            if type(nodeid) is not str or type(occurrence) is not int or occurrence < 0:
                issues.append(issue("malformed_item_finish", "accounting"))
                accounting_loss = True
                continue
            key = _case_key(nodeid, occurrence)
            get_case(nodeid, occurrence)["terminal_item"] = True
            terminal.add(key)
            execution["finished"].append(nodeid)
        elif kind == "session_finish":
            status = event.get("exit_status")
            if status is None or type(status) is int:
                execution["pytest_exit_status"] = status
            else:
                issues.append(issue("malformed_pytest_exit_status", "accounting"))
                accounting_loss = True
        elif kind == "execution_mode":
            collect_only = bool(event.get("collect_only"))
            setup_only = bool(event.get("setup_only"))
            topology_unsupported = topology_unsupported or bool(event.get("xdist_active"))
        elif kind == "adapter_issue":
            code = event.get("code")
            issues.append(issue(code if type(code) is str else "child_adapter_issue", "accounting"))
            accounting_loss = True
        elif kind == "serialization_error":
            issues.append(issue(
                "event_serialization_failed",
                "accounting",
                source_type=event.get("source_type") if type(event.get("source_type")) is str else "unknown",
                error_type=event.get("error_type") if type(event.get("error_type")) is str else "unknown",
            ))
            accounting_loss = True
        else:
            issues.append(issue("unknown_child_event", "accounting", event_type=kind if type(kind) is str else "unknown"))
            accounting_loss = True

    if not collection_receipt_seen:
        issues.append(issue("missing_collection_receipt", "accounting"))
        accounting_loss = True
    if execution["collection_completed"] and not selected_receipt_seen:
        issues.append(issue("missing_selected_receipt", "accounting"))
        execution["collection_completed"] = False
        accounting_loss = True
    if topology_unsupported:
        issues.append(issue("unsupported_execution_topology", "accounting"))

    duplicate_nodeids = set()
    seen_nodeids = set()
    for nodeid in execution["candidates"]:
        if nodeid in seen_nodeids:
            duplicate_nodeids.add(nodeid)
        seen_nodeids.add(nodeid)
    for nodeid in duplicate_nodeids:
        issues.append(issue("ambiguous_duplicate_nodeid", "accounting", nodeid=nodeid))
    for key in case_order:
        case = cases[key]
        nodeid, occurrence = key
        if "call" in case["reported_phases"]:
            case["call_status"] = "reported"
        elif "call" in case["attempted_phases"]:
            case["call_status"] = "unfinished"
        elif case["attempted_phases"] or case["terminal_item"] or execution["collection_completed"]:
            case["call_status"] = "not_executed"
        else:
            case["call_status"] = "unknown"
        if key not in attempted_keys and key in selected_keys:
            execution["unexecuted"].append(nodeid)

    execution["cases"] = [cases[key] for key in case_order]
    if topology_unsupported:
        execution["coverage"] = "unknown"
    elif execution["collection_completed"] and not execution["selected"]:
        execution["coverage"] = "empty"
    elif not execution["collection_completed"]:
        execution["coverage"] = "unknown"
    elif (
        not accounting_loss
        and not collect_only
        and not setup_only
        and selected_keys
        and all(key in terminal for key in selected_keys)
    ):
        execution["coverage"] = "complete"
    else:
        execution["coverage"] = "partial"
    return execution, reports, issues


def _context(events: list[Record], request: PytestRequest, issues: list[Record]) -> Record:
    startup = next((event for event in events if event.get("type") == "startup"), None)
    end = next((event for event in reversed(events) if event.get("type") == "end_context"), None)
    requested = list(request.packages)
    acquisitions: list[Record] = []
    versions: list[tuple[str, str]] = []
    package_rows = startup.get("packages") if startup else None
    rows_by_name: dict[str, list[Record]] = {}
    if isinstance(package_rows, list):
        for item in package_rows:
            if isinstance(item, dict) and type(item.get("name")) is str:
                rows_by_name.setdefault(item["name"], []).append(item)
    missing = False
    for name in requested:
        rows = rows_by_name.get(name, [])
        item = rows.pop(0) if rows else None
        if item is None:
            entry = {"kind": "package", "name": name, "version": None, "status": "unknown"}
            missing = True
        elif item.get("status") == "recorded" and type(item.get("version")) is str:
            entry = {
                "kind": "package",
                "name": name,
                "version": item["version"],
                "status": "recorded",
            }
            versions.append((name, item["version"]))
        else:
            entry = {
                "kind": "package",
                "name": name,
                "version": None,
                "status": "unavailable",
            }
            if type(item.get("error_type")) is str:
                entry["error_type"] = item["error_type"]
        acquisitions.append(entry)

    environment: Record = {"coordinates": []}
    environment_error = None
    try:
        from mountainash.core.capabilities.capture import Environment, EnvironmentCoordinate

        native = Environment(tuple(
            EnvironmentCoordinate("package", name, version) for name, version in versions
        ))
        environment = serialize_environment(native)
    except Exception as exc:
        environment_error = type(exc).__name__
        problem = issue("environment_serialization_unavailable", "context", error_type=environment_error)
        issues.append(problem)

    target_start: Record
    target_end: Record
    start_valid = False
    end_valid = False
    start_value = startup.get("captured_at") if startup is not None else None
    if type(start_value) is str:
        try:
            parse_time(start_value)
            start_valid = True
        except ValueError:
            issues.append(issue("invalid_target_start_receipt", "context"))
    if start_valid:
        target_start = {
            "value": start_value,
            "status": "recorded",
            "receipt": {
                "python": startup.get("python"),
                "mountainash": startup.get("mountainash"),
            },
        }
    else:
        target_start = {"value": None, "status": "unavailable" if events else "unknown"}

    end_value = end.get("captured_at") if end is not None else None
    if type(end_value) is str:
        try:
            parse_time(end_value)
            end_valid = True
        except ValueError:
            issues.append(issue("invalid_target_end_receipt", "context"))
    if end_valid:
        target_end = {
            "value": end_value,
            "status": "recorded",
            "receipt": {"mountainash": end.get("mountainash")},
        }
    else:
        target_end = {"value": None, "status": "unknown"}

    if startup is None:
        status = "unavailable"
    elif missing or environment_error or not start_valid or not end_valid or any(
        entry["status"] != "recorded" for entry in acquisitions
    ):
        status = "partial"
    else:
        status = "recorded"
    mode = next((event for event in events if event.get("type") == "execution_mode"), {})
    pytest_version = mode.get("pytest_version")
    return {
        "companion": {"name": "mountainash-capabilities", "version": _companion_version()},
        "adapter": {
            "pytest_version": pytest_version,
            "adapter_compatibility": "qualified" if pytest_version == "8.3.5" else "unverified",
        },
        "target_start": target_start,
        "target_end": target_end,
        "environment": environment,
        "supplied_facts": {},
        "status": status,
        "acquisitions": acquisitions,
    }



def _prepare_private_run(
    request: PytestRequest,
    work: Path,
    cwd: Path,
    raw: bool,
) -> tuple[Path, Path | None, Path | None, Path]:
    work.mkdir(mode=0o700)
    os.chmod(work, 0o700)
    config_path = work / "request.json"
    events_path = work / "events.jsonl"
    stdout_path = work / "stdout.bin" if raw else None
    stderr_path = work / "stderr.bin" if raw else None
    _make_work_file(events_path)
    if stdout_path is not None:
        _make_work_file(stdout_path)
    if stderr_path is not None:
        _make_work_file(stderr_path)
    config = {
        "cwd": str(cwd),
        "events_path": str(events_path),
        "pytest_args": [*request.selection, *request.args],
        "packages": list(request.packages),
        "raw": raw,
    }
    fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(config, stream, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    os.chmod(config_path, 0o600)
    return config_path, stdout_path, stderr_path, events_path


def _launch_and_wait(
    request: PytestRequest,
    config_path: Path,
    stdout_path: Path | None,
    stderr_path: Path | None,
    cwd: Path,
    cancel: Event | None,
    problems: list[Record],
) -> tuple[subprocess.Popen[bytes] | None, str, bool, bool, bool, bool, bool, float, Record | None]:
    started = time.monotonic()
    out_stream = err_stream = None
    try:
        if stdout_path is None:
            out_stream = open(os.devnull, "wb")
            err_stream = open(os.devnull, "wb")
        else:
            out_stream = open(stdout_path, "ab", buffering=0)
            err_stream = open(stderr_path, "ab", buffering=0)
        command = [
            str(request.python),
            str(Path(__file__).resolve().with_name("_pytest_child.py")),
            str(config_path),
        ]
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=out_stream,
            stderr=err_stream,
            shell=False,
            start_new_session=(os.name == "posix"),
        )
    except (OSError, ValueError) as exc:
        if out_stream is not None:
            out_stream.close()
        if err_stream is not None:
            err_stream.close()
        problem = issue(
            "pytest_launch_failed",
            "launch",
            error_type=type(exc).__name__,
            errno=getattr(exc, "errno", None),
        )
        problems.append(problem)
        return None, "launch_failed", False, False, False, True, False, time.monotonic() - started, {
            "error_type": type(exc).__name__,
            "errno": getattr(exc, "errno", None),
        }

    if out_stream is not None:
        out_stream.close()
    if err_stream is not None:
        err_stream.close()

    status = "exited"
    termination_requested = False
    escalated = False
    interrupted = False
    timed_out = False
    cancelled = False
    deadline = None if request.timeout_seconds is None else started + request.timeout_seconds
    while process.poll() is None:
        try:
            if cancel is not None and cancel.is_set():
                cancelled = True
                termination_requested = True
                _, escalated = _terminate(process, request.terminate_grace_seconds)
                break
            if deadline is not None and time.monotonic() >= deadline:
                timed_out = True
                termination_requested = True
                _, escalated = _terminate(process, request.terminate_grace_seconds)
                break
            time.sleep(0.02)
        except KeyboardInterrupt:
            interrupted = True
            termination_requested = True
            _, escalated = _terminate(process, request.terminate_grace_seconds)
            break
    if process.poll() is None:
        process.wait()
    if interrupted:
        status = "interrupted"
    elif timed_out:
        status = "timed_out"
    elif cancelled:
        status = "cancelled"
    return (
        process,
        status,
        termination_requested,
        escalated,
        interrupted,
        True,
        False,
        time.monotonic() - started,
        None,
    )


def run_pytest(request: PytestRequest, destination: Path, *, cancel: Event | None = None) -> RunResult:
    """Run precisely the requested pytest invocation and retain observed facts."""
    validate_request(request)
    retained_request, notices = sanitize_request(request)
    identity = uuid4().hex
    record = _base_record(request, retained_request, identity)
    record["issues"].extend(notices)
    begun = time.monotonic()
    record_dir: Path | None = None
    persistence_issues: list[Record] = []
    work: Path | None = None
    snapshot_paths: dict[str, Path] = {}
    events_path: Path | None = None
    process: subprocess.Popen[bytes] | None = None
    process_status = "launch_failed"
    termination_requested = False
    escalated = False
    reaped = True
    returncode: int | None = None
    launch_error: Record | None = None
    process_elapsed = 0.0

    try:
        record_dir = store.reserve(Path(destination))
        record["id"] = record_dir.name
    except (OSError, ValueError, TypeError) as exc:
        problem = issue(
            "storage_reservation_failed",
            "reserve",
            error_type=type(exc).__name__,
            errno=getattr(exc, "errno", None),
        )
        record["issues"].append(problem)
        persistence_issues.append(problem)
        record["process"]["status"] = "launch_failed"
        record["process"]["launch_error"] = {"stage": "reserve", "error_type": type(exc).__name__}
        record["process"]["capture_cutoff"] = utc_now()
        record["process"]["cleanup"] = {"reaped": True}
        record["ended_at"] = utc_now()
        elapsed = time.monotonic() - begun
        record["elapsed_seconds"] = elapsed
        record["process"]["elapsed_seconds"] = elapsed
        return RunResult(record, None, tuple(persistence_issues))

    cwd = request.cwd.absolute()
    source, source_issues = _source_facts(request)
    record["source"] = source
    record["issues"].extend(source_issues)
    problems: list[Record] = []
    work = record_dir / ".work"
    events_path = work / "events.jsonl"

    try:
        config_path, stdout_path, stderr_path, events_path = _prepare_private_run(
            request, work, cwd, request.retention.raw
        )
        result = _launch_and_wait(
            request, config_path, stdout_path, stderr_path, cwd, cancel, problems
        )
        (
            process,
            process_status,
            termination_requested,
            escalated,
            _interrupted,
            reaped,
            _unused,
            process_elapsed,
            launch_error,
        ) = result
        if process is not None:
            returncode = process.returncode
    except (OSError, ValueError, TypeError) as exc:
        process_status = "launch_failed"
        launch_error = {"stage": "private_work", "error_type": type(exc).__name__,
                        "errno": getattr(exc, "errno", None)}
        problems.append(issue(
            "private_work_failed",
            "launch",
            error_type=type(exc).__name__,
            errno=getattr(exc, "errno", None),
        ))
        reaped = True
    record["issues"].extend(problems)
    if launch_error is not None:
        record["process"]["launch_error"] = launch_error
    record["process"]["status"] = process_status
    record["process"]["returncode"] = returncode
    record["process"]["signal"] = -returncode if returncode is not None and returncode < 0 else None
    record["process"]["elapsed_seconds"] = process_elapsed
    record["process"]["termination"] = {
        "requested": termination_requested,
        "escalated": escalated,
        "reason": process_status if termination_requested else None,
    }
    record["process"]["cleanup"] = {"reaped": reaped}

    capture_sources: dict[str, Path] = {}
    if work is not None:
        if request.retention.raw:
            capture_sources.update(stdout=work / "stdout.bin", stderr=work / "stderr.bin")
        if events_path is not None:
            capture_sources["events"] = events_path
    cutoff_sizes: dict[str, int] = {}
    for role, source_path in capture_sources.items():
        try:
            cutoff_sizes[role] = _cutoff_size(source_path)
        except OSError as exc:
            problem = issue(
                "artifact_cutoff_unavailable",
                "snapshot",
                role=role,
                error_type=type(exc).__name__,
                errno=exc.errno,
            )
            record["issues"].append(problem)
            record["artifacts"][role] = {
                "availability": "unavailable",
                "content": "redacted" if (role == "events" and not request.retention.raw) else (
                    "redacted" if not request.retention.raw else "original"
                ),
                "reason": "capture_cutoff_unavailable",
            }
    cutoff = utc_now()
    record["process"]["capture_cutoff"] = cutoff
    record["process"]["capture_sizes"] = cutoff_sizes

    for role, source_path in capture_sources.items():
        if role not in cutoff_sizes:
            continue
        target = work / f"snapshot-{role}.bin"
        try:
            _snapshot(source_path, target, cutoff_sizes[role])
            snapshot_paths[role] = target
        except OSError as exc:
            problem = issue(
                "artifact_snapshot_failed",
                "snapshot",
                role=role,
                error_type=type(exc).__name__,
                errno=exc.errno,
            )
            record["issues"].append(problem)
            record["artifacts"][role] = {
                "availability": "unavailable",
                "content": "redacted" if (role == "events" and not request.retention.raw) else (
                    "redacted" if not request.retention.raw else "original"
                ),
                "reason": "snapshot_failed",
            }

    if "events" in snapshot_paths:
        events, decode_issues = decode_events(snapshot_paths["events"])
    elif events_path is not None:
        events, decode_issues = decode_events(events_path)
        decode_issues.append(issue("event_snapshot_unavailable", "decode"))
    else:
        events = []
        decode_issues = [issue("event_stream_unavailable", "decode")]
    execution, reports, accounting_issues = _account(events, decode_issues)
    record["execution"] = execution
    record["reports"] = reports
    record["issues"].extend(accounting_issues)
    record["context"] = _context(events, request, record["issues"])
    record["context"]["supplied_facts"] = retained_request["supplied_metadata"]
 
    if not request.retention.raw:
        record["artifacts"]["stdout"] = {
            "availability": "omitted", "content": "redacted", "reason": "raw_retention_disabled"
        }
        record["artifacts"]["stderr"] = {
            "availability": "omitted", "content": "redacted", "reason": "raw_retention_disabled"
        }
    for role in ("stdout", "stderr", "events"):
        if role in snapshot_paths:
            record["artifacts"][role] = {
                "availability": "unavailable",
                "content": "redacted" if (role == "events" and not request.retention.raw) else "original",
                "reason": "awaiting_publication",
            }

    record["ended_at"] = utc_now()
    elapsed = time.monotonic() - begun
    record["elapsed_seconds"] = elapsed
    record["process"]["elapsed_seconds"] = process_elapsed
    record["process"]["capture_cutoff"] = cutoff

    try:
        published = store.publish(record_dir, record, snapshot_paths)
        persistence_issues.extend(published.issues)
        if published.record is not None:
            record = published.record
        else:
            record["issues"].extend(published.issues)
    except Exception as exc:
        problem = issue(
            "publication_failed",
            "publish",
            error_type=type(exc).__name__,
            errno=getattr(exc, "errno", None),
        )
        record["issues"].append(problem)
        persistence_issues.append(problem)

    if work is not None and os.path.lexists(work):
        try:
            shutil.rmtree(work)
        except OSError as exc:
            problem = issue(
                "private_work_cleanup_failed",
                "cleanup",
                path=str(work),
                error_type=type(exc).__name__,
                errno=exc.errno,
            )
            record["issues"].append(problem)
            persistence_issues.append(problem)
    return RunResult(record, record_dir, tuple(persistence_issues))
