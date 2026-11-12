"""`jts-ops adopt`: render the Copier template onto an existing repo and open a PR."""

import re
from collections.abc import Callable, Mapping
from pathlib import Path

from jts_ops.gh import Gh
from jts_ops.inventory import InventoryEntry
from jts_ops.migrate import LICENSE_FILES
from jts_ops.naming import check_name
from jts_ops.new import (
    DEFAULT_TEMPLATE,
    LICENSES,
    STACKS,
    Cmd,
    copier_command,
    is_local_template,
    rewrite_src_path,
)
from jts_ops.standard import Standard

ISSUE_TITLE = "Adopt the engineering standard"
COMMIT_TITLE = "chore: adopt the engineering standard (#{n})"
BRANCH = "chore/{n}-adopt-standard"
SCRATCH_BRANCH = "scratch/adopt-standard"
ISSUE_LABELS = ("type: chore", "status: in-progress")
CI_WORKFLOW = ".github/workflows/ci.yml"
ADOPTABLE_PROFILE = "active"
AI_NOTE = "AI assistance: Claude Code, rendered by jts-ops adopt"
_ISSUE_NUMBER = re.compile(r"/issues/(\d+)")


class AdoptError(Exception):
    """The adopt flow stopped; the message says why."""


def _pr_body(number: str, template: str, kept: list[str]) -> str:
    kept_lines = "".join(f"- {text}\n" for text in kept)
    return (
        "## Summary\n\n"
        f"Renders the engineering standard from `{template}` onto this repo with the project "
        "skeleton switched off, so no source, manifest or test file is touched.\n\n"
        f"Left alone:\n\n{kept_lines}\n"
        f"## Linked issue\n\nCloses #{number}\n\n"
        "## Type\n\n"
        "- [ ] feat\n- [ ] fix\n- [ ] docs\n- [ ] refactor\n- [ ] test\n- [ ] ci\n"
        "- [x] chore\n- [ ] perf\n- [ ] build\n\n"
        "## How tested\n\nRendered with `copier copy`; the `ci` check on this PR.\n\n"
        "## Checklist\n\n"
        "- [ ] Tests added or updated, and passing\n"
        "- [ ] Docs updated where behavior changed\n"
        "- [ ] No secrets, tokens or private data in the diff\n"
        "- [x] PR title follows Conventional Commits\n\n"
        "## AI assistance\n\n"
        f"{AI_NOTE}\n\n"
        "- **Tool and model:** Claude Code, rendered by jts-ops adopt\n"
        "- **What it did:** ran `copier copy` onto the existing repo, then committed and "
        "opened this PR\n"
        "- **What a human verified:** reviews the diff before merging\n"
    )


def adopt_repo(
    name: str,
    *,
    stack: str,
    std: Standard,
    inv: Mapping[str, InventoryEntry],
    gh: Gh,
    cmd: Cmd,
    projects_dir: Path,
    template: str = DEFAULT_TEMPLATE,
    vcs_ref: str | None = None,
    execute: bool = False,
    render_only: bool = False,
    on_step: Callable[[str], None] | None = None,
) -> list[str]:
    """Adopt the standard on the clone `projects_dir/name`; return the steps (planned or done).

    `name` is the repo's current name, as keyed in the inventory. Without `execute` or
    `render_only` nothing is run (only the filesystem is read). `render_only` runs the local
    steps only, without needing `execute`: precheck, a scratch branch and copier, leaving the
    changes uncommitted. No issue, no commit, no push, no GitHub call.
    """
    entry = inv.get(name)
    if entry is None:
        raise AdoptError(f"{name} is not in the inventory")
    if entry.profile != ADOPTABLE_PROFILE:
        raise AdoptError(
            f"{name} has profile {entry.profile!r}; only {ADOPTABLE_PROFILE!r} repos are adopted"
        )
    problems = check_name(entry.target, entry.kind, std)
    if stack not in STACKS:
        problems.append(f"unknown stack {stack!r}, expected one of {', '.join(STACKS)}")
    if entry.license != "company" and entry.license not in LICENSES:
        problems.append(f"unknown license {entry.license!r}")
    if problems:
        raise AdoptError("; ".join(problems))
    clone = projects_dir / name
    if not clone.is_dir():
        raise AdoptError(f"no clone at {clone}")

    kept: list[str] = []
    excludes = ["README.md"]
    kept.append("README.md kept as it is")
    license_files = sorted(f for f in LICENSE_FILES if (clone / f).is_file())
    if license_files or entry.license == "company":
        excludes.append("LICENSE")
        why = license_files[0] if license_files else "company license"
        kept.append(f"license kept ({why}); no LICENSE rendered")
    if (clone / CI_WORKFLOW).is_file():
        excludes.append(CI_WORKFLOW)
        kept.append(f"existing {CI_WORKFLOW} kept, not replaced by the standard workflow")

    # The template has no `company` choice; that repo's LICENSE is excluded above anyway.
    license_answer = "proprietary" if entry.license == "company" else entry.license
    answers = {
        "repo_name": entry.target,
        "description": entry.description,
        "kind": entry.kind,
        "stack": stack,
        "visibility": entry.visibility,
        "license": license_answer,
        "status": entry.status,
        "skeleton": "false",
    }
    copier = [*copier_command(), "copy", "--defaults", "--overwrite"]
    if vcs_ref:
        copier += ["--vcs-ref", vcs_ref]
    for key, value in answers.items():
        copier += ["--data", f"{key}={value}"]
    for pattern in excludes:
        copier += ["--exclude", pattern]
    copier += [template, "."]

    state: dict[str, str] = {"n": "<n>", "branch": "", "default": "main"}
    repo = f"{gh.owner}/{name}"
    steps: list[str] = []

    def sh(argv: list[str], text: str) -> str:
        try:
            result = cmd(argv, clone)
        except OSError as err:
            raise AdoptError(f"step failed: {text}: cannot run {argv[0]}: {err.strerror}") from err
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip() or "no output"
            raise AdoptError(f"step failed: {text}: {detail} (the clone is left as it is)")
        return result.stdout.strip()

    def precheck() -> None:
        head = sh(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], "default branch")
        default = head.removeprefix("origin/")
        state["default"] = default
        reasons: list[str] = []
        if sh(["git", "status", "--porcelain"], "git status"):
            reasons.append("uncommitted changes")
        branch = sh(["git", "branch", "--show-current"], "git branch")
        if branch != default:
            reasons.append(f"on {branch or 'a detached HEAD'}, not {default}")
        sh(["git", "fetch", "origin"], "git fetch")
        counts = sh(
            ["git", "rev-list", "--left-right", "--count", f"HEAD...origin/{default}"],
            "git rev-list",
        ).split()
        ahead, behind = int(counts[0]), int(counts[1])
        if ahead:
            reasons.append(f"{ahead} unpushed commits")
        if behind:
            reasons.append(f"behind origin/{default} by {behind}")
        if reasons:
            raise AdoptError(f"{name} is not ready to adopt: {', '.join(reasons)}")

    def create_issue() -> None:
        argv = [
            "gh", "issue", "create", "--repo", repo, "--title", ISSUE_TITLE,
            "--body", f"Render the engineering standard from `{template}` onto this repo.",
        ]  # fmt: skip
        for label in ISSUE_LABELS:
            argv += ["--label", label]
        match = _ISSUE_NUMBER.search(sh(argv, "gh issue create"))
        if match is None:
            raise AdoptError("cannot read the issue number from `gh issue create` output")
        state["n"] = match.group(1)
        state["branch"] = BRANCH.format(n=state["n"])

    def switch() -> None:
        sh(["git", "switch", "-c", state["branch"]], "git switch")

    def render() -> None:
        sh(copier, "copier copy")
        if is_local_template(template):
            rewrite_src_path(clone)

    def commit() -> None:
        sh(["git", "add", "-A"], "git add")
        sh(["git", "commit", "-m", COMMIT_TITLE.format(n=state["n"])], "git commit")

    def push() -> None:
        sh(["git", "push", "-u", "origin", state["branch"]], "git push")

    def pull_request() -> None:
        title = COMMIT_TITLE.format(n=state["n"])
        body = _pr_body(state["n"], template, kept)
        url = sh(
            [
                "gh", "pr", "create", "--repo", repo, "--base", state["default"],
                "--head", state["branch"], "--title", title, "--body", body,
            ],
            "gh pr create",
        )  # fmt: skip
        state["url"] = url.splitlines()[-1] if url else ""

    def scratch_switch() -> None:
        state["branch"] = SCRATCH_BRANCH
        switch()

    branch_text = BRANCH.format(n="<n>")
    plan: list[tuple[str, Callable[[], None]]] = [
        ("precheck: clean tree, default branch, up to date with origin", precheck)
    ]
    if render_only:
        plan.append((f"git switch -c {SCRATCH_BRANCH}", scratch_switch))
    else:
        plan += [
            (f"gh issue create: {ISSUE_TITLE}", create_issue),
            (f"git switch -c {branch_text}", switch),
        ]
    plan.append((" ".join(copier), render))
    if not render_only:
        plan += [
            (f"git add -A && git commit -m '{COMMIT_TITLE.format(n='<n>')}'", commit),
            (f"git push -u origin {branch_text}", push),
            (f"gh pr create ({AI_NOTE})", pull_request),
        ]

    def emit(text: str) -> None:
        steps.append(text)
        if on_step:
            on_step(text)

    for text, action in plan:
        emit(text)
        if execute or render_only:
            action()
    for text in kept:
        emit(f"kept: {text}")
    if execute and not render_only:
        emit(state["url"])
    return steps
