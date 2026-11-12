"""Checks against the real standard and inventories.

Skipped when the `.github` checkout or the private inventory is absent (for example in CI).
"""

import os
from pathlib import Path

import pytest

from jts_ops.inventory import InventoryEntry, default_private_path, load_inventory
from jts_ops.naming import check_name
from jts_ops.standard import Standard, load_standard

STANDARD_DIR = Path(os.environ.get("JTS_STANDARD_DIR", "~/Projects/.github")).expanduser()
PRIVATE_PATH = default_private_path()

if not (STANDARD_DIR / "standard.yml").is_file() or not PRIVATE_PATH.is_file():
    pytest.skip("real standard or private inventory not available", allow_module_level=True)


@pytest.fixture(scope="module")
def real_std() -> Standard:
    return load_standard(STANDARD_DIR)


@pytest.fixture(scope="module")
def real_inv() -> dict[str, InventoryEntry]:
    return load_inventory(STANDARD_DIR, PRIVATE_PATH)


def test_labels(real_std: Standard):
    assert len(real_std.labels) == 25


def test_kinds(real_std: Standard):
    assert real_std.kinds["ai"].prefix == "ai-"


def test_required_files(real_std: Standard):
    assert {"AGENTS.md", ".claude/settings.json"} <= set(real_std.required_files["active"])


def test_inventory_size(real_inv: dict[str, InventoryEntry]):
    assert len(real_inv) == 40


def test_kinds_and_licenses_valid(real_std: Standard, real_inv: dict[str, InventoryEntry]):
    assert all(e.kind in real_std.kinds for e in real_inv.values())
    assert all(e.license in real_std.licenses for e in real_inv.values())


def test_every_target_is_valid(real_std: Standard, real_inv: dict[str, InventoryEntry]):
    problems = {
        e.target: check_name(e.target, e.kind, real_std)
        for e in real_inv.values()
        if check_name(e.target, e.kind, real_std)
    }
    assert problems == {}
