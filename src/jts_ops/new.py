"""`jts-ops new`: render the Copier template, lock, commit, create the GitHub repo, apply."""

import datetime
import os
import re
import shlex
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from jts_ops.gh import Gh, GhError
from jts_ops.inventory import InventoryEntry
from jts_ops.naming import check_name
from jts_ops.standard import Standard

DEFAULT_TEMPLATE = "gh:JonathanTSilva/template-repo"
FIRST_COMMIT = "chore: initial commit from template-repo"
COPIER_ENV = "JTS_COPIER"

# The template's choices (Plan 1). `company` is never rendered from the template.
STACKS = ("python", "node", "shell", "none")
VISIBILITIES = ("public", "private")
LICENSES = ("mit", "cc-by-4.0", "mit+cc-by-4.0", "proprietary", "agpl-3.0")
STATUSES = ("concept", "wip", "active", "inactive")

LOCK_COMMANDS: dict[str, list[str]] = {
    "python": ["uv", "lock"],
    "node": ["npx", "--yes", "pnpm@9", "install", "--lockfile-only"],
}

# Runs one external command; the second argument is its working directory.
Cmd = Callable[[Sequence[str], Path | None], "subprocess.CompletedProcess[str]"]
ApplyFn = Callable[[InventoryEntry], int]


def default_cmd(args: Sequence[str], cwd: Path | None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), cwd=cwd, capture_output=True, text=True, check=False)


class NewRepoError(Exception):
    """The new-repo flow stopped; the message says why."""


def _is_local(template: str) -> bool:
    return "://" not in template and not template.startswith(("gh:", "gl:", "git@", "git+"))


def _validate(
    name: str,
    *,
    kind: str,
    stack: str,
    visibility: str,
    license: str,
    status: str,
    description: str,
    std: Standard,
) -> list[str]:
    problems = check_name(name, kind, std)
    if license == "company":
        problems.append("license 'company' is never set by tooling; keep the company license")
    elif license not in LICENSES:
        problems.append(f"unknown license {license!r}, expected one of {', '.join(LICENSES)}")
    if stack not in STACKS:
        problems.append(f"unknown stack {stack!r}, expected one of {', '.join(STACKS)}")
    if visibility not in VISIBILITIES:
        problems.append(f"unknown visibility {visibility!r}, expected public or private")
    if status not in STATUSES:
        problems.append(f"unknown status {status!r}, expected one of {', '.join(STATUSES)}")
    if not description.strip():
        problems.append("a description is required")
    return problems


def _copier_command() -> list[str]:
    return shlex.split(os.environ.get(COPIER_ENV, "")) or ["copier"]


def _rewrite_src_path(dst: Path) -> None:
    """Point `_src_path` at the canonical template so `copier update` works later."""
    answers = dst / ".copier-answers.yml"
    text = answers.read_text(encoding="utf-8")
    new_text, count = re.subn(
        r"^_src_path:.*$", f"_src_path: {DEFAULT_TEMPLATE}", text, flags=re.MULTILINE
    )
    if count != 1:
        raise NewRepoError(f"cannot rewrite _src_path: not found in {answers}")
    answers.write_text(new_text, encoding="utf-8")


def new_repo(
    name: str,
    *,
    kind: str,
    stack: str,
    visibility: str,
    license: str,
    description: str,
    std: Standard,
    gh: Gh,
    projects_dir: Path,
    template: str = DEFAULT_TEMPLATE,
    execute: bool = False,
    status: str = "wip",
    has_release: bool = False,
    year: int | None = None,
    vcs_ref: str | None = None,
    render_only: bool = False,
    run: Cmd = default_cmd,
    apply: ApplyFn | None = None,
    on_step: Callable[[str], None] | None = None,
) -> list[str]:
    """Create a compliant repo; return the steps performed (or, without `execute`, planned).

    Everything is validated before the first external call. `render_only` stops after the
    first commit, before `gh repo create` (no GitHub write, no GitHub read). `apply` is called
    with an inventory entry for the new repo once it exists on GitHub.
    """
    problems = _validate(
        name,
        kind=kind,
        stack=stack,
        visibility=visibility,
        license=license,
        status=status,
        description=description,
        std=std,
    )
    if problems:
        raise NewRepoError("; ".join(problems))
    dst = projects_dir / name
    if dst.exists():
        raise NewRepoError(f"{dst} already exists; refusing to overwrite it")
    if execute and not render_only and apply is None:
        raise NewRepoError("internal: apply function required for a full run")
    repo = f"{gh.owner}/{name}"
    if not render_only:
        try:
            gh.api(f"/repos/{repo}")
        except GhError as err:
            if err.status != 404:
                raise NewRepoError(f"cannot check whether {repo} exists: {err.message}") from err
        else:
            raise NewRepoError(f"{repo} already exists on GitHub")

    year = year or datetime.date.today().year
    answers = {
        "repo_name": name,
        "description": description,
        "kind": kind,
        "stack": stack,
        "visibility": visibility,
        "license": license,
        "status": status,
        "has_release": "true" if has_release else "false",
        "year": str(year),
    }
    copier = [*_copier_command(), "copy", "--defaults"]
    for key, value in answers.items():
        copier += ["--data", f"{key}={value}"]
    if vcs_ref:
        copier += ["--vcs-ref", vcs_ref]
    copier += [template, str(dst)]

    lock = LOCK_COMMANDS.get(stack)
    create = [
        "gh", "repo", "create", repo, f"--{visibility}",
        "--description", description, "--source", ".", "--push",
    ]  # fmt: skip

    def sh(command: list[str], cwd: Path | None, text: str) -> Callable[[], None]:
        def action() -> None:
            result = run(command, cwd)
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip() or "no output"
                raise NewRepoError(
                    f"step failed: {text}: {detail} (files left in place at {dst}, nothing deleted)"
                )

        return action

    def rewrite() -> None:
        _rewrite_src_path(dst)

    def apply_step() -> None:
        assert apply is not None
        code = apply(entry)
        if code != 0:
            raise NewRepoError(
                f"apply exited {code}; the repo exists on GitHub, re-run "
                f"`jts-ops apply --repo {name} --execute` once it is in the inventory"
            )

    entry = InventoryEntry(
        name=name,
        target=name,
        kind=kind,
        visibility=visibility,
        fate="keep",
        profile="active",
        license=license,
        status=status,
        featured=False,
        description=description,
    )
    commit = f"git commit -m '{FIRST_COMMIT}'"
    plan: list[tuple[str, Callable[[], None]]] = [
        (f"copier copy {template} -> {dst}", sh(copier, None, "copier copy"))
    ]
    if _is_local(template):
        plan.append((f"rewrite _src_path in .copier-answers.yml to {DEFAULT_TEMPLATE}", rewrite))
    if lock:
        plan.append((" ".join(lock), sh(lock, dst, " ".join(lock))))
    plan += [
        ("git init -b main", sh(["git", "init", "-b", "main"], dst, "git init")),
        ("git add -A", sh(["git", "add", "-A"], dst, "git add")),
        (commit, sh(["git", "commit", "-m", FIRST_COMMIT], dst, "git commit")),
    ]
    if not render_only:
        create_text = f"gh repo create {repo} --{visibility} --source . --push"
        plan.append((create_text, sh(create, dst, create_text)))
        plan.append((f"apply --repo {name} --execute", apply_step))
    if visibility == "private":
        reminder = "add it to ~/.config/jts-ops/inventory.private.yml (never the public file)"
    else:
        reminder = "add it to ~/Projects/.github/inventory.yml"

    steps: list[str] = []
    for text, action in plan:
        steps.append(text)
        if on_step:
            on_step(text)
        if execute:
            action()
    steps.append(reminder)
    if on_step:
        on_step(reminder)
    return steps
