"""Read-side adapter over the `gh` CLI: JSON in, typed repo state out."""

import base64
import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from jts_ops.state import RepoState

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]

_STATUS = re.compile(r"HTTP (\d{3})")


def default_runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=False)


class GhError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
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


class Gh:
    def __init__(self, owner: str, runner: Runner = default_runner) -> None:
        self.owner = owner
        self._runner = runner

    def api(
        self,
        path: str,
        method: str = "GET",
        fields: Mapping[str, Any] | None = None,
        paginate: bool = False,
    ) -> Any:
        """Call the GitHub API and return decoded JSON (None for an empty body).

        With `paginate`, pages are fetched with `--slurp` and list pages are flattened
        into one list.
        """
        args = ["api", "-H", "Accept: application/vnd.github+json", "--method", method]
        if paginate:
            args += ["--paginate", "--slurp"]
        if fields:
            args += _field_args(fields)
        args.append(path)
        result = self._runner(args)
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


def _flatten(pages: Any) -> list[Any]:
    out: list[Any] = []
    for page in pages:
        if isinstance(page, list):
            out.extend(page)  # pyright: ignore[reportUnknownArgumentType]
        else:
            out.append(page)
    return out


def list_repos(gh: Gh) -> list[dict[str, Any]]:
    return gh.api("/user/repos?affiliation=owner&per_page=100", paginate=True)


def _or_none_on(status: int, gh: Gh, path: str) -> Any:
    """GET `path`, mapping one expected HTTP status to None; other errors propagate."""
    try:
        return gh.api(path)
    except GhError as err:
        if err.status == status:
            return None
        raise


def fetch_state(gh: Gh, repo: dict[str, Any]) -> RepoState:
    base = f"/repos/{gh.owner}/{repo['name']}"
    if "allow_squash_merge" not in repo:
        # The list endpoint omits merge settings; only the single-repo endpoint has them.
        repo = {**repo, **gh.api(base)}
    branch = repo["default_branch"]

    # 409 "Git Repository is empty": a repo with no commits has no tree.
    tree = _or_none_on(409, gh, f"{base}/git/trees/{branch}?recursive=1")
    files = (
        None if tree is None else frozenset(e["path"] for e in tree["tree"] if e["type"] == "blob")
    )

    readme_json = _or_none_on(404, gh, f"{base}/readme")
    readme = (
        None if readme_json is None else base64.b64decode(readme_json["content"]).decode("utf-8")
    )

    runs = gh.api(f"{base}/actions/runs?branch={branch}&per_page=1")["workflow_runs"]
    ci_conclusion = runs[0]["conclusion"] if runs else None

    labels = tuple(label["name"] for label in gh.api(f"{base}/labels", paginate=True))
    license_info = repo.get("license")

    return RepoState(
        name=repo["name"],
        private=repo["private"],
        archived=repo["archived"],
        description=repo.get("description") or "",
        topics=tuple(repo.get("topics") or ()),
        has_wiki=repo["has_wiki"],
        has_discussions=repo.get("has_discussions", False),
        allow_squash_merge=repo["allow_squash_merge"],
        allow_merge_commit=repo["allow_merge_commit"],
        allow_rebase_merge=repo["allow_rebase_merge"],
        delete_branch_on_merge=repo["delete_branch_on_merge"],
        default_branch=branch,
        license_spdx=license_info["spdx_id"] if license_info else None,
        files=files,
        readme=readme,
        ci_conclusion=ci_conclusion,
        labels=labels,
    )
