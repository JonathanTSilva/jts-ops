"""The GitHub standard as data: loads `standard.yml` and `labels.yml`."""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class Kind:
    id: str
    prefix: str | None
    suffix: str | None
    emoji: str
    topic: str


@dataclass(frozen=True)
class Label:
    name: str
    color: str
    description: str


@dataclass(frozen=True)
class Standard:
    owner: str
    author: str
    contact_email: str
    plan: str
    name_pattern: re.Pattern[str]
    special_names: frozenset[str]
    kinds: dict[str, Kind]
    statuses: tuple[str, ...]
    required_files: dict[str, tuple[str, ...]]
    licenses: tuple[str, ...]
    labels: tuple[Label, ...]


def _read_yaml(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def load_standard(standard_dir: Path) -> Standard:
    """Load the standard from `standard.yml` and `labels.yml` in `standard_dir`."""
    data = _read_yaml(standard_dir / "standard.yml")
    labels = _read_yaml(standard_dir / "labels.yml")
    kinds = {
        k["id"]: Kind(
            id=k["id"],
            prefix=k["prefix"],
            suffix=k["suffix"],
            emoji=k["emoji"],
            topic=k["topic"],
        )
        for k in data["kinds"]
    }
    return Standard(
        owner=data["owner"],
        author=data["author"],
        contact_email=data["contact_email"],
        plan=data["plan"],
        name_pattern=re.compile(data["name_pattern"]),
        special_names=frozenset(data["special_names"]),
        kinds=kinds,
        statuses=tuple(data["statuses"]),
        required_files={
            profile: tuple(spec["required_files"]) for profile, spec in data["profiles"].items()
        },
        licenses=tuple(data["licenses"]),
        labels=tuple(
            Label(name=label["name"], color=label["color"], description=label["description"])
            for label in labels
        ),
    )
