"""Repo inventory: public entries from the standard plus an optional private file."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

PRIVATE_INVENTORY_ENV = "JTS_PRIVATE_INVENTORY"
IGNORED_REPOS_ENV = "JTS_IGNORED_REPOS"


@dataclass(frozen=True)
class InventoryEntry:
    name: str
    target: str
    kind: str
    visibility: str
    fate: str
    profile: str
    license: str
    status: str
    featured: bool
    description: str


def default_private_path() -> Path:
    """Path of the private inventory: `$JTS_PRIVATE_INVENTORY`, else under `~/.config`."""
    override = os.environ.get(PRIVATE_INVENTORY_ENV)
    if override:
        return Path(override).expanduser()
    return Path("~/.config/jts-ops/inventory.private.yml").expanduser()


def default_ignored_path() -> Path:
    """Path of the ignore list: `$JTS_IGNORED_REPOS`, else under `~/.config`."""
    override = os.environ.get(IGNORED_REPOS_ENV)
    if override:
        return Path(override).expanduser()
    return Path("~/.config/jts-ops/ignored.txt").expanduser()


def load_ignored(path: Path | None = None) -> frozenset[str]:
    """Names of repos that are not part of the system, one per line (`#` starts a comment).

    A missing file means nothing is ignored. The file lives outside every repo because
    a repo name may be private.
    """
    target = path if path is not None else default_ignored_path()
    if not target.is_file():
        return frozenset()
    names: set[str] = set()
    for line in target.read_text(encoding="utf-8").splitlines():
        name = line.split("#", 1)[0].strip()
        if name:
            names.add(name)
    return frozenset(names)


def _read_entries(path: Path) -> list[InventoryEntry]:
    with path.open(encoding="utf-8") as handle:
        rows: list[dict[str, Any]] = yaml.safe_load(handle) or []
    return [InventoryEntry(**row) for row in rows]


def load_inventory(
    standard_dir: Path, private_path: Path | None = None
) -> dict[str, InventoryEntry]:
    """Merge the public and private inventories, keyed by current repo name.

    A missing private file means public entries only. A name present in both files
    raises `ValueError`.
    """
    entries = _read_entries(standard_dir / "inventory.yml")
    inventory = {entry.name: entry for entry in entries}
    private = private_path if private_path is not None else default_private_path()
    if private.is_file():
        for entry in _read_entries(private):
            if entry.name in inventory:
                raise ValueError(f"inventory name in both public and private files: {entry.name}")
            inventory[entry.name] = entry
    return inventory


def by_target(inv: dict[str, InventoryEntry]) -> dict[str, InventoryEntry]:
    """Index the inventory by target name instead of current name."""
    return {entry.target: entry for entry in inv.values()}
