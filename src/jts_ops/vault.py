"""`jts-ops vault-sync`: one Obsidian note per GitHub repo, kept current.

`render_note` is pure: it turns the observed state of one repo plus the note's current text
into the note's new text, or None when nothing changed. The rules:

- A note has YAML properties and a generated block between `START` and `END`. Only the
  properties `jts-ops` owns (`OWNED`) and the text between the markers are ever rewritten.
  Everything else, Jonathan's body and his own properties, is kept byte for byte, and his key
  order is never changed: an owned property is replaced in place, a missing one is appended.
- `date`, `tags` and `language` are only filled in when missing: after that they are his.
- The frontmatter is split into top-level key blocks by line, not parsed and dumped, so the
  formatting of properties `jts-ops` does not touch survives exactly.
- A note is written only when its text actually changed, so there is no timestamp churn.
- Nothing is deleted. A note whose repo was renamed is moved to the new name, body kept.
- Obsidian hides files whose name starts with ".", so the note of `.github` is `dot-github.md`;
  an existing `.github.md` is moved there, body kept.
"""

import datetime
import re
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import yaml

from jts_ops.gh import Gh, GhError, fetch_state, list_repos
from jts_ops.inventory import InventoryEntry
from jts_ops.state import RepoState
from jts_ops.sync import LocalClone, github_url, resolve_renames

START = "<!-- jts-ops:start -->"
END = "<!-- jts-ops:end -->"
NOTES_DIR = Path("02-Areas/Open Source/Repositories")
TAG_REGISTRY = Path("99-Meta/Schema/tags.yaml")
NOTE_TYPE = "repository"
NOTE_LANGUAGE = "en"
DATE_FORMAT = "%Y-%m-%dT%H:%M"
# Properties `jts-ops` keeps in line with GitHub on every run, in the order they are appended.
OWNED = (
    "type",
    "repo",
    "url",
    "visibility",
    "kind",
    "status",
    "stack",
    "topics",
    "last_release",
    "ci",
    "open_issues",
    "archived",
)
# Properties filled in when missing and never touched again.
CREATE_ONLY = ("date", "tags", "language")

# The new-note template used to leave two blank lines between the heading and the block.
_DOUBLE_BLANK = re.compile(r"\A(# [^\r\n]*)(\r?\n)(?:\r?\n){2}(?=" + re.escape(START) + ")")
_KEY = re.compile(r"^([A-Za-z_][\w-]*):(?:[ \t]|\r?\n?$)")
_BOM = "﻿"


class VaultError(Exception):
    """A note cannot be updated safely; the message is the reason shown to the user."""


@dataclass(frozen=True)
class RepoExtra:
    """What GitHub tells about a repo beyond `RepoState`."""

    stack: str  # primary language, lowercased, or "none"
    last_release: str  # latest release tag, or ""
    open_issues: int  # open issues, pull requests excluded


@dataclass(frozen=True)
class VaultResult:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    renamed: int = 0
    failed: int = 0

    def summary(self, *, dry_run: bool) -> str:
        line = (
            f"created {self.created}, updated {self.updated}, "
            f"unchanged {self.unchanged}, renamed {self.renamed}"
        )
        return f"dry run: {line}" if dry_run else line


# ------------------------------------------------------------------ frontmatter splitting


@dataclass
class _Entry:
    """A run of frontmatter lines: one top-level key with its continuation lines, or the
    comment and blank lines before the first key (`key` is None)."""

    key: str | None
    lines: list[str]


@dataclass
class _Note:
    prefix: str  # BOM, if any
    nl: str
    entries: list[_Entry]
    body: str


def _split(text: str) -> _Note:
    bom = _BOM if text.startswith(_BOM) else ""
    rest = text[len(bom) :]
    nl = "\n"
    if rest.startswith("---\r\n"):
        nl = "\r\n"
    elif not rest.startswith("---\n"):
        return _Note(bom, "\r\n" if "\r\n" in rest else "\n", [], rest)
    lines = rest.splitlines(keepends=True)
    for end in range(1, len(lines)):
        if lines[end].rstrip("\r\n") == "---":
            break
    else:  # never closed: not frontmatter
        return _Note(bom, nl, [], rest)
    entries: list[_Entry] = []
    for line in lines[1:end]:
        match = _KEY.match(line)
        if match:
            entries.append(_Entry(match[1], [line]))
        elif entries:
            entries[-1].lines.append(line)
        else:
            entries.append(_Entry(None, [line]))
    return _Note(bom, nl, entries, "".join(lines[end + 1 :]))


def _join(note: _Note) -> str:
    fm = "".join(line for entry in note.entries for line in entry.lines)
    return f"{note.prefix}---{note.nl}{fm}---{note.nl}{note.body}"


def _value_of(entry: _Entry) -> Any:
    """The parsed value of one key block; raises ValueError when it is not valid YAML."""
    try:
        data = yaml.safe_load("".join(entry.lines))
    except yaml.YAMLError as err:
        raise ValueError(str(err)) from err
    if not isinstance(data, dict) or entry.key not in data:
        raise ValueError("not a mapping")
    return data[entry.key]  # pyright: ignore[reportUnknownVariableType]


def _dump(value: Any) -> str:
    """One YAML value on one line, in the flow style the vault uses for lists."""
    text = yaml.safe_dump(value, default_flow_style=True, width=1_000_000, allow_unicode=True)
    return text.removesuffix("\n...\n").rstrip("\n")


def _same(have: Any, want: Any) -> bool:
    if have is None:
        have = ""
    if isinstance(have, list):
        have = [str(x) for x in cast(list[Any], have)]
    return type(have) is type(want) and have == want


def note_filename(repo: str) -> str:
    """The note's file name: Obsidian hides names starting with ".", so `.github` is
    `dot-github.md`."""
    return f"dot-{repo[1:]}.md" if repo.startswith(".") else f"{repo}.md"


def read_properties(text: str) -> dict[str, Any]:
    """The frontmatter properties of a note that can be read; unreadable ones are left out."""
    props: dict[str, Any] = {}
    for entry in _split(text).entries:
        if entry.key is None:
            continue
        try:
            props[entry.key] = _value_of(entry)
        except ValueError:
            continue
    return props


def set_properties(text: str, updates: Mapping[str, Any]) -> str:
    """`text` with the given properties replaced in place, or appended when missing. Nothing
    else in the note changes, not even the key order or the formatting of other properties."""
    note = _split(text)
    by_key = {e.key: e for e in note.entries if e.key is not None}
    for key, value in updates.items():
        lines = [f"{key}: {_dump(value)}{note.nl}"]
        if key in by_key:
            by_key[key].lines = lines
        else:
            note.entries.append(_Entry(key, lines))
    return _join(note)


def note_repo(text: str) -> str | None:
    """The `repo` property of a note, or None when it has none or cannot be read."""
    for entry in _split(text).entries:
        if entry.key == "repo":
            try:
                value = _value_of(entry)
            except ValueError:
                return None
            return value if isinstance(value, str) and value else None
    return None


# ------------------------------------------------------------------ rendering


def _clean(text: str) -> str:
    return " ".join(text.split())


def _block(
    state: RepoState, entry: InventoryEntry, extra: RepoExtra, owner: str, status: str
) -> str:
    url = f"https://github.com/{owner}/{state.name}"
    links = [f"[GitHub]({url})"]
    if not state.private:
        links.append(f"[Codecov](https://app.codecov.io/gh/{owner}/{state.name})")
        sonar = f"https://sonarcloud.io/project/overview?id={owner}_{state.name}"
        links.append(f"[SonarQube Cloud]({sonar})")
    facts = [
        ("Kind", entry.kind),
        ("Status", status),
        ("Visibility", "private" if state.private else "public"),
        ("Stack", extra.stack),
        ("License", state.license_spdx or "none"),
        ("Default branch", state.default_branch or "none"),
        ("Last release", extra.last_release or "none"),
        ("CI", state.ci_conclusion or "none"),
        ("Open issues", str(extra.open_issues)),
    ]
    parts = [
        START,
        "<!-- Generated by jts-ops vault-sync; edits in this block are overwritten. -->",
    ]
    if state.description:
        parts += ["", _clean(state.description)]
    parts += ["", "Links: " + " · ".join(links), "", "| Fact | Value |", "|---|---|"]
    parts += [f"| {name} | {value} |" for name, value in facts]
    parts.append(END)
    return "\n".join(parts)


def _vault_status(state: RepoState, entry: InventoryEntry) -> str:
    if state.archived:
        return "archived"
    # The vault has no "unsupported"; an unsupported repo is inactive there.
    return "inactive" if entry.status == "unsupported" else entry.status


def _apply_block(body: str, block: str, nl: str) -> str:
    block = block.replace("\n", nl)
    start = body.find(START)
    end = body.find(END)
    if start == -1 and end == -1:
        if not body.strip():
            return block + nl
        sep = "" if body.endswith(nl) else nl
        return f"{body}{sep}{nl}{block}{nl}"
    if start == -1 or end == -1 or end < start or body.count(START) > 1 or body.count(END) > 1:
        raise VaultError("the jts-ops markers are unbalanced; fix the note by hand")
    return body[:start] + block + body[end + len(END) :]


def render_note(
    state: RepoState,
    entry: InventoryEntry,
    existing_text: str | None,
    extra: RepoExtra,
    *,
    owner: str,
    now: datetime.datetime,
    tags: Collection[str] = (),
) -> str | None:
    """The new text of the repo's note, or None when it would not change.

    `existing_text` is None for a repo with no note yet. `tags` is the vault's tag registry:
    a new note is tagged `github`, plus the repo kind when the registry lists it.
    """
    status = _vault_status(state, entry)
    owned: dict[str, Any] = {
        "type": NOTE_TYPE,
        "repo": state.name,
        "url": f"https://github.com/{owner}/{state.name}",
        "visibility": "private" if state.private else "public",
        "kind": entry.kind,
        "status": status,
        "stack": extra.stack,
        "topics": list(state.topics),
        "last_release": extra.last_release,
        "ci": state.ci_conclusion or "",
        "open_issues": extra.open_issues,
        "archived": state.archived,
    }
    once: dict[str, Any] = {
        "date": now.strftime(DATE_FORMAT),
        "tags": [
            "github",
            *([entry.kind] if entry.kind in tags and entry.kind != "github" else []),
        ],
        "language": NOTE_LANGUAGE,
    }
    note = _split(existing_text if existing_text is not None else "")
    if existing_text is None:
        note.body = f"# {state.name}{note.nl}"
    by_key = {e.key: e for e in note.entries if e.key is not None}

    def line(key: str, value: Any) -> list[str]:
        return [f"{key}: {_dump(value)}{note.nl}"]

    for key, value in owned.items():
        found = by_key.get(key)
        if found is None:
            entry_ = _Entry(key, line(key, value))
            note.entries.append(entry_)
            by_key[key] = entry_
            continue
        try:
            same = _same(_value_of(found), value)
        except ValueError:
            same = False
        if not same:
            found.lines = line(key, value)
    for key, value in once.items():
        if key not in by_key:
            entry_ = _Entry(key, line(key, value))
            note.entries.append(entry_)
            by_key[key] = entry_
    note.body = _DOUBLE_BLANK.sub(r"\1\2\2", note.body, count=1)
    note.body = _apply_block(note.body, _block(state, entry, extra, owner, status), note.nl)
    text = _join(note)
    return None if text == existing_text else text


# ------------------------------------------------------------------ GitHub reads


def fetch_extra(gh: Gh, listed: Mapping[str, Any]) -> RepoExtra:
    """Language, latest release and open issue count (without PRs) of one listed repo."""
    base = f"/repos/{gh.owner}/{listed['name']}"
    release = ""
    try:
        data = gh.api(f"{base}/releases/latest")
        release = str(data.get("tag_name") or "") if data else ""
    except GhError as err:
        if err.status != 404:  # 404: the repo has no release
            raise
    count = 0
    try:
        issues: list[dict[str, Any]] = (
            gh.api(f"{base}/issues?state=open&per_page=100", paginate=True) or []
        )
        for item in issues:
            if "pull_request" not in item:
                count += 1
    except GhError as err:
        if err.status != 410:  # 410: issues are disabled for this repo
            raise
    language = str(listed.get("language") or "").lower()
    return RepoExtra(language or "none", release, count)


def read_tag_registry(vault: Path) -> frozenset[str]:
    """The tags the vault allows; empty when the registry is missing."""
    path = vault / TAG_REGISTRY
    if not path.is_file():
        return frozenset()
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    allowed: list[Any] = data.get("allowed") or []
    return frozenset(str(t) for t in allowed)


# ------------------------------------------------------------------ the sync


def _read(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def find_entry(inv: Mapping[str, InventoryEntry], name: str) -> InventoryEntry | None:
    found = inv.get(name)
    return found or next((e for e in inv.values() if e.target == name), None)


def sync_vault(
    gh: Gh,
    vault: Path,
    inv: Mapping[str, InventoryEntry],
    *,
    dry_run: bool,
    now: datetime.datetime,
    report: Callable[[str], None] = print,
) -> VaultResult:
    """Bring the repo notes of `vault` in line with GitHub. Reads GitHub, writes only notes.

    Raises GhError when GitHub cannot be listed; a failure on one repo is reported through
    `report` and counted, and the others still run.
    """
    notes_dir = vault / NOTES_DIR
    remote = list_repos(gh)
    listed_names = {str(r["name"]).lower() for r in remote}
    tags = read_tag_registry(vault)

    notes: dict[str, Path] = {}  # lowercase repo name -> note path
    if notes_dir.is_dir():
        for path in sorted(notes_dir.rglob("*.md")):
            try:
                repo = note_repo(_read(path)) or path.stem
            except (OSError, UnicodeDecodeError):
                continue
            notes.setdefault(repo.lower(), path)

    # A note whose repo is no longer listed may belong to a renamed repo: ask GitHub.
    stale = {
        key: LocalClone(path, github_url(gh.owner, key), "", None, False, (), False)
        for key, path in notes.items()
        if key not in listed_names
    }
    renamed = resolve_renames(gh, {str(p): c for p, c in stale.items()}, remote, gh.owner)
    renamed = {old: full.partition("/")[2].lower() for old, full in renamed.items()}
    moved_from = {new: old for old, new in renamed.items() if old in notes}

    counts = {"created": 0, "updated": 0, "unchanged": 0, "renamed": 0, "failed": 0}
    for listed in sorted(remote, key=lambda r: str(r["name"])):
        name = str(listed["name"])
        entry = find_entry(inv, name)
        if entry is None:
            report(f"  skip: {name}: not in inventory")
            continue
        try:
            state = fetch_state(gh, listed)
            extra = fetch_extra(gh, listed)
            path = notes.get(name.lower())
            old_path: Path | None = None
            if path is None and name.lower() in moved_from:
                old_path = notes[moved_from[name.lower()]]
                path = notes_dir / note_filename(name)
                if path.exists():
                    raise VaultError(f"cannot rename {old_path.name}: {path.name} exists")
            if path is not None and path.name == f"{name}.md" and name.startswith("."):
                old_path, path = path, path.with_name(note_filename(name))  # hidden by Obsidian
                if path.exists():
                    raise VaultError(f"cannot rename {old_path.name}: {path.name} exists")
            source = old_path or path
            existing = _read(source) if source is not None else None
            target = path or notes_dir / note_filename(name)
            text = render_note(state, entry, existing, extra, owner=gh.owner, now=now, tags=tags)
            if old_path is not None:
                report(f"  renamed: {old_path.name} -> {target.name}")
                counts["renamed"] += 1
                if not dry_run:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    old_path.rename(target)
            elif existing is None:
                report(f"  created: {target.name}")
                counts["created"] += 1
            elif text is not None:
                report(f"  updated: {target.name}")
                counts["updated"] += 1
            else:
                counts["unchanged"] += 1
            if text is not None and not dry_run:
                _write(target, text)
        except (GhError, VaultError, OSError, UnicodeDecodeError) as err:
            counts["failed"] += 1
            message = getattr(err, "message", None) or str(err)
            report(f"  FAILED {name}: {(message.splitlines() or ['unknown error'])[0]}")
    return VaultResult(**counts)
