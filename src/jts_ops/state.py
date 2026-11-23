"""Observed state of one GitHub repo, as the audit sees it."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LabelInfo:
    name: str
    color: str
    description: str


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
    # Optional extras read by `apply`; the defaults mean "not fetched".
    label_details: tuple[LabelInfo, ...] = ()
    squash_merge_commit_title: str | None = None
    squash_merge_commit_message: str | None = None
    # The repo's parsed `.copier-answers.yml`; None when it has none (or it could not be read).
    answers: Mapping[str, Any] | None = None
