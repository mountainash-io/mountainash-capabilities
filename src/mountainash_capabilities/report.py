"""Pure factual exports for retained observations; never execute or reconstruct."""

from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any

from .records import QueryResult, ReadResult, Record, RunResult


def _issues(result: ReadResult | RunResult) -> tuple[Record, ...]:
    return result.issues if isinstance(result, ReadResult) else result.persistence_issues


def _path(value: Path | None) -> str | None:
    return None if value is None else str(value)


def _data(result: ReadResult | RunResult | QueryResult) -> dict[str, Any]:
    if isinstance(result, ReadResult):
        return {"record": result.record, "read_issues": list(result.issues)}
    if isinstance(result, RunResult):
        return {
            "record": result.observation,
            "record_dir": _path(result.record_dir),
            "persistence_issues": list(result.persistence_issues),
        }
    if isinstance(result, QueryResult):
        return {
            "query": {
                "witness": result.query.witness,
                "captured_from": result.query.captured_from,
                "captured_before": result.query.captured_before,
                "process_status": result.query.process_status,
                "coverage_status": result.query.coverage_status,
                "context_status": result.query.context_status,
            },
            "destination": str(result.destination),
            "enumerated_at": result.enumerated_at,
            "complete": result.complete,
            "records": [
                {
                    "record": item.record,
                    "read_issues": list(item.issues),
                    "witness_roles": _roles(item.record, result.query.witness),
                }
                for item in result.records
            ],
            "query_issues": list(result.issues),
        }
    raise TypeError("result must be ReadResult, RunResult, or QueryResult")


def _roles(record: Record | None, witness: str | None) -> list[str]:
    if record is None or witness is None:
        return []
    execution = record["execution"]
    roles = [role for role in ("selected", "deselected", "attempted") if witness in execution[role]]
    if any(report["nodeid"] == witness for report in record["reports"]):
        roles.append("reported")
    return roles


def export_json(result: ReadResult | RunResult | QueryResult) -> str:
    """Export all stored facts and current read, persistence and query issues."""
    return json.dumps(_data(result), ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False)


def _text(value: Any) -> str:
    text = str(value)
    text = html.escape(text, quote=True)
    for char in "\\`*_{}[]()#+-.!|":
        text = text.replace(char, "\\" + char)
    return text


def _code(value: Any) -> str:
    text = str(value).replace("\n", " ").replace("\r", " ")
    fence = "`" * max(
        1, max((len(match.group()) for match in re.finditer(r"`+", text)), default=0) + 1
    )
    return f"{fence} {text} {fence}"


def _diagnostic(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return []
    availability = value.get("availability", "unknown")
    lines = [f"  Diagnostic: {_text(availability)}"]
    if availability == "retained" and "text" in value:
        raw = str(value["text"])
        fence = "`" * max(
            3, max((len(match.group()) for match in re.finditer(r"`+", raw)), default=0) + 1
        )
        lines.extend(("", f"{fence}text", raw, fence))
    elif value.get("reason"):
        lines.append(f"  Reason: {_text(value['reason'])}")
    return lines


def _record_markdown(
    record: Record, issues: tuple[Record, ...], roles: list[str] | None = None
) -> list[str]:
    lines = [
        f"## Observation {_text(record.get('id', 'unknown'))}",
        "",
        f"- Captured: {_text(record.get('captured_at', 'unknown'))}",
        f"- Request: {_text(json.dumps(record.get('request', {}), ensure_ascii=True, sort_keys=True))}",
        f"- Process status: {_text(record.get('process', {}).get('status', 'unknown'))}",
        f"- Execution coverage: {_text(record.get('execution', {}).get('coverage', 'unknown'))}",
    ]
    process = record.get("process", {})
    lines.append(
        f"- Process facts: {_text(json.dumps(process, ensure_ascii=True, sort_keys=True))}"
    )
    execution = record.get("execution", {})
    lines.append(
        f"- Collection completed: {_text(execution.get('collection_completed', 'unknown'))}"
    )
    lines.append(
        f"- Candidate scope: {_text(json.dumps(execution.get('candidates', []), ensure_ascii=True))}"
    )
    for key in ("selected", "deselected", "attempted", "finished", "unexecuted"):
        lines.append(
            f"- {key.title()} items: {_text(json.dumps(execution.get(key, []), ensure_ascii=True))}"
        )
    for case in execution.get("cases", []):
        lines.append(f"- Case: {_text(json.dumps(case, ensure_ascii=True, sort_keys=True))}")
    if roles:
        lines.append(f"- Witness roles: {_text(', '.join(roles))}")
    lines.extend(("", "### Reports"))
    if record.get("reports"):
        for report in record["reports"]:
            nodeid = _code(report.get("nodeid", "unknown"))
            phase = _text(report.get("phase", "unknown"))
            lines.extend(
                (
                    f"- {phase}: {nodeid} — {_text(report.get('outcome', 'unknown'))}; "
                    f"xfail metadata {('present' if report.get('wasxfail_present') else 'unavailable')}",
                )
            )
            if "wasxfail" in report:
                lines.append(f"  Xfail: {_text(report['wasxfail'])}")
            lines.extend(_diagnostic(report.get("diagnostic")))
            sections = report.get("sections", {})
            lines.append(f"  Sections: {_text(sections.get('availability', 'unknown'))}")
            if sections.get("availability") == "retained":
                for section in sections.get("items", []):
                    lines.append(f"  Section: {_text(section.get('name', 'unknown'))}")
                    lines.extend(
                        _diagnostic({"availability": "retained", "text": section.get("text", "")})
                    )
    else:
        lines.append("- No report events were recorded.")
    lines.extend(("", "### Context and source"))
    for name, value in (
        ("Context", record.get("context", {})),
        ("Source", record.get("source", {})),
    ):
        lines.append(f"- {name}: {_text(json.dumps(value, ensure_ascii=True, sort_keys=True))}")
    lines.extend(("", "### Artifacts"))
    issues_by_role = {(item.get("role"), item.get("code")) for item in issues}
    for role, artifact in record.get("artifacts", {}).items():
        state = artifact.get("availability", "unknown")
        if any(
            (role, code) in issues_by_role
            for code in ("artifact_unavailable", "artifact_integrity", "invalid_artifact_path")
        ):
            state = "unavailable_or_corrupt_on_read"
        lines.append(f"- {_text(role)}: {_text(state)}")
    lines.extend(("", "### Issues"))
    all_issues = list(record.get("issues", [])) + list(issues)
    if all_issues:
        for item in all_issues:
            lines.append(
                f"- Issue {_code(item.get('code', 'unknown'))}: "
                f"{_text(json.dumps(item, ensure_ascii=True, sort_keys=True))}"
            )
    else:
        lines.append("- None recorded.")
    return lines


def render_markdown(result: ReadResult | RunResult | QueryResult) -> str:
    """Render known facts and uncertainty as inert, escaped Markdown text."""
    data = _data(result)
    lines = ["# Retained observation facts", ""]
    if isinstance(result, QueryResult):
        lines.extend(
            (
                f"- Query destination: {_text(result.destination)}",
                f"- Enumerated at: {_text(result.enumerated_at)}",
                f"- Query complete: {_text(result.complete)}",
                f"- Filters: {_text(json.dumps(data['query'], ensure_ascii=True, sort_keys=True))}",
                "",
            )
        )
        if not result.records:
            lines.append("No matching readable observations.")
        for item in result.records:
            lines.extend(
                _record_markdown(
                    item.record, item.issues, _roles(item.record, result.query.witness)
                )
            )
            lines.append("")
        lines.append("## Query issues")
        if result.issues:
            lines.extend(
                f"- Issue {_code(issue.get('code', 'unknown'))}: "
                f"{_text(json.dumps(issue, ensure_ascii=True, sort_keys=True))}"
                for issue in result.issues
            )
        else:
            lines.append("- None recorded.")
    else:
        record = result.record if isinstance(result, ReadResult) else result.observation
        current_issues = _issues(result)
        if record is None:
            lines.append("No readable observation record.")
            if current_issues:
                lines.extend(
                    f"- Issue {_code(issue.get('code', 'unknown'))}: "
                    f"{_text(json.dumps(issue, ensure_ascii=True, sort_keys=True))}"
                    for issue in current_issues
                )
        else:
            lines.extend(_record_markdown(record, current_issues))
        if isinstance(result, RunResult):
            lines.append(f"\n- Record directory: {_text(result.record_dir)}")
    return "\n".join(lines) + "\n"
