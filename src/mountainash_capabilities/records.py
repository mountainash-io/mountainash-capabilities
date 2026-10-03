"""Versioned JSON observations; no executable objects in retained evidence."""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID

if TYPE_CHECKING:
    from mountainash.core.capabilities.capture import Environment

Record = dict[str, Any]
PROCESS_STATUSES = frozenset({"exited", "launch_failed", "timed_out", "cancelled", "interrupted"})
COVERAGE_STATUSES = frozenset({"complete", "partial", "unknown", "empty"})
CONTEXT_STATUSES = frozenset({"recorded", "partial", "unavailable"})


@dataclass(frozen=True)
class Retention:
    raw: bool


@dataclass(frozen=True)
class PytestRequest:
    python: Path
    cwd: Path
    selection: tuple[str, ...]
    retention: Retention
    args: tuple[str, ...] = ()
    packages: tuple[str, ...] = ()
    source_checkout: Path | None = None
    timeout_seconds: float | None = None
    terminate_grace_seconds: float = 2.0
    supplied_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReadResult:
    record: Record | None
    issues: tuple[Record, ...]


@dataclass(frozen=True)
class RunResult:
    observation: Record
    record_dir: Path | None
    persistence_issues: tuple[Record, ...]


@dataclass(frozen=True)
class Query:
    witness: str | None = None
    captured_from: str | None = None
    captured_before: str | None = None
    process_status: str | None = None
    coverage_status: str | None = None
    context_status: str | None = None


@dataclass(frozen=True)
class QueryResult:
    records: tuple[ReadResult, ...]
    issues: tuple[Record, ...]
    complete: bool
    query: Query
    destination: Path
    enumerated_at: str


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def issue(code: str, stage: str, **details: Any) -> Record:
    return {"code": code, "stage": stage, **details}


def _json_value(value: Any) -> None:
    if isinstance(value, dict):
        if any(type(key) is not str for key in value):
            raise ValueError("JSON object keys must be strings")
        for child in value.values():
            _json_value(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _json_value(child)


def encode_record(record: Record) -> str:
    """Encode strict JSON without lossy mapping-key conversion."""
    try:
        _json_value(record)
        return json.dumps(record, ensure_ascii=False, allow_nan=False, indent=2)
    except (TypeError, OverflowError, RecursionError) as exc:
        raise ValueError("record is not JSON-compatible") from exc


def serialize_environment(environment: Environment) -> Record:
    """Preserve native canonical coordinates, unknowns and supplied labels."""
    return {"coordinates": [asdict(coordinate) for coordinate in environment.coordinates]}


def valid_identity(value: Any) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(r"[0-9a-f]{32}", value) is not None
        and UUID(hex=value).version == 4
    )


def parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
            raise ValueError("timestamp requires UTC")
        return parsed
    except (TypeError, AttributeError, OverflowError) as exc:
        raise ValueError("invalid UTC timestamp") from exc


def validate_record(record: Record) -> None:
    """Validate format-1 structure, not the meaning of pytest outcomes."""
    encode_record(record)
    if not isinstance(record, dict) or type(record.get("format_version")) is not int:
        raise ValueError("missing format")
    if record["format_version"] != 1 or record.get("kind") != "pytest_execution":
        raise ValueError("unsupported format")
    if not valid_identity(record.get("id")):
        raise ValueError("invalid identity")
    for key in ("producer", "request", "source", "context", "process", "execution", "artifacts"):
        if not isinstance(record.get(key), dict):
            raise ValueError(f"missing {key} object")
    required = {
        "producer": {"name", "version", "protocol_version"},
        "request": {
            "python",
            "cwd",
            "selection",
            "args",
            "packages",
            "source_checkout",
            "timeout_seconds",
            "terminate_grace_seconds",
            "retention",
            "supplied_metadata",
        },
        "source": {"checkout", "revision", "dirty", "acquisition_issues"},
        "context": {
            "companion",
            "target_start",
            "target_end",
            "environment",
            "supplied_facts",
            "status",
        },
        "process": {
            "status",
            "returncode",
            "signal",
            "launch_error",
            "elapsed_seconds",
            "termination",
            "cleanup",
        },
    }
    for group, keys in required.items():
        if not keys <= record[group].keys():
            raise ValueError(f"missing {group} facts")
    if record["producer"]["protocol_version"] != 1:
        raise ValueError("unsupported adapter protocol")
    for group, key in (
        ("request", "python"),
        ("request", "cwd"),
        ("producer", "name"),
        ("producer", "version"),
    ):
        if type(record[group][key]) is not str:
            raise ValueError(f"invalid {group}.{key}")
    elapsed = record["process"]["elapsed_seconds"]
    if type(elapsed) not in (float, int) or elapsed < 0:
        raise ValueError("invalid process elapsed time")
    environment = record["context"]["environment"]
    if not isinstance(environment, dict) or not isinstance(environment.get("coordinates"), list):
        raise ValueError("invalid environment encoding")
    for coordinate in environment["coordinates"]:
        if (
            not isinstance(coordinate, dict)
            or not {"kind", "name", "version", "original_label"} <= coordinate.keys()
        ):
            raise ValueError("invalid environment coordinate")
    if set(record["artifacts"]) != {"stdout", "stderr", "events"}:
        raise ValueError("unknown or missing artifact role")
    for key in ("reports", "issues"):
        if not isinstance(record.get(key), list):
            raise ValueError(f"missing {key} array")
    for problem in record["issues"]:
        if not isinstance(problem, dict) or any(
            type(problem.get(key)) is not str for key in ("code", "stage")
        ):
            raise ValueError("invalid retained issue")
    for key in ("captured_at", "ended_at"):
        parse_time(record.get(key))
    for group, key, allowed in (
        ("process", "status", PROCESS_STATUSES),
        ("execution", "coverage", COVERAGE_STATUSES),
        ("context", "status", CONTEXT_STATUSES),
    ):
        if type(record[group].get(key)) is not str or record[group][key] not in allowed:
            raise ValueError(f"invalid {group}.{key}")
    request = record["request"]
    if (
        not isinstance(request.get("retention"), dict)
        or type(request["retention"].get("raw")) is not bool
    ):
        raise ValueError("missing raw retention choice")
    for key in ("selection", "args", "packages"):
        if not isinstance(request.get(key), list) or any(type(x) is not str for x in request[key]):
            raise ValueError(f"invalid request {key}")
    execution = record["execution"]
    if type(execution.get("collection_completed")) is not bool:
        raise ValueError("missing collection completion")
    for key in (
        "candidates",
        "selected",
        "deselected",
        "attempted",
        "finished",
        "unexecuted",
        "cases",
    ):
        if not isinstance(execution.get(key), list):
            raise ValueError(f"missing execution {key}")
        if key != "cases" and any(type(x) is not str for x in execution[key]):
            raise ValueError(f"invalid execution {key}")
    for case in execution["cases"]:
        if not isinstance(case, dict) or not isinstance(case.get("nodeid"), str):
            raise ValueError("invalid case identity")
        if type(case.get("occurrence")) is not int or case["occurrence"] < 0:
            raise ValueError("invalid occurrence")
        if type(case.get("terminal_item")) is not bool or case.get("call_status") not in {
            "reported",
            "not_executed",
            "unfinished",
            "unknown",
        }:
            raise ValueError("invalid case completion")
        for key in ("attempted_phases", "reported_phases"):
            if not isinstance(case.get(key), list) or any(
                x not in {"setup", "call", "teardown"} for x in case[key]
            ):
                raise ValueError("invalid case phases")
    for report in record["reports"]:
        if not isinstance(report, dict) or not isinstance(report.get("nodeid"), str):
            raise ValueError("invalid report identity")
        if report.get("phase") not in {"collection", "setup", "call", "teardown"}:
            raise ValueError("invalid report phase")
        if report.get("outcome") not in {"passed", "failed", "skipped"}:
            raise ValueError("invalid native outcome")
        if type(report.get("wasxfail_present")) is not bool:
            raise ValueError("missing xfail metadata availability")
        for key in ("occurrence", "report_position"):
            if type(report.get(key)) is not int or report[key] < 0:
                raise ValueError(f"invalid report {key}")
        if "duration_seconds" not in report or (
            report["duration_seconds"] is not None
            and (
                type(report["duration_seconds"]) not in (float, int)
                or report["duration_seconds"] < 0
            )
        ):
            raise ValueError("invalid report duration")
        for key in ("diagnostic", "sections"):
            value = report.get(key)
            if not isinstance(value, dict) or value.get("availability") not in (
                "retained",
                "omitted",
                "unavailable",
            ):
                raise ValueError(f"missing report {key} availability")
            if value["availability"] == "retained":
                if key == "diagnostic" and type(value.get("text")) is not str:
                    raise ValueError("missing retained diagnostic text")
                if key == "sections":
                    items = value.get("items")
                    if not isinstance(items, list) or any(
                        not isinstance(item, dict)
                        or type(item.get("name")) is not str
                        or type(item.get("text")) is not str
                        for item in items
                    ):
                        raise ValueError("invalid retained sections")
    for role in ("stdout", "stderr", "events"):
        artifact = record["artifacts"].get(role)
        if not isinstance(artifact, dict) or artifact.get("availability") not in {
            "retained",
            "omitted",
            "unavailable",
        }:
            raise ValueError("invalid artifact availability")
        if artifact.get("content") not in {"original", "redacted"}:
            raise ValueError("invalid artifact content")
        if artifact["availability"] == "retained":
            if not isinstance(artifact.get("path"), str):
                raise ValueError("missing artifact path")
            if type(artifact.get("size")) is not int or artifact["size"] < 0:
                raise ValueError("invalid artifact size")
            if not isinstance(artifact.get("sha256"), str) or not re.fullmatch(
                r"[0-9a-f]{64}", artifact["sha256"]
            ):
                raise ValueError("invalid artifact digest")


_CREDENTIAL_KEYS = frozenset(
    {"password", "token", "accesstoken", "apikey", "authorization", "cookie", "secret"}
)


def _credential(key: str) -> bool:
    return key.lower().replace("-", "").replace("_", "") in _CREDENTIAL_KEYS


def sanitize_request(request: PytestRequest) -> tuple[Record, list[Record]]:
    """Sanitize a retained copy, never mutate executable inputs."""
    notices = []

    def redact_url(value: str, field_path: str) -> str:
        prefix, url = "", value
        if value.startswith("-") and "=" in value:
            prefix, url = value.split("=", 1)
            prefix += "="
        if not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", url):
            return value
        try:
            parts = urlsplit(url)
            query = parse_qsl(parts.query, keep_blank_values=True)
            if "@" not in parts.netloc and not any(_credential(key) for key, _ in query):
                return value
            notices.append(issue("redacted", "request", field=field_path))
            return prefix + urlunsplit(
                (
                    parts.scheme,
                    parts.netloc.rsplit("@", 1)[-1],
                    parts.path,
                    urlencode(
                        [(key, "[redacted]" if _credential(key) else val) for key, val in query]
                    ),
                    parts.fragment,
                )
            )
        except ValueError:
            notices.append(issue("redacted_invalid_url", "request", field=field_path))
            return prefix + "[redacted URL]"

    def sanitize(value: Any, field_path: str) -> Any:
        if isinstance(value, dict):
            result = {}
            for key, val in value.items():
                path = f"{field_path}.{key}"
                if _credential(key):
                    result[key] = "[redacted]"
                    notices.append(issue("redacted", "request", field=path))
                else:
                    result[key] = sanitize(val, path)
            return result
        if isinstance(value, (list, tuple)):
            return [sanitize(val, f"{field_path}[{index}]") for index, val in enumerate(value)]
        return redact_url(value, field_path) if isinstance(value, str) else value

    retained = asdict(request)
    for key in ("python", "cwd", "source_checkout"):
        if retained[key] is not None:
            retained[key] = str(retained[key])
    return sanitize(retained, "request"), notices


def validate_request(request: PytestRequest) -> None:
    if not isinstance(request, PytestRequest):
        raise ValueError("expected PytestRequest")
    if not isinstance(request.python, Path) or not isinstance(request.cwd, Path):
        raise ValueError("python and cwd must be paths")
    if request.source_checkout is not None and not isinstance(request.source_checkout, Path):
        raise ValueError("source_checkout must be a path")
    for name in ("selection", "args", "packages"):
        values = getattr(request, name)
        if not isinstance(values, tuple) or any(type(x) is not str for x in values):
            raise ValueError(f"{name} must be a tuple of strings")
    if not request.selection or any(not x.strip() for x in request.selection):
        raise ValueError("explicit nonempty selection required")
    if not isinstance(request.retention, Retention) or type(request.retention.raw) is not bool:
        raise ValueError("explicit raw retention boolean required")
    for name in ("timeout_seconds", "terminate_grace_seconds"):
        value = getattr(request, name)
        if value is None and name == "timeout_seconds":
            continue
        if type(value) not in (float, int) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be positive and finite")
    if not isinstance(request.supplied_metadata, dict):
        raise ValueError("supplied_metadata must be a JSON object")
    encode_record(request.supplied_metadata)
