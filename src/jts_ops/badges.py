"""shields.io endpoint badges built from a public dashboard JSON."""

import json
from pathlib import Path
from typing import Any

Badge = dict[str, Any]


def _endpoint(label: str, message: str, color: str) -> Badge:
    return {"schemaVersion": 1, "label": label, "message": message, "color": color}


def build_badges(dashboard: dict[str, Any]) -> dict[str, Badge]:
    """Map badge file names to endpoint payloads, from the report's `summary` only."""
    s = dashboard["summary"]
    percent: int = s["percent"]
    compliance_color = "brightgreen" if percent == 100 else "yellow" if percent >= 75 else "red"
    green: int = s["ci_green"]
    total: int = s["ci_total"]
    if total == 0:
        ci = _endpoint("CI green", "n/a", "lightgrey")
    else:
        ci_color = (
            "brightgreen" if green == total else "yellow" if green * 4 >= total * 3 else "red"
        )
        ci = _endpoint("CI green", f"{green}/{total}", ci_color)
    return {
        "badge-compliance.json": _endpoint("repos on standard", f"{percent}%", compliance_color),
        "badge-ci.json": ci,
    }


def write_badges(dashboard: dict[str, Any], out_dir: Path) -> list[Path]:
    """Write the badge files into `out_dir` (created if needed) and return their paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for name, badge in build_badges(dashboard).items():
        path = out_dir / name
        path.write_text(json.dumps(badge, indent=2) + "\n", encoding="utf-8")
        paths.append(path)
    return paths
