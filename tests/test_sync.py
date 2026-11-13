import errno
import json
import os
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from jts_ops.cli import main
from jts_ops.gh import Gh
from jts_ops.inventory import InventoryEntry
from jts_ops.new import default_cmd
from jts_ops.sync import (
    LocalClone,
    SyncAction,
    execute_sync,
    inspect_clone,
    plan_sync,
    scan_local,
)

FIXTURES = Path(__file__).parent / "fixtures" / "standard"
OWNER = "ExampleOwner"
READ_ONLY_GIT = {
    "rev-parse",
    "remote",
    "status",
    "branch",
    "rev-list",
    "symbolic-ref",
    "config",
    "for-each-ref",
}
RENAMED = {"old-widget": f"{OWNER}/widget"}


def url(name: str) -> str:
    return f"git@github.com:{OWNER}/{name}.git"


def remote(name: str, **overrides: Any) -> dict[str, Any]:
    base = {
        "name": name,
        "archived": False,
        "private": False,
        "default_branch": "main",
        "has_wiki": False,
    }
    return {**base, **overrides}


def local(projects: Path, key: str, origin_repo: str | None = None, **overrides: Any) -> LocalClone:
    base: dict[str, Any] = {
        "path": projects / key,
        "origin": url(origin_repo or key.removeprefix("_archive/")),
        "branch": "main",
        "default_branch": "main",
        "dirty": False,
        "dirty_reasons": (),
        "in_progress": False,
        "is_repo": True,
    }
    return LocalClone(**{**base, **overrides})


def make_entry(name: str) -> InventoryEntry:
    return InventoryEntry(
        name=name,
        target=name,
        kind="product",
        visibility="public",
        fate="keep",
        profile="active",
        license="mit",
        status="active",
        featured=False,
        description="Example",
    )


def kinds(actions: Sequence[SyncAction], repo: str | None = None) -> list[str]:
    return [a.kind for a in actions if repo is None or a.repo == repo]


# ------------------------------------------------------------------ plan_sync (pure)

PROJECTS = Path("/p")


def plan(
    remotes: list[dict[str, Any]],
    clones: dict[str, LocalClone],
    inv: dict[str, InventoryEntry],
    renamed: dict[str, str] | None = None,
) -> list[SyncAction]:
    return plan_sync(remotes, clones, inv, PROJECTS, owner=OWNER, renamed=renamed)


def test_new_repo_is_cloned(inv: dict[str, InventoryEntry]):
    [action] = plan([remote("fresh")], {}, inv)
    assert (action.kind, action.path, action.target) == ("clone", "/p/fresh", url("fresh"))


def test_new_archived_repo_is_cloned_into_archive(inv: dict[str, InventoryEntry]):
    [action] = plan([remote("fresh", archived=True)], {}, inv)
    assert (action.kind, action.path) == ("clone", "/p/_archive/fresh")


def test_clean_clone_on_default_branch_fast_forwards(inv: dict[str, InventoryEntry]):
    [action] = plan([remote("ai-helper")], {"ai-helper": local(PROJECTS, "ai-helper")}, inv)
    assert (action.kind, action.path, action.target) == ("fast-forward", "/p/ai-helper", "main")


def test_clean_clone_on_a_side_branch_only_fetches(inv: dict[str, InventoryEntry]):
    clone = local(PROJECTS, "ai-helper", branch="feature")
    assert kinds(plan([remote("ai-helper")], {"ai-helper": clone}, inv)) == ["fetch"]


def test_repo_missing_from_the_inventory_fails_closed(inv: dict[str, InventoryEntry]):
    # A jts-brain-like repo with no inventory entry must never be merged.
    clones = {"jts-brain-like": local(PROJECTS, "jts-brain-like")}
    actions = plan([remote("jts-brain-like")], clones, {})
    assert kinds(actions) == ["fetch", "skip"]
    assert "not in inventory" in actions[1].detail


def test_exempt_profile_only_fetches(inv: dict[str, InventoryEntry]):
    clone = local(PROJECTS, "hidden-notes")
    assert kinds(plan([remote("hidden-notes")], {"hidden-notes": clone}, inv)) == ["fetch"]


def test_archived_on_github_moves_clean_clone_into_archive(inv: dict[str, InventoryEntry]):
    clones = {"ai-helper": local(PROJECTS, "ai-helper")}
    actions = plan([remote("ai-helper", archived=True)], clones, inv)
    assert kinds(actions) == ["move", "fast-forward"]
    assert (actions[0].path, actions[0].target) == ("/p/ai-helper", "/p/_archive/ai-helper")
    assert actions[1].path == "/p/_archive/ai-helper"  # later steps follow the new location


def test_archived_clone_already_in_archive_stays(inv: dict[str, InventoryEntry]):
    clones = {"_archive/ai-helper": local(PROJECTS, "_archive/ai-helper")}
    assert kinds(plan([remote("ai-helper", archived=True)], clones, inv)) == ["fast-forward"]


def test_unarchived_repo_moves_back_out_of_the_archive(inv: dict[str, InventoryEntry]):
    clones = {"_archive/ai-helper": local(PROJECTS, "_archive/ai-helper")}
    actions = plan([remote("ai-helper")], clones, inv)
    assert kinds(actions) == ["move", "fast-forward"]
    assert actions[0].target == "/p/ai-helper"


def test_rename_moves_then_sets_url_then_updates(inv: dict[str, InventoryEntry]):
    clones = {"old-widget": local(PROJECTS, "old-widget")}
    actions = plan([remote("widget")], clones, inv, RENAMED)
    assert kinds(actions) == ["move", "set-url", "fast-forward"]
    assert (actions[0].path, actions[0].target) == ("/p/old-widget", "/p/widget")
    assert (actions[1].path, actions[1].target) == ("/p/widget", url("widget"))
    assert actions[2].path == "/p/widget"


def test_rename_whose_target_folder_exists_touches_nothing(inv: dict[str, InventoryEntry]):
    clones = {
        "old-widget": local(PROJECTS, "old-widget"),
        "widget": local(PROJECTS, "widget", None, is_repo=False, origin=None),
    }
    actions = plan([remote("widget")], clones, inv, RENAMED)
    assert [a.kind for a in actions if a.kind != "unmanaged"] == ["skip"]
    assert "exists" in actions[0].detail


def test_renamed_origin_in_place_only_sets_the_url(inv: dict[str, InventoryEntry]):
    clones = {"widget": local(PROJECTS, "widget", "old-widget")}
    assert kinds(plan([remote("widget")], clones, inv, RENAMED)) == ["set-url", "fast-forward"]


def test_rename_is_found_without_any_inventory_entry():
    # GitHub, not the inventory, says old-widget is now widget: no duplicate clone.
    new_name_only = {"widget": make_entry("widget")}
    clones = {"old-widget": local(PROJECTS, "old-widget")}
    actions = plan([remote("widget")], clones, new_name_only, RENAMED)
    assert kinds(actions) == ["move", "set-url", "fast-forward"]
    assert "clone" not in kinds(actions)
    assert plan([remote("widget")], clones, {}, RENAMED)[0].kind == "move"


def test_rename_matches_case_insensitively_and_by_origin_not_folder(
    inv: dict[str, InventoryEntry],
):
    clones = {"my-folder": local(PROJECTS, "my-folder", "Old-Widget")}
    actions = plan([remote("widget")], clones, inv, RENAMED)
    assert kinds(actions) == ["move", "set-url", "fast-forward"]
    assert actions[0].target == "/p/widget"


def test_wiki_of_a_renamed_repo_follows_it(inv: dict[str, InventoryEntry]):
    clones = {
        "old-widget": local(PROJECTS, "old-widget"),
        "old-widget.wiki": local(PROJECTS, "old-widget.wiki", default_branch="master"),
    }
    actions = plan([remote("widget", has_wiki=True)], clones, inv, RENAMED)
    assert [(a.path, a.target) for a in actions if a.kind == "set-url"] == [
        ("/p/widget", url("widget")),
        ("/p/widget.wiki", url("widget.wiki")),
    ]
    assert "clone-wiki" not in kinds(actions)


def test_a_repo_transferred_to_another_owner_is_unmanaged(inv: dict[str, InventoryEntry]):
    clones = {"old-widget": local(PROJECTS, "old-widget")}
    actions = plan([], clones, inv, {"old-widget": "Someone/old-widget"})
    assert [(a.kind, a.detail) for a in actions] == [("unmanaged", "repo moved to another owner")]


def test_origin_pointing_at_another_repo_is_left_alone(inv: dict[str, InventoryEntry]):
    clones = {"ai-helper": local(PROJECTS, "ai-helper", "something-else")}
    [action] = plan([remote("ai-helper")], clones, inv)
    assert action.kind == "skip"
    assert "something-else" in action.detail


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"dirty": True, "dirty_reasons": ("uncommitted changes",)}, id="uncommitted"),
        pytest.param({"dirty": True, "dirty_reasons": ("detached HEAD",)}, id="detached"),
        pytest.param({"dirty": True, "dirty_reasons": ("no upstream",)}, id="no-upstream"),
        pytest.param({"dirty": True, "dirty_reasons": ("unpushed commits",)}, id="unpushed"),
        pytest.param(
            {"dirty": True, "in_progress": True, "dirty_reasons": ("merge or rebase",)},
            id="in-progress",
        ),
    ],
)
def test_dirty_clone_is_only_fetched_and_reported(
    inv: dict[str, InventoryEntry], overrides: dict[str, Any]
):
    clones = {"old-widget": local(PROJECTS, "old-widget", **overrides)}
    actions = plan([remote("widget")], clones, inv, RENAMED)
    assert kinds(actions) == ["fetch", "skip"]
    assert actions[0].path == "/p/old-widget"  # not moved, not retargeted
    assert actions[1].detail.startswith("dirty:")


def test_dirty_clone_of_an_archived_repo_is_not_moved(inv: dict[str, InventoryEntry]):
    clone = local(PROJECTS, "ai-helper", dirty=True, dirty_reasons=("uncommitted changes",))
    actions = plan([remote("ai-helper", archived=True)], {"ai-helper": clone}, inv)
    assert kinds(actions) == ["fetch", "skip"]


def test_foreign_origin_and_plain_folders_are_unmanaged(inv: dict[str, InventoryEntry]):
    clones = {
        "upstream-clone": local(PROJECTS, "upstream-clone", origin="git@github.com:Other/x.git"),
        "notes": local(PROJECTS, "notes", origin=None, is_repo=False),
        "no-origin": local(PROJECTS, "no-origin", origin=None),
    }
    actions = plan([], clones, inv)
    assert sorted((a.repo, a.kind) for a in actions) == [
        ("no-origin", "unmanaged"),
        ("notes", "unmanaged"),
        ("upstream-clone", "unmanaged"),
    ]


def test_clone_gone_from_github_is_orphaned(inv: dict[str, InventoryEntry]):
    clones = {"vanished": local(PROJECTS, "vanished")}
    assert [(a.repo, a.kind) for a in plan([], clones, inv)] == [("vanished", "orphaned")]


def test_wiki_is_cloned_when_the_repo_has_one(inv: dict[str, InventoryEntry]):
    clones = {"ai-helper": local(PROJECTS, "ai-helper")}
    actions = plan([remote("ai-helper", has_wiki=True)], clones, inv)
    wiki = [a for a in actions if a.kind == "clone-wiki"]
    assert [(a.path, a.target) for a in wiki] == [
        ("/p/ai-helper.wiki", f"git@github.com:{OWNER}/ai-helper.wiki.git")
    ]


def test_wiki_of_an_archived_repo_is_not_cloned(inv: dict[str, InventoryEntry]):
    actions = plan([remote("ai-helper", has_wiki=True, archived=True)], {}, inv)
    assert kinds(actions) == ["clone"]


def test_wiki_clone_follows_the_main_clone_on_a_move(inv: dict[str, InventoryEntry]):
    clones = {
        "ai-helper": local(PROJECTS, "ai-helper"),
        "ai-helper.wiki": local(PROJECTS, "ai-helper.wiki", default_branch="master"),
    }
    actions = plan([remote("ai-helper", archived=True, has_wiki=True)], clones, inv)
    moves = [(a.path, a.target) for a in actions if a.kind == "move"]
    assert moves == [
        ("/p/ai-helper", "/p/_archive/ai-helper"),
        ("/p/ai-helper.wiki", "/p/_archive/ai-helper.wiki"),
    ]


def test_dirty_wiki_blocks_the_whole_group_move(inv: dict[str, InventoryEntry]):
    clones = {
        "ai-helper": local(PROJECTS, "ai-helper"),
        "ai-helper.wiki": local(
            PROJECTS, "ai-helper.wiki", dirty=True, dirty_reasons=("uncommitted changes",)
        ),
    }
    actions = plan([remote("ai-helper", archived=True, has_wiki=True)], clones, inv)
    assert "move" not in kinds(actions)
    assert any(a.kind == "skip" and a.detail.startswith("dirty:") for a in actions)


# ------------------------------------------------------------------ real git


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout.strip()


class World:
    """A projects dir, bare remotes standing in for GitHub, and a fake `gh`."""

    def __init__(self, root: Path, standard_dir: Path) -> None:
        self.projects = root / "projects"
        self.projects.mkdir()
        self.remotes = root / "remotes"
        self.remotes.mkdir()
        self.scratch = root / "scratch"
        self.scratch.mkdir()
        self.standard_dir = standard_dir
        self.calls: list[list[str]] = []
        self.notifications: list[list[str]] = []
        # GET /repos/<owner>/<old> answers: former name -> full_name; anything else is a 404.
        self.renames: dict[str, str] = {}
        self.api_error: str | None = None  # a repo name whose lookup fails with HTTP 500

    def add_remote(self, name: str) -> None:
        """A bare remote with one commit on main, reachable as the repo's ssh URL."""
        bare = self.remotes / f"{name}.git"
        bare.mkdir()
        git(bare, "init", "--bare", "-b", "main")
        work = self.scratch / name
        git(self.scratch, "clone", str(bare), name)
        git(work, "checkout", "-b", "main")
        self.push_commit(name, "f.txt", "one")

    def push_commit(self, name: str, filename: str, text: str) -> None:
        work = self.scratch / name
        (work / filename).write_text(text, encoding="utf-8")
        git(work, "add", filename)
        git(work, "commit", "-m", f"add {filename}")
        git(work, "push", "origin", "HEAD:main")

    def clone(self, key: str, name: str) -> Path:
        dest = self.projects / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        git(self.projects, "clone", url(name), str(dest))
        return dest

    def runner(self, args: Sequence[str], cwd: Path | None) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        if args[0] == "notify-send":
            self.notifications.append(list(args))
            return subprocess.CompletedProcess(args, 0, "", "")
        return default_cmd(args, cwd)

    def sync(self, repos: list[dict[str, Any]], *flags: str) -> int:
        def gh_runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
            assert args[args.index("--method") + 1] == "GET"
            path = next(a for a in args if a.startswith("/"))
            if path.startswith("/user/repos"):
                return subprocess.CompletedProcess(args, 0, json.dumps([repos]), "")
            old = path.rsplit("/", 1)[1]
            if old == self.api_error:
                return subprocess.CompletedProcess(args, 1, "", "gh: Server Error (HTTP 500)")
            if old in self.renames:
                body = {"full_name": self.renames[old], "name": self.renames[old].split("/")[1]}
                return subprocess.CompletedProcess(args, 0, json.dumps(body), "")
            return subprocess.CompletedProcess(args, 1, "", "gh: Not Found (HTTP 404)")

        argv = [
            "sync",
            "--projects-dir",
            str(self.projects),
            "--standard-dir",
            str(self.standard_dir),
            *flags,
        ]
        return main(argv, gh=Gh(OWNER, gh_runner), run=self.runner)

    def git_subcommands(self) -> set[str]:
        return {
            next(a for a in c[1:] if not a.startswith("-")) for c in self.calls if c[0] == "git"
        }


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    standard_dir = tmp_path / "standard"
    standard_dir.mkdir()
    for name in ("standard.yml", "labels.yml", "inventory.yml"):
        (standard_dir / name).write_bytes((FIXTURES / name).read_bytes())
    monkeypatch.setenv("JTS_PRIVATE_INVENTORY", str(FIXTURES / "inventory.private.yml"))
    root = tmp_path / "world"
    root.mkdir()
    # Resolve every ssh GitHub URL to a bare repo in the test world.
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{root / 'remotes'}/.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", f"git@github.com:{OWNER}/")
    for key, value in (("NAME", "Test"), ("EMAIL", "test@example.com")):
        monkeypatch.setenv(f"GIT_AUTHOR_{key}", value)
        monkeypatch.setenv(f"GIT_COMMITTER_{key}", value)
    return World(root, standard_dir)


def test_sync_clones_a_new_repo(world: World):
    world.add_remote("fresh")
    assert world.sync([remote("fresh")]) == 0
    assert (world.projects / "fresh" / "f.txt").read_text(encoding="utf-8") == "one"


def test_new_archived_repo_is_cloned_under_archive(world: World):
    world.add_remote("fresh")
    assert world.sync([remote("fresh", archived=True)]) == 0
    assert (world.projects / "_archive" / "fresh" / "f.txt").is_file()
    assert not (world.projects / "fresh").exists()


def test_repo_with_a_wiki_gets_it_cloned_when_it_has_pages(world: World):
    world.add_remote("fresh")
    world.add_remote("fresh.wiki")
    world.add_remote("no-pages")  # wiki enabled but never edited: the .wiki remote is absent
    assert world.sync([remote("fresh", has_wiki=True), remote("no-pages", has_wiki=True)]) == 0
    assert (world.projects / "fresh.wiki" / "f.txt").is_file()
    assert not (world.projects / "no-pages.wiki").exists()


def test_archived_on_github_moves_the_clone_into_archive(world: World):
    world.add_remote("ai-helper")
    world.clone("ai-helper", "ai-helper")
    assert world.sync([remote("ai-helper", archived=True)]) == 0
    assert (world.projects / "_archive" / "ai-helper" / "f.txt").is_file()
    assert not (world.projects / "ai-helper").exists()


def test_rename_moves_the_clone_and_sets_the_url(world: World):
    world.add_remote("widget")
    clone = world.clone("old-widget", "widget")
    git(clone, "remote", "set-url", "origin", url("old-widget"))
    world.renames = RENAMED
    world.push_commit("widget", "g.txt", "two")
    git(world.remotes / "widget.git", "update-ref", "refs/heads/keep", "HEAD")  # unrelated ref
    # `old-widget` no longer exists as a bare remote, so fetching needs the new URL: the
    # test's insteadOf rule only knows names, so give the old name a remote too.
    subprocess.run(
        ["cp", "-r", str(world.remotes / "widget.git"), str(world.remotes / "old-widget.git")],
        check=True,
    )
    assert world.sync([remote("widget")]) == 0
    moved = world.projects / "widget"
    assert moved.is_dir()
    assert not (world.projects / "old-widget").exists()
    assert git(moved, "config", "remote.origin.url") == url("widget")
    assert (moved / "g.txt").is_file()  # fast-forwarded after the move


def test_rename_whose_target_exists_touches_nothing(world: World):
    world.add_remote("widget")
    clone = world.clone("old-widget", "widget")
    git(clone, "remote", "set-url", "origin", url("old-widget"))
    world.renames = RENAMED
    (world.projects / "widget").mkdir()
    (world.projects / "widget" / "mine.txt").write_text("keep", encoding="utf-8")
    world.calls.clear()
    assert world.sync([remote("widget")]) == 0
    assert (clone / "f.txt").is_file()
    assert git(clone, "config", "remote.origin.url") == url("old-widget")
    assert (world.projects / "widget" / "mine.txt").read_text(encoding="utf-8") == "keep"
    assert not {"fetch", "merge", "clone"} & world.git_subcommands()


def _dirty_modify(clone: Path) -> None:
    (clone / "f.txt").write_text("edited", encoding="utf-8")


def _dirty_untracked(clone: Path) -> None:
    (clone / "new.txt").write_text("wip", encoding="utf-8")


def _dirty_detached(clone: Path) -> None:
    git(clone, "checkout", "--detach")


def _dirty_no_upstream(clone: Path) -> None:
    git(clone, "checkout", "-b", "topic")


def _dirty_unpushed(clone: Path) -> None:
    (clone / "local.txt").write_text("x", encoding="utf-8")
    git(clone, "add", "local.txt")
    git(clone, "commit", "-m", "local only")


def _dirty_rebase(clone: Path) -> None:
    (clone / ".git" / "rebase-merge").mkdir()


def _dirty_merge(clone: Path) -> None:
    (clone / ".git" / "MERGE_HEAD").write_text(git(clone, "rev-parse", "HEAD") + "\n")


def _dirty_sequencer(clone: Path) -> None:
    (clone / ".git" / "sequencer").mkdir()


def _dirty_bisect(clone: Path) -> None:
    (clone / ".git" / "BISECT_LOG").write_text("git bisect start\n", encoding="utf-8")


@pytest.mark.parametrize(
    "make_dirty",
    [
        pytest.param(_dirty_sequencer, id="cherry-pick-sequence"),
        pytest.param(_dirty_bisect, id="bisect"),
        pytest.param(_dirty_modify, id="uncommitted"),
        pytest.param(_dirty_untracked, id="untracked"),
        pytest.param(_dirty_detached, id="detached-head"),
        pytest.param(_dirty_no_upstream, id="no-upstream"),
        pytest.param(_dirty_unpushed, id="unpushed"),
        pytest.param(_dirty_rebase, id="rebase-in-progress"),
        pytest.param(_dirty_merge, id="merge-in-progress"),
    ],
)
@pytest.mark.parametrize("archived", [False, True], ids=["active", "archived"])
def test_dirty_clone_is_only_fetched(
    world: World, make_dirty: Callable[[Path], None], archived: bool
):
    world.add_remote("ai-helper")
    clone = world.clone("ai-helper", "ai-helper")
    make_dirty(clone)
    world.push_commit("ai-helper", "g.txt", "two")
    head = git(clone, "rev-parse", "HEAD")
    branch = git(clone, "branch", "--show-current")
    files = sorted(p.name for p in clone.iterdir())
    world.calls.clear()

    assert world.sync([remote("ai-helper", archived=archived)]) == 0

    assert clone.is_dir()  # never moved
    assert git(clone, "rev-parse", "HEAD") == head
    assert git(clone, "branch", "--show-current") == branch
    assert sorted(p.name for p in clone.iterdir()) == files
    assert git(clone, "rev-parse", "origin/main") != head  # the fetch did happen
    assert not {"merge", "checkout", "reset", "stash", "pull", "clone"} & world.git_subcommands()
    assert not (world.projects / "_archive").exists()


def test_clean_and_behind_fast_forwards(world: World):
    world.add_remote("ai-helper")
    clone = world.clone("ai-helper", "ai-helper")
    world.push_commit("ai-helper", "g.txt", "two")
    assert world.sync([remote("ai-helper")]) == 0
    assert (clone / "g.txt").read_text(encoding="utf-8") == "two"
    assert "pull" not in world.git_subcommands()


def test_exempt_profile_is_fetched_but_never_merged(world: World):
    world.add_remote("hidden-notes")
    clone = world.clone("hidden-notes", "hidden-notes")
    world.push_commit("hidden-notes", "g.txt", "two")
    head = git(clone, "rev-parse", "HEAD")
    assert world.sync([remote("hidden-notes", private=True)]) == 0
    assert git(clone, "rev-parse", "HEAD") == head
    assert git(clone, "rev-parse", "origin/main") != head


def test_unmanaged_and_orphaned_folders_are_reported_and_untouched(
    world: World, capsys: pytest.CaptureFixture[str]
):
    world.add_remote("vanished")
    world.clone("vanished", "vanished")
    (world.projects / "notes").mkdir()
    foreign = world.projects / "upstream-clone"
    git(world.projects, "init", "-b", "main", str(foreign))
    git(foreign, "remote", "add", "origin", "git@github.com:Other/thing.git")
    before = sorted(p.name for p in world.projects.iterdir())
    assert world.sync([]) == 0
    assert sorted(p.name for p in world.projects.iterdir()) == before
    out = capsys.readouterr().out
    assert "orphaned 1" in out
    assert "unmanaged 2" in out


def test_one_failing_fetch_does_not_stop_the_others(
    world: World, capsys: pytest.CaptureFixture[str]
):
    for name in ("broken", "ai-helper"):
        world.add_remote(name)
        world.clone(name, name)
    world.push_commit("ai-helper", "g.txt", "two")
    (world.remotes / "broken.git" / "HEAD").unlink()  # the remote is no longer a repository
    assert world.sync([remote("broken"), remote("ai-helper")]) == 1
    assert (world.projects / "ai-helper" / "g.txt").is_file()
    captured = capsys.readouterr()
    assert "FAILED broken" in captured.err
    assert "failed 1" in captured.out


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_a_failed_move_skips_the_repos_later_steps(world: World):
    world.add_remote("widget")
    clone = world.clone("old-widget", "widget")
    git(clone, "remote", "set-url", "origin", url("old-widget"))
    world.renames = RENAMED
    world.projects.chmod(0o500)  # the move cannot create the destination
    try:
        code = world.sync([remote("widget")])
    finally:
        world.projects.chmod(0o700)
    assert code == 1
    assert git(clone, "config", "remote.origin.url") == url("old-widget")


def test_dry_run_changes_nothing_and_runs_only_git_reads(
    world: World, capsys: pytest.CaptureFixture[str]
):
    world.add_remote("fresh")
    world.add_remote("ai-helper")
    world.add_remote("old-widget")
    world.renames = RENAMED
    helper = world.clone("ai-helper", "ai-helper")
    old = world.clone("old-widget", "old-widget")
    world.push_commit("ai-helper", "g.txt", "two")
    before = sorted(p.name for p in world.projects.iterdir())
    refs = (git(helper, "show-ref"), git(old, "config", "remote.origin.url"))
    world.calls.clear()

    code = world.sync(
        [remote("fresh"), remote("ai-helper", archived=True), remote("widget")],
        "--dry-run",
        "--notify",
    )

    assert code == 0
    assert sorted(p.name for p in world.projects.iterdir()) == before
    assert (git(helper, "show-ref"), git(old, "config", "remote.origin.url")) == refs
    assert world.git_subcommands() <= READ_ONLY_GIT
    assert world.notifications == []
    out = capsys.readouterr().out
    assert "dry run" in out
    assert "cloned 1" in out
    assert "moved 2" in out


def test_summary_names_the_skipped_dirty_repos(world: World, capsys: pytest.CaptureFixture[str]):
    world.add_remote("ai-helper")
    _dirty_modify(world.clone("ai-helper", "ai-helper"))
    assert world.sync([remote("ai-helper")]) == 0
    assert "skipped-dirty 1 (ai-helper)" in capsys.readouterr().out


def test_notify_fires_for_a_dirty_clone_but_not_a_quiet_run(world: World):
    world.add_remote("ai-helper")
    clone = world.clone("ai-helper", "ai-helper")
    assert world.sync([remote("ai-helper")], "--notify") == 0
    assert world.notifications == []
    _dirty_modify(clone)
    assert world.sync([remote("ai-helper")], "--notify") == 0
    assert len(world.notifications) == 1


def test_notify_fires_for_a_failure(world: World):
    world.add_remote("broken")
    world.clone("broken", "broken")
    (world.remotes / "broken.git" / "HEAD").unlink()
    assert world.sync([remote("broken")], "--notify") == 1
    assert len(world.notifications) == 1


def test_listing_failure_exits_1(world: World, capsys: pytest.CaptureFixture[str]):
    def failing(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 1, "", "gh: Server Error (HTTP 500)")

    code = main(
        [
            "sync",
            "--projects-dir",
            str(world.projects),
            "--standard-dir",
            str(world.standard_dir),
        ],
        gh=Gh(OWNER, failing),
        run=world.runner,
    )
    assert code == 1
    assert "cannot read GitHub" in capsys.readouterr().err


def test_missing_projects_dir_exits_2(world: World, capsys: pytest.CaptureFixture[str]):
    code = main(
        [
            "sync",
            "--projects-dir",
            str(world.projects / "nope"),
            "--standard-dir",
            str(world.standard_dir),
        ],
        gh=Gh(OWNER, lambda a: subprocess.CompletedProcess(a, 0, "[[]]", "")),
        run=world.runner,
    )
    assert code == 2


# ------------------------------------------------------------------ inspect and execute


def test_inspect_clone_reports_a_clean_clone(world: World):
    world.add_remote("ai-helper")
    clone = world.clone("ai-helper", "ai-helper")
    info = inspect_clone(clone, default_cmd)
    assert (info.is_repo, info.origin, info.branch, info.default_branch) == (
        True,
        url("ai-helper"),
        "main",
        "main",
    )
    assert (info.dirty, info.dirty_reasons, info.in_progress) == (False, (), False)


def test_inspect_clone_reports_why_a_clone_is_dirty(world: World):
    world.add_remote("ai-helper")
    clone = world.clone("ai-helper", "ai-helper")
    _dirty_no_upstream(clone)
    _dirty_rebase(clone)
    info = inspect_clone(clone, default_cmd)
    assert info.dirty and info.in_progress
    assert any("upstream" in r for r in info.dirty_reasons)
    assert any("rebase" in r or "merge" in r for r in info.dirty_reasons)


def test_inspect_clone_of_a_plain_folder(tmp_path: Path):
    (tmp_path / "plain").mkdir()
    info = inspect_clone(tmp_path / "plain", default_cmd)
    assert (info.is_repo, info.origin) == (False, None)


def test_scan_local_finds_archive_children_and_dot_folders(world: World):
    world.add_remote(".github")
    world.add_remote("old")
    world.clone(".github", ".github")
    world.clone("_archive/old", "old")
    found = scan_local(world.projects, default_cmd)
    assert set(found) == {".github", "_archive/old"}


def test_execute_refuses_to_act_outside_the_projects_dir(world: World, tmp_path: Path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    action = SyncAction("x", "move", "m", path=str(outside), target=str(world.projects / "x"))
    [(_, error)] = execute_sync([action], default_cmd, world.projects)
    assert error is not None
    assert outside.is_dir()


def test_execute_rechecks_cleanliness_right_before_merging(world: World):
    world.add_remote("ai-helper")
    clone = world.clone("ai-helper", "ai-helper")
    world.push_commit("ai-helper", "g.txt", "two")
    _dirty_modify(clone)  # became dirty after the plan was made
    action = SyncAction("ai-helper", "fast-forward", "", path=str(clone), target="main")
    [(_, error)] = execute_sync([action], default_cmd, world.projects)
    assert error is not None
    assert (clone / "f.txt").read_text(encoding="utf-8") == "edited"
    assert not (clone / "g.txt").exists()


# ------------------------------------------------------------------ review fixes


def alias_remote(world: World, old: str, new: str) -> None:
    """`old` also resolves to the bare remote of `new`, as GitHub redirects a renamed repo."""
    subprocess.run(
        ["cp", "-r", str(world.remotes / f"{new}.git"), str(world.remotes / f"{old}.git")],
        check=True,
    )


def test_clone_of_a_repo_missing_from_the_inventory_is_never_merged(
    world: World, capsys: pytest.CaptureFixture[str]
):
    world.add_remote("brain-like")
    clone = world.clone("brain-like", "brain-like")
    world.push_commit("brain-like", "g.txt", "two")
    head = git(clone, "rev-parse", "HEAD")
    assert world.sync([remote("brain-like")]) == 0
    assert git(clone, "rev-parse", "HEAD") == head
    assert git(clone, "rev-parse", "origin/main") != head
    assert "not in inventory" in capsys.readouterr().out


def test_rename_found_through_github_needs_no_inventory_entry(world: World):
    world.add_remote("gizmo")
    clone = world.clone("gizmo-old", "gizmo")
    git(clone, "remote", "set-url", "origin", url("gizmo-old"))
    alias_remote(world, "gizmo-old", "gizmo")
    world.renames = {"gizmo-old": f"{OWNER}/gizmo"}
    assert world.sync([remote("gizmo")]) == 0
    assert sorted(p.name for p in world.projects.iterdir()) == ["gizmo"]  # no duplicate clone
    assert git(world.projects / "gizmo", "config", "remote.origin.url") == url("gizmo")


def test_a_clone_gone_from_github_is_orphaned_and_untouched(
    world: World, capsys: pytest.CaptureFixture[str]
):
    world.add_remote("vanished")
    world.clone("vanished", "vanished")
    assert world.sync([]) == 0
    assert (world.projects / "vanished").is_dir()
    assert "orphaned 1" in capsys.readouterr().out


def test_a_repo_transferred_away_is_unmanaged(world: World, capsys: pytest.CaptureFixture[str]):
    world.add_remote("vanished")
    world.clone("vanished", "vanished")
    world.renames = {"vanished": "Someone/vanished"}
    assert world.sync([]) == 0
    out = capsys.readouterr().out
    assert "unmanaged 1" in out
    assert "orphaned 0" in out


def test_a_github_lookup_error_aborts_the_run_with_exit_1(
    world: World, capsys: pytest.CaptureFixture[str]
):
    world.add_remote("ai-helper")
    world.add_remote("vanished")
    helper = world.clone("ai-helper", "ai-helper")
    world.clone("vanished", "vanished")
    world.push_commit("ai-helper", "g.txt", "two")
    world.api_error = "vanished"
    world.calls.clear()
    assert world.sync([remote("ai-helper")]) == 1
    assert not (helper / "g.txt").exists()
    assert not {"fetch", "merge", "clone"} & world.git_subcommands()
    assert "cannot read GitHub" in capsys.readouterr().err


def test_fast_forward_targets_origin_default_not_the_upstream_shorthand(world: World):
    world.add_remote("ai-helper")
    world.clone("ai-helper", "ai-helper")
    world.push_commit("ai-helper", "g.txt", "two")
    assert world.sync([remote("ai-helper")]) == 0
    merges = [c for c in world.calls if c[:2] == ["git", "merge"]]
    assert merges == [["git", "merge", "--ff-only", "origin/main"]]


def test_status_does_not_take_optional_locks(world: World):
    world.add_remote("ai-helper")
    world.clone("ai-helper", "ai-helper")
    assert world.sync([remote("ai-helper")], "--dry-run") == 0
    statuses = [c for c in world.calls if "status" in c]
    assert statuses
    assert all("--no-optional-locks" in c for c in statuses)


def test_branch_merged_upstream_and_pruned_is_not_unpushed(world: World):
    world.add_remote("ai-helper")
    clone = world.clone("ai-helper", "ai-helper")
    git(clone, "checkout", "-b", "feature")
    (clone / "feat.txt").write_text("x", encoding="utf-8")
    git(clone, "add", "feat.txt")
    git(clone, "commit", "-m", "feature")
    git(clone, "push", "-u", "origin", "feature")
    git(clone, "checkout", "main")
    git(clone, "merge", "--ff-only", "feature")
    git(clone, "push", "origin", "main")
    git(world.remotes / "ai-helper.git", "branch", "-D", "feature")  # merged, then deleted
    git(clone, "fetch", "--prune")
    info = inspect_clone(clone, default_cmd)
    assert not info.dirty
    assert world.sync([remote("ai-helper")]) == 0


def _leftover_squash_merged_branch(world: World, clone: Path) -> None:
    git(clone, "checkout", "-b", "feature")
    (clone / "feat.txt").write_text("x", encoding="utf-8")
    git(clone, "add", "feat.txt")
    git(clone, "commit", "-m", "feature")
    git(clone, "push", "-u", "origin", "feature")
    git(clone, "checkout", "main")
    # Squash-merged on GitHub: main gets different commits, the branch is deleted remotely.
    world.push_commit("ai-helper", "squashed.txt", "x")
    git(world.remotes / "ai-helper.git", "branch", "-D", "feature")
    git(clone, "fetch", "--prune")


def test_leftover_squash_merged_branch_does_not_block_fast_forward(
    world: World, capsys: pytest.CaptureFixture[str]
):
    world.add_remote("ai-helper")
    clone = world.clone("ai-helper", "ai-helper")
    _leftover_squash_merged_branch(world, clone)
    world.push_commit("ai-helper", "g.txt", "two")

    assert not inspect_clone(clone, default_cmd).dirty
    assert world.sync([remote("ai-helper")]) == 0

    assert (clone / "g.txt").is_file()  # main fast-forwarded
    assert git(clone, "branch", "--list", "feature")  # the branch is never deleted
    out = capsys.readouterr().out
    assert "skipped-dirty 0" in out
    assert "stale local branches: 1 (safe to delete with `git branch -D`)" in out


def test_unpushed_commit_on_default_branch_still_blocks_fast_forward(world: World):
    world.add_remote("ai-helper")
    clone = world.clone("ai-helper", "ai-helper")
    _leftover_squash_merged_branch(world, clone)
    _dirty_unpushed(clone)
    world.push_commit("ai-helper", "g.txt", "two")

    info = inspect_clone(clone, default_cmd)
    assert info.dirty_reasons == ("unpushed commits",)
    assert world.sync([remote("ai-helper")]) == 0
    assert not (clone / "g.txt").exists()


def test_dirty_working_tree_still_blocks_with_a_leftover_branch(world: World):
    world.add_remote("ai-helper")
    clone = world.clone("ai-helper", "ai-helper")
    _leftover_squash_merged_branch(world, clone)
    _dirty_modify(clone)
    world.push_commit("ai-helper", "g.txt", "two")

    assert inspect_clone(clone, default_cmd).dirty_reasons == ("uncommitted changes",)
    assert world.sync([remote("ai-helper")]) == 0
    assert not (clone / "g.txt").exists()


def test_a_failed_main_move_also_skips_the_wiki_move(world: World):
    world.add_remote("ai-helper")
    world.add_remote("ai-helper.wiki")
    main_clone = world.clone("ai-helper", "ai-helper")
    wiki_clone = world.clone("ai-helper.wiki", "ai-helper.wiki")
    (world.projects / "_archive" / "ai-helper").mkdir(parents=True)  # blocks the main move
    actions = [
        SyncAction(
            "ai-helper",
            "move",
            "",
            str(main_clone),
            str(world.projects / "_archive" / "ai-helper"),
        ),
        SyncAction(
            "ai-helper.wiki",
            "move",
            "",
            str(wiki_clone),
            str(world.projects / "_archive" / "ai-helper.wiki"),
        ),
    ]
    results = execute_sync(actions, default_cmd, world.projects)
    assert results[0][1] is not None
    assert results[1][1] == "skipped: earlier step failed"
    assert wiki_clone.is_dir()


def test_a_move_across_devices_fails_cleanly(world: World, monkeypatch: pytest.MonkeyPatch):
    world.add_remote("ai-helper")
    clone = world.clone("ai-helper", "ai-helper")

    def cross_device(src: object, dst: object) -> None:
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr("jts_ops.sync.os.rename", cross_device)
    action = SyncAction(
        "ai-helper", "move", "", str(clone), str(world.projects / "_archive" / "ai-helper")
    )
    [(_, error)] = execute_sync([action], default_cmd, world.projects)
    assert error is not None
    assert clone.is_dir()


def test_an_unreadable_folder_is_marked_dirty(tmp_path: Path):
    def exploding(args: Sequence[str], cwd: Path | None) -> subprocess.CompletedProcess[str]:
        raise PermissionError("denied")

    folder = tmp_path / "locked"
    folder.mkdir()
    found = scan_local(tmp_path, exploding)
    assert found["locked"].dirty
    assert "cannot inspect" in found["locked"].dirty_reasons[0]
