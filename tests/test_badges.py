import json
from pathlib import Path
from typing import Any

from jts_ops.badges import build_badges, write_badges
from jts_ops.cli import main


def dashboard(**summary: int) -> dict[str, Any]:
    base = {"total": 4, "compliant": 4, "percent": 100, "ci_green": 3, "ci_total": 3}
    return {"generated_at": "2026-11-13T00:00:00Z", "summary": {**base, **summary}, "repos": []}


def test_all_green_badges():
    badges = build_badges(dashboard())
    assert badges["badge-compliance.json"] == {
        "schemaVersion": 1,
        "label": "repos on standard",
        "message": "100%",
        "color": "brightgreen",
    }
    assert badges["badge-ci.json"] == {
        "schemaVersion": 1,
        "label": "CI green",
        "message": "3/3",
        "color": "brightgreen",
    }


def test_colors_degrade_with_the_numbers():
    badges = build_badges(dashboard(percent=80, ci_green=3, ci_total=4))
    assert badges["badge-compliance.json"]["color"] == "yellow"
    assert badges["badge-ci.json"]["color"] == "yellow"
    badges = build_badges(dashboard(percent=40, ci_green=0))
    assert badges["badge-compliance.json"]["color"] == "red"
    assert badges["badge-ci.json"]["color"] == "red"


def test_no_ci_runs_is_grey():
    badge = build_badges(dashboard(ci_green=0, ci_total=0))["badge-ci.json"]
    assert (badge["message"], badge["color"]) == ("n/a", "lightgrey")


def test_cli_writes_both_files(tmp_path: Path):
    source = tmp_path / "dashboard.json"
    source.write_text(json.dumps(dashboard()), encoding="utf-8")
    out = tmp_path / "site" / "nested"
    assert main(["badges", "--from", str(source), "--out", str(out)]) == 0
    assert sorted(p.name for p in out.iterdir()) == ["badge-ci.json", "badge-compliance.json"]
    data = json.loads((out / "badge-ci.json").read_text(encoding="utf-8"))
    assert data["schemaVersion"] == 1


def test_write_badges_returns_paths(tmp_path: Path):
    paths = write_badges(dashboard(), tmp_path)
    assert [p.name for p in paths] == ["badge-compliance.json", "badge-ci.json"]
