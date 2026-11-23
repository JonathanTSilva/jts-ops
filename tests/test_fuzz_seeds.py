"""Replay the fuzz corpus through the fuzz target, without atheris."""

import importlib.util
from collections.abc import Callable
from pathlib import Path

import pytest

FUZZ_DIR = Path(__file__).resolve().parent.parent / "fuzz"
SEEDS = sorted((FUZZ_DIR / "corpus" / "frontmatter").glob("*.note"))


def _load_fuzz_one() -> Callable[[bytes], None]:
    spec = importlib.util.spec_from_file_location(
        "fuzz_frontmatter", FUZZ_DIR / "fuzz_frontmatter.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.fuzz_one


fuzz_one = _load_fuzz_one()


def test_seed_corpus_is_present() -> None:
    assert SEEDS


@pytest.mark.parametrize("seed", SEEDS, ids=lambda p: p.name)
def test_frontmatter_seed_does_not_raise(seed: Path) -> None:
    fuzz_one(seed.read_bytes())


def test_invalid_utf8_is_skipped() -> None:
    # find_ideas skips notes that are not valid UTF-8 before parsing them.
    fuzz_one(b"---\ngithub: idea\n---\n\xff\xfe")
