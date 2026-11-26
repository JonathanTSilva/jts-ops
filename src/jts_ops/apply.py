"""Plan and apply reversible GitHub metadata and settings changes.

`plan_changes` is pure: it compares one repo's observed state with the standard and returns
what would change plus the reasons something was left alone. `execute` performs the writes.
Nothing here renames, archives, changes visibility or deletes anything.
"""

import os
import stat
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import quote

import yaml

from jts_ops.gh import Gh, GhError, ReadOnlyError
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
    "release-secret",
]

CI_WORKFLOW = ".github/workflows/ci.yml"
RULESET_NAME = "main"

# The Actions secret a repo's ci.yml hands to the shared release workflow. Release PRs opened
# with it (rather than GITHUB_TOKEN) trigger CI, so the required `ci` check runs on them.
RELEASE_SECRET = "RELEASE_PLEASE_TOKEN"
RELEASE_SECRET_EXPR = "${{ secrets." + RELEASE_SECRET + " }}"
RELEASE_TOKEN_ENV = RELEASE_SECRET
RELEASE_TOKEN_FILE_ENV = "JTS_RELEASE_TOKEN_FILE"
RELEASE_TOKEN_FILE = "~/.config/jts-ops/release-please-token"
# Printed text is written out in full, not built from the names above: CodeQL treats any
# identifier that looks like a secret as sensitive, and these only name the secret.
RELEASE_SETUP_HINT = (
    "RELEASE_PLEASE_TOKEN missing; export RELEASE_PLEASE_TOKEN or put the token in "
    "~/.config/jts-ops/release-please-token (chmod 600), then re-run with --execute"
)
RELEASE_SETUP_REASON = "set RELEASE_PLEASE_TOKEN (ci.yml runs release-please)"

ReleaseSecret = Literal["present", "missing"]
SecretSetter = Callable[[str, str, str, str], None]  # (owner, repo, name, value)


class ReleaseTokenError(Exception):
    """The release-please token file exists but is unsafe to read."""


def default_release_token_path() -> Path:
    """`$JTS_RELEASE_TOKEN_FILE`, else `~/.config/jts-ops/release-please-token`."""
    return Path(os.environ.get(RELEASE_TOKEN_FILE_ENV) or RELEASE_TOKEN_FILE).expanduser()


def load_release_token(environ: Mapping[str, str], path: Path) -> str | None:
    """The token from `$RELEASE_PLEASE_TOKEN`, else from `path`; None when neither has one.

    The file is refused when its group or others can access it, like an SSH key.
    """
    from_env = environ.get(RELEASE_TOKEN_ENV, "").strip()
    if from_env:
        return from_env
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        return None
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ReleaseTokenError(f"{path} is readable by others; run: chmod 600 {path}")
    return path.read_text(encoding="utf-8").strip() or None


def ci_passes_release_token(ci_text: str, owner: str) -> bool:
    """True when a ci.yml job calls the owner's shared release workflow with the secret.

    Only that exact shape counts: a comment, an unrelated job or another workflow that names
    the secret does not, so `apply` never hands the token to a repo for some other use.
    """
    try:
        data: Any = yaml.safe_load(ci_text)
    except yaml.YAMLError:
        return False
    if not isinstance(data, dict):
        return False
    jobs: Any = cast("dict[str, Any]", data).get("jobs")
    if not isinstance(jobs, dict):
        return False
    workflow = f"{owner}/.github/.github/workflows/release.yml@"
    for job in cast("dict[str, Any]", jobs).values():
        if not isinstance(job, dict):
            continue
        fields = cast("dict[str, Any]", job)
        uses: Any = fields.get("uses")
        secrets: Any = fields.get("secrets")
        if (
            isinstance(uses, str)
            and uses.startswith(workflow)
            and isinstance(secrets, dict)
            and cast("dict[str, Any]", secrets).get("token") == RELEASE_SECRET_EXPR
        ):
            return True
    return False


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
    release_secret: ReleaseSecret | None = None,
    release_token: bool = False,
) -> tuple[list[Change], list[str]]:
    """Return (changes, skip reasons) for one repo. Pure: makes no API calls.

    `codeql_state` is the repo's current default-setup state: "configured", "not-configured",
    "unavailable" (unsupported or no languages), or None when it was not read.
    `release_secret` says whether the repo's ci.yml needs `RELEASE_PLEASE_TOKEN` and has it:
    "present", "missing", or None when ci.yml does not use it (or it was not read).
    `release_token` is whether a token value is available to set it with.
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

    # Release-please token: only added when missing; an existing value is never replaced.
    if profile == "active" and release_secret == "missing":
        if release_token:
            changes.append(
                Change(
                    name,
                    "release-secret",
                    {"name": RELEASE_SECRET},
                    RELEASE_SETUP_REASON,
                )
            )
        else:
            skips.append(RELEASE_SETUP_HINT)
    return changes, skips


def _repo_path(gh: Gh, change: Change, suffix: str = "") -> str:
    return f"/repos/{gh.owner}/{change.repo}{suffix}"


def _apply_one(
    change: Change, gh: Gh, set_secret: SecretSetter | None, release_token: str | None
) -> None:
    payload = change.payload
    match change.kind:
        case "release-secret":
            # Not an API call, so Gh's own dry-run guard does not see it: check it here.
            if gh.read_only:
                raise ReadOnlyError(f"refusing to set {payload['name']}: read-only mode")
            if not release_token:
                raise ValueError(f"no release-please token to set {payload['name']} with")
            if set_secret is None:
                raise ValueError("internal: no secret setter")
            set_secret(gh.owner, change.repo, payload["name"], release_token)
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


def execute(
    changes: list[Change],
    gh: Gh,
    set_secret: SecretSetter | None = None,
    release_token: str | None = None,
) -> list[tuple[Change, str]]:
    """Apply every change; a failing change is recorded and the rest still run.

    `set_secret` and `release_token` are only used by "release-secret" changes.
    Returns the failures as (change, first line of the error).
    """
    failures: list[tuple[Change, str]] = []
    for change in changes:
        try:
            _apply_one(change, gh, set_secret, release_token)
        except (GhError, ValueError, OSError) as err:
            message = err.message if isinstance(err, GhError) else str(err)
            failures.append((change, (message.splitlines() or ["unknown error"])[0]))
    return failures
