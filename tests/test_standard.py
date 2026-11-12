import re

from jts_ops.standard import Standard


def test_scalars(std: Standard):
    assert std.owner == "ExampleOwner"
    assert std.author == "Example Author"
    assert std.plan == "free"
    assert std.contact_email == "author@example.invalid"


def test_name_pattern_is_compiled(std: Standard):
    assert isinstance(std.name_pattern, re.Pattern)
    assert std.name_pattern.match("a-b-c")
    assert not std.name_pattern.match("A-b")


def test_special_names(std: Standard):
    assert std.special_names == frozenset({"ExampleOwner", "exampleowner.github.io", ".github"})


def test_kinds(std: Standard):
    assert std.kinds["ai"].prefix == "ai-"
    assert std.kinds["ai"].suffix is None
    assert std.kinds["action"].suffix == "-action"
    assert std.kinds["product"].prefix is None
    assert std.kinds["ai"].topic == "kind-ai"


def test_statuses_and_licenses(std: Standard):
    assert std.statuses == ("concept", "wip", "active", "archived")
    assert std.licenses == ("mit", "proprietary")


def test_required_files_per_profile(std: Standard):
    assert "AGENTS.md" in std.required_files["active"]
    assert std.required_files["archived"] == ("README.md",)
    assert std.required_files["exempt"] == ()


def test_labels(std: Standard):
    assert [label.name for label in std.labels] == ["type: bug", "type: docs"]
    assert std.labels[0].color == "d22d48"
    assert std.labels[0].description == "Something is broken"
