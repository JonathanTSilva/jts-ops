import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from jts_ops.cli import UserPathError, _resolve_user_path, main  # pyright: ignore[reportPrivateUsage]

DASHBOARD = {"summary": {"total": 1, "compliant": 1, "percent": 100, "ci_green": 1, "ci_total": 1}}


def migrate(*extra: str) -> list[str]:
    return ["migrate", *extra]


def test_plan_outside_roots_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cwd = tmp_path / "a" / "b"
    cwd.mkdir(parents=True)
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "x.json").write_text("{}", encoding="utf-8")
    assert main(migrate("--execute", "--plan", "../../x.json")) == 2
    assert main(migrate("--plan-out", "../../x.json")) == 2


def test_plan_error_names_the_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.chdir(tmp_path)
    assert main(migrate("--execute", "--plan", "/etc/hostname")) == 2
    assert capsys.readouterr().err.startswith("jts-ops: path /etc/hostname")


def test_plan_in_reports_allowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    reports = tmp_path / "Projects" / "_reports"
    reports.mkdir(parents=True)
    plan = reports / "plan.json"
    plan.write_text("{}", encoding="utf-8")
    roots = [Path.cwd(), reports]
    assert _resolve_user_path(plan, roots=roots, must_exist=True) == plan.resolve()
    # through the CLI: the confinement passes, so the failure is the later "altered" check (3)
    args = migrate("--projects-dir", str(tmp_path / "Projects"), "--execute", "--plan", str(plan))
    assert main(args) == 3


def test_must_exist_needs_a_regular_file(tmp_path: Path):
    with pytest.raises(UserPathError, match="not a regular file"):
        _resolve_user_path(tmp_path / "missing.json", roots=[tmp_path], must_exist=True)
    with pytest.raises(UserPathError, match="not a regular file"):
        _resolve_user_path(tmp_path, roots=[tmp_path], must_exist=True)
    assert _resolve_user_path(tmp_path / "new.json", roots=[tmp_path], must_exist=False)


def test_symlink_escape_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cwd = tmp_path / "work"
    cwd.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "plan.json").write_text("{}", encoding="utf-8")
    (cwd / "link").symlink_to(outside)
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    with pytest.raises(UserPathError, match="outside the allowed folders"):
        _resolve_user_path(Path("link/plan.json"), roots=[cwd], must_exist=True)
    assert main(migrate("--execute", "--plan", "link/plan.json")) == 2


def test_audit_json_in_cwd_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    # the audit.yml sequence: badges --from site/dashboard.json --out site, from the repo root
    monkeypatch.chdir(tmp_path)
    site = tmp_path / "site"
    site.mkdir()
    (site / "dashboard.json").write_text(json.dumps(DASHBOARD), encoding="utf-8")
    assert main(["badges", "--from", "site/dashboard.json", "--out", "site"]) == 0
    assert (site / "badge-ci.json").is_file()
    assert "wrote" in capsys.readouterr().out


def test_audit_outputs_outside_roots_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cwd = tmp_path / "work"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert main(["audit", "--json", "../report.json"]) == 2
    assert main(["audit", "--markdown", "../report.md"]) == 2
    assert not (tmp_path / "report.json").exists()


def test_badges_out_outside_roots_or_a_file_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    cwd = tmp_path / "work"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (cwd / "dashboard.json").write_text(json.dumps(DASHBOARD), encoding="utf-8")
    (cwd / "afile").write_text("x", encoding="utf-8")
    assert main(["badges", "--from", "dashboard.json", "--out", str(tmp_path / "elsewhere")]) == 2
    assert main(["badges", "--from", "dashboard.json", "--out", "afile"]) == 2


def test_python_m_entrypoint():
    args: Sequence[str] = [sys.executable, "-m", "jts_ops", "--help"]
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    assert result.returncode == 0
    assert "jts-ops" in result.stdout
