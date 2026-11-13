"""`jts-ops ideas`: turn vault notes marked as ideas into GitHub issues.

A note is an idea when its properties say `github: idea` and `repo: <name>` and it has no
`issue:`. The command is a dry run unless asked to execute. When it executes it:

1. creates the issue (title: the note's title; body: the note's "## Idea" section, else its
   first paragraph, plus a "From the vault" footer that names no vault path);
2. writes `issue: <url>` and `github: issued` back into the note, right away, so the note is
   never converted twice even if a later step fails;
3. adds the issue to the "Engineering" project, if that exists and the token may; this step
   only warns when it fails, because the issue is already created and recorded.

The safety rule: an issue in a public repo is never created from a note whose text names a
private repo. Any private inventory name anywhere in the note refuses it.
"""

import re
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jts_ops.gh import Gh, GhError, list_repos, printable
from jts_ops.inventory import InventoryEntry
from jts_ops.vault import find_entry, read_properties, set_properties

PROJECT_TITLE = "Engineering"
FOOTER_TEXT = "From the vault"
FOOTER = f"\n\n---\n\n{FOOTER_TEXT}"
# Directories the vault tooling never reads: Obsidian's own state, git and the trash.
_SKIPPED_DIRS = frozenset({".obsidian", ".git", ".trash"})
_HEADING = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
_NEWLINE = re.compile(r"\r\n?")


@dataclass(frozen=True)
class IdeasResult:
    created: int = 0
    refused: int = 0
    skipped: int = 0
    failed: int = 0

    def summary(self, *, execute: bool) -> str:
        verb = "created" if execute else "would create"
        line = f"{verb} {self.created}, refused {self.refused}, skipped {self.skipped}"
        return line if execute else f"dry run: {line}"


@dataclass(frozen=True)
class Idea:
    path: Path
    title: str
    repo: str
    text: str


# ------------------------------------------------------------------ reading a note


def _body(text: str) -> list[str]:
    """The lines of a note after its frontmatter."""
    lines = _NEWLINE.sub("\n", text.removeprefix("﻿")).split("\n")
    if lines and lines[0].rstrip() == "---":
        for end in range(1, len(lines)):
            if lines[end].rstrip() == "---":
                return lines[end + 1 :]
    return lines


def extract_idea(text: str) -> str:
    """The "## Idea" section of a note (up to the next heading of its level or higher), else
    its first paragraph after the title; empty when it has neither."""
    lines = _body(text)
    for i, line in enumerate(lines):
        match = _HEADING.match(line)
        if match and len(match[1]) == 2 and match[2].lower() == "idea":
            section: list[str] = []
            for line_ in lines[i + 1 :]:
                other = _HEADING.match(line_)
                if other and len(other[1]) <= 2:
                    break
                section.append(line_)
            return "\n".join(section).strip()
    paragraph: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or _HEADING.match(line) or stripped.startswith("<!--"):
            if paragraph:
                break
            continue
        paragraph.append(line.rstrip())
    return "\n".join(paragraph)


def find_ideas(vault: Path) -> tuple[list[Idea], list[str]]:
    """Notes with `github: idea` and a `repo`, and a line for each idea note left out: it has
    an `issue:` already, or no `repo`."""
    ideas: list[Idea] = []
    skipped: list[str] = []
    for path in sorted(vault.rglob("*.md")):
        if _SKIPPED_DIRS.intersection(path.relative_to(vault).parts):
            continue
        try:
            text = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "github" not in text:  # cheap filter before parsing
            continue
        props = read_properties(text)
        if props.get("github") != "idea":
            continue
        repo = props.get("repo")
        if props.get("issue"):
            skipped.append(f"  skipped: {path.stem}: it already has an issue")
        elif not isinstance(repo, str) or not repo:
            skipped.append(f"  skipped: {path.stem}: no repo property")
        else:
            ideas.append(Idea(path, path.stem, repo, text))
    return ideas, skipped


# ------------------------------------------------------------------ the safety rule


def private_names(inv: Mapping[str, InventoryEntry], live_private: Collection[str]) -> set[str]:
    """Every name (current, target, live) of a repo the inventory or GitHub says is private."""
    names = {
        n.lower() for e in inv.values() if e.visibility == "private" for n in (e.name, e.target)
    }
    return names | {n.lower() for n in live_private}


def names_in(text: str, names: Collection[str]) -> list[str]:
    """The names that appear in `text` as whole words (case-insensitive). A name is not part
    of a longer repo name: a word, dot or dash character on either side means a different word,
    except a trailing dot, which is just the end of a sentence."""
    found: list[str] = []
    for name in sorted(names):
        if re.search(rf"(?<![\w.-]){re.escape(name)}(?![\w-])", text, re.IGNORECASE):
            found.append(name)
    return found


# ------------------------------------------------------------------ writing to GitHub


def _project_id(gh: Gh) -> str | None:
    data = gh.api(
        "graphql",
        "POST",
        fields={
            "query": "query($owner: String!) { user(login: $owner) "
            "{ projectsV2(first: 50) { nodes { id title } } } }",
            "owner": gh.owner,
        },
    )
    nodes: list[dict[str, Any]] = data["data"]["user"]["projectsV2"]["nodes"]
    return next((str(n["id"]) for n in nodes if n["title"] == PROJECT_TITLE), None)


def add_to_project(gh: Gh, node_id: str) -> str | None:
    """Add an issue to the Engineering project; None when done, else why it was not."""
    try:
        project = _project_id(gh)
        if project is None:
            return f"the {PROJECT_TITLE} project does not exist yet"
        gh.api(
            "graphql",
            "POST",
            fields={
                "query": "mutation($projectId: ID!, $contentId: ID!) { addProjectV2ItemById("
                "input: {projectId: $projectId, contentId: $contentId}) { item { id } } }",
                "projectId": project,
                "contentId": node_id,
            },
        )
    except (GhError, KeyError, TypeError) as err:
        return (getattr(err, "message", None) or str(err)).splitlines()[0]
    return None


def issue_body(text: str) -> str:
    idea = extract_idea(text)
    return (idea + FOOTER) if idea else FOOTER_TEXT


# ------------------------------------------------------------------ the command


def run_ideas(
    gh: Gh,
    vault: Path,
    inv: Mapping[str, InventoryEntry],
    *,
    execute: bool,
    report: Callable[[str], None] = print,
) -> IdeasResult:
    """Convert the vault's idea notes. Reads GitHub always; writes only when `execute`.

    Raises GhError when GitHub cannot be listed; a failure on one note is reported and counted.
    """
    gh.read_only = not execute
    ideas, left_out = find_ideas(vault)
    for line in left_out:
        report(line)
    live = {str(r["name"]).lower(): bool(r["private"]) for r in list_repos(gh)}
    private = private_names(inv, [n for n, is_private in live.items() if is_private])
    created = refused = failed = 0
    for idea in ideas:
        entry = find_entry(inv, idea.repo)
        if entry is None:
            report(f"  refused: {printable(idea.title)}: {idea.repo} is not in the inventory")
            refused += 1
            continue
        # Look the target up by both of its names; a repo GitHub does not list is refused.
        flags = [live[n.lower()] for n in {entry.name, entry.target} if n.lower() in live]
        if not flags:
            report(f"  refused: {printable(idea.title)}: {entry.name} was not found on GitHub")
            refused += 1
            continue
        # The target is private only when every source of truth says so.
        body = issue_body(idea.text)
        if not (entry.visibility == "private" and all(flags)):
            # Scan exactly what will be sent, plus the whole note.
            leaked = names_in("\n".join((idea.title, body, idea.text)), private)
            if leaked:
                report(
                    f"  refused: {printable(idea.title)}: names the private repo {leaked[0]}, "
                    f"but {entry.name} is public"
                )
                refused += 1
                continue
        if not execute:
            report(f"  would create: {entry.name}: {printable(idea.title)}")
            created += 1
            continue
        try:
            issue = gh.api(
                f"/repos/{gh.owner}/{entry.name}/issues",
                "POST",
                fields={"title": idea.title, "body": body},
            )
            url = str(issue["html_url"])
        except (GhError, KeyError, TypeError) as err:
            failed += 1
            message = getattr(err, "message", None) or str(err)
            report(
                f"  FAILED {printable(idea.title)}: "
                f"{printable((message.splitlines() or ['unknown error'])[0])}"
            )
            continue
        created += 1
        report(f"  created: {entry.name}: {printable(idea.title)}: {url}")
        try:  # right away: the note must never be converted twice
            updated = set_properties(idea.text, {"github": "issued", "issue": url})
            idea.path.write_bytes(updated.encode("utf-8"))
        except OSError as err:
            failed += 1
            report(
                f"  FAILED {printable(idea.title)}: issue {url} exists but the note "
                f"was not updated: {printable(err)}"
            )
        problem = add_to_project(gh, str(issue.get("node_id", "")))
        if problem:
            report(
                f"  warning: {printable(idea.title)}: not added to the {PROJECT_TITLE} "
                f"project: {printable(problem)}"
            )
    return IdeasResult(created, refused, len(left_out), failed)
