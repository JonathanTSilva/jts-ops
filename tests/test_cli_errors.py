"""CLI error paths: GitHub unreachable, bad directories, project errors."""

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from jts_ops import cli
from jts_ops.cli import main
from jts_ops.gh import Gh, GhError, MissingScopeError
from jts_ops.project import ProjectError

FIXTURES = Path(__file__).parent / "fixtures"


def broken_runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args, 1, "", "gh: Server Error (HTTP 500)\n")


def unavailable_runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args, 1, "", "gh: Not Found (HTTP 404)\n")


@pytest.fixture
def standard_dir(tmp_path: Path) -> Path:
    for name in ("standard.yml", "labels.yml", "inventory.yml"):
        (tmp_path / name).write_bytes((FIXTURES / "standard" / name).read_bytes())
    return tmp_path


@pytest.fixture(autouse=True)
def _no_private_inventory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("JTS_PRIVATE_INVENTORY", str(tmp_path / "none.yml"))


def broken_gh() -> Gh:
    return Gh("ExampleOwner", broken_runner)


def test_audit_reports_when_github_cannot_be_listed(
    standard_dir: Path, capsys: pytest.CaptureFixture[str]
):
    code = main(["audit", "--standard-dir", str(standard_dir)], broken_gh())
    assert code == 1
    assert "cannot list repos" in capsys.readouterr().err


def test_apply_reports_when_github_cannot_be_listed(
    standard_dir: Path, capsys: pytest.CaptureFixture[str]
):
    code = main(["apply", "--standard-dir", str(standard_dir)], broken_gh())
    assert code == 1
    assert "cannot list repos" in capsys.readouterr().err


def test_codeql_state_is_unavailable_on_403_and_404():
    assert cli._codeql_state(Gh("ExampleOwner", unavailable_runner), "r") == "unavailable"  # pyright: ignore[reportPrivateUsage]


def test_codeql_state_reraises_other_errors():
    with pytest.raises(GhError):
        cli._codeql_state(broken_gh(), "r")  # pyright: ignore[reportPrivateUsage]


def test_codeql_state_reads_the_setup_state():
    def ok(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, json.dumps({"state": "configured"}), "")

    assert cli._codeql_state(Gh("ExampleOwner", ok), "r") == "configured"  # pyright: ignore[reportPrivateUsage]


def test_vault_sync_rejects_a_missing_vault(
    standard_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    argv = ["vault-sync", "--standard-dir", str(standard_dir), "--vault", str(tmp_path / "nope")]
    assert main(argv, broken_gh()) == 2
    assert "no such vault" in capsys.readouterr().err


def test_vault_sync_reports_when_github_cannot_be_read(
    standard_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    vault = tmp_path / "vault"
    vault.mkdir()
    argv = ["vault-sync", "--standard-dir", str(standard_dir), "--vault", str(vault)]
    assert main(argv, broken_gh()) == 1
    assert "cannot read GitHub" in capsys.readouterr().err


@pytest.mark.parametrize("notify", [False, True])
def test_sync_reports_when_github_cannot_be_read(
    notify: bool, standard_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    projects = tmp_path / "projects"
    projects.mkdir()
    argv = ["sync", "--standard-dir", str(standard_dir), "--projects-dir", str(projects)]
    calls: list[Sequence[str]] = []

    def run(args: Sequence[str], cwd: Path | None) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    assert main([*argv, *(["--notify"] if notify else [])], broken_gh(), run) == 1
    assert "cannot read GitHub" in capsys.readouterr().err
    assert bool(calls) is notify  # a desktop notification only when asked


@pytest.mark.parametrize(
    ("error", "needle"),
    [
        (MissingScopeError("scope"), cli.SCOPE_HELP),
        (ProjectError("no project"), "no project"),
        (GhError("boom"), "GitHub call failed: boom"),
    ],
)
@pytest.mark.parametrize("sub", ["ensure", "sync"])
def test_project_maps_errors_to_exit_1(
    sub: str,
    error: GhError | ProjectError,
    needle: str,
    standard_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    def raiser(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(cli, "ensure_project", raiser)
    monkeypatch.setattr(cli, "sync_project", raiser)
    assert main(["project", sub, "--standard-dir", str(standard_dir)], broken_gh()) == 1
    assert needle in capsys.readouterr().err


def test_main_module_runs_console_main(monkeypatch: pytest.MonkeyPatch):
    import runpy

    monkeypatch.setattr("sys.argv", ["jts-ops", "--help"])
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_module("jts_ops", run_name="__main__")
    assert exit_info.value.code == 0
