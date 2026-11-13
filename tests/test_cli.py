import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from jts_ops.cli import main
from jts_ops.gh import Gh

FIXTURES = Path(__file__).parent / "fixtures"


def repo_json(name: str, private: bool) -> dict[str, object]:
    data = json.loads((FIXTURES / "gh" / "repo.json").read_text(encoding="utf-8"))
    return {**data, "name": name, "private": private}


class FakeGithub:
    """Serves a repo list and per-repo endpoints; `broken` repos answer 500 on the tree call."""

    def __init__(self, repos: list[dict[str, object]], broken: set[str]) -> None:
        self.repos = repos
        self.broken = broken
        self.paths: list[str] = []

    def __call__(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        path = next(a for a in args if a.startswith("/"))
        self.paths.append(path)
        assert args[args.index("--method") + 1] == "GET"

        def ok(body: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args, 0, json.dumps(body), "")

        if path.startswith(("/user/repos", "/users/ExampleOwner/repos")):
            page: list[object] = list(self.repos)
            return ok([page])
        name = path.split("/")[3]
        if "/git/trees/" in path:
            if name in self.broken:
                return subprocess.CompletedProcess(args, 1, "", "gh: Server Error (HTTP 500)\n")
            return ok({"tree": [{"path": "README.md", "type": "blob"}]})
        if path.endswith("/readme"):
            return ok({"encoding": "base64", "content": "IyBoaQo="})
        if "/actions/workflows/ci.yml/runs" in path:
            return ok({"workflow_runs": []})
        if path.endswith("/labels"):
            no_labels: list[object] = []
            return ok([no_labels])
        raise AssertionError(path)


@pytest.fixture
def standard_dir(tmp_path: Path) -> Path:
    for name in ("standard.yml", "labels.yml", "inventory.yml"):
        (tmp_path / name).write_bytes((FIXTURES / "standard" / name).read_bytes())
    return tmp_path


@pytest.fixture(autouse=True)
def no_real_private_inventory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("JTS_PRIVATE_INVENTORY", str(tmp_path / "absent.yml"))


def test_fetch_error_is_recorded_and_run_continues(standard_dir: Path, tmp_path: Path):
    fake = FakeGithub(
        [repo_json("ai-helper", False), repo_json("ai-broken", False), repo_json("zz-last", False)],
        broken={"ai-broken"},
    )
    out = tmp_path / "audit.json"
    code = main(
        ["audit", "--standard-dir", str(standard_dir), "--json", str(out)],
        Gh("ExampleOwner", fake),
    )
    assert code == 0
    repos = {r["name"]: r for r in json.loads(out.read_text(encoding="utf-8"))["repos"]}
    assert set(repos) == {"ai-helper", "ai-broken", "zz-last"}
    broken = repos["ai-broken"]
    assert [f["check"] for f in broken["findings"]] == ["fetch.error"]
    assert broken["findings"][0]["severity"] == "error"
    assert broken["compliant"] is False
    # The repos after the broken one were still audited.
    assert repos["zz-last"]["findings"][0]["check"] == "inventory.missing"


def test_public_only_never_fetches_or_reports_private(standard_dir: Path, tmp_path: Path):
    fake = FakeGithub(
        [repo_json("ai-helper", False), repo_json("ai-hidden", True)],
        broken={"ai-hidden"},
    )
    json_out, md_out = tmp_path / "a.json", tmp_path / "a.md"
    main(
        [
            "audit",
            "--standard-dir",
            str(standard_dir),
            "--public-only",
            "--json",
            str(json_out),
            "--markdown",
            str(md_out),
        ],
        Gh("ExampleOwner", fake),
    )
    assert not any("ai-hidden" in p for p in fake.paths)
    for out in (json_out, md_out):
        assert "ai-hidden" not in out.read_text(encoding="utf-8")
    assert json.loads(json_out.read_text(encoding="utf-8"))["summary"]["total"] == 1


def test_private_fetch_error_still_hidden_when_not_skipped(standard_dir: Path, tmp_path: Path):
    # Without --public-only the private repo is fetched and reported.
    fake = FakeGithub([repo_json("ai-hidden", True)], broken={"ai-hidden"})
    out = tmp_path / "a.json"
    main(
        ["audit", "--standard-dir", str(standard_dir), "--json", str(out)], Gh("ExampleOwner", fake)
    )
    assert "ai-hidden" in out.read_text(encoding="utf-8")


def test_repo_filter(standard_dir: Path, tmp_path: Path):
    fake = FakeGithub([repo_json("ai-helper", False), repo_json("zz-last", False)], broken=set())
    out = tmp_path / "a.json"
    main(
        ["audit", "--standard-dir", str(standard_dir), "--repo", "ai-helper", "--json", str(out)],
        Gh("ExampleOwner", fake),
    )
    assert [r["name"] for r in json.loads(out.read_text(encoding="utf-8"))["repos"]] == [
        "ai-helper"
    ]


def test_standard_dir_defaults_to_env(
    standard_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("JTS_STANDARD_DIR", str(standard_dir))
    fake = FakeGithub([repo_json("ai-helper", False)], broken=set())
    assert main(["audit"], Gh("ExampleOwner", fake)) == 0


def test_public_only_lists_through_the_public_users_endpoint(standard_dir: Path, tmp_path: Path):
    # A GITHUB_TOKEN cannot call /user/repos, and the public listing cannot see private repos.
    fake = FakeGithub([repo_json("ai-helper", False)], broken=set())
    main(["audit", "--standard-dir", str(standard_dir), "--public-only"], Gh("ExampleOwner", fake))
    assert fake.paths[0] == "/users/ExampleOwner/repos?type=owner&per_page=100"
    assert not any(p.startswith("/user/") for p in fake.paths)


def test_ci_style_dashboard_has_no_private_name(standard_dir: Path, tmp_path: Path):
    # CI has no private inventory (the autouse fixture points it at a missing file), and the
    # token may see private repos anyway. Neither the JSON, the Markdown nor the badges may
    # carry a private name, listed in the private inventory or not.
    fake = FakeGithub(
        [
            repo_json("ai-helper", False),
            repo_json("hidden-notes", True),
            repo_json("ai-secret-unlisted", True),
        ],
        broken=set(),
    )
    dashboard, md, badges = tmp_path / "dashboard.json", tmp_path / "a.md", tmp_path / "out"
    assert (
        main(
            [
                "audit",
                "--standard-dir",
                str(standard_dir),
                "--public-only",
                "--json",
                str(dashboard),
                "--markdown",
                str(md),
            ],
            Gh("ExampleOwner", fake),
        )
        == 0
    )
    assert main(["badges", "--from", str(dashboard), "--out", str(badges)]) == 0
    written = [dashboard, md, *badges.iterdir()]
    assert {p.name for p in badges.iterdir()} == {"badge-compliance.json", "badge-ci.json"}
    for path in written:
        text = path.read_text(encoding="utf-8")
        assert "hidden-notes" not in text
        assert "ai-secret-unlisted" not in text
    assert [r["name"] for r in json.loads(dashboard.read_text(encoding="utf-8"))["repos"]] == [
        "ai-helper"
    ]
    assert not any("hidden-notes" in p or "ai-secret-unlisted" in p for p in fake.paths)
