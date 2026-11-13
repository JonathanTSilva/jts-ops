"""Read-side adapter over the `gh` CLI: JSON in, typed repo state out."""

import base64
import json
import re
import subprocess
import tempfile
import unicodedata
from collections.abc import Callable, Collection, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from jts_ops.state import LabelInfo, RepoState

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]

_STATUS = re.compile(r"HTTP (\d{3})")
# Every GraphQL write is an operation introduced by this keyword, so its absence proves a query.
_MUTATION = re.compile(r"\bmutation\b", re.IGNORECASE)
_SCOPE_PROBLEM = re.compile(
    r"INSUFFICIENT_SCOPES|required scopes|read:project|missing.*scope", re.I
)
GRAPHQL = "graphql"


def default_runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=False)


def printable(text: object) -> str:
    """`text` without control characters (Unicode category C*, so no escape sequences), a
    plain space excepted. Newlines and tabs become spaces. Use it on every piece of
    GitHub-supplied text before it reaches a terminal."""
    return "".join(
        " " if ch in "\n\r\t" else ch
        for ch in str(text)
        if ch in "\n\r\t" or unicodedata.category(ch)[0] != "C"
    )


class GhError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        # The message comes from GitHub and is printed by every caller: strip it here, but keep
        # line breaks, which callers split on.
        message = "".join(
            ch for ch in message if ch in "\n\t" or unicodedata.category(ch)[0] != "C"
        )
        super().__init__(message)
        self.message = message
        self.status = status


def _field_args(fields: Mapping[str, Any]) -> list[str]:
    args: list[str] = []
    for key, value in fields.items():
        if isinstance(value, bool):
            args += ["-F", f"{key}={'true' if value else 'false'}"]
        elif isinstance(value, int):
            args += ["-F", f"{key}={value}"]
        elif isinstance(value, (list, tuple)):
            for item in value:  # pyright: ignore[reportUnknownVariableType]
                args += ["-f", f"{key}[]={item}"]
        else:
            args += ["-f", f"{key}={value}"]
    return args


class ReadOnlyError(GhError):
    """A non-GET call was attempted on a read-only `Gh`."""


class MissingScopeError(GhError):
    """The token lacks an OAuth scope the call needs (for Projects: `project`)."""


def _is_graphql_query(path: str, fields: Mapping[str, Any] | None, body: Any) -> bool:
    """True when this call is a GraphQL read: the endpoint is `graphql` and its query text has
    no `mutation` keyword. Anything else that is not GET is a write."""
    if path != GRAPHQL:
        return False
    text: Any = (fields or {}).get("query")
    if text is None and isinstance(body, Mapping):
        text = cast("Mapping[str, Any]", body).get("query")
    return isinstance(text, str) and not _MUTATION.search(text)


class Gh:
    def __init__(self, owner: str, runner: Runner = default_runner, read_only: bool = True) -> None:
        self.owner = owner
        self._runner = runner
        # Structural dry-run guard: while set, `api` refuses every non-GET call, except a
        # GraphQL query (a POST that cannot write).
        self.read_only = read_only

    def api(
        self,
        path: str,
        method: str = "GET",
        fields: Mapping[str, Any] | None = None,
        paginate: bool = False,
        body: Any = None,
    ) -> Any:
        """Call the GitHub API and return decoded JSON (None for an empty body).

        With `paginate`, pages are fetched with `--slurp` and list pages are flattened
        into one list. `body` is sent as a JSON request body (`gh api --input`), for
        payloads the `-f/-F` field flags cannot express.
        """
        if self.read_only and method.upper() != "GET" and not _is_graphql_query(path, fields, body):
            raise ReadOnlyError(f"refusing {method} {path}: read-only mode")
        args = ["api", "-H", "Accept: application/vnd.github+json", "--method", method]
        if paginate:
            args += ["--paginate", "--slurp"]
        if fields:
            args += _field_args(fields)
        args.append(path)
        if body is None:
            result = self._runner(args)
        else:
            with tempfile.TemporaryDirectory() as tmp:
                body_file = Path(tmp) / "body.json"
                body_file.write_text(json.dumps(body), encoding="utf-8")
                result = self._runner([*args, "--input", str(body_file)])
        if result.returncode != 0:
            message = result.stderr.strip()
            match = _STATUS.search(message)
            raise GhError(message, int(match.group(1)) if match else None)
        if not result.stdout.strip():
            return None
        data = json.loads(result.stdout)
        if paginate:
            return _flatten(data)
        return data

    def graphql(self, query: str, variables: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Run a GraphQL operation and return its `data`. A mutation is refused while read-only.

        Raises `MissingScopeError` when the token lacks a scope, `GhError` for any other
        failure, including the `errors` GitHub can return with a 200.
        """
        try:
            reply: dict[str, Any] | None = self.api(
                GRAPHQL, "POST", body={"query": query, "variables": dict(variables or {})}
            )
        except ReadOnlyError:
            raise
        except GhError as err:
            raise _classify(err.message, err.status) from err
        if reply is None:
            raise GhError("empty GraphQL reply")
        errors: list[dict[str, Any]] = reply.get("errors") or []
        if errors:
            message = "; ".join(str(e.get("message", e)) for e in errors)
            kinds = " ".join(str(e.get("type", "")) for e in errors)
            raise _classify(f"{message} {kinds}", None)
        return reply["data"]


def _classify(message: str, status: int | None) -> GhError:
    if _SCOPE_PROBLEM.search(message):
        return MissingScopeError(message, status)
    return GhError(message, status)


def _flatten(pages: Any) -> list[Any]:
    out: list[Any] = []
    for page in pages:
        if isinstance(page, list):
            out.extend(page)  # pyright: ignore[reportUnknownArgumentType]
        else:
            out.append(page)
    return out


def list_repos(gh: Gh, public_only: bool = False) -> list[dict[str, Any]]:
    """List the owner's repos. `public_only` uses the public listing, which a `GITHUB_TOKEN`
    can call (`/user/repos` needs a user token) and which cannot return private repos."""
    if public_only:
        return gh.api(f"/users/{gh.owner}/repos?type=owner&per_page=100", paginate=True)
    return gh.api("/user/repos?affiliation=owner&per_page=100", paginate=True)


def _or_none_on(statuses: Collection[int], gh: Gh, path: str) -> Any:
    """GET `path`, mapping the expected HTTP statuses to None; other errors propagate."""
    try:
        return gh.api(path)
    except GhError as err:
        if err.status is not None and err.status in statuses:
            return None
        raise


def fetch_state(gh: Gh, repo: dict[str, Any]) -> RepoState:
    base = f"/repos/{gh.owner}/{repo['name']}"
    if "allow_squash_merge" not in repo:
        # The list endpoint omits merge settings; only the single-repo endpoint has them.
        repo = {**repo, **gh.api(base)}
    branch = repo["default_branch"]

    # 409 "Git Repository is empty": a repo with no commits has no tree.
    tree = _or_none_on({409}, gh, f"{base}/git/trees/{branch}?recursive=1")
    # A truncated tree is incomplete, so a "missing file" verdict from it would be wrong.
    files = (
        None
        if tree is None or tree.get("truncated")
        else frozenset(e["path"] for e in tree["tree"] if e["type"] == "blob")
    )

    readme_json = _or_none_on({404}, gh, f"{base}/readme")
    readme = (
        base64.b64decode(readme_json["content"]).decode("utf-8", errors="replace")
        if readme_json is not None and readme_json.get("encoding") == "base64"
        else None
    )

    # The CI signal is the latest completed run of ci.yml on the default branch, so other
    # workflows (and runs still in progress) never mask it. 403/404: Actions disabled or not
    # visible, or the repo has no ci.yml; there is simply no CI signal.
    runs_json = _or_none_on(
        {403, 404},
        gh,
        f"{base}/actions/workflows/ci.yml/runs?branch={branch}&status=completed&per_page=1",
    )
    runs: list[dict[str, Any]] = runs_json["workflow_runs"] if runs_json else []
    ci_conclusion: str | None = runs[0]["conclusion"] if runs else None

    label_rows: list[dict[str, Any]] = gh.api(f"{base}/labels", paginate=True)
    labels = tuple(label["name"] for label in label_rows)
    label_details = tuple(
        LabelInfo(label["name"], label["color"], label.get("description") or "")
        for label in label_rows
    )
    license_info = repo.get("license")

    return RepoState(
        name=repo["name"],
        private=repo["private"],
        archived=repo["archived"],
        description=repo.get("description") or "",
        topics=tuple(repo.get("topics") or ()),
        has_wiki=repo["has_wiki"],
        has_discussions=repo.get("has_discussions", False),
        # A token without push access (GITHUB_TOKEN reading another repo) never sees the merge
        # settings. Unknown settings default to the standard values so they cannot be flagged.
        allow_squash_merge=repo.get("allow_squash_merge", True),
        allow_merge_commit=repo.get("allow_merge_commit", False),
        allow_rebase_merge=repo.get("allow_rebase_merge", False),
        delete_branch_on_merge=repo.get("delete_branch_on_merge", True),
        default_branch=branch,
        license_spdx=license_info["spdx_id"] if license_info else None,
        files=files,
        readme=readme,
        ci_conclusion=ci_conclusion,
        labels=labels,
        label_details=label_details,
        squash_merge_commit_title=repo.get("squash_merge_commit_title"),
        squash_merge_commit_message=repo.get("squash_merge_commit_message"),
    )
