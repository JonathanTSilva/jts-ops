import datetime
import json
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from jts_ops.cli import main
from jts_ops.gh import Gh
from jts_ops.inventory import InventoryEntry
from jts_ops.state import RepoState
from jts_ops.vault import END, NOTES_DIR, START, RepoExtra, render_note

FIXTURES = Path(__file__).parent / "fixtures"
OWNER = "ExampleOwner"
NOW = datetime.datetime(2026, 11, 14, 9, 30)
EXTRA = RepoExtra("python", "v1.2.0", 3)


def make_state(**overrides: Any) -> RepoState:
    base: dict[str, Any] = {
        "name": "ai-helper",
        "private": False,
        "archived": False,
        "description": "A helper",
        "topics": ("ai", "tool"),
        "has_wiki": False,
        "has_discussions": False,
        "allow_squash_merge": True,
        "allow_merge_commit": False,
        "allow_rebase_merge": False,
        "delete_branch_on_merge": True,
        "default_branch": "main",
        "license_spdx": "MIT",
        "files": frozenset(),
        "readme": None,
        "ci_conclusion": "success",
        "labels": (),
    }
    return RepoState(**{**base, **overrides})


def make_entry(**overrides: Any) -> InventoryEntry:
    base: dict[str, Any] = {
        "name": "ai-helper",
        "target": "ai-helper",
        "kind": "ai",
        "visibility": "public",
        "fate": "keep",
        "profile": "active",
        "license": "mit",
        "status": "active",
        "featured": False,
        "description": "A helper",
    }
    return InventoryEntry(**{**base, **overrides})


def render(
    existing: str | None = None,
    state: RepoState | None = None,
    entry: InventoryEntry | None = None,
    extra: RepoExtra = EXTRA,
    tags: Sequence[str] = ("github", "ai"),
) -> str | None:
    return render_note(
        state or make_state(),
        entry or make_entry(),
        existing,
        extra,
        owner=OWNER,
        now=NOW,
        tags=tags,
    )


def frontmatter(text: str) -> list[str]:
    return text.split("---\n")[1].splitlines()


# ------------------------------------------------------------------ render_note (pure)


def test_new_note_has_all_properties_and_block():
    text = render()
    assert text is not None
    assert frontmatter(text) == [
        "type: repository",
        "repo: ai-helper",
        f"url: https://github.com/{OWNER}/ai-helper",
        "visibility: public",
        "kind: ai",
        "status: active",
        "stack: python",
        "topics: [ai, tool]",
        "last_release: v1.2.0",
        "ci: success",
        "open_issues: 3",
        "archived: false",
        "date: 2026-11-14T09:30",
        "tags: [github, ai]",
        "language: en",
    ]
    assert text.count(START) == 1
    assert text.count(END) == 1
    assert "| Open issues | 3 |" in text
    assert "A helper" in text
    assert f"https://app.codecov.io/gh/{OWNER}/ai-helper" in text
    assert f"id={OWNER}_ai-helper" in text


def test_kind_tag_only_when_in_the_registry():
    text = render(tags=("github",))
    assert text is not None
    assert "tags: [github]" in text


def test_private_repo_has_no_codecov_or_sonar_links():
    text = render(state=make_state(private=True))
    assert text is not None
    assert "visibility: private" in text
    assert "codecov" not in text.lower()
    assert "sonarcloud" not in text


def test_empty_values():
    text = render(state=make_state(ci_conclusion=None, topics=()), extra=RepoExtra("none", "", 0))
    assert text is not None
    lines = frontmatter(text)
    assert "stack: none" in lines
    assert "topics: []" in lines
    assert "last_release: ''" in lines
    assert "ci: ''" in lines
    assert "open_issues: 0" in lines


def test_rendering_is_idempotent():
    first = render()
    assert first is not None
    assert render(first) is None


def test_unsupported_inventory_status_is_inactive():
    text = render(entry=make_entry(status="unsupported"))
    assert text is not None
    assert "status: inactive" in text


def test_jonathans_body_and_properties_survive_and_keep_their_order():
    existing = (
        "---\n"
        "# my comment\n"
        "description: Mine, with: colon\n"
        "type: repository\n"
        "repo: ai-helper\n"
        "status: wip\n"
        "my_rating:   4   # keep\n"
        "tags:\n"
        "  - github\n"
        "  - mine\n"
        "related: ['[[Other]]']\n"
        "language: en\n"
        "date: 2026-01-01\n"
        "---\n"
        "# My title\n\n"
        "My own thoughts.\n\n"
        f"{START}\nold block\n{END}\n\n"
        "More thoughts after.\n"
    )
    text = render(existing)
    assert text is not None
    assert text.startswith(existing.split("status: wip")[0])
    for kept in (
        "# my comment\n",
        "description: Mine, with: colon\n",
        "my_rating:   4   # keep\n",
        "tags:\n  - github\n  - mine\n",
        "related: ['[[Other]]']\n",
        "date: 2026-01-01\n",
    ):
        assert kept in text
    assert text.endswith("\n\nMore thoughts after.\n")
    assert "My own thoughts.\n\n" + START in text
    assert "old block" not in text
    assert "status: active\n" in text
    keys = [line.split(":")[0] for line in frontmatter(text) if line[0].isalpha()]
    # His keys keep their order; the missing owned keys are appended after them.
    assert keys[:8] == [
        "description",
        "type",
        "repo",
        "status",
        "my_rating",
        "tags",
        "related",
        "language",
    ]
    assert keys[8] == "date"
    assert keys[9:] == ["url", "visibility", "kind", "stack", "topics", "last_release", "ci"] + [
        "open_issues",
        "archived",
    ]
    # One more run changes nothing.
    assert render(text) is None


def test_note_without_markers_gets_the_block_appended():
    existing = "---\ntype: repository\n---\nJust my notes."
    text = render(existing)
    assert text is not None
    assert "Just my notes.\n\n" + START in text
    assert text.endswith(END + "\n")


def test_note_without_frontmatter_gets_one():
    text = render("Only a body.\n")
    assert text is not None
    assert text.startswith("---\ntype: repository\n")
    assert "Only a body.\n" in text


def test_crlf_note_stays_crlf():
    existing = render()
    assert existing is not None
    crlf = existing.replace("\n", "\r\n")
    changed = render(crlf, state=make_state(ci_conclusion="failure"))
    assert changed is not None
    assert "\n" not in changed.replace("\r\n", "")
    assert "ci: failure\r\n" in changed


def test_archived_repo_sets_archived_and_status():
    text = render(state=make_state(archived=True))
    assert text is not None
    assert "archived: true" in text
    assert "status: archived" in text


def test_unbalanced_markers_are_refused():
    from jts_ops.vault import VaultError

    with pytest.raises(VaultError):
        render(f"---\ntype: repository\n---\n{START}\nno end\n")


def test_create_only_properties_are_not_overwritten():
    existing = "---\ntype: repository\ndate: 1999-01-01\ntags: [mine]\nlanguage: pt-BR\n---\n"
    text = render(existing)
    assert text is not None
    assert "date: 1999-01-01\n" in text
    assert "tags: [mine]\n" in text
    assert "language: pt-BR\n" in text


# ------------------------------------------------------------------ the command


def repo_json(name: str, **overrides: Any) -> dict[str, Any]:
    data = json.loads((FIXTURES / "gh" / "repo.json").read_text(encoding="utf-8"))
    return {**data, "name": name, "language": "Python", **overrides}


class FakeGithub:
    def __init__(self, repos: list[dict[str, Any]], renamed: dict[str, str] | None = None):
        self.repos = repos
        self.renamed = renamed or {}
        self.methods: set[str] = set()

    def __call__(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        path = next(a for a in args if a.startswith("/"))
        self.methods.add(args[args.index("--method") + 1])

        def ok(body: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args, 0, json.dumps(body), "")

        def fail(status: int) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args, 1, "", f"gh: error (HTTP {status})\n")

        if path.startswith("/user/repos"):
            page: list[object] = list(self.repos)
            return ok([page])
        name = path.split("/")[3]
        if len(path.split("/")) == 4:  # /repos/Owner/name
            if name in self.renamed:
                return ok(repo_json(self.renamed[name], full_name=f"{OWNER}/{self.renamed[name]}"))
            return fail(404)
        if "/git/trees/" in path:
            return ok({"tree": [{"path": "README.md", "type": "blob"}]})
        if path.endswith("/readme"):
            return ok({"encoding": "base64", "content": "IyBoaQo="})
        if "/actions/workflows/ci.yml/runs" in path:
            return ok({"workflow_runs": [{"conclusion": "success"}]})
        if path.endswith("/labels"):
            labels: list[object] = []
            return ok([labels])
        if path.endswith("/releases/latest"):
            return ok({"tag_name": "v2.0.0"}) if name == "ai-helper" else fail(404)
        if "/issues?" in path:
            issues: list[object] = [{"number": 1}, {"number": 2, "pull_request": {}}]
            return ok([issues])
        raise AssertionError(path)


@pytest.fixture
def standard_dir(tmp_path: Path) -> Path:
    std = tmp_path / "std"
    std.mkdir()
    for name in ("standard.yml", "labels.yml", "inventory.yml"):
        (std / name).write_bytes((FIXTURES / "standard" / name).read_bytes())
    return std


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "99-Meta" / "Schema").mkdir(parents=True)
    (root / "99-Meta" / "Schema" / "tags.yaml").write_text(
        "allowed:\n  - github\n  - ai\naliases: {}\n", encoding="utf-8"
    )
    return root


@pytest.fixture(autouse=True)
def no_real_private_inventory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("JTS_PRIVATE_INVENTORY", str(tmp_path / "absent.yml"))


def run(
    standard_dir: Path,
    vault: Path,
    fake: FakeGithub,
    capsys: pytest.CaptureFixture[str],
    *extra: str,
) -> tuple[int, str]:
    code = main(
        ["vault-sync", "--standard-dir", str(standard_dir), "--vault", str(vault), *extra],
        Gh(OWNER, fake),
    )
    return code, capsys.readouterr().out.strip().splitlines()[-1]


def test_creates_then_is_unchanged(
    standard_dir: Path, vault: Path, capsys: pytest.CaptureFixture[str]
):
    fake = FakeGithub([repo_json("ai-helper")])
    code, summary = run(standard_dir, vault, fake, capsys)
    assert (code, summary) == (0, "created 1, updated 0, unchanged 0, renamed 0")
    note = vault / NOTES_DIR / "ai-helper.md"
    text = note.read_text(encoding="utf-8")
    assert "last_release: v2.0.0" in text
    assert "open_issues: 1" in text  # the PR is not counted
    assert "stack: python" in text
    assert "tags: [github, ai]" in text
    before = note.stat().st_mtime_ns
    code, summary = run(standard_dir, vault, fake, capsys)
    assert (code, summary) == (0, "created 0, updated 0, unchanged 1, renamed 0")
    assert note.stat().st_mtime_ns == before  # not rewritten
    assert fake.methods == {"GET"}


def test_dry_run_writes_nothing(
    standard_dir: Path, vault: Path, capsys: pytest.CaptureFixture[str]
):
    fake = FakeGithub([repo_json("ai-helper")])
    code, summary = run(standard_dir, vault, fake, capsys, "--dry-run")
    assert (code, summary) == (0, "dry run: created 1, updated 0, unchanged 0, renamed 0")
    assert not (vault / NOTES_DIR).exists()


def test_repo_not_in_inventory_is_skipped(
    standard_dir: Path, vault: Path, capsys: pytest.CaptureFixture[str]
):
    fake = FakeGithub([repo_json("unknown-thing")])
    code, summary = run(standard_dir, vault, fake, capsys)
    assert (code, summary) == (0, "created 0, updated 0, unchanged 0, renamed 0")
    assert not (vault / NOTES_DIR / "unknown-thing.md").exists()


def test_update_keeps_jonathans_edits(
    standard_dir: Path, vault: Path, capsys: pytest.CaptureFixture[str]
):
    fake = FakeGithub([repo_json("ai-helper")])
    run(standard_dir, vault, fake, capsys)
    note = vault / NOTES_DIR / "ai-helper.md"
    text = note.read_text(encoding="utf-8")
    note.write_text(
        text.replace("# ai-helper\n", "# ai-helper\n\nMy idea.\n").replace(
            "type: repository\n", "type: repository\nmine: yes\n"
        ),
        encoding="utf-8",
    )
    fake.repos = [repo_json("ai-helper", description="New description")]
    code, summary = run(standard_dir, vault, fake, capsys)
    assert (code, summary) == (0, "created 0, updated 1, unchanged 0, renamed 0")
    after = note.read_text(encoding="utf-8")
    assert "My idea." in after
    assert "mine: yes\n" in after
    assert "New description" in after


def test_renamed_repo_moves_its_note_and_keeps_the_body(
    standard_dir: Path, vault: Path, capsys: pytest.CaptureFixture[str]
):
    notes = vault / NOTES_DIR
    notes.mkdir(parents=True)
    (notes / "old-widget.md").write_text(
        "---\ntype: repository\nrepo: old-widget\nmine: 1\n---\n# Widget\n\nMy notes.\n",
        encoding="utf-8",
    )
    fake = FakeGithub([repo_json("widget")], renamed={"old-widget": "widget"})
    code, summary = run(standard_dir, vault, fake, capsys)
    assert (code, summary) == (0, "created 0, updated 0, unchanged 0, renamed 1")
    assert not (notes / "old-widget.md").exists()
    text = (notes / "widget.md").read_text(encoding="utf-8")
    assert "repo: widget\n" in text
    assert "mine: 1\n" in text
    assert "My notes." in text


def test_rename_onto_an_existing_note_is_refused(
    standard_dir: Path, vault: Path, capsys: pytest.CaptureFixture[str]
):
    notes = vault / NOTES_DIR
    notes.mkdir(parents=True)
    (notes / "old-widget.md").write_text("---\nrepo: old-widget\n---\nold\n", encoding="utf-8")
    (notes / "widget.md").write_text("---\nrepo: elsewhere\n---\nmine\n", encoding="utf-8")
    fake = FakeGithub([repo_json("widget")], renamed={"old-widget": "widget"})
    code, _ = run(standard_dir, vault, fake, capsys)
    assert code == 1
    assert (notes / "old-widget.md").read_text(
        encoding="utf-8"
    ) == "---\nrepo: old-widget\n---\nold\n"
    assert (notes / "widget.md").read_text(encoding="utf-8") == "---\nrepo: elsewhere\n---\nmine\n"


def test_archived_repo_gets_archived_true(
    standard_dir: Path, vault: Path, capsys: pytest.CaptureFixture[str]
):
    fake = FakeGithub([repo_json("ai-helper")])
    run(standard_dir, vault, fake, capsys)
    fake.repos = [repo_json("ai-helper", archived=True)]
    code, summary = run(standard_dir, vault, fake, capsys)
    assert (code, summary) == (0, "created 0, updated 1, unchanged 0, renamed 0")
    text = (vault / NOTES_DIR / "ai-helper.md").read_text(encoding="utf-8")
    assert "archived: true" in text
    assert "status: archived" in text


def test_deleted_repo_leaves_its_note_alone(
    standard_dir: Path, vault: Path, capsys: pytest.CaptureFixture[str]
):
    notes = vault / NOTES_DIR
    notes.mkdir(parents=True)
    note = notes / "gone-repo.md"
    note.write_text("---\nrepo: gone-repo\n---\nkept\n", encoding="utf-8")
    fake = FakeGithub([repo_json("ai-helper")])
    code, summary = run(standard_dir, vault, fake, capsys)
    assert (code, summary) == (0, "created 1, updated 0, unchanged 0, renamed 0")
    assert note.read_text(encoding="utf-8") == "---\nrepo: gone-repo\n---\nkept\n"


def test_missing_vault_is_an_error(
    standard_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    code = main(
        ["vault-sync", "--standard-dir", str(standard_dir), "--vault", str(tmp_path / "nope")],
        Gh(OWNER, FakeGithub([])),
    )
    assert code == 2
    assert "no such vault" in capsys.readouterr().err
