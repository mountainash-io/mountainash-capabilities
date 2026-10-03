"""Local immutable publication and independently verifiable reads."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from uuid import uuid4

from .records import (
    CONTEXT_STATUSES, COVERAGE_STATUSES, PROCESS_STATUSES, Query, QueryResult,
    ReadResult, Record, encode_record, issue, parse_time, utc_now, valid_identity, validate_record,
)

_ARTIFACTS = {"stdout": "stdout.bin", "stderr": "stderr.bin", "events": "events.jsonl"}
_CHUNK = 1024 * 1024


def reserve(destination: Path) -> Path:
    destination = Path(destination).absolute()
    destination.mkdir(parents=True, exist_ok=True)
    while True:
        entry = destination / uuid4().hex
        try:
            entry.mkdir(mode=0o700)
            break
        except FileExistsError:
            continue
    with (entry / "pending.json").open("x", encoding="utf-8") as pending:
        pending.write(encode_record({"id": entry.name, "captured_at": utc_now()}))
    return entry


def _safe_path(entry: Path, reference: str) -> Path:
    relative = Path(reference)
    if not reference or relative.is_absolute() or ".." in relative.parts:
        raise ValueError("invalid artifact path")
    current = entry
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("symlink artifact path")
    if current == entry:
        raise ValueError("artifact must be a file")
    return current


def _open_regular(path: Path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("expected a regular file")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def _strict_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _reject_constant(value):
    raise ValueError("nonfinite JSON number")


def _load(path: Path) -> Record:
    with _open_regular(path) as stream:
        return json.load(stream, object_pairs_hook=_strict_object, parse_constant=_reject_constant)


def publish(record_dir: Path, record: Record, artifacts: dict[str, Path]) -> ReadResult:
    """Publish once; a failed claim is left visible and never silently retried."""
    entry = Path(record_dir)
    if entry.is_symlink() or not entry.is_dir():
        return ReadResult(None, (issue("invalid_record_directory", "publish"),))
    if os.path.lexists(entry / "record.json"):
        return ReadResult(None, (issue("already_published", "publish"),))
    try:
        validate_record(record)
        if record["id"] != entry.name:
            return ReadResult(None, (issue("identity_mismatch", "publish"),))
        if any(role not in _ARTIFACTS for role in artifacts):
            raise ValueError("unknown artifact role")
        # Publication annotates a private JSON copy; the caller's record stays unchanged.
        retained = json.loads(encode_record(record))
    except (ValueError, TypeError, KeyError, RecursionError):
        return ReadResult(None, (issue("invalid_record", "publish"),))
    try:
        with (entry / ".publish-claim").open("xb"):
            pass
    except FileExistsError:
        return ReadResult(None, (issue("publication_in_progress", "publish"),))
    except OSError as exc:
        return ReadResult(None, (issue("publication_failed", "publish", errno=exc.errno),))

    problems = []
    raw = retained["request"]["retention"]["raw"]
    if not raw:
        for report in retained["reports"]:
            report.pop("wasxfail", None)
            report.pop("diagnostic_text", None)
            report["diagnostic"] = {"availability": "omitted"}
            report["sections"] = {"availability": "omitted"}
    for role, filename in _ARTIFACTS.items():
        if not raw and role in {"stdout", "stderr"}:
            retained["artifacts"][role] = {
                "availability": "omitted", "content": "redacted", "reason": "raw_retention_disabled"
            }
            continue
        source = artifacts.get(role)
        if source is None:
            continue
        metadata = {
            "availability": "unavailable",
            "content": "redacted" if not raw else "original",
            "media_type": "application/x-ndjson" if role == "events" else "application/octet-stream",
            "encoding": "utf-8" if role == "events" else "binary",
        }
        retained["artifacts"][role] = metadata
        try:
            with _open_regular(Path(source)) as reader:
                expected = os.fstat(reader.fileno()).st_size
                metadata["source_size_at_cutoff"] = expected
                digest = hashlib.sha256()
                remaining = expected
                with (entry / filename).open("xb") as writer:
                    while remaining:
                        block = reader.read(min(remaining, _CHUNK))
                        if not block:
                            raise OSError("source shrank during snapshot")
                        writer.write(block)
                        digest.update(block)
                        remaining -= len(block)
                    writer.flush()
                    os.fsync(writer.fileno())
                metadata.update(
                    availability="retained", path=filename, size=expected, sha256=digest.hexdigest()
                )
        except (OSError, ValueError) as exc:
            metadata["reason"] = "artifact_snapshot_failed"
            problems.append(issue("artifact_unavailable", "publish", role=role,
                                  error_type=type(exc).__name__, errno=getattr(exc, "errno", None)))

    retained["issues"].extend(problems)
    temporary = entry / f".record-{uuid4().hex}.tmp"
    try:
        validate_record(retained)
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(encode_record(retained))
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, entry / "record.json")
    except (OSError, ValueError) as exc:
        problems.append(issue("publication_failed", "publish", error_type=type(exc).__name__,
                              errno=getattr(exc, "errno", None)))
        return ReadResult(retained, tuple(problems))
    try:
        temporary.unlink()
    except OSError as exc:
        problems.append(issue("temporary_cleanup_failed", "publish", errno=exc.errno))
    return ReadResult(retained, tuple(problems))


def read_observation(destination: Path, identity: str) -> ReadResult:
    """Read facts plus current integrity issues; never repair or execute."""
    if not valid_identity(identity):
        return ReadResult(None, (issue("invalid_identity", "read"),))
    entry = Path(destination) / identity
    try:
        if entry.is_symlink():
            return ReadResult(None, (issue("invalid_record_directory", "read", id=identity),))
        record = _load(entry / "record.json")
        if isinstance(record, dict) and record.get("format_version") != 1:
            return ReadResult(None, (issue("unsupported_format", "read", id=identity),))
        validate_record(record)
        if record["id"] != identity:
            return ReadResult(None, (issue("identity_mismatch", "read", id=identity),))
    except FileNotFoundError:
        return ReadResult(None, (issue("incomplete_entry", "read", id=identity),))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return ReadResult(None, (issue("invalid_record", "read", id=identity),))
    except OSError as exc:
        return ReadResult(None, (issue("unreadable_record", "read", id=identity, errno=exc.errno),))

    problems = []
    for role, artifact in record["artifacts"].items():
        if artifact["availability"] == "unavailable":
            problems.append(issue("artifact_unavailable", "read", id=identity, role=role))
            continue
        if artifact["availability"] != "retained":
            continue
        try:
            path = _safe_path(entry, artifact["path"])
        except ValueError:
            problems.append(issue("invalid_artifact_path", "read", id=identity, role=role))
            continue
        except OSError as exc:
            problems.append(issue("artifact_unavailable", "read", id=identity, role=role,
                                  errno=exc.errno))
            continue
        try:
            with _open_regular(path) as stream:
                size = os.fstat(stream.fileno()).st_size
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if size != artifact["size"] or digest != artifact["sha256"]:
                problems.append(issue("artifact_integrity", "read", id=identity, role=role))
        except (OSError, ValueError) as exc:
            problems.append(issue("artifact_unavailable", "read", id=identity, role=role,
                                  error_type=type(exc).__name__))
    return ReadResult(record, tuple(problems))


def _validate_query(query: Query) -> tuple[object | None, object | None]:
    if not isinstance(query, Query):
        raise ValueError("query must be a Query")
    for key, value, allowed in (
        ("process_status", query.process_status, PROCESS_STATUSES),
        ("coverage_status", query.coverage_status, COVERAGE_STATUSES),
        ("context_status", query.context_status, CONTEXT_STATUSES),
    ):
        if value is not None and (type(value) is not str or value not in allowed):
            raise ValueError(f"invalid {key}")
    if query.witness is not None and type(query.witness) is not str:
        raise ValueError("invalid witness")
    lower = parse_time(query.captured_from) if query.captured_from is not None else None
    upper = parse_time(query.captured_before) if query.captured_before is not None else None
    if lower is not None and upper is not None and lower >= upper:
        raise ValueError("captured_from must precede captured_before")
    return lower, upper


def _witness_roles(record: Record, witness: str) -> tuple[str, ...]:
    execution = record["execution"]
    roles = []
    for role in ("selected", "deselected", "attempted"):
        if witness in execution[role]:
            roles.append(role)
    if any(report["nodeid"] == witness for report in record["reports"]):
        roles.append("reported")
    return tuple(roles)


def query_observations(destination: Path, query: Query) -> QueryResult:
    """Enumerate immediate records once; retain independent uncertainty."""
    lower, upper = _validate_query(query)
    root = Path(destination).absolute()
    enumerated_at = utc_now()
    records = []
    problems = []
    try:
        with os.scandir(root) as entries:
            names = [entry.name for entry in entries]
    except OSError as exc:
        names = []
        problems.append(issue("destination_unavailable", "query", errno=exc.errno))

    for identity in names:
        if not valid_identity(identity):
            problems.append(issue("invalid_entry", "query", entry=identity))
            continue
        result = read_observation(root, identity)
        if result.record is None:
            problems.extend(result.issues)
            continue
        record = result.record
        # Read/integrity checks happen before filters; every readable record's
        # current integrity issues remain visible even when it does not match.
        problems.extend(result.issues)
        try:
            captured = parse_time(record["captured_at"])
        except (ValueError, TypeError):
            problems.append(issue("invalid_record", "query", id=identity))
            continue
        if lower is not None and captured < lower:
            continue
        if upper is not None and captured >= upper:
            continue
        if query.process_status is not None and record["process"].get("status") != query.process_status:
            continue
        if query.coverage_status is not None and record["execution"].get("coverage") != query.coverage_status:
            continue
        if query.context_status is not None and record["context"].get("status") != query.context_status:
            continue
        if query.witness is not None and not _witness_roles(record, query.witness):
            continue
        records.append(result)
    records.sort(key=lambda item: (parse_time(item.record["captured_at"]), item.record["id"]))
    return QueryResult(tuple(records), tuple(problems), not problems, query, root, enumerated_at)
