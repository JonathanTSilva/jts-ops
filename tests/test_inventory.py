from pathlib import Path

import pytest

from jts_ops.inventory import InventoryEntry, by_target, load_inventory
from jts_ops.standard import Standard


def test_public_and_private_are_merged(inv: dict[str, InventoryEntry]):
    assert set(inv) == {"ExampleOwner", "old-widget", "ai-helper", "hidden-notes"}


def test_keyed_by_current_name(inv: dict[str, InventoryEntry]):
    assert inv["old-widget"].target == "widget"


def test_entry_fields(inv: dict[str, InventoryEntry]):
    entry = inv["old-widget"]
    assert entry.kind == "product"
    assert entry.visibility == "public"
    assert entry.fate == "rebuild"
    assert entry.profile == "active"
    assert entry.license == "mit"
    assert entry.status == "wip"
    assert entry.featured is True
    assert entry.description == "Example product that gets renamed"


def test_every_entry_uses_known_kind_and_license(std: Standard, inv: dict[str, InventoryEntry]):
    assert all(e.kind in std.kinds for e in inv.values())
    assert all(e.license in std.licenses for e in inv.values())


def test_by_target(inv: dict[str, InventoryEntry]):
    targets = by_target(inv)
    assert targets["widget"].name == "old-widget"
    assert "old-widget" not in targets


def test_missing_private_file_means_public_only(fixtures_dir: Path, tmp_path: Path):
    inv = load_inventory(fixtures_dir, tmp_path / "absent.yml")
    assert set(inv) == {"ExampleOwner", "old-widget", "ai-helper"}


def test_name_in_both_files_is_an_error(fixtures_dir: Path):
    with pytest.raises(ValueError, match="ai-helper"):
        load_inventory(fixtures_dir, fixtures_dir / "inventory.conflict.yml")


def test_private_path_from_env(fixtures_dir: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JTS_PRIVATE_INVENTORY", str(fixtures_dir / "inventory.private.yml"))
    assert "hidden-notes" in load_inventory(fixtures_dir)


def test_default_private_path_is_under_home(
    fixtures_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("JTS_PRIVATE_INVENTORY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert "hidden-notes" not in load_inventory(fixtures_dir)
    default = tmp_path / ".config" / "jts-ops" / "inventory.private.yml"
    default.parent.mkdir(parents=True)
    default.write_text((fixtures_dir / "inventory.private.yml").read_text())
    assert "hidden-notes" in load_inventory(fixtures_dir)
