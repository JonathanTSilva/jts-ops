"""`jts-ops sync`: keep the local clones in `~/Projects` in line with GitHub.

`plan_sync` is pure: it compares the repos GitHub lists with the clones found on disk and
returns the actions needed. `execute_sync` runs them through the `Cmd` runner. The rules:

- A dirty clone (uncommitted or untracked changes, detached HEAD, no upstream, unpushed
  commits, a merge or rebase in progress) is never modified: it only gets `git fetch --prune`.
- `git pull` is never used. Updating is `git fetch --prune` then `git merge --ff-only @{u}`,
  and only on the default branch.
- Nothing is ever deleted. Clones with no repo on GitHub are reported as orphaned, and
  folders that are not clones of the owner's repos as unmanaged.
"""

import re
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from jts_ops.inventory import InventoryEntry, by_target
from jts_ops.migrate import MigrationError, run_git, unclean_reasons
from jts_ops.new import Cmd

Kind = Literal[
    "clone",
    "clone-wiki",
    "fetch",
    "fast-forward",
    "move",
    "set-url",
    "skip",
    "orphaned",
    "unmanaged",
]

ARCHIVE_DIR = "_archive"
DIRTY_PREFIX = "dirty:"
SKIPPED = "skipped: earlier step failed"
# Git state files whose presence means a merge, rebase or similar is under way.
IN_PROGRESS_FILES = (
    "rebase-merge",
    "rebase-apply",
    "MERGE_HEAD",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
)

_ORIGIN = re.compile(
    r"^(?:git@github\.com:|ssh://git@github\.com/|https?://(?:[^/@]+@)?github\.com/)"
    r"(?P<owner>[^/]+)/(?P<repo>.+?)(?:\.git)?/?$"
)


@dataclass(frozen=True)
class SyncAction:
    """One thing sync does (or reports) for one repo.

    `path` is the absolute folder the action acts on. `target` depends on the kind: the new
    folder for `move`, the URL for `clone`, `clone-wiki` and `set-url`, the branch that must
    be checked out for `fast-forward`.
    """

    repo: str
    kind: Kind
    detail: str
    path: str = ""
    target: str = ""


@dataclass(frozen=True)
class LocalClone:
    path: Path
    origin: str | None  # None: not a clone, or no origin remote
    branch: str  # "" when HEAD is detached
    default_branch: str | None  # from origin/HEAD, when known
    dirty: bool
    dirty_reasons: tuple[str, ...]
    in_progress: bool  # a merge, rebase, cherry-pick or revert is under way
    is_repo: bool = True


class SyncError(Exception):
    """An action could not be completed; the message is the reason shown to the user."""


def origin_parts(origin: str | None) -> tuple[str, str] | None:
    """(owner, repo) of a GitHub origin URL, or None for anything else."""
    match = _ORIGIN.match(origin) if origin else None
    return (match["owner"], match["repo"]) if match else None


def github_url(owner: str, name: str) -> str:
    return f"git@github.com:{owner}/{name}.git"


def _retarget(origin: str, name: str) -> str:
    """The origin URL with its repo name replaced, keeping the host and style."""
    head = origin.rstrip("/")
    cut = max(head.rfind("/"), head.rfind(":"))
    return f"{head[: cut + 1]}{name}.git"


# ------------------------------------------------------------------ inspection (git reads)


def _in_progress(path: Path, cmd: Cmd) -> list[str]:
    found: list[str] = []
    for name in IN_PROGRESS_FILES:
        result = cmd(["git", "rev-parse", "--git-path", name], path)
        if result.returncode == 0 and (path / result.stdout.strip()).exists():
            found.append(name)
    return found


def dirty_reasons(path: Path, cmd: Cmd) -> tuple[list[str], bool]:
    """Why a clone must only be fetched, and whether a merge or rebase is under way."""
    try:
        reasons = unclean_reasons(path, None, cmd)
        on_branch = cmd(["git", "symbolic-ref", "-q", "HEAD"], path).returncode == 0
        if (
            not reasons
            and on_branch
            and cmd(["git", "rev-parse", "--abbrev-ref", "@{u}"], path).returncode != 0
        ):
            reasons.append("branch has no upstream")
    except MigrationError as err:
        return [str(err)], False
    progress = _in_progress(path, cmd)
    if progress:
        reasons.append(f"merge or rebase in progress ({', '.join(progress)})")
    return reasons, bool(progress)


def inspect_clone(path: Path, cmd: Cmd) -> LocalClone:
    """Read a folder's git state (reads only). A folder that is not a clone root says so."""
    top = cmd(["git", "rev-parse", "--show-toplevel"], path)
    if top.returncode != 0 or Path(top.stdout.strip()).resolve() != path.resolve():
        return LocalClone(path, None, "", None, False, (), False, is_repo=False)
    # The raw config value: `remote get-url` would apply `insteadOf` rewrites.
    origin = cmd(["git", "config", "--get", "remote.origin.url"], path)
    branch = cmd(["git", "branch", "--show-current"], path).stdout.strip()
    head = cmd(["git", "symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD"], path)
    default = head.stdout.strip().removeprefix("origin/") if head.returncode == 0 else None
    reasons, progress = dirty_reasons(path, cmd)
    return LocalClone(
        path=path,
        origin=origin.stdout.strip() if origin.returncode == 0 else None,
        branch=branch,
        default_branch=default or None,
        dirty=bool(reasons),
        dirty_reasons=tuple(reasons),
        in_progress=progress,
    )


def scan_local(projects_dir: Path, cmd: Cmd) -> dict[str, LocalClone]:
    """Inspect every folder of the projects dir and of `_archive/`, keyed by relative path."""
    found: dict[str, LocalClone] = {}
    for folder in sorted(projects_dir.iterdir()):
        if not folder.is_dir():
            continue
        if folder.name == ARCHIVE_DIR:
            for child in sorted(folder.iterdir()):
                if child.is_dir():
                    found[f"{ARCHIVE_DIR}/{child.name}"] = inspect_clone(child, cmd)
        else:
            found[folder.name] = inspect_clone(folder, cmd)
    return found


# ------------------------------------------------------------------ planning (pure)


@dataclass(frozen=True)
class _Member:
    label: str  # "name" or "name.wiki"
    key: str  # folder, relative to the projects dir
    clone: LocalClone
    wiki: bool


def _is_ours(clone: LocalClone, owner: str) -> bool:
    parts = origin_parts(clone.origin)
    return clone.is_repo and parts is not None and parts[0].lower() == owner.lower()


def _plan_repo(
    repo: Mapping[str, object],
    local: Mapping[str, LocalClone],
    consumed: set[str],
    entry: InventoryEntry | None,
    old_names: Sequence[str],
    projects_dir: Path,
    owner: str,
) -> list[SyncAction]:
    name = str(repo["name"])
    archived = bool(repo.get("archived", False))
    has_wiki = bool(repo.get("has_wiki", False))
    default = str(repo["default_branch"]) if repo.get("default_branch") else None
    exempt = entry is not None and entry.profile == "exempt"
    prefix = f"{ARCHIVE_DIR}/" if archived else ""
    other = "" if archived else f"{ARCHIVE_DIR}/"

    def path_of(key: str) -> str:
        return str(projects_dir / key)

    actions: list[SyncAction] = []
    members: list[_Member] = []
    for suffix in ("", ".wiki"):
        for candidate in (name, *old_names):
            key = next(
                (
                    k
                    for k in (f"{prefix}{candidate}{suffix}", f"{other}{candidate}{suffix}")
                    if k in local and k not in consumed and _is_ours(local[k], owner)
                ),
                None,
            )
            if key is not None:
                consumed.add(key)
                members.append(_Member(f"{name}{suffix}", key, local[key], suffix == ".wiki"))
                break

    # Clones are matched by folder name, so a different origin repo means something is off.
    checked: list[_Member] = []
    for member in members:
        parts = origin_parts(member.clone.origin)
        suffix = ".wiki" if member.wiki else ""
        allowed = {f"{c}{suffix}" for c in (name, *old_names)}
        if parts is not None and parts[1] not in allowed:
            actions.append(
                SyncAction(
                    member.label,
                    "skip",
                    f"origin is {parts[1]}, expected {name}{suffix}; left alone",
                )
            )
        else:
            checked.append(member)

    wanted = {m.label: f"{prefix}{m.label}" for m in checked}
    movers = [m for m in checked if m.key != wanted[m.label]]
    blocked = False
    for member in movers:
        if member.clone.dirty:
            blocked = True
        elif wanted[member.label] in local:
            blocked = True
            actions.append(
                SyncAction(
                    member.label,
                    "skip",
                    f"destination exists: {wanted[member.label]}; nothing moved",
                )
            )

    for member in checked:
        clone = member.clone
        if clone.dirty:
            actions.append(SyncAction(member.label, "fetch", "dirty clone", path_of(member.key)))
            reasons = ", ".join(clone.dirty_reasons)
            actions.append(
                SyncAction(member.label, "skip", f"{DIRTY_PREFIX} {reasons}; only fetched")
            )
            continue
        here = member.key
        if member in movers:
            if blocked:
                continue  # touch nothing
            there = wanted[member.label]
            actions.append(
                SyncAction(
                    member.label, "move", f"{here} -> {there}", path_of(here), path_of(there)
                )
            )
            here = there
        parts = origin_parts(clone.origin)
        if parts is not None and parts[1] != member.label and clone.origin is not None:
            actions.append(
                SyncAction(
                    member.label,
                    "set-url",
                    f"origin is {parts[1]}",
                    path_of(here),
                    _retarget(clone.origin, member.label),
                )
            )
        branch = clone.default_branch if member.wiki else default
        if branch is not None and clone.branch == branch and not exempt:
            actions.append(
                SyncAction(member.label, "fast-forward", f"on {branch}", path_of(here), branch)
            )
        else:
            why = "exempt profile" if exempt else f"not on the default branch ({branch})"
            actions.append(SyncAction(member.label, "fetch", why, path_of(here)))

    if not any(not m.wiki for m in members):
        dest = f"{prefix}{name}"
        if dest in local:
            actions.append(SyncAction(name, "skip", f"destination exists: {dest}; not cloned"))
        else:
            actions.append(SyncAction(name, "clone", dest, path_of(dest), github_url(owner, name)))
    if has_wiki and not archived and not any(m.wiki for m in members):
        dest = f"{name}.wiki"
        if dest in local:
            actions.append(SyncAction(f"{name}.wiki", "skip", f"destination exists: {dest}"))
        else:
            actions.append(
                SyncAction(
                    f"{name}.wiki",
                    "clone-wiki",
                    f"{dest} (only if the wiki has pages)",
                    path_of(dest),
                    github_url(owner, f"{name}.wiki"),
                )
            )
    return actions


def plan_sync(
    remote: Sequence[Mapping[str, object]],
    local: Mapping[str, LocalClone],
    inv: Mapping[str, InventoryEntry],
    projects_dir: Path,
    *,
    owner: str,
) -> list[SyncAction]:
    """The actions that bring `projects_dir` in line with the repos GitHub lists. Pure.

    `local` is keyed by folder relative to `projects_dir` (`name`, `name.wiki`,
    `_archive/name`). A repo renamed on GitHub is found by its old name through the
    inventory (`entry.name` -> `entry.target`).
    """
    targets = by_target(dict(inv))
    consumed: set[str] = set()
    actions: list[SyncAction] = []
    for repo in sorted(remote, key=lambda r: str(r["name"])):
        name = str(repo["name"])
        entry = inv.get(name) or targets.get(name)
        old_names = [e.name for e in inv.values() if e.target == name and e.name != name]
        actions += _plan_repo(repo, local, consumed, entry, old_names, projects_dir, owner)
    for key in sorted(set(local) - consumed):
        clone = local[key]
        if _is_ours(clone, owner):
            actions.append(
                SyncAction(key, "orphaned", "no matching repo on GitHub", str(clone.path))
            )
        else:
            why = "not a git clone" if not clone.is_repo else "origin is not one of ours"
            actions.append(SyncAction(key, "unmanaged", why, str(clone.path)))
    return actions


# ------------------------------------------------------------------ execution


def _inside(path: str, projects_dir: Path) -> Path:
    resolved = Path(path).resolve()
    if not resolved.is_relative_to(projects_dir.resolve()):
        raise SyncError(f"refusing to act outside {projects_dir}: {path}")
    return resolved


def _require_clean(path: Path, cmd: Cmd, branch: str | None = None) -> None:
    """Re-check, right before a change, that the clone is still safe to modify."""
    reasons, _ = dirty_reasons(path, cmd)
    if reasons:
        raise SyncError(f"{path.name} is not clean: {', '.join(reasons)}")
    if branch is not None:
        current = run_git(path, "branch", "--show-current", run=cmd)
        if current != branch:
            raise SyncError(f"{path.name} is on {current or 'a detached HEAD'}, not {branch}")


def _make_parent(destination: Path) -> None:
    if destination.exists():
        raise SyncError(f"destination exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)


def _run_action(action: SyncAction, cmd: Cmd, projects_dir: Path) -> None:
    match action.kind:
        case "clone" | "clone-wiki":
            destination = _inside(action.path, projects_dir)
            if action.kind == "clone-wiki":
                # A wiki that was never edited has no repository: not an error, just no clone.
                probe = cmd(["git", "ls-remote", action.target], None)
                if probe.returncode != 0 or not probe.stdout.strip():
                    return
            _make_parent(destination)
            result = cmd(["git", "clone", action.target, str(destination)], None)
            if result.returncode != 0:
                raise SyncError(f"git clone failed: {result.stderr.strip()}")
        case "fetch":
            run_git(_inside(action.path, projects_dir), "fetch", "--prune", run=cmd)
        case "fast-forward":
            path = _inside(action.path, projects_dir)
            _require_clean(path, cmd, action.target)
            run_git(path, "fetch", "--prune", run=cmd)
            run_git(path, "merge", "--ff-only", "@{u}", run=cmd)
        case "move":
            source = _inside(action.path, projects_dir)
            destination = _inside(action.target, projects_dir)
            _require_clean(source, cmd)
            _make_parent(destination)
            shutil.move(source, destination)
        case "set-url":
            path = _inside(action.path, projects_dir)
            _require_clean(path, cmd)
            run_git(path, "remote", "set-url", "origin", action.target, run=cmd)
        case "skip" | "orphaned" | "unmanaged":
            pass


def execute_sync(
    actions: Sequence[SyncAction], cmd: Cmd, projects_dir: Path
) -> list[tuple[SyncAction, str | None]]:
    """Run every action; return (action, error or None) for each.

    Repos are independent: after an action fails, only that repo's remaining actions are
    skipped. Before any change to an existing clone it is checked for cleanliness again.
    """
    results: list[tuple[SyncAction, str | None]] = []
    failed: set[str] = set()
    for action in actions:
        if action.repo in failed:
            results.append((action, SKIPPED))
            continue
        try:
            _run_action(action, cmd, projects_dir)
        except (SyncError, MigrationError, OSError) as err:
            failed.add(action.repo)
            results.append((action, (str(err).splitlines() or ["unknown error"])[0]))
            continue
        results.append((action, None))
    return results


# ------------------------------------------------------------------ reporting


def summarize(results: Sequence[tuple[SyncAction, str | None]], *, dry_run: bool) -> str:
    """One line: cloned, updated, moved, skipped-dirty (names), orphaned, unmanaged, failed.

    In a dry run the numbers are what would happen. `updated` counts fast-forward attempts,
    whether or not anything was new. Actions skipped after an earlier failure count once,
    through the failure itself.
    """

    def count(kind: Kind) -> int:
        return sum(1 for a, e in results if a.kind == kind and e is None)

    dirty = sorted(
        {a.repo for a, _ in results if a.kind == "skip" and a.detail.startswith(DIRTY_PREFIX)}
    )
    failed = sum(1 for _, e in results if e is not None and e != SKIPPED)
    names = f" ({', '.join(dirty)})" if dirty else ""
    line = (
        f"cloned {count('clone')}, updated {count('fast-forward')}, moved {count('move')}, "
        f"skipped-dirty {len(dirty)}{names}, orphaned {count('orphaned')}, "
        f"unmanaged {count('unmanaged')}, failed {failed}"
    )
    return f"dry run: {line}" if dry_run else line
