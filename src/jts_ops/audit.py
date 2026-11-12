"""Audit repo state against the standard and render JSON and Markdown reports."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from jts_ops.inventory import InventoryEntry
from jts_ops.naming import check_name
from jts_ops.standard import Standard
from jts_ops.state import RepoState

Severity = Literal["error", "warning", "info"]

ARCHIVE_BANNER_MARKER = "<!-- archive-banner:start -->"

# Checks that run for the `exempt` and `special` profiles; `active` and `archived` run the rest.
_EXEMPT_CHECKS = ("inventory.missing", "description.emoji", "topics.kind", "topics.status")
_SPECIAL_CHECKS = ("inventory.missing", "license.missing", "wiki.enabled")


@dataclass(frozen=True)
class Finding:
    repo: str
    check: str
    severity: Severity
    message: str


def audit_repo(state: RepoState, entry: InventoryEntry | None, std: Standard) -> list[Finding]:
    """Return the findings for one repo, sorted by check id (then message)."""
    found: list[Finding] = []

    def add(check: str, severity: Severity, message: str) -> None:
        found.append(Finding(state.name, check, severity, message))

    if entry is None:
        add("inventory.missing", "error", "repo is not in the inventory")
        return found

    profile = entry.profile
    kind = std.kinds.get(entry.kind)
    full = profile in ("active", "archived")

    if profile in ("exempt", "active", "archived") and kind is not None:
        if not state.description.startswith(kind.emoji):
            add("description.emoji", "warning", f"description must start with {kind.emoji}")
        if kind.topic not in state.topics:
            add("topics.kind", "warning", f"missing topic {kind.topic}")
        status_topic = f"status-{entry.status}"
        if status_topic not in state.topics:
            add("topics.status", "warning", f"missing topic {status_topic}")

    if full:
        for problem in check_name(state.name, entry.kind, std):
            add("name.convention", "error", problem)
        if state.ci_conclusion == "failure":
            add("ci.failing", "error", "latest CI run on the default branch failed")

    if profile != "exempt" and state.has_wiki:
        add("wiki.enabled", "warning", "wiki is enabled; docs live in the repo")

    missing_license = state.license_spdx is None and entry.license != "company"
    if profile != "exempt" and not state.private and missing_license:
        add("license.missing", "error", "public repo has no detected license")

    if profile == "active":
        _audit_active(state, std, add)

    if profile == "archived":
        if not state.archived:
            # Plan 2 does the archiving, so a pending archive is only worth a note.
            severity: Severity = "info" if entry.fate == "archive" else "error"
            add("archived.flag", severity, "inventory profile is archived but repo is not")
        if state.readme is None or ARCHIVE_BANNER_MARKER not in state.readme:
            add("archived.banner", "warning", "README has no archive banner")

    return sorted(found, key=lambda f: (f.check, f.message))


def _audit_active(state: RepoState, std: Standard, add: Any) -> None:
    if state.files is None:
        add("files.required", "info", "no file listing (empty or truncated tree); skipped")
    else:
        for path in std.required_files.get("active", ()):
            if path not in state.files:
                add("files.required", "error", f"missing {path}")

    problems: list[str] = []
    if not state.allow_squash_merge:
        problems.append("squash merge is off")
    if state.allow_merge_commit:
        problems.append("merge commit is on")
    if state.allow_rebase_merge:
        problems.append("rebase merge is on")
    if not state.delete_branch_on_merge:
        problems.append("delete branch on merge is off")
    if problems:
        add("settings.merge", "warning", "; ".join(problems))

    have = {label.lower() for label in state.labels}
    missing = [label.name for label in std.labels if label.name.lower() not in have]
    if missing:
        add("labels.missing", "warning", "missing labels: " + ", ".join(missing))


Result = tuple[RepoState, InventoryEntry | None, list[Finding]]


@dataclass
class Report:
    generated_at: str
    repos: list[dict[str, Any]]
    summary: dict[str, int]

    def to_json(self) -> str:
        data = {"generated_at": self.generated_at, "summary": self.summary, "repos": self.repos}
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n"

    def to_markdown(self) -> str:
        s = self.summary
        lines = [
            "# GitHub standard audit",
            "",
            f"Generated {self.generated_at}.",
            "",
            f"- Repos audited: {s['total']}",
            f"- Compliant (no errors): {s['compliant']} ({s['percent']}%)",
            f"- CI green: {s['ci_green']} of {s['ci_total']}",
            "",
            "| Repo | Profile | Errors | Warnings | CI |",
            "| --- | --- | ---: | ---: | --- |",
        ]
        for repo in self.repos:
            lines.append(
                f"| {_cell(repo['name'])} | {repo['profile'] or '-'} | {repo['errors']} "
                f"| {repo['warnings']} | {repo['ci'] or '-'} |"
            )
        flagged = [r for r in self.repos if r["findings"]]
        if flagged:
            lines += ["", "## Findings"]
        for repo in flagged:
            lines += ["", f"### {repo['name']}", ""]
            for f in repo["findings"]:
                lines.append(f"- {f['severity']} `{f['check']}`: {_cell(f['message'])}")
        return "\n".join(lines) + "\n"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _percent(compliant: int, total: int) -> int:
    """Whole-number percentage, rounding half up (2 of 3 is 67)."""
    if total == 0:
        return 0
    return (200 * compliant + total) // (2 * total)


def build_report(
    results: list[Result], public_only: bool, generated_at: str | None = None
) -> Report:
    """Build the report; `public_only` drops private repos before anything is counted."""
    rows: list[dict[str, Any]] = []
    for state, entry, findings in sorted(results, key=lambda r: r[0].name):
        if public_only and state.private:
            continue
        ordered = sorted(findings, key=lambda f: (f.check, f.message))
        errors = sum(f.severity == "error" for f in ordered)
        rows.append(
            {
                "name": state.name,
                "visibility": "private" if state.private else "public",
                "archived": state.archived,
                "kind": entry.kind if entry else None,
                "profile": entry.profile if entry else None,
                "status": entry.status if entry else None,
                "fate": entry.fate if entry else None,
                "ci": state.ci_conclusion,
                "compliant": errors == 0,
                "errors": errors,
                "warnings": sum(f.severity == "warning" for f in ordered),
                "findings": [
                    {"check": f.check, "severity": f.severity, "message": f.message}
                    for f in ordered
                ],
            }
        )
    compliant = sum(r["compliant"] for r in rows)
    ci_runs = [r["ci"] for r in rows if r["ci"] is not None]
    summary = {
        "total": len(rows),
        "compliant": compliant,
        "percent": _percent(compliant, len(rows)),
        "ci_green": sum(c == "success" for c in ci_runs),
        "ci_total": len(ci_runs),
    }
    stamp = generated_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return Report(generated_at=stamp, repos=rows, summary=summary)
