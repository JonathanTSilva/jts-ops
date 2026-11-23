"""Atheris harness for the vault note frontmatter parser.

Arbitrary text is treated as the content of an Obsidian note and fed to the readers and the
writer that `vault-sync` and `ideas` run over every note in the vault: `read_properties`,
`note_repo`, `set_properties` (vault.py) and `extract_idea` (ideas.py). Their documented
contract is that a note that cannot be read is skipped or left unchanged, never an error, so
any exception is a finding.

atheris is not a project dependency. Run from the repository root with:

    uv run --with atheris python fuzz/fuzz_frontmatter.py -runs=100000 fuzz/corpus/frontmatter
"""

import sys


def fuzz_one(data: bytes) -> None:
    """Treat `data` as the UTF-8 text of a note, as `find_ideas` reads it."""
    # Imported here so main() can import them under atheris instrumentation, and so the
    # pytest seed replay needs no atheris.
    from jts_ops.ideas import extract_idea
    from jts_ops.vault import note_repo, read_properties, set_properties

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return  # find_ideas skips notes that are not valid UTF-8 before parsing
    read_properties(text)
    note_repo(text)
    extract_idea(text)
    updated = set_properties(text, {"github": "issued", "issue": "https://example.test/1"})
    # The writer must leave a note it just wrote readable, with the values it set.
    props = read_properties(updated)
    assert props.get("github") == "issued", props
    assert props.get("issue") == "https://example.test/1", props


def main() -> None:
    import atheris

    with atheris.instrument_imports():
        import jts_ops.ideas  # noqa: F401
        import jts_ops.vault  # noqa: F401

    atheris.Setup(sys.argv, fuzz_one)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
