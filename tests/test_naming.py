from jts_ops.inventory import InventoryEntry
from jts_ops.naming import check_name
from jts_ops.standard import Standard


def test_valid_research_name(std: Standard):
    assert check_name("research-pump-cavitation", "research", std) == []


def test_product_needs_no_prefix(std: Standard):
    assert check_name("scriptize", "product", std) == []


def test_uppercase_rejected(std: Standard):
    assert check_name("SI-Cavitation", "research", std) != []


def test_prefix_must_match_kind(std: Standard):
    assert check_name("learn-example", "research", std) != []


def test_prefix_needs_subject(std: Standard):
    assert check_name("research-", "research", std) != []


def test_suffix_kind(std: Standard):
    assert check_name("setup-qemu-action", "action", std) == []


def test_suffix_must_match_kind(std: Standard):
    assert check_name("setup-qemu", "action", std) != []


def test_special_names_exempt(std: Standard):
    assert check_name("ExampleOwner", "special", std) == []
    assert check_name(".github", "special", std) == []
    assert check_name("exampleowner.github.io", "special", std) == []


def test_special_name_requires_special_kind(std: Standard):
    assert check_name("ExampleOwner", "product", std) != []


def test_non_special_name_with_special_kind_is_checked(std: Standard):
    assert check_name("Not-Special", "special", std) != []


def test_product_must_not_look_like_another_kind(std: Standard):
    assert check_name("ai-thing", "product", std) != []
    assert check_name("build-action", "product", std) != []


def test_unknown_kind(std: Standard):
    assert check_name("anything", "nonsense", std) != []


def test_every_inventory_target_is_valid(std: Standard, inv: dict[str, InventoryEntry]):
    problems = {
        e.target: check_name(e.target, e.kind, std)
        for e in inv.values()
        if check_name(e.target, e.kind, std)
    }
    assert problems == {}
