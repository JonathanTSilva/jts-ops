import re
import subprocess
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from jts_ops.adopt import AdoptError, adopt_repo
from jts_ops.cli import main
from jts_ops.gh import Gh
from jts_ops.inventory import InventoryEntry
from jts_ops.new import DEFAULT_TEMPLATE
from jts_ops.standard import Standard

OWNER = "ExampleOwner"
CLONE = "old-widget"  # current name; the inventory target is "widget"
ISSUE_URL = f"https://github.com/{OWNER}/{CLONE}/issues/42"
PR_URL = f"https://github.com/{OWNER}/{CLONE}/pull/43"
ANSWERS = "_commit: v1.0.1\n_src_path: {src}\nrepo_name: widget\n"

# The regex of .github/actions/branch-name/check.sh, re-implemented.
TYPES = "feat|fix|docs|refactor|test|ci|chore|perf|build"
BRANCH_PATTERN = re.compile(rf"^({TYPES})/[0-9]+-[a-z0-9]+(-[a-z0-9]+)*$")


class FakeCommands:
    """Records every external command and answers like a clean clone of `main`."""

    def __init__(self, status: str = "", branch: str = "main", counts: str = "0\t0") -> None:
        self.calls: list[tuple[list[str], Path | None]] = []
        self.status = status
        self.branch = branch
        self.counts = counts
        self.fail_on: str | None = None

    def __call__(self, args: Sequence[str], cwd: Path | None) -> subprocess.CompletedProcess[str]:
        argv = list(args)
        self.calls.append((argv, cwd))
        out = ""
        if self.fail_on and self.fail_on in argv:
            return subprocess.CompletedProcess(args, 1, "", "boom\n")
        match argv[:3]:
            case ["git", "status", "--porcelain"]:
                out = self.status
            case ["git", "symbolic-ref", *_]:
                out = "origin/main\n"
            case ["git", "branch", "--show-current"]:
                out = f"{self.branch}\n"
            case ["git", "rev-list", *_]:
                out = f"{self.counts}\n"
            case ["gh", "issue", "create"]:
                out = f"{ISSUE_URL}\n"
            case ["gh", "pr", "create"]:
                out = f"{PR_URL}\n"
            case _:
                pass
        if "copy" in argv and cwd is not None:
            (cwd / ".copier-answers.yml").write_text(ANSWERS.format(src=argv[-2]), encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, out, "")

    @property
    def commands(self) -> list[list[str]]:
        return [argv for argv, _ in self.calls]

    @property
    def programs(self) -> list[str]:
        return [" ".join(argv[:2]) for argv in self.commands]

    def find(self, *prefix: str) -> list[str]:
        return next(argv for argv in self.commands if argv[: len(prefix)] == list(prefix))


def clone_dir(projects: Path, *files: str) -> Path:
    clone = projects / CLONE
    clone.mkdir()
    for name in files:
        path = clone / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("existing\n", encoding="utf-8")
    return clone


def adopt(
    std: Standard,
    inv: dict[str, InventoryEntry],
    projects: Path,
    cmd: FakeCommands,
    **overrides: Any,
) -> list[str]:
    args: dict[str, Any] = {
        "stack": "python",
        "template": "gh:ExampleOwner/template-repo",
        "vcs_ref": "v1.1.0",
        "execute": True,
        "render_only": False,
    }
    args.update(overrides)
    return adopt_repo(
        str(args.pop("name", CLONE)),
        std=std,
        inv=inv,
        gh=Gh(OWNER, read_only=False),
        cmd=cmd,
        projects_dir=projects,
        **args,
    )


def exclusions(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == "--exclude"]


def test_never_overwrites_readme_license(
    std: Standard, inv: dict[str, InventoryEntry], tmp_path: Path
):
    clone_dir(tmp_path, "LICENSE")
    cmd = FakeCommands()
    adopt(std, inv, tmp_path, cmd)
    copier = cmd.find("copier")
    assert "--exclude" in copier
    assert {"README.md", "LICENSE"} <= set(exclusions(copier))
    assert "--overwrite" in copier


@pytest.mark.parametrize("license_file", ["LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING"])
def test_existing_license_file_is_kept(
    std: Standard, inv: dict[str, InventoryEntry], tmp_path: Path, license_file: str
):
    clone_dir(tmp_path, license_file)
    cmd = FakeCommands()
    steps = adopt(std, inv, tmp_path, cmd)
    assert "LICENSE" in exclusions(cmd.find("copier"))
    assert any("license" in s.lower() and "kept" in s.lower() for s in steps)


def test_company_license_is_kept_even_without_a_file(
    std: Standard, inv: dict[str, InventoryEntry], tmp_path: Path
):
    clone_dir(tmp_path)
    company = {CLONE: replace(inv[CLONE], license="company")}
    cmd = FakeCommands()
    adopt(std, company, tmp_path, cmd)
    copier = cmd.find("copier")
    assert "LICENSE" in exclusions(copier)
    assert "README.md" in exclusions(copier)
    assert "license=company" not in copier  # the template has no such choice


def test_missing_license_is_rendered_from_the_inventory(
    std: Standard, inv: dict[str, InventoryEntry], tmp_path: Path
):
    clone_dir(tmp_path)  # no license file at all
    cmd = FakeCommands()
    adopt(std, inv, tmp_path, cmd)
    copier = cmd.find("copier")
    assert "LICENSE" not in exclusions(copier)
    assert "README.md" in exclusions(copier)
    assert "license=mit" in copier


def test_existing_ci_yml_excluded_and_reported(
    std: Standard, inv: dict[str, InventoryEntry], tmp_path: Path
):
    clone_dir(tmp_path, ".github/workflows/ci.yml")
    cmd = FakeCommands()
    steps = adopt(std, inv, tmp_path, cmd)
    assert ".github/workflows/ci.yml" in exclusions(cmd.find("copier"))
    assert any("ci.yml" in s and "kept" in s.lower() for s in steps)


def test_no_ci_yml_means_it_is_rendered(
    std: Standard, inv: dict[str, InventoryEntry], tmp_path: Path
):
    clone_dir(tmp_path)
    cmd = FakeCommands()
    steps = adopt(std, inv, tmp_path, cmd)
    assert ".github/workflows/ci.yml" not in exclusions(cmd.find("copier"))
    assert not any("ci.yml" in s for s in steps)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"status": " M README.md\n"}, "uncommitted"),
        ({"branch": "feature"}, "main"),
        ({"counts": "2\t0"}, "unpushed"),
        ({"counts": "0\t3"}, "behind"),
    ],
)
def test_dirty_clone_aborts(
    std: Standard,
    inv: dict[str, InventoryEntry],
    tmp_path: Path,
    overrides: dict[str, str],
    reason: str,
):
    clone_dir(tmp_path)
    cmd = FakeCommands(**overrides)
    with pytest.raises(AdoptError, match=reason):
        adopt(std, inv, tmp_path, cmd)
    for banned in ("copier", "gh", "git switch", "git commit", "git push", "git add"):
        assert not any(" ".join(argv).startswith(banned) for argv in cmd.commands), banned


def test_dirty_clone_aborts_in_render_only_too(
    std: Standard, inv: dict[str, InventoryEntry], tmp_path: Path
):
    clone_dir(tmp_path)
    cmd = FakeCommands(status="?? scratch.txt\n")
    with pytest.raises(AdoptError, match="uncommitted"):
        adopt(std, inv, tmp_path, cmd, render_only=True)
    assert not any(argv[0] == "copier" for argv in cmd.commands)


def test_dry_run_runs_nothing(std: Standard, inv: dict[str, InventoryEntry], tmp_path: Path):
    clone = clone_dir(tmp_path, ".github/workflows/ci.yml")
    cmd = FakeCommands()
    steps = adopt(std, inv, tmp_path, cmd, execute=False)
    assert cmd.calls == []
    assert sorted(p.name for p in clone.iterdir()) == [".github"]
    text = "\n".join(steps)
    for expected in (
        "gh issue create",
        "chore/<n>-adopt-standard",
        "copier copy",
        "chore: adopt the engineering standard (#<n>)",
        "git push",
        "gh pr create",
    ):
        assert expected in text, expected


def test_dry_run_needs_the_clone(std: Standard, inv: dict[str, InventoryEntry], tmp_path: Path):
    with pytest.raises(AdoptError, match="no clone"):
        adopt(std, inv, tmp_path, FakeCommands(), execute=False)


def test_branch_name_matches_standard(
    std: Standard, inv: dict[str, InventoryEntry], tmp_path: Path
):
    clone_dir(tmp_path)
    cmd = FakeCommands()
    adopt(std, inv, tmp_path, cmd)
    switch = cmd.find("git", "switch")
    assert switch == ["git", "switch", "-c", "chore/42-adopt-standard"]
    assert BRANCH_PATTERN.fullmatch(switch[-1])
    assert BRANCH_PATTERN.fullmatch("chore/42-adopt-standard")
    assert not BRANCH_PATTERN.fullmatch("chore/adopt-standard")


def test_answers_from_inventory(std: Standard, inv: dict[str, InventoryEntry], tmp_path: Path):
    clone_dir(tmp_path)
    entry = inv[CLONE]
    cmd = FakeCommands()
    adopt(std, inv, tmp_path, cmd, stack="node")
    copier = cmd.find("copier")
    answers = {a for a in copier if "=" in a and not a.startswith("--")}
    assert {
        "repo_name=widget",  # the target name, not the current one
        f"description={entry.description}",
        f"kind={entry.kind}",
        "stack=node",
        f"visibility={entry.visibility}",
        f"license={entry.license}",
        f"status={entry.status}",
        "skeleton=false",
    } <= answers
    assert f"repo_name={CLONE}" not in answers
    assert copier[copier.index("--vcs-ref") + 1] == "v1.1.0"
    assert {"--defaults", "--overwrite"} <= set(copier)
    assert "gh:ExampleOwner/template-repo" in copier
    assert "." in copier


def test_execute_runs_every_step_in_order(
    std: Standard, inv: dict[str, InventoryEntry], tmp_path: Path
):
    clone = clone_dir(tmp_path)
    cmd = FakeCommands()
    steps = adopt(std, inv, tmp_path, cmd)
    assert cmd.programs == [
        "git symbolic-ref",
        "git status",
        "git branch",
        "git fetch",
        "git rev-list",
        "gh issue",
        "git switch",
        "copier copy",
        "git add",
        "git commit",
        "git push",
        "gh pr",
    ]
    assert all(cwd == clone for _, cwd in cmd.calls)
    issue = cmd.find("gh", "issue", "create")
    assert issue[issue.index("--repo") + 1] == f"{OWNER}/{CLONE}"
    labels = [issue[i + 1] for i, a in enumerate(issue) if a == "--label"]
    assert labels == ["type: chore", "status: in-progress"]
    assert issue[issue.index("--title") + 1] == "Adopt the engineering standard"
    assert cmd.find("git", "commit")[-1] == "chore: adopt the engineering standard (#42)"
    assert cmd.find("git", "push") == ["git", "push", "-u", "origin", "chore/42-adopt-standard"]
    pr = cmd.find("gh", "pr", "create")
    assert pr[pr.index("--head") + 1] == "chore/42-adopt-standard"
    assert pr[pr.index("--base") + 1] == "main"
    body = pr[pr.index("--body") + 1]
    assert "Closes #42" in body
    assert "- [x] chore" in body
    assert "AI assistance: Claude Code, rendered by jts-ops adopt" in body
    assert steps[-1] == PR_URL


def test_local_template_rewrites_src_path(
    std: Standard, inv: dict[str, InventoryEntry], tmp_path: Path
):
    clone = clone_dir(tmp_path)
    adopt(std, inv, tmp_path, FakeCommands(), template="/somewhere/template-repo")
    answers = (clone / ".copier-answers.yml").read_text(encoding="utf-8")
    assert f"_src_path: {DEFAULT_TEMPLATE}" in answers


def test_render_only_stops_after_copier_on_a_scratch_branch(
    std: Standard, inv: dict[str, InventoryEntry], tmp_path: Path
):
    clone_dir(tmp_path)
    cmd = FakeCommands()
    steps = adopt(std, inv, tmp_path, cmd, render_only=True)
    assert cmd.programs[-2:] == ["git switch", "copier copy"]
    assert cmd.find("git", "switch")[2] == "-c"
    assert "chore/" not in cmd.find("git", "switch")[-1]
    for banned in ("gh", "git push", "git commit", "git add"):
        assert not any(" ".join(argv).startswith(banned) for argv in cmd.commands), banned
    assert not any("PR" in s or "pull" in s.lower() for s in steps)


def test_render_only_runs_without_execute(
    std: Standard, inv: dict[str, InventoryEntry], tmp_path: Path
):
    clone_dir(tmp_path)
    cmd = FakeCommands()
    adopt(std, inv, tmp_path, cmd, render_only=True, execute=False)
    assert cmd.programs[-2:] == ["git switch", "copier copy"]
    assert not any(argv[0] == "gh" for argv in cmd.commands)


def test_failed_step_is_reported(std: Standard, inv: dict[str, InventoryEntry], tmp_path: Path):
    clone_dir(tmp_path)
    cmd = FakeCommands()
    cmd.fail_on = "copier"
    with pytest.raises(AdoptError, match="copier"):
        adopt(std, inv, tmp_path, cmd)
    assert not any(argv[:2] == ["git", "push"] for argv in cmd.commands)


def test_missing_tool_is_reported(std: Standard, inv: dict[str, InventoryEntry], tmp_path: Path):
    clone_dir(tmp_path)

    def missing(args: Sequence[str], cwd: Path | None) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(2, "No such file or directory", args[0])

    with pytest.raises(AdoptError, match="cannot run"):
        adopt(std, inv, tmp_path, missing)  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize(
    ("name", "overrides", "message"),
    [
        ("unknown-repo", {}, "not in the inventory"),
        ("hidden-notes", {}, "profile"),  # exempt profile: not adoptable
        (CLONE, {"stack": "go"}, "stack"),
    ],
)
def test_rejects_unadoptable_repos_before_any_call(
    std: Standard,
    inv: dict[str, InventoryEntry],
    tmp_path: Path,
    name: str,
    overrides: dict[str, Any],
    message: str,
):
    clone_dir(tmp_path)
    cmd = FakeCommands()
    with pytest.raises(AdoptError, match=message):
        adopt(std, inv, tmp_path, cmd, name=name, **overrides)
    assert cmd.calls == []


def test_cli_dry_run(fixtures_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    clone_dir(tmp_path)
    cmd = FakeCommands()
    code = main(
        [
            "adopt", CLONE, "--stack", "python",
            "--standard-dir", str(fixtures_dir), "--projects-dir", str(tmp_path),
        ],
        Gh(OWNER),
        cmd,
    )  # fmt: skip
    out = capsys.readouterr().out
    assert code == 0
    assert cmd.calls == []
    assert "chore/<n>-adopt-standard" in out
    assert "dry run" in out


def test_cli_error_exits_2(fixtures_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    cmd = FakeCommands()
    code = main(
        [
            "adopt", "unknown-repo", "--stack", "none",
            "--standard-dir", str(fixtures_dir), "--projects-dir", str(tmp_path),
        ],
        Gh(OWNER),
        cmd,
    )  # fmt: skip
    assert code == 2
    assert "not in the inventory" in capsys.readouterr().err
    assert cmd.calls == []
