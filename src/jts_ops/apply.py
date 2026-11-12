"""Plan and apply reversible GitHub metadata and settings changes.

`plan_changes` is pure: it compares one repo's observed state with the standard and returns
what would change plus the reasons something was left alone. `execute` performs the writes.
Nothing here renames, archives, changes visibility or deletes anything.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import quote

from jts_ops.gh import Gh, GhError
from jts_ops.inventory import InventoryEntry
from jts_ops.standard import Standard
from jts_ops.state import LabelInfo, RepoState

Kind = Literal[
    "settings",
    "topics",
    "description",
    "label-create",
    "label-update",
    "codeql-default-setup",
    "security",
    "ruleset",
]

CI_WORKFLOW = ".github/workflows/ci.yml"
RULESET_NAME = "main"

ACTIVE_SETTINGS: dict[str, Any] = {
    "allow_squash_merge": True,
    "allow_merge_commit": False,
    "allow_rebase_merge": False,
    "delete_branch_on_merge": True,
    "has_wiki": False,
}
# Sent together and as their own change: GitHub validates the pair, and a rejection must not
# take the flags above down with it.
SQUASH_FORMAT: dict[str, Any] = {
    "squash_merge_commit_title": "PR_TITLE",
    "squash_merge_commit_message": "COMMIT_MESSAGES",
}
NO_WIKI_SETTINGS: dict[str, Any] = {"has_wiki": False}

SECRET_SCANNING_BODY: dict[str, Any] = {
    "security_and_analysis": {
        "secret_scanning": {"status": "enabled"},
        "secret_scanning_push_protection": {"status": "enabled"},
    }
}

RULESET_BODY: dict[str, Any] = {
    "name": RULESET_NAME,
    "target": "branch",
    "enforcement": "active",
    "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
    "rules": [
        {"type": "deletion"},
        {"type": "non_fast_forward"},
        {"type": "required_linear_history"},
        {
            "type": "pull_request",
            "parameters": {
                "required_approving_review_count": 0,
                "dismiss_stale_reviews_on_push": False,
                "require_code_owner_review": False,
                "require_last_push_approval": False,
                "required_review_thread_resolution": False,
            },
        },
        {
            "type": "required_status_checks",
            "parameters": {
                "required_status_checks": [{"context": "ci"}],
                "strict_required_status_checks_policy": False,
            },
        },
    ],
}


@dataclass(frozen=True)
class Change:
    repo: str
    kind: Kind
    payload: dict[str, Any]
    reason: str


def ruleset_eligible(state: RepoState, entry: InventoryEntry | None, std: Standard) -> bool:
    """True when `plan_changes` would consider a `main` ruleset for this repo."""
    return (
        entry is not None
        and entry.profile == "active"
        and not state.archived
        and (not state.private or std.plan == "pro")
    )


def _covers(existing: Any, desired: Any) -> bool:
    """True when `existing` has everything in `desired`; extra server-added fields are fine."""
    if isinstance(desired, dict):
        return isinstance(existing, dict) and all(
            key in existing and _covers(existing[key], value)  # pyright: ignore[reportUnknownArgumentType]
            for key, value in desired.items()  # pyright: ignore[reportUnknownVariableType]
        )
    if isinstance(desired, list):
        return (
            isinstance(existing, list)
            and len(existing) == len(desired)  # pyright: ignore[reportUnknownArgumentType]
            and all(_covers(e, d) for e, d in zip(existing, desired, strict=True))  # pyright: ignore[reportUnknownArgumentType, reportUnknownVariableType]
        )
    return existing == desired


def _ruleset_current(existing: dict[str, Any]) -> bool:
    """Whether an existing `main` ruleset already matches the standard."""
    existing_rules: list[dict[str, Any]] = existing.get("rules") or []
    rules: dict[str, dict[str, Any]] = {r["type"]: r for r in existing_rules}
    wanted = {r["type"]: r for r in RULESET_BODY["rules"]}
    if set(rules) != set(wanted):
        return False
    for key in ("target", "enforcement", "conditions"):
        if not _covers(existing.get(key), RULESET_BODY[key]):
            return False
    return all(
        _covers(rules[t].get("parameters"), r["parameters"])
        for t, r in wanted.items()
        if "parameters" in r
    )


def merged_topics(current: Iterable[str], entry: InventoryEntry, std: Standard) -> set[str]:
    """Keep every topic except stale kind-/status- ones, then add the entry's two."""
    kind = std.kinds.get(entry.kind)
    topics = {t for t in current if not t.startswith(("kind-", "status-"))}
    topics.add(kind.topic if kind else f"kind-{entry.kind}")
    topics.add(f"status-{entry.status}")
    return topics


def _diff(current: dict[str, Any], wanted: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in wanted.items() if current.get(k) != v}


def _describe(values: dict[str, Any], current: dict[str, Any]) -> str:
    return ", ".join(f"{k} {current.get(k)} -> {v}" for k, v in values.items())


def _plan_labels(state: RepoState, std: Standard) -> tuple[list[Change], list[str]]:
    existing: dict[str, LabelInfo | None] = {
        info.name.lower(): info for info in state.label_details
    }
    for name in state.labels:  # names without fetched details
        existing.setdefault(name.lower(), None)
    changes: list[Change] = []
    wanted_names = {label.name.lower() for label in std.labels}
    for label in std.labels:
        key = label.name.lower()
        if key not in existing:
            changes.append(
                Change(
                    state.name,
                    "label-create",
                    {"name": label.name, "color": label.color, "description": label.description},
                    f"create {label.name}",
                )
            )
            continue
        info = existing[key]
        current_name = next(n for n in state.labels if n.lower() == key)
        payload: dict[str, Any] = {"name": current_name}
        notes: list[str] = []
        if current_name != label.name:
            payload["new_name"] = label.name
            notes.append(f"name {current_name} -> {label.name}")
        if info is not None and info.color.lower().lstrip("#") != label.color.lower():
            payload["color"] = label.color
            notes.append(f"color {info.color} -> {label.color}")
        if info is not None and info.description != label.description:
            payload["description"] = label.description
            notes.append("description")
        if notes:
            changes.append(
                Change(state.name, "label-update", payload, f"{label.name} ({', '.join(notes)})")
            )
    extras = sorted(n for n in state.labels if n.lower() not in wanted_names)
    skips = [f"extra (kept): {', '.join(extras)}"] if extras else []
    return changes, skips


def plan_changes(
    state: RepoState,
    entry: InventoryEntry | None,
    std: Standard,
    existing_rulesets: list[dict[str, Any]] | None = None,
    codeql_state: str | None = None,
) -> tuple[list[Change], list[str]]:
    """Return (changes, skip reasons) for one repo. Pure: makes no API calls.

    `codeql_state` is the repo's current default-setup state: "configured", "not-configured",
    "unavailable" (unsupported or no languages), or None when it was not read.
    """
    name = state.name
    if state.archived:
        return [], ["archived: left untouched"]
    if entry is None:
        return [], ["not in the inventory: left untouched"]

    changes: list[Change] = []
    skips: list[str] = []
    profile = entry.profile
    current = {
        "allow_squash_merge": state.allow_squash_merge,
        "allow_merge_commit": state.allow_merge_commit,
        "allow_rebase_merge": state.allow_rebase_merge,
        "delete_branch_on_merge": state.delete_branch_on_merge,
        "has_wiki": state.has_wiki,
        "squash_merge_commit_title": state.squash_merge_commit_title,
        "squash_merge_commit_message": state.squash_merge_commit_message,
        "has_discussions": state.has_discussions,
    }

    # Differences that need a rename, archive or visibility change are Plan 2's job.
    if state.name != entry.target:
        skips.append(f"rename to {entry.target}: deferred to Plan 2")
    if entry.status == "archived":
        skips.append("archive: deferred to Plan 2")
    if (entry.visibility == "private") != state.private:
        skips.append(f"visibility change to {entry.visibility}: deferred to Plan 2")

    # Settings
    wanted_settings: dict[str, Any] = {}
    if profile == "active":
        wanted_settings = dict(ACTIVE_SETTINGS)
        if entry.featured:
            wanted_settings["has_discussions"] = True
    elif profile in ("special", "archived"):
        wanted_settings = dict(NO_WIKI_SETTINGS)
    settings = _diff(current, wanted_settings)
    if settings:
        changes.append(Change(name, "settings", settings, _describe(settings, current)))
    if profile == "active":
        squash = _diff(current, SQUASH_FORMAT)
        if squash:
            # Always send the pair together.
            changes.append(
                Change(
                    name,
                    "settings",
                    dict(SQUASH_FORMAT),
                    "squash commit format: " + _describe(squash, current),
                )
            )

    # Topics: keep everything except stale kind-/status- topics, add the right two.
    wanted_topics = merged_topics(state.topics, entry, std)
    if wanted_topics != set(state.topics):
        added = sorted(wanted_topics - set(state.topics))
        removed = sorted(set(state.topics) - wanted_topics)
        parts = [f"+{t}" for t in added] + [f"-{t}" for t in removed]
        changes.append(Change(name, "topics", {"names": sorted(wanted_topics)}, " ".join(parts)))

    # Description
    if entry.description != state.description:
        changes.append(
            Change(
                name,
                "description",
                {"description": entry.description},
                f"{state.description!r} -> {entry.description!r}",
            )
        )

    # Labels
    if profile == "active":
        label_changes, label_notes = _plan_labels(state, std)
        changes += label_changes
        skips += label_notes

    # Security: idempotent ensure calls, planned on every run.
    if profile in ("active", "special"):
        features = ["vulnerability-alerts", "automated-security-fixes"]
        if not state.private:
            features += ["private-vulnerability-reporting", "secret-scanning"]
        for feature in features:
            changes.append(Change(name, "security", {"feature": feature}, f"enable {feature}"))

    # CodeQL default setup
    if profile == "active" and not state.private:
        if codeql_state == "unavailable":
            skips.append("codeql: not available")
        elif codeql_state != "configured":
            changes.append(
                Change(
                    name,
                    "codeql-default-setup",
                    {"state": "configured"},
                    "configure CodeQL default setup",
                )
            )

    # Ruleset
    if ruleset_eligible(state, entry, std):
        existing = next((r for r in existing_rulesets or [] if r.get("name") == RULESET_NAME), None)
        if existing is None:
            if state.files is not None and CI_WORKFLOW in state.files:
                changes.append(
                    Change(
                        name,
                        "ruleset",
                        {"id": None, "body": RULESET_BODY},
                        f"create ruleset {RULESET_NAME} (requires check ci)",
                    )
                )
            else:
                skips.append("ruleset: no ci.yml yet")
        elif not _ruleset_current(existing):
            changes.append(
                Change(
                    name,
                    "ruleset",
                    {"id": existing.get("id"), "body": RULESET_BODY},
                    f"update ruleset {RULESET_NAME} to the standard rules",
                )
            )
    return changes, skips


def _repo_path(gh: Gh, change: Change, suffix: str = "") -> str:
    return f"/repos/{gh.owner}/{change.repo}{suffix}"


def _apply_one(change: Change, gh: Gh) -> None:
    payload = change.payload
    match change.kind:
        case "settings" | "description":
            gh.api(_repo_path(gh, change), "PATCH", body=payload)
        case "topics":
            gh.api(_repo_path(gh, change, "/topics"), "PUT", body=payload)
        case "label-create":
            gh.api(_repo_path(gh, change, "/labels"), "POST", body=payload)
        case "label-update":
            body = {k: v for k, v in payload.items() if k not in ("name", "new_name")}
            if "new_name" in payload:
                body["new_name"] = payload["new_name"]
            gh.api(
                _repo_path(gh, change, f"/labels/{quote(payload['name'], safe='')}"),
                "PATCH",
                body=body,
            )
        case "codeql-default-setup":
            gh.api(_repo_path(gh, change, "/code-scanning/default-setup"), "PATCH", body=payload)
        case "security":
            feature: str = payload["feature"]
            if feature == "secret-scanning":
                gh.api(_repo_path(gh, change), "PATCH", body=SECRET_SCANNING_BODY)
            else:
                gh.api(_repo_path(gh, change, f"/{feature}"), "PUT")
        case "ruleset":
            if payload["id"] is None:
                gh.api(_repo_path(gh, change, "/rulesets"), "POST", body=payload["body"])
            else:
                gh.api(
                    _repo_path(gh, change, f"/rulesets/{payload['id']}"),
                    "PUT",
                    body=payload["body"],
                )


def execute(changes: list[Change], gh: Gh) -> list[tuple[Change, str]]:
    """Apply every change; a failing change is recorded and the rest still run.

    Returns the failures as (change, first line of the error).
    """
    failures: list[tuple[Change, str]] = []
    for change in changes:
        try:
            _apply_one(change, gh)
        except (GhError, ValueError, OSError) as err:
            message = err.message if isinstance(err, GhError) else str(err)
            failures.append((change, (message.splitlines() or ["unknown error"])[0]))
    return failures
