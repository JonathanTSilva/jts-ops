"""Observed state of one GitHub repo, as the audit sees it."""

from dataclasses import dataclass


@dataclass
class RepoState:
    name: str
    private: bool
    archived: bool
    description: str
    topics: tuple[str, ...]
    has_wiki: bool
    has_discussions: bool
    allow_squash_merge: bool
    allow_merge_commit: bool
    allow_rebase_merge: bool
    delete_branch_on_merge: bool
    default_branch: str
    license_spdx: str | None
    # None means the repo has no commits yet, so there is no tree to list.
    files: frozenset[str] | None
    readme: str | None
    ci_conclusion: str | None
    labels: tuple[str, ...]
