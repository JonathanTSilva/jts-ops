import os
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from jts_ops.cli import main
from jts_ops.gh import Gh
from jts_ops.inventory import InventoryEntry
from jts_ops.new import DEFAULT_TEMPLATE, ApplyFn, NewRepoError, new_repo
from jts_ops.standard import Standard

ANSWERS = (
    "# Changes here will be overwritten by Copier\n"
    "_commit: v0.1.0\n"
    "_src_path: {src}\n"
    "repo_name: ai-sample\n"
)


class FakeCommands:
    """Records every external command; `copier copy` creates the destination like the real one."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.calls: list[tuple[list[str], Path | None]] = []
        self.fail_on = fail_on

    def __call__(self, args: Sequence[str], cwd: Path | None) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(args), cwd))
        if self.fail_on and self.fail_on in args:
            return subprocess.CompletedProcess(args, 1, "", "boom\n")
        if "copy" in args:
            dst = Path(args[-1])
            dst.mkdir(parents=True)
            src = args[-2]
            (dst / ".copier-answers.yml").write_text(ANSWERS.format(src=src), encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, "", "")

    @property
    def programs(self) -> list[str]:
        return [" ".join(call[0][:2]) for call in self.calls]


class FakeGhApi:
    """Answers the repo-exists probe: 404 unless `exists`. Records every path."""

    def __init__(self, exists: bool = False) -> None:
        self.exists = exists
        self.paths: list[str] = []

    def __call__(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        path = next(a for a in args if a.startswith("/"))
        self.paths.append(path)
        assert args[args.index("--method") + 1] == "GET"
        if self.exists:
            return subprocess.CompletedProcess(args, 0, '{"name": "x"}', "")
        return subprocess.CompletedProcess(args, 1, "", "gh: Not Found (HTTP 404)\n")


def make(
    std: Standard,
    projects: Path,
    run: FakeCommands,
    gh_api: FakeGhApi | None = None,
    apply: ApplyFn | None = None,
    **overrides: Any,
) -> list[str]:
    args: dict[str, Any] = {
        "kind": "ai",
        "stack": "python",
        "visibility": "public",
        "license": "mit",
        "description": "A sample repository.",
        "template": "gh:ExampleOwner/template-repo",
        "execute": True,
        "year": 2026,
        "run": run,
    }
    args.update(overrides)
    name = str(args.pop("name", "ai-sample"))
    return new_repo(
        name,
        std=std,
        gh=Gh("ExampleOwner", gh_api or FakeGhApi()),
        projects_dir=projects,
        apply=apply,
        **args,
    )


def test_invalid_name_aborts_before_any_call(std: Standard, tmp_path: Path):
    run, api = FakeCommands(), FakeGhApi()
    with pytest.raises(NewRepoError, match="does not match"):
        make(std, tmp_path, run, api, name="Bad_Name")
    with pytest.raises(NewRepoError, match="prefix"):
        make(std, tmp_path, run, api, name="sample")  # kind ai needs the ai- prefix
    assert run.calls == []
    assert api.paths == []
    assert list(tmp_path.iterdir()) == []


def test_dry_run_lists_steps_without_running(std: Standard, tmp_path: Path):
    run, api = FakeCommands(), FakeGhApi()
    steps = make(std, tmp_path, run, api, execute=False)
    assert run.calls == []
    assert list(tmp_path.iterdir()) == []
    text = "\n".join(steps)
    for expected in (
        "copier copy",
        "uv lock",
        "git init -b main",
        "chore: initial commit from template-repo",
        "gh repo create ExampleOwner/ai-sample --public",
        "apply",
    ):
        assert expected in text
    assert steps.index(next(s for s in steps if "copier copy" in s)) < steps.index(
        next(s for s in steps if "git init" in s)
    )
    assert api.paths == ["/repos/ExampleOwner/ai-sample"]  # only a read-only existence probe


def test_existing_directory_aborts(std: Standard, tmp_path: Path):
    target = tmp_path / "ai-sample"
    target.mkdir()
    (target / "keep.txt").write_text("mine", encoding="utf-8")
    run = FakeCommands()
    with pytest.raises(NewRepoError, match="already exists"):
        make(std, tmp_path, run)
    assert run.calls == []
    assert (target / "keep.txt").read_text(encoding="utf-8") == "mine"


def test_existing_github_repo_aborts_before_any_command(std: Standard, tmp_path: Path):
    run = FakeCommands()
    with pytest.raises(NewRepoError, match="already exists on GitHub"):
        make(std, tmp_path, run, FakeGhApi(exists=True), apply=lambda _e: 0)
    assert run.calls == []


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"license": "company"}, "company"),
        ({"license": "gpl"}, "license"),
        ({"stack": "go"}, "stack"),
        ({"visibility": "internal"}, "visibility"),
        ({"status": "archived"}, "status"),
        ({"description": "  "}, "description"),
    ],
)
def test_rejects_bad_answers_before_any_call(
    std: Standard, tmp_path: Path, overrides: dict[str, Any], message: str
):
    run = FakeCommands()
    with pytest.raises(NewRepoError, match=message):
        make(std, tmp_path, run, **overrides)
    assert run.calls == []


def test_execute_runs_every_step_in_order(std: Standard, tmp_path: Path):
    run = FakeCommands()
    applied: list[str] = []
    steps = make(
        std,
        tmp_path,
        run,
        apply=lambda entry: applied.append(entry.name) or 0,
        status="active",
        has_release=True,
    )
    dst = tmp_path / "ai-sample"
    assert run.programs == [
        "copier copy",
        "uv lock",
        "git init",
        "git add",
        "git commit",
        "gh repo",
    ]
    copier = run.calls[0][0]
    assert copier[-2:] == ["gh:ExampleOwner/template-repo", str(dst)]
    assert "--defaults" in copier
    assert "--vcs-ref" not in copier
    answers = {a for a in copier if "=" in a}
    assert {
        "repo_name=ai-sample",
        "description=A sample repository.",
        "kind=ai",
        "stack=python",
        "visibility=public",
        "license=mit",
        "status=active",
        "has_release=true",
        "year=2026",
    } <= answers
    assert "company" not in " ".join(copier)
    assert run.calls[1] == (["uv", "lock"], dst)
    assert run.calls[2] == (["git", "init", "-b", "main"], dst)
    assert run.calls[4][0][-1] == "chore: initial commit from template-repo"
    assert run.calls[5] == (
        [
            "gh", "repo", "create", "ExampleOwner/ai-sample", "--public",
            "--description", "A sample repository.", "--source", ".", "--push",
        ],
        dst,
    )  # fmt: skip
    assert applied == ["ai-sample"]  # apply ran last, for this repo only
    assert "inventory.yml" in steps[-1]
    assert "inventory.private.yml" not in steps[-1]


def test_private_repo_reminder_names_the_private_file(std: Standard, tmp_path: Path):
    steps = make(std, tmp_path, FakeCommands(), visibility="private", apply=lambda _e: 0)
    assert "~/.config/jts-ops/inventory.private.yml" in steps[-1]
    assert "never the public file" in steps[-1]


@pytest.mark.parametrize(
    ("stack", "lock"),
    [
        ("python", ["uv", "lock"]),
        ("node", ["npx", "--yes", "pnpm@9", "install", "--lockfile-only"]),
        ("shell", None),
        ("none", None),
    ],
)
def test_lock_step_per_stack(std: Standard, tmp_path: Path, stack: str, lock: list[str] | None):
    run = FakeCommands()
    make(std, tmp_path, run, stack=stack, apply=lambda _e: 0)
    commands = [c[0] for c in run.calls]
    if lock is None:
        assert not any(c[0] in ("uv", "npx") for c in commands)
    else:
        assert commands[1] == lock
        assert commands.index(lock) < commands.index(["git", "add", "-A"])  # lock before commit


def test_local_template_source_is_rewritten_for_copier_update(std: Standard, tmp_path: Path):
    local = tmp_path / "tpl"
    local.mkdir()
    projects = tmp_path / "projects"
    projects.mkdir()
    make(std, projects, FakeCommands(), template=str(local), vcs_ref="HEAD", apply=lambda _e: 0)
    answers = (projects / "ai-sample" / ".copier-answers.yml").read_text(encoding="utf-8")
    assert f"_src_path: {DEFAULT_TEMPLATE}\n" in answers
    assert str(local) not in answers
    assert "_commit: v0.1.0" in answers  # the rest is untouched


def test_remote_template_source_is_left_alone(std: Standard, tmp_path: Path):
    make(std, tmp_path, FakeCommands(), apply=lambda _e: 0)
    answers = (tmp_path / "ai-sample" / ".copier-answers.yml").read_text(encoding="utf-8")
    assert "_src_path: gh:ExampleOwner/template-repo\n" in answers


def test_vcs_ref_is_passed_to_copier(std: Standard, tmp_path: Path):
    run = FakeCommands()
    make(std, tmp_path, run, vcs_ref="HEAD", apply=lambda _e: 0)
    copier = run.calls[0][0]
    assert copier[copier.index("--vcs-ref") + 1] == "HEAD"


def test_render_only_stops_before_github(std: Standard, tmp_path: Path):
    run = FakeCommands()
    applied: list[str] = []
    steps = make(std, tmp_path, run, render_only=True, apply=lambda e: applied.append(e.name) or 0)
    assert run.programs[-1] == "git commit"
    assert applied == []
    assert not any("gh repo create" in s for s in steps)


def test_failing_step_stops_and_names_the_step(std: Standard, tmp_path: Path):
    run = FakeCommands(fail_on="lock")
    with pytest.raises(NewRepoError, match="uv lock.*boom"):
        make(std, tmp_path, run, apply=lambda _e: 0)
    assert run.programs == ["copier copy", "uv lock"]
    assert (tmp_path / "ai-sample").is_dir()  # left in place for inspection, never deleted


def test_failing_apply_is_reported(std: Standard, tmp_path: Path):
    with pytest.raises(NewRepoError, match="apply"):
        make(std, tmp_path, FakeCommands(), apply=lambda _e: 1)


def test_cli_dry_run_prints_steps(
    fixtures_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    run = FakeCommands()
    code = main(
        [
            "new", "ai-sample", "--kind", "ai", "--stack", "python", "--visibility", "public",
            "--license", "mit", "--description", "A sample.",
            "--standard-dir", str(fixtures_dir), "--projects-dir", str(tmp_path),
        ],
        Gh("ExampleOwner", FakeGhApi()),
        run,
    )  # fmt: skip
    out = capsys.readouterr().out
    assert code == 0
    assert run.calls == []
    assert "copier copy" in out
    assert "dry run" in out
    assert DEFAULT_TEMPLATE in out


def test_cli_invalid_name_exits_2(
    fixtures_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    run = FakeCommands()
    code = main(
        [
            "new", "Bad_Name", "--kind", "ai", "--stack", "none", "--visibility", "public",
            "--license", "mit", "--description", "x",
            "--standard-dir", str(fixtures_dir), "--projects-dir", str(tmp_path),
        ],
        Gh("ExampleOwner", FakeGhApi()),
        run,
    )  # fmt: skip
    assert code == 2
    assert "does not match" in capsys.readouterr().err
    assert run.calls == []


def test_cli_rejects_company_license(fixtures_dir: Path, tmp_path: Path):
    with pytest.raises(SystemExit):
        main(
            [
                "new", "ai-sample", "--kind", "ai", "--stack", "none", "--visibility", "public",
                "--license", "company", "--description", "x",
                "--standard-dir", str(fixtures_dir), "--projects-dir", str(tmp_path),
            ],
            Gh("ExampleOwner", FakeGhApi()),
            FakeCommands(),
        )  # fmt: skip


def test_entry_passed_to_apply_describes_the_new_repo(std: Standard, tmp_path: Path):
    seen: list[InventoryEntry] = []
    make(
        std,
        tmp_path,
        FakeCommands(),
        status="active",
        apply=lambda entry: seen.append(entry) or 0,
    )
    (entry,) = seen
    assert (entry.name, entry.target, entry.kind, entry.visibility) == (
        "ai-sample",
        "ai-sample",
        "ai",
        "public",
    )
    assert (entry.profile, entry.fate, entry.license, entry.status) == (
        "active",
        "keep",
        "mit",
        "active",
    )
    assert entry.description == "A sample repository."


TEMPLATE_DIR = Path(os.environ.get("JTS_TEMPLATE_DIR", "~/Projects/TP-Repository")).expanduser()
COPIER = os.environ.get("JTS_COPIER") or str(TEMPLATE_DIR / ".venv" / "bin" / "copier")


@pytest.mark.skipif(
    os.environ.get("JTS_NEW_INTEGRATION") != "1",
    reason="opt-in: set JTS_NEW_INTEGRATION=1 (real copier, uv lock and git; no GitHub)",
)
@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
@pytest.mark.parametrize("stack", ["python", "none"])
def test_integration_render_lock_and_commit_locally(
    std: Standard, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stack: str
):
    monkeypatch.setenv("JTS_COPIER", COPIER)
    for var, value in (
        ("GIT_AUTHOR_NAME", "Test"),
        ("GIT_AUTHOR_EMAIL", "test@example.invalid"),
        ("GIT_COMMITTER_NAME", "Test"),
        ("GIT_COMMITTER_EMAIL", "test@example.invalid"),
    ):
        monkeypatch.setenv(var, value)
    gh_api = FakeGhApi()
    steps = new_repo(
        "ai-sample",
        kind="ai",
        stack=stack,
        visibility="public",
        license="mit",
        description="A sample repository.",
        std=std,
        gh=Gh("ExampleOwner", gh_api),
        projects_dir=tmp_path,
        template=str(TEMPLATE_DIR),
        vcs_ref="HEAD",
        execute=True,
        render_only=True,
    )
    dst = tmp_path / "ai-sample"
    assert not any("gh repo create" in s for s in steps)
    assert (dst / "README.md").is_file()
    assert (dst / "AGENTS.md").is_file()
    assert (dst / "uv.lock").is_file() == (stack == "python")
    answers = (dst / ".copier-answers.yml").read_text(encoding="utf-8")
    assert f"_src_path: {DEFAULT_TEMPLATE}\n" in answers
    log = subprocess.run(
        ["git", "log", "--format=%s"], cwd=dst, capture_output=True, text=True, check=True
    )
    assert log.stdout.strip() == "chore: initial commit from template-repo"
    assert gh_api.paths == []  # render-only never touches GitHub
