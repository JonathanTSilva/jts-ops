import base64
import json
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from jts_ops.cli import main
from jts_ops.gh import Gh
from jts_ops.inventory import InventoryEntry
from jts_ops.migrate import (
    Step,
    load_banner,
    load_license_texts,
    plan_digest,
    plan_migration,
)
from jts_ops.migrate import execute_migration as run_steps
from jts_ops.standard import Standard
from jts_ops.state import RepoState

FIXTURES = Path(__file__).parent / "fixtures"
OWNER = "ExampleOwner"
BANNER = "<!-- archive-banner:start -->\n> [!NOTE]\n> Archived.\n<!-- archive-banner:end -->"
LICENSES = {"mit": "MIT text\n", "proprietary": "All rights reserved\n"}


def make_state(**overrides: Any) -> RepoState:
    """A public `old-gadget` repo with a README, no LICENSE and the wiki on."""
    base = RepoState(
        name="old-gadget",
        private=False,
        archived=False,
        description="Example gadget",
        topics=("kind-product", "status-archived"),
        has_wiki=True,
        has_discussions=False,
        allow_squash_merge=True,
        allow_merge_commit=False,
        allow_rebase_merge=False,
        delete_branch_on_merge=True,
        default_branch="main",
        license_spdx=None,
        files=frozenset({"README.md"}),
        readme="# old-gadget\n",
        ci_conclusion=None,
        labels=(),
    )
    return replace(base, **overrides)


def make_entry(**overrides: Any) -> InventoryEntry:
    base = InventoryEntry(
        name="old-gadget",
        target="gadget",
        kind="product",
        visibility="public",
        fate="archive",
        profile="archived",
        license="mit",
        status="archived",
        featured=False,
        description="Example gadget",
    )
    return replace(base, **overrides)


def actions(steps: Sequence[Step]) -> list[str]:
    return [s.action for s in steps]


def plan(std: Standard, state: RepoState | None = None, entry: InventoryEntry | None = None):
    return plan_migration(state or make_state(), entry or make_entry(), BANNER, LICENSES, std)


def test_order_is_fixed(std: Standard):
    steps = plan(std, make_state(description="stale", topics=()))
    assert actions(steps) == [
        "banner",
        "license",
        "wiki-off",
        "metadata",
        "rename",
        "archive",
        "local-move",
    ]
    assert {s.repo for s in steps} == {"old-gadget"}


def test_metadata_only_when_description_or_topics_differ(std: Standard):
    assert "metadata" not in actions(plan(std))
    meta = next(s for s in plan(std, make_state(description="stale")) if s.action == "metadata")
    assert meta.payload == {"description": "Example gadget"}
    meta = next(s for s in plan(std, make_state(topics=("tool",))) if s.action == "metadata")
    assert meta.payload == {"topics": ["kind-product", "status-archived", "tool"]}


def test_metadata_not_planned_for_active_fates(std: Standard):
    entry = make_entry(fate="keep", profile="active", status="active")
    steps = plan(std, make_state(description="stale", topics=()), entry)
    assert "metadata" not in actions(steps)


def test_company_never_gets_license(std: Standard):
    assert "license" not in actions(plan(std, entry=make_entry(license="company")))


def test_private_repo_gets_no_license(std: Standard):
    assert "license" not in actions(plan(std, make_state(private=True)))


def test_existing_license_file_is_left_alone(std: Standard):
    state = make_state(files=frozenset({"README.md", "LICENSE"}))
    assert "license" not in actions(plan(std, state))


def test_unknown_license_text_is_an_error(std: Standard):
    with pytest.raises(ValueError, match="agpl-3.0"):
        plan(std, entry=make_entry(license="agpl-3.0"))


def test_banner_idempotent(std: Standard):
    state = make_state(readme=f"{BANNER}\n\n# old-gadget\n")
    assert "banner" not in actions(plan(std, state))


def test_active_rename_only(std: Standard):
    state = make_state(has_wiki=False, files=frozenset({"README.md", "LICENSE"}))
    entry = make_entry(fate="keep", profile="active", status="active")
    steps = plan(std, state, entry)
    assert actions(steps) == ["rename", "local-move"]
    assert steps[0].payload["target"] == "gadget"
    assert steps[1].payload["archived"] is False


def test_already_archived_has_no_archive_or_rename_step(std: Standard):
    steps = plan(std, make_state(archived=True, name="gadget"), make_entry(name="gadget"))
    assert "archive" not in actions(steps)
    assert "rename" not in actions(steps)


def test_active_repo_gets_no_license_step(std: Standard):
    entry = make_entry(fate="keep", profile="active", status="active")
    assert "license" not in actions(plan(std, make_state(), entry))


def test_special_repo_gets_license_step(std: Standard):
    entry = make_entry(name="old-gadget", target="old-gadget", fate="keep", profile="special")
    assert "license" in actions(plan(std, make_state(), entry))


@pytest.mark.parametrize(
    "files", [{"LICENSE.md"}, {"LICENSE.txt"}, {"COPYING"}], ids=["md", "txt", "copying"]
)
def test_other_license_file_names_count(std: Standard, files: set[str]):
    state = make_state(files=frozenset({"README.md", *files}))
    assert "license" not in actions(plan(std, state))


def test_license_detected_by_github_counts(std: Standard):
    assert "license" not in actions(plan(std, make_state(license_spdx="MIT")))


def test_already_archived_plans_only_local_move(std: Standard):
    state = make_state(
        name="gadget", archived=True, readme=None, description="stale", topics=(), has_wiki=True
    )
    steps = plan(std, state, make_entry(name="gadget"))
    assert actions(steps) == ["local-move"]
    assert steps[0].payload["archived"] is True


def test_already_archived_unrenamed_clone_keeps_its_current_name(std: Standard):
    steps = plan(std, make_state(archived=True))  # still named old-gadget, target gadget
    assert actions(steps) == ["local-move"]
    assert steps[0].payload["target"] == "old-gadget"


def test_nothing_to_do_gives_no_steps(std: Standard):
    state = make_state(name="gadget", has_wiki=False, files=frozenset({"LICENSE"}))
    entry = make_entry(name="gadget", fate="keep", profile="active", status="active")
    assert plan(std, state, entry) == []


def test_digest_is_stable_and_sensitive(std: Standard):
    steps = plan(std)
    assert plan_digest(steps) == plan_digest(list(steps))
    assert plan_digest(steps) != plan_digest(steps[:-1])
    assert len(plan_digest(steps)) == 64


# ---------------------------------------------------------------- execution


class FakeGithub:
    """Records every call; `fail_when` picks calls to answer with an HTTP error status."""

    def __init__(self, fail_when: Callable[[str, str, Any], int | None] | None = None) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.fail_when = fail_when
        self.readme: dict[str, Any] | None = {
            "encoding": "base64",
            "content": base64.b64encode(b"# old-gadget\n").decode(),
            "sha": "abc123",
            "path": "README.md",
        }

    def __call__(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        method = args[args.index("--method") + 1]
        path = next(a for a in args if a.startswith("/"))
        body: Any = None
        if "--input" in args:
            body = json.loads(Path(args[args.index("--input") + 1]).read_text(encoding="utf-8"))
        self.calls.append((method, path, body))
        status = self.fail_when(method, path, body) if self.fail_when else None
        if status is not None:
            return subprocess.CompletedProcess(args, 1, "", f"gh: Failed (HTTP {status})\n")
        if method == "GET" and path.endswith("/readme"):
            if self.readme is None:
                return subprocess.CompletedProcess(args, 1, "", "gh: Not Found (HTTP 404)\n")
            return subprocess.CompletedProcess(args, 0, json.dumps(self.readme), "")
        return subprocess.CompletedProcess(args, 0, "{}", "")

    def writes(self) -> list[tuple[str, str]]:
        return [(m, p) for m, p, _ in self.calls if m != "GET"]


def writable(fake: FakeGithub) -> Gh:
    return Gh(OWNER, fake, read_only=False)


def test_full_sequence_calls(std: Standard, tmp_path: Path):
    steps = plan(std, make_state(description="stale"))
    fake = FakeGithub()
    results = run_steps(steps, writable(fake), tmp_path)
    assert all(err is None for _, err in results)
    assert actions([s for s, _ in results]) == actions(steps)
    old, new = f"/repos/{OWNER}/old-gadget", f"/repos/{OWNER}/gadget"
    assert [(m, p) for m, p, _ in fake.calls] == [
        ("GET", f"{old}/readme"),
        ("PUT", f"{old}/contents/README.md"),
        ("PUT", f"{old}/contents/LICENSE"),
        ("PATCH", old),  # wiki off
        ("PATCH", old),  # description
        ("PATCH", old),  # rename
        ("PATCH", new),  # archive uses the new name
    ]
    bodies = [b for _, _, b in fake.calls]
    assert bodies[1]["message"] == "docs: add archive banner"
    assert bodies[1]["sha"] == "abc123"
    assert base64.b64decode(bodies[1]["content"]).decode() == f"{BANNER}\n\n# old-gadget\n"
    assert bodies[2]["message"] == "docs: add license"
    assert base64.b64decode(bodies[2]["content"]).decode() == "MIT text\n"
    assert bodies[3] == {"has_wiki": False}
    assert bodies[4] == {"description": "Example gadget"}
    assert bodies[5] == {"name": "gadget"}
    assert bodies[6] == {"archived": True}


def test_metadata_puts_topics_before_rename(std: Standard, tmp_path: Path):
    steps = [s for s in plan(std, make_state(topics=("tool",))) if s.action == "metadata"]
    fake = FakeGithub()
    assert run_steps(steps, writable(fake), tmp_path)[0][1] is None
    assert fake.calls == [
        (
            "PUT",
            f"/repos/{OWNER}/old-gadget/topics",
            {"names": ["kind-product", "status-archived", "tool"]},
        )
    ]


def test_missing_readme_creates_one(std: Standard, tmp_path: Path):
    steps = [s for s in plan(std, make_state(readme=None)) if s.action == "banner"]
    fake = FakeGithub()
    fake.readme = None
    results = run_steps(steps, writable(fake), tmp_path)
    assert results[0][1] is None
    method, path, body = fake.calls[1]
    assert (method, path) == ("PUT", f"/repos/{OWNER}/old-gadget/contents/README.md")
    assert "sha" not in body
    text = base64.b64decode(body["content"]).decode()
    assert text == f"{BANNER}\n\n# gadget\n\nExample gadget\n"


def test_large_readme_is_never_overwritten(std: Standard, tmp_path: Path):
    steps = [s for s in plan(std) if s.action == "banner"]
    fake = FakeGithub()
    fake.readme = {"encoding": "none", "content": "", "sha": "abc123", "path": "README.md"}
    results = run_steps(steps, writable(fake), tmp_path)
    assert results[0][1] == "README too large to edit via API"
    assert fake.writes() == []


def test_failed_rename_blocks_archive(std: Standard, tmp_path: Path):
    first = plan(std)
    second = plan(
        std,
        make_state(name="old-widget"),
        make_entry(name="old-widget", target="widget"),
    )

    def fail_gadget_rename(method: str, path: str, body: Any) -> int | None:
        return 422 if body == {"name": "gadget"} else None

    fake = FakeGithub(fail_gadget_rename)
    results = run_steps([*first, *second], writable(fake), tmp_path)
    errors = {(s.repo, s.action): err for s, err in results}
    assert "422" in str(errors[("old-gadget", "rename")])
    assert errors[("old-gadget", "archive")] == "skipped: earlier step failed"
    assert errors[("old-gadget", "local-move")] == "skipped: earlier step failed"
    assert errors[("old-gadget", "banner")] is None
    assert errors[("old-widget", "archive")] is None  # the next repo still ran
    archived = [p for m, p, b in fake.calls if m == "PATCH" and b == {"archived": True}]
    assert archived == [f"/repos/{OWNER}/widget"]


def test_read_only_gh_cannot_write(std: Standard, tmp_path: Path):
    fake = FakeGithub()
    results = run_steps(plan(std), Gh(OWNER, fake, read_only=True), tmp_path)
    assert fake.writes() == []
    assert "read-only" in str(results[0][1])


# ---------------------------------------------------------------- local move


def git(cwd: Path, *args: str) -> str:
    out = subprocess.run(
        [
            "git",
            "-c",
            "user.email=t@example.invalid",
            "-c",
            "user.name=t",
            "-c",
            "init.defaultBranch=main",
            *args,
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def make_clone(projects: Path, name: str) -> Path:
    """A clone of a local bare 'origin' with one pushed commit on main."""
    origin = projects.parent / "remotes" / f"{name}.git"
    origin.mkdir(parents=True)
    git(origin, "init", "--bare", "-b", "main")
    projects.mkdir(exist_ok=True)
    git(projects, "clone", str(origin), name)
    clone = projects / name
    (clone / "f.txt").write_text("x", encoding="utf-8")
    git(clone, "add", "f.txt")
    git(clone, "commit", "-m", "init")
    git(clone, "push", "origin", "HEAD:main")
    git(clone, "fetch", "origin")
    return clone


def move_step(archived: bool) -> Step:
    return Step(
        "old-gadget",
        "local-move",
        {
            "old": "old-gadget",
            "target": "gadget",
            "archived": archived,
            "default_branch": "main",
        },
    )


def move(projects: Path, archived: bool = True) -> str | None:
    results = run_steps([move_step(archived)], Gh(OWNER, FakeGithub(), read_only=False), projects)
    return results[0][1]


def test_local_move_clean_clone_to_archive(tmp_path: Path):
    projects = tmp_path / "Projects"
    make_clone(projects, "old-gadget")
    make_clone(projects, "old-gadget.wiki")
    assert move(projects) is None
    assert not (projects / "old-gadget").exists()
    assert not (projects / "old-gadget.wiki").exists()
    moved = projects / "_archive" / "gadget"
    assert git(moved, "remote", "get-url", "origin") == f"git@github.com:{OWNER}/gadget.git"
    assert (
        git(projects / "_archive" / "gadget.wiki", "remote", "get-url", "origin")
        == f"git@github.com:{OWNER}/gadget.wiki.git"
    )


def test_local_move_active_stays_in_projects(tmp_path: Path):
    projects = tmp_path / "Projects"
    make_clone(projects, "old-gadget")
    assert move(projects, archived=False) is None
    assert (projects / "gadget" / "f.txt").exists()


def test_local_move_without_clone_is_a_noop(tmp_path: Path):
    projects = tmp_path / "Projects"
    projects.mkdir()
    assert move(projects) is None
    assert not (projects / "_archive").exists()


def test_dirty_clone_untouched(tmp_path: Path):
    projects = tmp_path / "Projects"
    clone = make_clone(projects, "old-gadget")
    wiki = make_clone(projects, "old-gadget.wiki")
    before = git(clone, "remote", "get-url", "origin")
    (clone / "scratch.txt").write_text("uncommitted", encoding="utf-8")
    error = move(projects)
    assert error is not None
    assert "not clean" in error
    assert clone.exists()
    assert wiki.exists()  # the wiki is left alone too when the main clone is dirty
    assert not (projects / "_archive").exists()
    assert git(clone, "remote", "get-url", "origin") == before


def test_dirty_wiki_leaves_main_clone_untouched(tmp_path: Path):
    projects = tmp_path / "Projects"
    clone = make_clone(projects, "old-gadget")
    wiki = make_clone(projects, "old-gadget.wiki")
    (wiki / "scratch.txt").write_text("uncommitted", encoding="utf-8")
    assert move(projects) is not None
    assert clone.exists()
    assert git(clone, "remote", "get-url", "origin") != f"git@github.com:{OWNER}/gadget.git"


def _switch_branch(clone: Path) -> None:
    git(clone, "checkout", "-b", "feature")


def _commit_locally(clone: Path) -> None:
    (clone / "g.txt").write_text("y", encoding="utf-8")
    git(clone, "add", "g.txt")
    git(clone, "commit", "-m", "local only")


@pytest.mark.parametrize(
    "break_it",
    [
        pytest.param(_switch_branch, id="not-default-branch"),
        pytest.param(_commit_locally, id="unpushed-commit"),
    ],
)
def test_unclean_variants_untouched(tmp_path: Path, break_it: Callable[[Path], object]):
    projects = tmp_path / "Projects"
    clone = make_clone(projects, "old-gadget")
    break_it(clone)
    before = git(clone, "remote", "get-url", "origin")
    assert move(projects) is not None
    assert clone.exists()
    assert git(clone, "remote", "get-url", "origin") == before


def test_plain_directory_is_not_a_clone(tmp_path: Path):
    projects = tmp_path / "Projects"
    (projects / "old-gadget").mkdir(parents=True)
    error = move(projects)
    assert error is not None
    assert "not the root of a git clone" in error
    assert (projects / "old-gadget").exists()


def test_directory_inside_another_repo_is_not_a_clone(tmp_path: Path):
    projects = tmp_path / "Projects"
    projects.mkdir()
    git(projects, "init")  # the folder is only a subdirectory of this repo
    (projects / "old-gadget").mkdir()
    error = move(projects)
    assert error is not None
    assert "not the root of a git clone" in error
    assert not (projects / "_archive").exists()


def test_clone_without_origin_untouched(tmp_path: Path):
    projects = tmp_path / "Projects"
    clone = make_clone(projects, "old-gadget")
    git(clone, "remote", "remove", "origin")
    error = move(projects)
    assert error is not None
    assert "no origin remote" in error
    assert clone.exists()


def test_detached_wiki_leaves_both_clones_untouched(tmp_path: Path):
    projects = tmp_path / "Projects"
    clone = make_clone(projects, "old-gadget")
    wiki = make_clone(projects, "old-gadget.wiki")
    git(wiki, "checkout", "--detach")
    before = git(clone, "remote", "get-url", "origin")
    error = move(projects)
    assert error is not None
    assert "detached HEAD" in error
    assert clone.exists()
    assert wiki.exists()
    assert git(clone, "remote", "get-url", "origin") == before


def test_local_move_refuses_existing_destination(tmp_path: Path):
    projects = tmp_path / "Projects"
    make_clone(projects, "old-gadget")
    (projects / "_archive" / "gadget").mkdir(parents=True)
    assert move(projects) is not None
    assert (projects / "old-gadget").exists()


# ---------------------------------------------------------------- loaders


def test_load_banner_extracts_marker_block(tmp_path: Path):
    handbook = tmp_path / "handbook"
    handbook.mkdir()
    (handbook / "archiving.md").write_text(
        f"# Archiving\n\nintro\n\n```markdown\n{BANNER}\n```\n", encoding="utf-8"
    )
    assert load_banner(tmp_path) == BANNER


def test_load_license_texts_renders_template(tmp_path: Path):
    (tmp_path / "licenses").mkdir()
    (tmp_path / "licenses" / "mit.txt").write_text("MIT {{ year }}\nbody\n", encoding="utf-8")
    (tmp_path / "template").mkdir()
    (tmp_path / "template" / "LICENSE.jinja").write_text(
        "{% if license == 'mit' %}\n"
        "{% include 'licenses/mit.txt' %}\n"
        "{% elif license == 'proprietary' %}\n"
        "Copyright (c) {{ year }} Example. All rights reserved.\n"
        "{% endif %}\n",
        encoding="utf-8",
    )
    assert load_license_texts(tmp_path, 2030) == {
        "mit": "MIT 2030\nbody\n",
        "proprietary": "Copyright (c) 2030 Example. All rights reserved.\n",
    }


def test_load_license_texts_from_the_real_template():
    template = Path("~/Projects/template-repo").expanduser()
    if not (template / "template" / "LICENSE.jinja").is_file():
        pytest.skip("template-repo not checked out")
    texts = load_license_texts(template, 2026)
    assert {"mit", "proprietary", "cc-by-4.0", "mit+cc-by-4.0", "agpl-3.0"} <= set(texts)
    assert "Copyright (c) 2026" in texts["mit"]
    for text in texts.values():
        assert "{{" not in text
        assert "{%" not in text


# ---------------------------------------------------------------- CLI


class CliGithub:
    """Serves the read endpoints for a list of repos; a write is recorded and answered {}."""

    def __init__(self, repos: list[dict[str, Any]]) -> None:
        self.repos = repos
        self.calls: list[tuple[str, str]] = []

    def __call__(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        method = args[args.index("--method") + 1]
        path = next(a for a in args if a.startswith("/"))
        self.calls.append((method, path))

        def ok(body: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args, 0, json.dumps(body), "")

        if method != "GET":
            return ok({})
        if path.startswith("/user/repos"):
            page: list[object] = list(self.repos)
            return ok([page])
        if "/git/trees/" in path:
            files = ("README.md", "LICENSE")
            return ok({"tree": [{"path": f, "type": "blob"} for f in files]})
        if path.endswith("/readme"):
            content = base64.b64encode(b"# hi\n").decode()
            return ok({"encoding": "base64", "content": content, "sha": "s", "path": "README.md"})
        if "/actions/runs" in path:
            return ok({"workflow_runs": []})
        if path.endswith("/labels"):
            no_labels: list[object] = []
            return ok([no_labels])
        raise AssertionError(path)

    def writes(self) -> list[tuple[str, str]]:
        return [c for c in self.calls if c[0] != "GET"]


@pytest.fixture
def dirs(tmp_path: Path) -> dict[str, Path]:
    standard = tmp_path / "standard"
    (standard / "handbook").mkdir(parents=True)
    for name in ("standard.yml", "labels.yml"):
        (standard / name).write_bytes((FIXTURES / "standard" / name).read_bytes())
    (standard / "handbook" / "archiving.md").write_text(f"{BANNER}\n", encoding="utf-8")
    (standard / "inventory.yml").write_text(
        "- {name: old-gadget, target: gadget, kind: product, visibility: public, fate: archive,"
        " profile: archived, license: mit, status: archived, featured: false,"
        ' description: "Example gadget"}\n'
        "- {name: tidy-tool, target: tidy-tool, kind: product, visibility: public, fate: keep,"
        " profile: active, license: mit, status: active, featured: false,"
        ' description: "Example tool"}\n',
        encoding="utf-8",
    )
    template = tmp_path / "template"
    (template / "template").mkdir(parents=True)
    (template / "licenses").mkdir()
    (template / "licenses" / "mit.txt").write_text("MIT text\n", encoding="utf-8")
    (template / "template" / "LICENSE.jinja").write_text(
        "{% if license == 'mit' %}\n{% include 'licenses/mit.txt' %}\n{% endif %}\n",
        encoding="utf-8",
    )
    projects = tmp_path / "Projects"
    projects.mkdir()
    return {"standard": standard, "template": template, "projects": projects, "tmp": tmp_path}


def cli_repo(name: str, **overrides: Any) -> dict[str, Any]:
    data = json.loads((FIXTURES / "gh" / "repo.json").read_text(encoding="utf-8"))
    return {**data, "name": name, "has_wiki": True, "topics": [], **overrides}


@pytest.fixture(autouse=True)
def no_real_private_inventory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("JTS_PRIVATE_INVENTORY", str(tmp_path / "absent.yml"))


def migrate_args(dirs: dict[str, Path], *extra: str) -> list[str]:
    return [
        "migrate",
        "--standard-dir",
        str(dirs["standard"]),
        "--template-dir",
        str(dirs["template"]),
        "--projects-dir",
        str(dirs["projects"]),
        *extra,
    ]


def test_dry_run_read_only(dirs: dict[str, Path], capsys: pytest.CaptureFixture[str]):
    fake = CliGithub([cli_repo("old-gadget"), cli_repo("tidy-tool", has_wiki=False)])
    plan_file = dirs["tmp"] / "plan.json"
    code = main(migrate_args(dirs, "--plan-out", str(plan_file)), Gh(OWNER, fake))
    assert code == 0
    assert fake.writes() == []
    data = json.loads(plan_file.read_text(encoding="utf-8"))
    assert {s["repo"] for s in data["steps"]} == {"old-gadget"}
    assert [s["action"] for s in data["steps"]][-2:] == ["archive", "local-move"]
    assert len(data["digest"]) == 64
    out = capsys.readouterr().out
    assert "old-gadget" in out
    assert data["digest"] in out


def test_digest_does_not_depend_on_api_order(dirs: dict[str, Path]):
    first, second = dirs["tmp"] / "a.json", dirs["tmp"] / "b.json"
    one = CliGithub([cli_repo("old-gadget"), cli_repo("tidy-tool")])  # tidy-tool: wiki on
    two = CliGithub([cli_repo("tidy-tool"), cli_repo("old-gadget")])
    assert main(migrate_args(dirs, "--plan-out", str(first)), Gh(OWNER, one)) == 0
    assert main(migrate_args(dirs, "--plan-out", str(second)), Gh(OWNER, two)) == 0
    plan_a = json.loads(first.read_text(encoding="utf-8"))
    assert {s["repo"] for s in plan_a["steps"]} == {"old-gadget", "tidy-tool"}
    assert plan_a == json.loads(second.read_text(encoding="utf-8"))


def test_unknown_repo_is_exit_2(dirs: dict[str, Path]):
    fake = CliGithub([cli_repo("old-gadget")])
    assert main(migrate_args(dirs, "--repo", "nope"), Gh(OWNER, fake)) == 2
    assert fake.writes() == []


def test_execute_without_plan_refuses(dirs: dict[str, Path]):
    fake = CliGithub([cli_repo("old-gadget")])
    assert main(migrate_args(dirs, "--execute"), Gh(OWNER, fake)) == 3
    assert fake.writes() == []


def test_plan_without_execute_is_a_usage_error(dirs: dict[str, Path]):
    fake = CliGithub([cli_repo("old-gadget")])
    plan_file = dirs["tmp"] / "plan.json"
    assert main(migrate_args(dirs, "--plan-out", str(plan_file)), Gh(OWNER, fake)) == 0
    assert main(migrate_args(dirs, "--plan", str(plan_file)), Gh(OWNER, fake)) == 2


def test_digest_mismatch_refuses(dirs: dict[str, Path]):
    fake = CliGithub([cli_repo("old-gadget"), cli_repo("tidy-tool", has_wiki=False)])
    plan_file = dirs["tmp"] / "plan.json"
    assert main(migrate_args(dirs, "--plan-out", str(plan_file)), Gh(OWNER, fake)) == 0
    data = json.loads(plan_file.read_text(encoding="utf-8"))
    data["digest"] = "0" * 64
    plan_file.write_text(json.dumps(data), encoding="utf-8")
    code = main(migrate_args(dirs, "--execute", "--plan", str(plan_file)), Gh(OWNER, fake))
    assert code == 3
    assert fake.writes() == []


def test_tampered_steps_refuse(dirs: dict[str, Path]):
    fake = CliGithub([cli_repo("old-gadget")])
    plan_file = dirs["tmp"] / "plan.json"
    assert main(migrate_args(dirs, "--plan-out", str(plan_file)), Gh(OWNER, fake)) == 0
    data = json.loads(plan_file.read_text(encoding="utf-8"))
    data["steps"] = data["steps"][:-1]  # the digest no longer matches the steps
    plan_file.write_text(json.dumps(data), encoding="utf-8")
    code = main(migrate_args(dirs, "--execute", "--plan", str(plan_file)), Gh(OWNER, fake))
    assert code == 3
    assert fake.writes() == []


def test_changed_github_state_refuses(dirs: dict[str, Path]):
    plan_file = dirs["tmp"] / "plan.json"
    before = CliGithub([cli_repo("old-gadget")])
    assert main(migrate_args(dirs, "--plan-out", str(plan_file)), Gh(OWNER, before)) == 0
    after = CliGithub([cli_repo("old-gadget", has_wiki=False)])  # the wiki was switched off
    code = main(migrate_args(dirs, "--execute", "--plan", str(plan_file)), Gh(OWNER, after))
    assert code == 3
    assert after.writes() == []


def test_execute_with_matching_plan_writes(dirs: dict[str, Path]):
    plan_file = dirs["tmp"] / "plan.json"
    dry = CliGithub([cli_repo("old-gadget")])
    assert main(migrate_args(dirs, "--plan-out", str(plan_file)), Gh(OWNER, dry)) == 0
    live = CliGithub([cli_repo("old-gadget")])
    code = main(migrate_args(dirs, "--execute", "--plan", str(plan_file)), Gh(OWNER, live))
    assert code == 0
    assert live.writes()[-1] == ("PATCH", f"/repos/{OWNER}/gadget")  # archive is the last write
    assert ("PATCH", f"/repos/{OWNER}/old-gadget") in live.writes()  # wiki off, then rename
