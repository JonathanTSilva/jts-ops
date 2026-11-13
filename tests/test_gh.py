import json
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from jts_ops.gh import Gh, GhError, fetch_state, list_repos
from jts_ops.state import LabelInfo, RepoState

FIXTURES = Path(__file__).parent / "fixtures" / "gh"

# A route value is a fixture file name (success) or an (HTTP status, stderr message) pair.
Route = str | tuple[int, str]


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeRunner:
    """Answers `gh` calls from canned routes keyed by a substring of the API path."""

    def __init__(self, routes: dict[str, Route]) -> None:
        self.routes = routes
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        path = next(a for a in args if a.startswith("/"))
        # An exact path wins over a substring match.
        keys = [k for k in self.routes if k == path] or [k for k in self.routes if k in path]
        if keys:
            route = self.routes[keys[0]]
            if isinstance(route, str):
                return subprocess.CompletedProcess(args, 0, fixture(route), "")
            status, message = route
            return subprocess.CompletedProcess(args, 1, "", f"gh: {message} (HTTP {status})\n")
        raise AssertionError(f"unrouted gh call: {args}")


def happy_routes() -> dict[str, Route]:
    return {
        "/git/trees/": "tree.json",
        "/readme": "readme.json",
        "/actions/workflows/ci.yml/runs": "runs.json",
        "/labels": "labels.json",
    }


def repo() -> dict[str, Any]:
    return json.loads(fixture("repo.json"))


def test_fetch_state_full():
    gh = Gh("ExampleOwner", FakeRunner(happy_routes()))
    state = fetch_state(gh, repo())
    assert state == RepoState(
        name="example-repo",
        private=False,
        archived=False,
        description="Example repo used by the test suite",
        topics=("tool", "example"),
        has_wiki=False,
        has_discussions=True,
        allow_squash_merge=True,
        allow_merge_commit=False,
        allow_rebase_merge=False,
        delete_branch_on_merge=True,
        default_branch="main",
        license_spdx="MIT",
        files=state.files,
        readme="# Example Repo\n\nA repo that exists only in tests.\n",
        ci_conclusion="success",
        labels=("bug", "type: feat", "status: wip"),
        label_details=(
            LabelInfo("bug", "d73a4a", "Something isn't working"),
            LabelInfo("type: feat", "a2eeef", "New feature"),
            LabelInfo("status: wip", "fbca04", "Work in progress"),
        ),
    )
    assert state.files is not None
    assert "AGENTS.md" in state.files
    assert ".github/workflows/ci.yml" in state.files
    assert "src" not in state.files  # directories are not files
    assert state.readme is not None and state.readme.startswith("# ")


def test_fetch_state_requests_expected_endpoints():
    runner = FakeRunner(happy_routes())
    fetch_state(Gh("ExampleOwner", runner), repo())
    paths = [next(a for a in call if a.startswith("/")) for call in runner.calls]
    assert paths == [
        "/repos/ExampleOwner/example-repo/git/trees/main?recursive=1",
        "/repos/ExampleOwner/example-repo/readme",
        "/repos/ExampleOwner/example-repo/actions/workflows/ci.yml/runs?branch=main&status=completed&per_page=1",
        "/repos/ExampleOwner/example-repo/labels",
    ]
    assert "--paginate" in runner.calls[3]


def test_empty_repo_yields_none_files():
    routes = happy_routes()
    routes["/git/trees/"] = (409, "Git Repository is empty.")
    routes["/readme"] = (404, "Not Found")
    state = fetch_state(Gh("ExampleOwner", FakeRunner(routes)), repo())
    assert state.files is None
    assert state.readme is None


def test_missing_readme():
    routes = happy_routes()
    routes["/readme"] = (404, "Not Found")
    state = fetch_state(Gh("ExampleOwner", FakeRunner(routes)), repo())
    assert state.readme is None
    assert state.files is not None


def test_no_completed_ci_runs():
    routes = happy_routes()
    routes["/actions/workflows/ci.yml/runs"] = "runs_empty.json"
    state = fetch_state(Gh("ExampleOwner", FakeRunner(routes)), repo())
    assert state.ci_conclusion is None


def test_list_shaped_repo_is_completed_from_repo_endpoint():
    # /user/repos omits merge settings; fetch_state must fetch the full repo for them.
    listed = {
        k: v
        for k, v in repo().items()
        if k
        not in {
            "allow_squash_merge",
            "allow_merge_commit",
            "allow_rebase_merge",
            "delete_branch_on_merge",
        }
    }
    routes = happy_routes()
    routes["/repos/ExampleOwner/example-repo"] = "repo.json"
    runner = FakeRunner(routes)
    state = fetch_state(Gh("ExampleOwner", runner), listed)
    assert state.allow_squash_merge is True
    assert state.allow_merge_commit is False
    assert state.delete_branch_on_merge is True


def test_merge_settings_hidden_from_a_read_only_token_are_not_flagged():
    # Without push access the repo endpoint omits the merge settings; they are unknown, so
    # they default to the standard values and cannot raise a finding.
    hidden = {
        k: v
        for k, v in repo().items()
        if k
        not in {
            "allow_squash_merge",
            "allow_merge_commit",
            "allow_rebase_merge",
            "delete_branch_on_merge",
        }
    }
    routes = happy_routes()
    routes["/repos/ExampleOwner/example-repo"] = "repo_no_settings.json"
    state = fetch_state(Gh("ExampleOwner", FakeRunner(routes)), hidden)
    assert (state.allow_squash_merge, state.allow_merge_commit) == (True, False)
    assert (state.allow_rebase_merge, state.delete_branch_on_merge) == (False, True)


@pytest.mark.parametrize("status", [403, 404])
def test_runs_unavailable_gives_no_ci_conclusion(status: int):
    # 404 also covers a repo with no ci.yml workflow.
    routes = happy_routes()
    routes["/actions/workflows/ci.yml/runs"] = (status, "Actions unavailable")
    state = fetch_state(Gh("ExampleOwner", FakeRunner(routes)), repo())
    assert state.ci_conclusion is None


def test_readme_decoded_with_replacement_on_bad_bytes():
    routes = happy_routes()
    routes["/readme"] = "readme_badbytes.json"
    state = fetch_state(Gh("ExampleOwner", FakeRunner(routes)), repo())
    assert state.readme == "# Title \ufffd\ufffd broken\n"


def test_readme_with_unknown_encoding_is_none():
    routes = happy_routes()
    routes["/readme"] = "readme_unencoded.json"
    state = fetch_state(Gh("ExampleOwner", FakeRunner(routes)), repo())
    assert state.readme is None


def test_truncated_tree_gives_no_files():
    routes = happy_routes()
    routes["/git/trees/"] = "tree_truncated.json"
    state = fetch_state(Gh("ExampleOwner", FakeRunner(routes)), repo())
    assert state.files is None


def test_license_none_when_repo_has_no_license():
    data = repo()
    data["license"] = None
    state = fetch_state(Gh("ExampleOwner", FakeRunner(happy_routes())), data)
    assert state.license_spdx is None


def test_other_errors_raise():
    routes = happy_routes()
    routes["/actions/workflows/ci.yml/runs"] = (500, "Server Error")
    with pytest.raises(GhError) as info:
        fetch_state(Gh("ExampleOwner", FakeRunner(routes)), repo())
    assert info.value.status == 500
    assert "Server Error" in info.value.message


def test_error_without_status_has_none():
    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 4, "", "gh: To use GitHub CLI, run: gh auth login")

    with pytest.raises(GhError) as info:
        Gh("ExampleOwner", runner).api("/user")
    assert info.value.status is None


def test_paginate_flag_passed_and_pages_flattened():
    runner = FakeRunner({"/user/repos": "repos_slurp.json"})
    repos = list_repos(Gh("ExampleOwner", runner))
    assert "--paginate" in runner.calls[0]
    assert "--slurp" in runner.calls[0]
    assert "/user/repos?affiliation=owner&per_page=100" in runner.calls[0]
    assert [r["name"] for r in repos] == ["example-repo", "example-second"]


def test_list_repos_public_only_uses_the_users_endpoint():
    runner = FakeRunner({"/users/ExampleOwner/repos": "repos_slurp.json"})
    list_repos(Gh("ExampleOwner", runner), public_only=True)
    assert "/users/ExampleOwner/repos?type=owner&per_page=100" in runner.calls[0]


def test_api_passes_method_and_fields():
    runner = FakeRunner({"/repos/x": "runs_empty.json"})
    Gh("ExampleOwner", runner, read_only=False).api(
        "/repos/x", method="PATCH", fields={"description": "hi", "private": True}
    )
    args = runner.calls[0]
    assert args[args.index("--method") + 1] == "PATCH"
    assert "description=hi" in args
    assert "private=true" in args


def test_empty_stdout_returns_none():
    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, "", "")

    assert Gh("ExampleOwner", runner, read_only=False).api("/repos/x", method="PUT") is None


def test_read_only_gh_refuses_writes_before_calling_the_runner():
    runner = FakeRunner({"/repos/x": "runs_empty.json"})
    gh = Gh("ExampleOwner", runner)
    for method in ("PATCH", "PUT", "POST", "DELETE"):
        with pytest.raises(GhError, match="read-only"):
            gh.api("/repos/x", method=method, body={"a": 1})
    assert runner.calls == []
    gh.api("/repos/x")  # GET still works
    assert len(runner.calls) == 1
