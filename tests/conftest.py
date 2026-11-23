from pathlib import Path

import pytest

from jts_ops.inventory import InventoryEntry, load_inventory
from jts_ops.standard import Standard, load_standard

FIXTURES = Path(__file__).parent / "fixtures" / "standard"


@pytest.fixture(autouse=True)
def _no_real_ignore_list(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Never read the developer's real ignore list."""
    monkeypatch.setenv("JTS_IGNORED_REPOS", str(tmp_path / "no-ignored.txt"))


@pytest.fixture(autouse=True)
def _cwd_is_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Report and plan paths are confined to the cwd (or _reports); tests write under tmp_path."""
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def std() -> Standard:
    return load_standard(FIXTURES)


@pytest.fixture
def inv() -> dict[str, InventoryEntry]:
    return load_inventory(FIXTURES, FIXTURES / "inventory.private.yml")
