"""`jts-ops project`: the user-level "Engineering" Project (v2), through GraphQL.

`ensure` finds or creates the project and its fields: Status, Priority, Size and Area (single
select) and Iteration (two weeks). It is idempotent and only ever adds: a missing option is
appended to an existing field, an existing option is never removed. `sync` adds every open
issue and pull request of the active repos that is not already in the project; a new item gets
Status=Inbox and an Area taken from the repo kind. Both are dry runs unless asked to execute.

The Projects API has no view mutations, so the views are made by hand in the UI.
"""

import datetime
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from jts_ops.gh import Gh, GhError, list_repos, printable
from jts_ops.ideas import PROJECT_TITLE
from jts_ops.inventory import InventoryEntry
from jts_ops.standard import Standard
from jts_ops.vault import find_entry

ITERATION_FIELD = "Iteration"
ITERATION_DAYS = 14
STATUS_FIELD = "Status"
AREA_FIELD = "Area"
INBOX = "Inbox"

# Single-select fields and their option colors; an Area option gets a color from the cycle.
STATUS: tuple[tuple[str, str], ...] = (
    (INBOX, "GRAY"),
    ("Todo", "BLUE"),
    ("In progress", "YELLOW"),
    ("Blocked", "RED"),
    ("Done", "GREEN"),
)
PRIORITY: tuple[tuple[str, str], ...] = (
    ("P0", "RED"),
    ("P1", "ORANGE"),
    ("P2", "YELLOW"),
    ("P3", "GRAY"),
)
SIZE: tuple[tuple[str, str], ...] = (
    ("XS", "GRAY"),
    ("S", "BLUE"),
    ("M", "GREEN"),
    ("L", "YELLOW"),
    ("XL", "ORANGE"),
)
_CYCLE = ("BLUE", "GREEN", "YELLOW", "ORANGE", "RED", "PINK", "PURPLE", "GRAY")

_OPTION_INPUT = "[ProjectV2SingleSelectFieldOptionInput!]!"

_PROJECT_FIELDS = """
id number title url
fields(first: 50) { nodes {
  __typename
  ... on ProjectV2FieldCommon { id name }
  ... on ProjectV2SingleSelectField { options { id name color description } }
  ... on ProjectV2IterationField { configuration { duration } }
} }
"""
Q_PROJECTS = (
    "query($owner: String!) { user(login: $owner) { id projectsV2(first: 100) { nodes {"
    + _PROJECT_FIELDS
    + "} } } }"
)
Q_ITEMS = (
    "query($id: ID!, $after: String) { node(id: $id) { ... on ProjectV2 { "
    "items(first: 100, after: $after) { pageInfo { hasNextPage endCursor } "
    "nodes { id content { ... on Issue { id } ... on PullRequest { id } } } } } } }"
)
M_CREATE_PROJECT = (
    "mutation($ownerId: ID!, $title: String!) { createProjectV2("
    "input: {ownerId: $ownerId, title: $title}) { projectV2 { id } } }"
)
M_CREATE_SELECT = (
    f"mutation($projectId: ID!, $name: String!, $options: {_OPTION_INPUT}) {{ "
    "createProjectV2Field(input: {projectId: $projectId, dataType: SINGLE_SELECT, "
    "name: $name, singleSelectOptions: $options}) { projectV2Field { "
    "... on ProjectV2SingleSelectField { id } } } }"
)
M_UPDATE_SELECT = (
    f"mutation($fieldId: ID!, $options: {_OPTION_INPUT}) {{ "
    "updateProjectV2Field(input: {fieldId: $fieldId, singleSelectOptions: $options}) { "
    "projectV2Field { ... on ProjectV2SingleSelectField { id } } } }"
)
M_CREATE_ITERATION = (
    "mutation($projectId: ID!, $name: String!, $start: Date!, $days: Int!) { "
    "createProjectV2Field(input: {projectId: $projectId, dataType: ITERATION, name: $name, "
    "iterationConfiguration: {duration: $days, startDate: $start, iterations: []}}) { "
    "projectV2Field { ... on ProjectV2IterationField { id } } } }"
)
M_ADD_ITEM = (
    "mutation($projectId: ID!, $contentId: ID!) { addProjectV2ItemById("
    "input: {projectId: $projectId, contentId: $contentId}) { item { id } } }"
)
M_SET_FIELDS = (
    "mutation($projectId: ID!, $itemId: ID!, $statusField: ID!, $status: String!, "
    "$areaField: ID!, $area: String!) { "
    "status: updateProjectV2ItemFieldValue(input: {projectId: $projectId, itemId: $itemId, "
    "fieldId: $statusField, value: {singleSelectOptionId: $status}}) { clientMutationId } "
    "area: updateProjectV2ItemFieldValue(input: {projectId: $projectId, itemId: $itemId, "
    "fieldId: $areaField, value: {singleSelectOptionId: $area}}) { clientMutationId } }"
)
M_SET_STATUS = (
    "mutation($projectId: ID!, $itemId: ID!, $statusField: ID!, $status: String!) { "
    "updateProjectV2ItemFieldValue(input: {projectId: $projectId, itemId: $itemId, "
    "fieldId: $statusField, value: {singleSelectOptionId: $status}}) { clientMutationId } }"
)


class ProjectError(Exception):
    """The project is not in a state the command can work with."""


@dataclass(frozen=True)
class Option:
    id: str
    name: str
    color: str
    description: str


@dataclass(frozen=True)
class Field:
    id: str
    name: str
    type: str
    options: tuple[Option, ...] = ()

    def option_id(self, name: str) -> str | None:
        return next((o.id for o in self.options if o.name.casefold() == name.casefold()), None)


@dataclass(frozen=True)
class Project:
    id: str
    number: int
    url: str
    fields: Mapping[str, Field] = field(default_factory=dict[str, Field])

    def get(self, name: str) -> Field | None:
        return next((f for f in self.fields.values() if f.name.casefold() == name.casefold()), None)


# ------------------------------------------------------------------ reading


def _parse_project(node: Mapping[str, Any]) -> Project:
    fields: dict[str, Field] = {}
    for raw in node["fields"]["nodes"]:
        if not raw or "id" not in raw:
            continue
        options = tuple(
            Option(o["id"], o["name"], o.get("color") or "GRAY", o.get("description") or "")
            for o in raw.get("options") or ()
        )
        fields[raw["name"]] = Field(raw["id"], raw["name"], raw["__typename"], options)
    return Project(node["id"], int(node["number"]), str(node.get("url") or ""), fields)


def load_project(gh: Gh) -> tuple[str, Project | None]:
    """The owner's node id and the Engineering project (the lowest-numbered one), if any."""
    user = gh.graphql(Q_PROJECTS, {"owner": gh.owner})["user"]
    nodes = [n for n in user["projectsV2"]["nodes"] if n and n["title"] == PROJECT_TITLE]
    if not nodes:
        return str(user["id"]), None
    return str(user["id"]), _parse_project(min(nodes, key=lambda n: int(n["number"])))


def project_content_ids(gh: Gh, project_id: str) -> tuple[int, set[str]]:
    """The number of items in the project and the node ids of their issues and PRs."""
    count = 0
    content: set[str] = set()
    after: str | None = None
    while True:
        items = gh.graphql(Q_ITEMS, {"id": project_id, "after": after})["node"]["items"]
        for item in items["nodes"]:
            count += 1
            if item.get("content") and item["content"].get("id"):
                content.add(str(item["content"]["id"]))
        if not items["pageInfo"]["hasNextPage"]:
            return count, content
        after = str(items["pageInfo"]["endCursor"])


# ------------------------------------------------------------------ ensure


def wanted_selects(std: Standard) -> dict[str, tuple[tuple[str, str], ...]]:
    """The single-select fields of the project and their options, in order."""
    area = tuple((kind, _CYCLE[i % len(_CYCLE)]) for i, kind in enumerate(std.kinds))
    return {STATUS_FIELD: STATUS, "Priority": PRIORITY, "Size": SIZE, AREA_FIELD: area}


def _merged_options(
    current: Sequence[Option], wanted: Sequence[tuple[str, str]]
) -> list[dict[str, str]]:
    """The wanted options in order (an existing one keeps its own spelling, color and
    description), then every other existing option: nothing is dropped."""
    by_name = {o.name.casefold(): o for o in current}
    merged: list[dict[str, str]] = []
    used: set[str] = set()
    for name, color in wanted:
        have = by_name.get(name.casefold())
        used.add(name.casefold())
        merged.append(
            {"name": have.name, "color": have.color, "description": have.description}
            if have
            else {"name": name, "color": color, "description": ""}
        )
    merged += [
        {"name": o.name, "color": o.color, "description": o.description}
        for o in current
        if o.name.casefold() not in used
    ]
    return merged


def ensure_project(
    gh: Gh,
    std: Standard,
    *,
    execute: bool,
    report: Callable[[str], None] = print,
    today: datetime.date | None = None,
) -> Project | None:
    """Make the project and its fields exist. Returns it, or None when a dry run found it
    missing. Raises GhError (MissingScopeError without the `project` scope)."""
    gh.read_only = not execute
    owner_id, project = load_project(gh)
    changed = False
    verb = "" if execute else "would "
    if project is None:
        report(f"  {verb}create project: {PROJECT_TITLE}")
        if not execute:
            project_id = ""
        else:
            made = gh.graphql(M_CREATE_PROJECT, {"ownerId": owner_id, "title": PROJECT_TITLE})
            project_id = str(made["createProjectV2"]["projectV2"]["id"])
            changed = True
        current: Mapping[str, Field] = {}
    else:
        project_id = project.id
        current = project.fields

    def have(name: str) -> Field | None:
        return next((f for f in current.values() if f.name.casefold() == name.casefold()), None)

    for name, options in wanted_selects(std).items():
        existing = have(name)
        if existing is None:
            report(f"  {verb}create field {name}: {', '.join(o for o, _ in options)}")
            if execute:
                gh.graphql(
                    M_CREATE_SELECT,
                    {
                        "projectId": project_id,
                        "name": name,
                        "options": _merged_options((), options),
                    },
                )
                changed = True
        elif existing.type != "ProjectV2SingleSelectField":
            report(f"  warning: field {name} exists but is not a single select; left alone")
        else:
            present = {o.name.casefold() for o in existing.options}
            missing = [o for o, _ in options if o.casefold() not in present]
            if missing:
                report(f"  {verb}add to field {name}: {', '.join(missing)}")
                if execute:
                    gh.graphql(
                        M_UPDATE_SELECT,
                        {
                            "fieldId": existing.id,
                            "options": _merged_options(existing.options, options),
                        },
                    )
                    changed = True
    if have(ITERATION_FIELD) is None:
        report(f"  {verb}create field {ITERATION_FIELD}: {ITERATION_DAYS} days")
        if execute:
            start = (today or datetime.date.today()).isoformat()
            gh.graphql(
                M_CREATE_ITERATION,
                {
                    "projectId": project_id,
                    "name": ITERATION_FIELD,
                    "start": start,
                    "days": ITERATION_DAYS,
                },
            )
            changed = True
    if not execute:
        return project
    if changed:
        _, project = load_project(gh)
    if project is None:
        raise ProjectError(f"the {PROJECT_TITLE} project is missing after creating it")
    return project


def ensure_summary(project: Project | None, *, execute: bool) -> str:
    if project is None:
        return f"dry run: the {PROJECT_TITLE} project does not exist yet"
    where = f" ({printable(project.url)})" if project.url else ""
    return (
        f"{PROJECT_TITLE} project is set up{where}"
        if execute
        else f"dry run: the {PROJECT_TITLE} project exists{where}"
    )


# ------------------------------------------------------------------ sync


@dataclass(frozen=True)
class ProjectSyncResult:
    added: int = 0
    present: int = 0
    failed: int = 0
    total: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple[str, ...])

    def summary(self, *, execute: bool) -> str:
        verb = "added" if execute else "would add"
        line = f"{verb} {self.added}, already in the project {self.present}"
        if execute:
            line += f", failed {self.failed}; the project has {self.total} items"
        return line if execute else f"dry run: {line}"


def _open_items(gh: Gh, repo: str) -> list[dict[str, Any]]:
    """Open issues and PRs of a repo (the issues endpoint lists both, with their node ids).
    A repo whose issues are disabled has none."""
    try:
        rows: list[dict[str, Any]] = gh.api(
            f"/repos/{gh.owner}/{repo}/issues?state=open&per_page=100", paginate=True
        )
    except GhError as err:
        if err.status in (404, 410):
            return []
        raise
    return rows


def sync_project(
    gh: Gh,
    inv: Mapping[str, InventoryEntry],
    *,
    execute: bool,
    report: Callable[[str], None] = print,
) -> ProjectSyncResult:
    """Add the open issues and PRs of every active repo that the project lacks.

    Raises ProjectError when the project or its Status field is missing (run `ensure`), and
    GhError (MissingScopeError without the `project` scope) when GitHub cannot be read.
    """
    gh.read_only = not execute
    _, project = load_project(gh)
    if project is None:
        raise ProjectError(f"no {PROJECT_TITLE} project yet: run `jts-ops project ensure`")
    status = project.get(STATUS_FIELD)
    inbox = status.option_id(INBOX) if status else None
    if status is None or inbox is None:
        raise ProjectError(f"the {STATUS_FIELD} field has no {INBOX} option: run `project ensure`")
    area = project.get(AREA_FIELD)
    count, known = project_content_ids(gh, project.id)

    notes: list[str] = []
    added = present = failed = 0
    for repo in sorted(list_repos(gh), key=lambda r: str(r["name"]).lower()):
        name = str(repo["name"])
        entry = find_entry(inv, name)
        if repo.get("archived") or (entry is not None and entry.status == "archived"):
            continue
        area_option = area.option_id(entry.kind) if area and entry else None
        if entry is not None and area is not None and area_option is None:
            note = printable(
                f"no Area option for kind {entry.kind}: {name} is added without an Area"
            )
            if note not in notes:
                notes.append(note)
                report(f"  warning: {note}")
        for item in _open_items(gh, name):
            node_id = str(item["node_id"])
            label = printable(f"{name}#{item['number']}")
            if node_id in known:
                present += 1
                continue
            if not execute:
                report(f"  would add: {label}: {printable(item.get('title', ''))}")
                added += 1
                continue
            try:
                new = gh.graphql(M_ADD_ITEM, {"projectId": project.id, "contentId": node_id})
                item_id = str(new["addProjectV2ItemById"]["item"]["id"])
                values: dict[str, Any] = {
                    "projectId": project.id,
                    "itemId": item_id,
                    "statusField": status.id,
                    "status": inbox,
                }
                if area is not None and area_option is not None:
                    gh.graphql(M_SET_FIELDS, {**values, "areaField": area.id, "area": area_option})
                else:
                    gh.graphql(M_SET_STATUS, values)
            except (GhError, KeyError, TypeError) as err:
                failed += 1
                message = getattr(err, "message", None) or str(err)
                report(
                    f"  FAILED {label}: {printable((message.splitlines() or ['unknown error'])[0])}"
                )
                continue
            known.add(node_id)
            count += 1
            added += 1
            report(f"  added: {label}: {printable(item.get('title', ''))}")
    return ProjectSyncResult(added, present, failed, count, tuple(notes))
