from pathlib import Path

import pytest

from jts_ops.inventory import InventoryEntry, load_inventory
from jts_ops.standard import Standard, load_standard

FIXTURES = Path(__file__).parent / "fixtures" / "standard"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def std() -> Standard:
    return load_standard(FIXTURES)


@pytest.fixture
def inv() -> dict[str, InventoryEntry]:
    return load_inventory(FIXTURES, FIXTURES / "inventory.private.yml")
