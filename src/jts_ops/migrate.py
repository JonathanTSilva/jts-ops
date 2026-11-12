"""Plan and run the irreversible archiving and renaming steps.

`plan_migration` is pure: it compares one repo's observed state with its inventory entry and
returns the steps still needed, always in the same order. `execute_migration` performs them.
The CLI only writes a plan after a dry run and only executes one whose digest matches the
plan it re-derives from live GitHub state, so nothing here runs ad hoc.
"""

import base64
import hashlib
import json
import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from jts_ops.apply import merged_topics
from jts_ops.gh import Gh, GhError
from jts_ops.inventory import InventoryEntry
from jts_ops.standard import Standard
from jts_ops.state import RepoState

Action = Literal["banner", "license", "wiki-off", "metadata", "rename", "archive", "local-move"]

ARCHIVE_FATES = frozenset({"archive", "redirect-archive"})
BANNER_START = "<!-- archive-banner:start -->"
BANNER_END = "<!-- archive-banner:end -->"
SKIPPED = "skipped: earlier step failed"


@dataclass(frozen=True)
class Step:
    repo: str  # the repo's name when the plan was made
    action: Action
    payload: dict[str, Any]


LICENSE_FILES = frozenset({"LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING"})


def _needs_license(state: RepoState, entry: InventoryEntry) -> bool:
    """A public, non-company repo with no license file and no license GitHub detected."""
    if state.private or entry.license == "company" or state.license_spdx is not None:
        return False
    # `files` is None for an empty or truncated tree; then a license file cannot be shown.
    return state.files is None or not (state.files & LICENSE_FILES)


def _local_move_step(state: RepoState, target: str, archived: bool) -> Step:
    return Step(
        state.name,
        "local-move",
        {
            "old": state.name,
            "target": target,
            "archived": archived,
            "default_branch": state.default_branch,
        },
    )


def plan_migration(
    state: RepoState,
    entry: InventoryEntry,
    banner: str,
    license_texts: Mapping[str, str],
    std: Standard,
) -> list[Step]:
    """Return the steps one repo still needs, in the fixed order. Pure: makes no API calls.

    Order: banner, license, wiki-off, metadata, rename, archive, local-move. Archived repos
    are read-only, so everything that writes to them comes before `archive`.
    """
    name = state.name
    archiving = entry.fate in ARCHIVE_FATES
    if state.archived:
        # GitHub rejects every write to an archived repo, so only the local clone can move.
        # The repo cannot be renamed either, so the clone follows the name it has now.
        move = _local_move_step(state, state.name, archiving)
        return [move] if archiving else []
    steps: list[Step] = []

    if archiving and (state.readme is None or BANNER_START not in state.readme):
        steps.append(
            Step(
                name,
                "banner",
                {"banner": banner, "title": entry.target, "description": entry.description},
            )
        )

    # Active repos get their LICENSE through the adopt PR: a direct commit to the default
    # branch would fight the ruleset. Only archive fates and special repos are written here.
    if (archiving or entry.profile == "special") and _needs_license(state, entry):
        if entry.license not in license_texts:
            raise ValueError(f"no license text for {entry.license!r}")
        steps.append(
            Step(name, "license", {"license": entry.license, "text": license_texts[entry.license]})
        )

    if state.has_wiki:
        steps.append(Step(name, "wiki-off", {}))

    if archiving:
        metadata: dict[str, Any] = {}
        if entry.description != state.description:
            metadata["description"] = entry.description
        topics = merged_topics(state.topics, entry, std)
        if topics != set(state.topics):
            metadata["topics"] = sorted(topics)
        if metadata:
            steps.append(Step(name, "metadata", metadata))

    if name != entry.target:
        steps.append(Step(name, "rename", {"target": entry.target}))

    if archiving:
        steps.append(Step(name, "archive", {}))

    if archiving or name != entry.target:
        steps.append(_local_move_step(state, entry.target, archiving))
    return steps


def plan_digest(steps: Sequence[Step]) -> str:
    """sha256 of the canonical JSON of the steps, so an approved plan cannot drift."""
    canonical = json.dumps([asdict(s) for s in steps], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def describe_step(step: Step) -> str:
    payload = step.payload
    match step.action:
        case "banner":
            return "add the archive banner to the README"
        case "license":
            return f"add LICENSE ({payload['license']})"
        case "wiki-off":
            return "turn the wiki off"
        case "metadata":
            parts = [k for k in ("description", "topics") if k in payload]
            return "set " + " and ".join(parts)
        case "rename":
            return f"rename {step.repo} -> {payload['target']}"
        case "archive":
            return "archive (read-only)"
        case "local-move":
            where = "_archive/" if payload["archived"] else ""
            return f"move the local clone to {where}{payload['target']} if clean"


# ------------------------------------------------------------------ template loaders


def load_banner(standard_dir: Path) -> str:
    """The exact banner block (markers included) from the archiving handbook page."""
    text = (standard_dir / "handbook" / "archiving.md").read_text(encoding="utf-8")
    match = re.search(f"{re.escape(BANNER_START)}.*?{re.escape(BANNER_END)}", text, re.DOTALL)
    if match is None:
        raise ValueError("archive banner markers not found in handbook/archiving.md")
    return match.group(0)


_LICENSE_BRANCH = re.compile(
    r"^\{% (?:if|elif) license == '([^']+)' %\}\n(.*?)(?=^\{% (?:elif|endif) )",
    re.DOTALL | re.MULTILINE,
)
_INCLUDE = re.compile(r"^\{% include '([^']+)' %\}\n?", re.MULTILINE)


def load_license_texts(template_dir: Path, year: int) -> dict[str, str]:
    """Render `template/LICENSE.jinja` for every license it knows, without a Jinja runtime.

    Only the constructs that file uses are supported (if/elif on `license`, includes, and
    `{{ year }}`); anything else left over raises, so a changed template cannot be misread.
    """
    source = (template_dir / "template" / "LICENSE.jinja").read_text(encoding="utf-8")
    texts: dict[str, str] = {}
    for match in _LICENSE_BRANCH.finditer(source):
        body = _INCLUDE.sub(
            lambda m: (template_dir / m.group(1)).read_text(encoding="utf-8"), match.group(2)
        )
        body = body.replace("{{ year }}", str(year))
        if "{{" in body or "{%" in body:
            raise ValueError(f"unsupported template syntax in the {match.group(1)} license")
        texts[match.group(1)] = body
    if not texts:
        raise ValueError("no licenses found in template/LICENSE.jinja")
    return texts


# ------------------------------------------------------------------ execution


class MigrationError(Exception):
    """A step could not be completed; the message is the reason shown to the user."""


def _contents(gh: Gh, repo: str, path: str, text: str, message: str, sha: str | None) -> None:
    body: dict[str, Any] = {
        "message": message,
        "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
    }
    if sha is not None:
        body["sha"] = sha
    gh.api(f"/repos/{gh.owner}/{repo}/contents/{path}", "PUT", body=body)


def _banner(step: Step, gh: Gh, repo: str) -> None:
    banner: str = step.payload["banner"]
    try:
        readme: dict[str, Any] | None = gh.api(f"/repos/{gh.owner}/{repo}/readme")
    except GhError as err:
        if err.status != 404:
            raise
        readme = None
    if readme is None:
        text = f"{banner}\n\n# {step.payload['title']}\n\n{step.payload['description']}\n"
        _contents(gh, repo, "README.md", text, "docs: add archive banner", None)
        return
    if readme.get("encoding") != "base64":
        # Large files come back without content; writing banner-only would destroy the README.
        raise GhError("README too large to edit via API")
    current = base64.b64decode(readme["content"]).decode("utf-8")
    path: str = readme.get("path", "README.md")
    _contents(gh, repo, path, f"{banner}\n\n{current}", "docs: add archive banner", readme["sha"])


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise MigrationError(f"git {args[0]} failed in {cwd.name}: {result.stderr.strip()}")
    return result.stdout.strip()


def _unclean_reasons(clone: Path, default_branch: str | None) -> list[str]:
    """Why a clone must not be moved.

    Not the root of its own clone, no `origin`, uncommitted work, a side branch (the main clone
    must be on its default branch, the wiki clone just not detached), or unpushed commits.
    """
    top = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=clone,
        capture_output=True,
        text=True,
        check=False,
    )
    if top.returncode != 0 or Path(top.stdout.strip()).resolve() != clone.resolve():
        return ["not the root of a git clone"]
    reasons: list[str] = []
    if "origin" not in _git(clone, "remote").split():
        reasons.append("no origin remote")
    if _git(clone, "status", "--porcelain"):
        reasons.append("uncommitted changes")
    branch = _git(clone, "branch", "--show-current")
    if default_branch is not None and branch != default_branch:
        reasons.append(f"on {branch or 'a detached HEAD'}, not {default_branch}")
    elif default_branch is None and not branch:
        reasons.append("detached HEAD")
    if int(_git(clone, "rev-list", "--branches", "--not", "--remotes", "--count")) > 0:
        reasons.append("unpushed commits")
    return reasons


def _local_move(payload: Mapping[str, Any], owner: str, projects_dir: Path) -> None:
    old: str = payload["old"]
    target: str = payload["target"]
    destination_root = projects_dir / "_archive" if payload["archived"] else projects_dir
    moves: list[tuple[Path, Path, str, str | None]] = []
    for suffix, branch in (("", payload["default_branch"]), (".wiki", None)):
        source = projects_dir / f"{old}{suffix}"
        if source.is_dir():
            url = f"git@github.com:{owner}/{target}{suffix}.git"
            moves.append((source, destination_root / f"{target}{suffix}", url, branch))
    # Check everything before touching anything, so a problem with either clone leaves both
    # exactly as they were.
    for source, destination, _, branch in moves:
        reasons = _unclean_reasons(source, branch)
        if reasons:
            raise MigrationError(f"{source.name} is not clean: {', '.join(reasons)}")
        if destination != source and destination.exists():
            raise MigrationError(f"destination exists: {destination}")
    for source, _, url, _ in moves:
        _git(source, "remote", "set-url", "origin", url)
    for source, destination, _, _ in moves:
        if destination != source:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(source, destination)


def _run_step(step: Step, gh: Gh, projects_dir: Path, current: str) -> None:
    payload = step.payload
    repo_path = f"/repos/{gh.owner}/{current}"
    match step.action:
        case "banner":
            _banner(step, gh, current)
        case "license":
            _contents(gh, current, "LICENSE", payload["text"], "docs: add license", None)
        case "wiki-off":
            gh.api(repo_path, "PATCH", body={"has_wiki": False})
        case "metadata":
            if "description" in payload:
                gh.api(repo_path, "PATCH", body={"description": payload["description"]})
            if "topics" in payload:
                gh.api(f"{repo_path}/topics", "PUT", body={"names": payload["topics"]})
        case "rename":
            gh.api(repo_path, "PATCH", body={"name": payload["target"]})
        case "archive":
            gh.api(repo_path, "PATCH", body={"archived": True})
        case "local-move":
            _local_move(payload, gh.owner, projects_dir)


def execute_migration(
    steps: Sequence[Step], gh: Gh, projects_dir: Path
) -> list[tuple[Step, str | None]]:
    """Run every step in order; return (step, error or None) for each.

    After a step fails, the repo's remaining steps are skipped, because later steps assume
    the earlier ones happened (archiving a repo whose rename failed would freeze the wrong
    name). Other repos still run.
    """
    results: list[tuple[Step, str | None]] = []
    failed: set[str] = set()
    current: dict[str, str] = {}  # name each repo has on GitHub right now
    for step in steps:
        if step.repo in failed:
            results.append((step, SKIPPED))
            continue
        try:
            _run_step(step, gh, projects_dir, current.get(step.repo, step.repo))
        except (GhError, MigrationError, OSError, ValueError, KeyError) as err:
            message = err.message if isinstance(err, GhError) else str(err)
            failed.add(step.repo)
            results.append((step, (message.splitlines() or ["unknown error"])[0]))
            continue
        if step.action == "rename":
            current[step.repo] = step.payload["target"]
        results.append((step, None))
    return results
