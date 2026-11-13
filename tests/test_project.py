import json
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from jts_ops.cli import main
from jts_ops.gh import Gh, GhError, MissingScopeError, ReadOnlyError
from jts_ops.inventory import InventoryEntry
from jts_ops.project import (
    PRIORITY,
    SIZE,
    STATUS,
    ProjectError,
    ensure_project,
    sync_project,
    wanted_selects,
)
from jts_ops.standard import Standard

OWNER = "ExampleOwner"
PAGE = 2  # a tiny page, so the items query has to paginate

REPOS = [
    {"name": "ai-helper", "private": False, "archived": False},
    {"name": "widget-clone", "private": True, "archived": False},
    {"name": "unlisted-tool", "private": False, "archived": False},
    {"name": "old-widget", "private": False, "archived": True},
]
ISSUES: dict[str, list[dict[str, Any]]] = {
    "ai-helper": [
        {"number": 1, "title": "Retry", "node_id": "I_1"},
        {"number": 2, "title": "Fix", "node_id": "PR_2", "pull_request": {}},
    ],
    "widget-clone": [{"number": 3, "title": "Hidden", "node_id": "I_3"}],
    "unlisted-tool": [{"number": 4, "title": "Free", "node_id": "I_4"}],
    "old-widget": [{"number": 5, "title": "Old", "node_id": "I_5"}],
}


class FakeProjects:
    """A fake `gh` with an in-memory Projects v2 backend and REST listings."""

    def __init__(self, *, project: bool = False, scope: bool = True) -> None:
        self.n = 0
        self.has_project = project
        self.scope = scope
        self.fields: list[dict[str, Any]] = []
        if project:  # what GitHub gives a new project: a Status with its three defaults
            self.fields.append(self._select("Status", ["Todo", "In Progress", "Done", "Parked"]))
        self.items: list[dict[str, Any]] = []
        self.requests: list[str] = []  # the GraphQL documents sent, in order

    def _id(self, prefix: str) -> str:
        self.n += 1
        return f"{prefix}_{self.n}"

    def _select(self, name: str, options: Sequence[str]) -> dict[str, Any]:
        return {
            "__typename": "ProjectV2SingleSelectField",
            "id": self._id("F"),
            "name": name,
            "options": [
                {"id": self._id("O"), "name": o, "color": "GRAY", "description": ""}
                for o in options
            ],
        }

    @property
    def mutations(self) -> list[str]:
        return [r for r in self.requests if r.startswith("mutation")]

    def field(self, name: str) -> dict[str, Any]:
        return next(f for f in self.fields if f["name"] == name)

    def option_names(self, name: str) -> list[str]:
        return [o["name"] for o in self.field(name)["options"]]

    def option_id(self, field: str, option: str) -> str:
        return next(o["id"] for o in self.field(field)["options"] if o["name"] == option)

    def __call__(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        def ok(body: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args, 0, json.dumps(body), "")

        path = next(a for a in args if a.startswith("/") or a == "graphql")
        if path == "graphql":
            if not self.scope:
                return subprocess.CompletedProcess(
                    args,
                    1,
                    "",
                    "gh: Your token has not been granted the required scopes to execute "
                    "this query. The 'id' field requires one of the following scopes: "
                    "['read:project'], but your token has only been granted the: ['repo'] "
                    "scopes.\n",
                )
            body = json.loads(Path(args[args.index("--input") + 1]).read_text())
            self.requests.append(body["query"].lstrip())
            return ok({"data": self.graphql(body["query"], body["variables"])})
        if path.startswith("/user/repos"):
            return ok([REPOS])
        name = path.split("/")[3]
        return ok([ISSUES[name]])

    def project_node(self) -> dict[str, Any]:
        return {
            "id": "P_1",
            "number": 1,
            "title": "Engineering",
            "url": "https://example.invalid/p/1",
            "fields": {"nodes": self.fields},
        }

    def graphql(self, query: str, v: dict[str, Any]) -> dict[str, Any]:  # noqa: C901
        if "projectsV2(" in query:
            nodes = [self.project_node()] if self.has_project else []
            return {"user": {"id": "U_1", "projectsV2": {"nodes": nodes}}}
        if "items(first" in query:
            start = int(v["after"] or 0)
            page = self.items[start : start + PAGE]
            more = start + PAGE < len(self.items)
            nodes = [{"id": i["id"], "content": {"id": i["content"]}} for i in page]
            info = {"hasNextPage": more, "endCursor": str(start + PAGE)}
            return {"node": {"items": {"pageInfo": info, "nodes": nodes}}}
        if "createProjectV2(" in query:
            self.has_project = True
            return {"createProjectV2": {"projectV2": {"id": "P_1"}}}
        if "dataType: SINGLE_SELECT" in query:
            self.fields.append(self._select(v["name"], [o["name"] for o in v["options"]]))
            return {"createProjectV2Field": {}}
        if "dataType: ITERATION" in query:
            assert v["days"] == 14
            self.fields.append(
                {
                    "__typename": "ProjectV2IterationField",
                    "id": self._id("F"),
                    "name": v["name"],
                    "configuration": {"duration": v["days"]},
                }
            )
            return {"createProjectV2Field": {}}
        if "updateProjectV2Field(" in query:
            f = next(f for f in self.fields if f["id"] == v["fieldId"])
            old = {o["name"]: o for o in f["options"]}
            f["options"] = [
                old.get(o["name"])
                or {"id": self._id("O"), "name": o["name"], "color": o["color"], "description": ""}
                for o in v["options"]
            ]
            return {"updateProjectV2Field": {}}
        if "addProjectV2ItemById" in query:
            found: dict[str, Any] | None = next(
                (i for i in self.items if i["content"] == v["contentId"]), None
            )
            if found is None:  # like GitHub: adding twice returns the existing item
                found = {"id": self._id("I"), "content": v["contentId"], "values": {}}
                self.items.append(found)
            return {"addProjectV2ItemById": {"item": {"id": found["id"]}}}
        if "updateProjectV2ItemFieldValue" in query:
            item = next(i for i in self.items if i["id"] == v["itemId"])
            item["values"]["Status"] = v["status"]
            if "area" in v:
                item["values"]["Area"] = v["area"]
            return {}
        raise AssertionError(query)


def gh_for(fake: FakeProjects, *, read_only: bool = True) -> Gh:
    return Gh(OWNER, fake, read_only=read_only)


def quiet(_: str) -> None:
    return None


# ------------------------------------------------------------------ the read-only guard


def test_read_only_refuses_a_graphql_mutation_before_the_runner():
    fake = FakeProjects(project=True)
    gh = gh_for(fake)
    with pytest.raises(ReadOnlyError, match="read-only"):
        gh.graphql("mutation { addProjectV2ItemById(input: {}) { item { id } } }")
    with pytest.raises(ReadOnlyError):
        gh.api("graphql", "POST", fields={"query": "mutation  {x}"})
    with pytest.raises(ReadOnlyError):
        gh.api("graphql", "POST", body={"query": "MUTATION {x}"})
    assert fake.requests == []


def test_read_only_still_allows_a_graphql_query_but_not_other_posts():
    fake = FakeProjects(project=True)
    gh = gh_for(fake)
    assert gh.graphql('query { user(login: "x") { projectsV2(first: 1) { nodes { id } } } }')
    assert len(fake.requests) == 1
    with pytest.raises(ReadOnlyError):
        gh.api("/repos/x/y/issues", "POST", fields={"query": "query { a }"})
    with pytest.raises(ReadOnlyError):
        gh.api("graphql", "POST")  # no query text at all: not provably a read


def test_graphql_errors_in_a_200_reply_raise():
    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args, 0, json.dumps({"errors": [{"message": "bad"}]}), ""
        )

    with pytest.raises(GhError, match="bad"):
        Gh(OWNER, runner).graphql("query { a }")


# ------------------------------------------------------------------ ensure


def test_ensure_dry_run_creates_nothing(std: Standard):
    fake = FakeProjects()
    lines: list[str] = []
    assert ensure_project(gh_for(fake), std, execute=False, report=lines.append) is None
    assert fake.mutations == []
    assert any("would create project" in line for line in lines)
    assert any("would create field Area" in line for line in lines)


def test_ensure_creates_the_project_and_every_field(std: Standard):
    fake = FakeProjects()
    project = ensure_project(gh_for(fake), std, execute=True, report=quiet)
    assert project is not None
    assert fake.option_names("Status") == [n for n, _ in STATUS]
    assert fake.option_names("Priority") == [n for n, _ in PRIORITY]
    assert fake.option_names("Size") == [n for n, _ in SIZE]
    assert fake.option_names("Area") == list(std.kinds)
    assert fake.field("Iteration")["configuration"]["duration"] == 14
    assert {f.name for f in project.fields.values()} == {
        "Status",
        "Priority",
        "Size",
        "Area",
        "Iteration",
    }


def test_ensure_twice_changes_nothing(std: Standard):
    fake = FakeProjects()
    ensure_project(gh_for(fake), std, execute=True, report=quiet)
    snapshot = json.dumps(fake.fields, sort_keys=True)
    fake.requests.clear()
    lines: list[str] = []
    ensure_project(gh_for(fake), std, execute=True, report=lines.append)
    assert fake.mutations == []
    assert json.dumps(fake.fields, sort_keys=True) == snapshot
    assert lines == []


def test_ensure_adds_missing_options_and_keeps_existing_ones(std: Standard):
    fake = FakeProjects(project=True)  # Status: Todo, In Progress, Done, Parked
    todo = fake.option_id("Status", "Todo")
    ensure_project(gh_for(fake), std, execute=True, report=quiet)
    names = fake.option_names("Status")
    assert names[:5] == ["Inbox", "Todo", "In Progress", "Blocked", "Done"]  # no duplicate
    assert "Parked" in names  # never deleted
    assert fake.option_id("Status", "Todo") == todo  # kept as it was
    assert len([n for n in names if n.casefold() == "in progress"]) == 1
    assert len([f for f in fake.fields if f["name"] == "Status"]) == 1


def test_wanted_area_options_come_from_the_standard(std: Standard):
    assert [n for n, _ in wanted_selects(std)["Area"]] == list(std.kinds)


# ------------------------------------------------------------------ sync


def ready(std: Standard) -> FakeProjects:
    fake = FakeProjects()
    ensure_project(gh_for(fake), std, execute=True, report=quiet)
    fake.requests.clear()
    return fake


def test_sync_needs_the_project_first(inv: dict[str, InventoryEntry]):
    with pytest.raises(ProjectError, match="project ensure"):
        sync_project(gh_for(FakeProjects()), inv, execute=True, report=quiet)


def test_sync_dry_run_lists_and_writes_nothing(std: Standard, inv: dict[str, InventoryEntry]):
    fake = ready(std)
    lines: list[str] = []
    result = sync_project(gh_for(fake), inv, execute=False, report=lines.append)
    assert (result.added, fake.items, fake.mutations) == (4, [], [])
    assert "dry run: would add 4" in result.summary(execute=False)
    assert not any("old-widget" in line for line in lines)  # archived: left out


def test_sync_adds_with_inbox_and_area_from_the_kind(std: Standard, inv: dict[str, InventoryEntry]):
    fake = ready(std)
    result = sync_project(gh_for(fake), inv, execute=True, report=quiet)
    assert (result.added, result.failed, result.total) == (4, 0, 4)
    by_content = {i["content"]: i["values"] for i in fake.items}
    assert set(by_content) == {"I_1", "PR_2", "I_3", "I_4"}  # not the archived repo's
    inbox = fake.option_id("Status", "Inbox")
    assert all(v["Status"] == inbox for v in by_content.values())
    kind = inv["ai-helper"].kind
    assert by_content["I_1"]["Area"] == fake.option_id("Area", kind)
    assert by_content["PR_2"]["Area"] == fake.option_id("Area", kind)
    assert "Area" not in by_content["I_4"]  # not in the inventory: no kind, no Area


def test_sync_twice_adds_no_duplicates(std: Standard, inv: dict[str, InventoryEntry]):
    fake = ready(std)
    sync_project(gh_for(fake), inv, execute=True, report=quiet)
    before = json.dumps(fake.items, sort_keys=True)
    fake.requests.clear()
    second = sync_project(gh_for(fake), inv, execute=True, report=quiet)
    assert (second.added, second.present, second.total) == (0, 4, 4)
    assert fake.mutations == []  # nothing is even sent the second time
    assert json.dumps(fake.items, sort_keys=True) == before
    contents = [i["content"] for i in fake.items]
    assert len(contents) == len(set(contents)) == 4


def test_sync_leaves_an_existing_item_alone(std: Standard, inv: dict[str, InventoryEntry]):
    fake = ready(std)
    fake.items.append({"id": "I_x", "content": "I_1", "values": {"Status": "moved by hand"}})
    result = sync_project(gh_for(fake), inv, execute=True, report=quiet)
    assert (result.added, result.present) == (3, 1)
    assert fake.items[0]["values"] == {"Status": "moved by hand"}


# ------------------------------------------------------------------ the command


@pytest.fixture(autouse=True)
def no_private_inventory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("JTS_PRIVATE_INVENTORY", str(tmp_path / "none.yml"))


def run(
    fake: FakeProjects, fixtures_dir: Path, *argv: str, read_only: bool = True
) -> tuple[int, Any]:
    return main(["project", *argv, "--standard-dir", str(fixtures_dir)], gh_for(fake)), fake


def test_cli_is_a_dry_run_by_default(fixtures_dir: Path, capsys: pytest.CaptureFixture[str]):
    fake = FakeProjects()
    assert run(fake, fixtures_dir, "ensure")[0] == 0
    assert "dry run" in capsys.readouterr().out
    assert fake.mutations == []


def test_cli_execute_writes(fixtures_dir: Path, capsys: pytest.CaptureFixture[str]):
    fake = FakeProjects()
    assert run(fake, fixtures_dir, "ensure", "--execute")[0] == 0
    assert run(fake, fixtures_dir, "sync", "--execute")[0] == 0
    out = capsys.readouterr().out
    assert "the project has 4 items" in out
    assert len(fake.items) == 4


@pytest.mark.parametrize("command", ["ensure", "sync"])
def test_missing_project_scope_points_to_gh_auth_refresh(
    command: str, fixtures_dir: Path, capsys: pytest.CaptureFixture[str]
):
    fake = FakeProjects(scope=False)
    code, _ = run(fake, fixtures_dir, command)
    err = capsys.readouterr().err
    assert code == 1
    assert "gh auth refresh -s project" in err
    assert "Traceback" not in err


def test_scope_error_is_a_missing_scope_error():
    with pytest.raises(MissingScopeError):
        gh_for(FakeProjects(scope=False)).graphql("query { a }")


def test_sync_without_a_project_says_to_ensure(
    fixtures_dir: Path, capsys: pytest.CaptureFixture[str]
):
    code, _ = run(FakeProjects(), fixtures_dir, "sync")
    assert code == 1
    assert "project ensure" in capsys.readouterr().err
