import datetime
import re
import subprocess
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from jts_ops.adopt import TEMPLATE_REF, AdoptError, adopt_repo
from jts_ops.cli import main
from jts_ops.gh import Gh
from jts_ops.inventory import InventoryEntry
from jts_ops.new import DEFAULT_TEMPLATE
from jts_ops.standard import Standard

OWNER = "ExampleOwner"
CLONE = "old-widget"  # current name; the inventory target is "widget"
ISSUE_URL = f"https://github.com/{OWNER}/{CLONE}/issues/42"
PR_URL = f"https://github.com/{OWNER}/{CLONE}/pull/43"
ANSWERS = "_commit: v1.1.1\n_src_path: {src}\nrepo_name: widget\nskeleton: {skeleton}\n"

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
        self.copier_output = ""  # what copier prints, for example `conflict .gitignore` lines
        self.diff = ""  # tracked-file modifications seen after copier
        self.skeleton = "false"  # what the rendered answers file records

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
            case ["git", "diff", "--name-only"]:
                out = self.diff
            case ["gh", "issue", "create"]:
                out = f"{ISSUE_URL}\n"
            case ["gh", "pr", "create"]:
                out = f"{PR_URL}\n"
            case _:
                pass
        if "copy" in argv and cwd is not None:
            answers = ANSWERS.format(src=argv[-2], skeleton=self.skeleton)
            (cwd / ".copier-answers.yml").write_text(answers, encoding="utf-8")
            out = self.copier_output
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
        "vcs_ref": "v1.1.1",
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
    clone_dir(tmp_path, "README.md", "LICENSE")
    cmd = FakeCommands()
    adopt(std, inv, tmp_path, cmd)
    copier = cmd.find("copier")
    assert "--exclude" in copier
    assert {"README.md", "LICENSE"} <= set(exclusions(copier))
    assert "--overwrite" not in copier
    assert "-w" not in copier
    assert copier[copier.index("--skip") + 1] == "**"


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
    clone_dir(tmp_path, "README.md")
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
    clone_dir(tmp_path, "README.md")  # no license file at all
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
    assert copier[copier.index("--vcs-ref") + 1] == "v1.1.1"
    assert "--defaults" in copier
    assert "--overwrite" not in copier
    assert {"has_release=false", f"year={datetime.date.today().year}"} <= answers
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
        "git diff",
        "git ls-files",
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
    assert cmd.programs[-4:] == ["git switch", "copier copy", "git diff", "git ls-files"]
    assert cmd.find("git", "switch")[2] == "-c"
    branch = cmd.find("git", "switch")[-1]
    assert re.fullmatch(r"scratch/adopt-standard-\d{14}", branch)
    for banned in ("gh", "git push", "git commit", "git add"):
        assert not any(" ".join(argv).startswith(banned) for argv in cmd.commands), banned
    assert not any("PR" in s or "pull" in s.lower() for s in steps)


def test_render_only_runs_without_execute(
    std: Standard, inv: dict[str, InventoryEntry], tmp_path: Path
):
    clone_dir(tmp_path)
    cmd = FakeCommands()
    adopt(std, inv, tmp_path, cmd, render_only=True, execute=False)
    assert "copier copy" in cmd.programs
    assert not any(argv[0] == "gh" for argv in cmd.commands)


CONFLICT_OUTPUT = (
    "Copying from template version 1.1.0\n"
    "\x1b[1m    conflict\x1b[0m  .gitignore\n"
    "\x1b[1m        skip\x1b[0m  .gitignore\n"
    "\x1b[1m    conflict\x1b[0m  .editorconfig\n"
    "\x1b[1m      create\x1b[0m  AGENTS.md\n"
)


def test_conflicts_reach_the_steps_and_the_pr_body(
    std: Standard, inv: dict[str, InventoryEntry], tmp_path: Path
):
    clone_dir(tmp_path, ".gitignore", ".editorconfig")
    cmd = FakeCommands()
    cmd.copier_output = CONFLICT_OUTPUT
    steps = adopt(std, inv, tmp_path, cmd)
    note = "exists and differs from the standard; not replaced, merge by hand"
    for path in (".gitignore", ".editorconfig"):
        assert f"kept: {path} {note}" in steps
    assert not any("AGENTS.md" in s and "kept" in s for s in steps)
    pr = cmd.find("gh", "pr", "create")
    body = pr[pr.index("--body") + 1]
    left_alone = body.split("Left alone:")[1].split("## Linked issue")[0]
    assert f"- .gitignore {note}" in left_alone
    assert f"- .editorconfig {note}" in left_alone


def test_existing_gitignore_is_reported(
    std: Standard, inv: dict[str, InventoryEntry], tmp_path: Path
):
    clone_dir(tmp_path, ".gitignore")
    cmd = FakeCommands()
    cmd.copier_output = "    conflict  .gitignore\n        skip  .gitignore\n"
    steps = adopt(std, inv, tmp_path, cmd, render_only=True)
    assert any(s.startswith("kept: .gitignore exists and differs") for s in steps)
    assert (tmp_path / CLONE / ".gitignore").read_text(encoding="utf-8") == "existing\n"


def test_tracked_file_modification_aborts_before_staging(
    std: Standard, inv: dict[str, InventoryEntry], tmp_path: Path
):
    clone_dir(tmp_path)
    cmd = FakeCommands()
    cmd.diff = ".gitignore\nsrc/app.py\n"
    with pytest.raises(AdoptError, match=r"modified existing files.*\.gitignore, src/app\.py"):
        adopt(std, inv, tmp_path, cmd)
    for banned in ("git add", "git commit", "git push", "gh pr"):
        assert not any(" ".join(argv).startswith(banned) for argv in cmd.commands), banned


def test_skeleton_not_recorded_false_aborts(
    std: Standard, inv: dict[str, InventoryEntry], tmp_path: Path
):
    clone_dir(tmp_path)
    cmd = FakeCommands()
    cmd.skeleton = "true"  # an older template ref that ignores the switch
    with pytest.raises(AdoptError, match="skeleton: false"):
        adopt(std, inv, tmp_path, cmd)
    assert not any(" ".join(argv).startswith("git add") for argv in cmd.commands)


def test_template_ref_defaults_to_the_pinned_tag(
    std: Standard, inv: dict[str, InventoryEntry], tmp_path: Path
):
    clone_dir(tmp_path)
    cmd = FakeCommands()
    adopt_repo(
        CLONE,
        stack="python",
        std=std,
        inv=inv,
        gh=Gh(OWNER, read_only=False),
        cmd=cmd,
        projects_dir=tmp_path,
        execute=True,
    )
    copier = cmd.find("copier")
    assert copier[copier.index("--vcs-ref") + 1] == TEMPLATE_REF == "v1.2.0"


def test_readme_only_excluded_when_it_exists(
    std: Standard, inv: dict[str, InventoryEntry], tmp_path: Path
):
    clone_dir(tmp_path)  # no README
    cmd = FakeCommands()
    steps = adopt(std, inv, tmp_path, cmd)
    assert "README.md" not in exclusions(cmd.find("copier"))
    assert not any("README.md kept" in s for s in steps)


def test_failure_names_what_was_already_created(
    std: Standard, inv: dict[str, InventoryEntry], tmp_path: Path
):
    clone_dir(tmp_path)
    cmd = FakeCommands()
    cmd.fail_on = "push"
    with pytest.raises(AdoptError) as info:
        adopt(std, inv, tmp_path, cmd)
    message = str(info.value)
    for expected in ("issue #42", "chore/42-adopt-standard", "rendered files", "a local commit"):
        assert expected in message, expected


@pytest.mark.parametrize(
    ("files", "workflow", "expected"),
    [
        ((), None, "false"),
        (("release-please-config.json",), None, "true"),
        ((".release-please-manifest.json",), None, "true"),
        ((), "uses: googleapis/release-please-action@v4\n", "true"),
        ((), "uses: actions/checkout@v4\n", "false"),
    ],
)
def test_has_release_from_release_please_signals(
    std: Standard,
    inv: dict[str, InventoryEntry],
    tmp_path: Path,
    files: tuple[str, ...],
    workflow: str | None,
    expected: str,
):
    clone = clone_dir(tmp_path, *files)
    if workflow is not None:
        (clone / ".github/workflows").mkdir(parents=True)
        (clone / ".github/workflows/release.yaml").write_text(workflow, encoding="utf-8")
    cmd = FakeCommands()
    adopt(std, inv, tmp_path, cmd, year=2031)
    copier = cmd.find("copier")
    assert f"has_release={expected}" in copier
    assert "year=2031" in copier


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
        (CLONE, {"stack": "cobol"}, "stack"),
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


def test_adopt_brands_before_the_commit(
    std: Standard, inv: dict[str, InventoryEntry], tmp_path: Path
):
    from jts_ops.brand import FOOTER_END, HEADER_START

    clone = clone_dir(tmp_path, "README.md")
    (clone / "README.md").write_text("# Widget\n\nBody.\n", encoding="utf-8")
    cmd = FakeCommands()
    steps = adopt(std, inv, tmp_path, cmd)
    texts = [s for s in steps if not s.startswith("kept:")]
    brand_at = next(i for i, s in enumerate(texts) if s.startswith("brand:"))
    assert texts[brand_at - 1].startswith("copier") or "copier" in texts[brand_at - 1]
    assert brand_at < next(i for i, s in enumerate(texts) if s.startswith("git add"))
    readme = (clone / "README.md").read_text(encoding="utf-8")
    assert readme.startswith(HEADER_START) and "# Widget\n\nBody." in readme
    assert readme.rstrip().endswith(FOOTER_END)
    assert len(list((clone / "assets" / "brand").glob("header-*.svg"))) == 2
    assert any("README.md kept" in s for s in steps)


def test_adopt_dry_run_does_not_brand(
    std: Standard, inv: dict[str, InventoryEntry], tmp_path: Path
):
    clone = clone_dir(tmp_path, "README.md")
    steps = adopt(std, inv, tmp_path, FakeCommands(), execute=False)
    assert any(s.startswith("brand:") for s in steps)
    assert not (clone / "assets").exists()
    assert (clone / "README.md").read_text(encoding="utf-8") == "existing\n"
