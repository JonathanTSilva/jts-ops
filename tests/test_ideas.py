import json
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path

import pytest

from jts_ops import ideas
from jts_ops.cli import main
from jts_ops.gh import Gh
from jts_ops.ideas import extract_idea

FIXTURES = Path(__file__).parent / "fixtures"
OWNER = "ExampleOwner"
ISSUE_URL = f"https://github.com/{OWNER}/ai-helper/issues/7"

PRIVATE_INVENTORY = """\
- name: "secret-lab"
  target: "secret-lab"
  kind: product
  visibility: private
  fate: keep
  profile: active
  license: none
  status: wip
  featured: false
  description: "Invented private repo"
"""


def idea(
    repo: str = "ai-helper", body: str = "# Better retries\n\n## Idea\n\nRetry with backoff.\n"
) -> str:
    return f"---\ntype: permanent\ngithub: idea\nrepo: {repo}\nmine: yes\n---\n{body}"


class FakeGithub:
    """A fake `gh`: lists repos, creates issues and knows an Engineering project."""

    def __init__(
        self,
        *,
        project: bool = True,
        project_add_fails: bool = False,
        listed: Sequence[dict[str, object]] | None = None,
    ) -> None:
        self.listed = listed
        self.project = project
        self.project_add_fails = project_add_fails
        self.calls: list[tuple[str, str]] = []  # (method, path)
        self.issues: list[dict[str, str]] = []
        self.added: list[dict[str, str]] = []

    def __call__(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        method = args[args.index("--method") + 1]
        path = next(a for a in args if a.startswith("/") or a == "graphql")
        self.calls.append((method, path))
        fields = {
            kv.partition("=")[0]: kv.partition("=")[2]
            for flag, kv in zip(args, args[1:], strict=False)
            if flag in ("-f", "-F")
        }

        def ok(body: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args, 0, json.dumps(body), "")

        if path.startswith("/user/repos"):
            repos: Sequence[object] = self.listed or [
                {"name": "ai-helper", "private": False},
                {"name": "secret-lab", "private": True},
            ]
            return ok([repos])
        if path == "graphql":
            if "addProjectV2ItemById" in fields["query"]:
                if self.project_add_fails:
                    return subprocess.CompletedProcess(args, 1, "", "gh: scope missing\n")
                self.added.append(fields)
                return ok({"data": {"addProjectV2ItemById": {"item": {"id": "PVTI_1"}}}})
            nodes = [{"id": "PVT_1", "title": "Engineering"}] if self.project else []
            return ok({"data": {"user": {"projectsV2": {"nodes": nodes}}}})
        if method == "POST" and path.endswith("/issues"):
            self.issues.append({"path": path, **fields})
            return ok({"html_url": ISSUE_URL, "node_id": "I_7", "number": 7})
        raise AssertionError((method, path))

    @property
    def methods(self) -> set[str]:
        return {m for m, _ in self.calls}


@pytest.fixture
def standard_dir(tmp_path: Path) -> Path:
    std = tmp_path / "std"
    std.mkdir()
    for name in ("standard.yml", "labels.yml", "inventory.yml"):
        (std / name).write_bytes((FIXTURES / "standard" / name).read_bytes())
    return std


@pytest.fixture(autouse=True)
def private_inventory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "private.yml"
    path.write_text(PRIVATE_INVENTORY, encoding="utf-8")
    monkeypatch.setenv("JTS_PRIVATE_INVENTORY", str(path))


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "04-Permanent").mkdir(parents=True)
    return root


def run(
    standard_dir: Path,
    vault: Path,
    fake: FakeGithub,
    capsys: pytest.CaptureFixture[str],
    *extra: str,
) -> tuple[int, str]:
    code = main(
        ["ideas", "--standard-dir", str(standard_dir), "--vault", str(vault), *extra],
        Gh(OWNER, fake),
    )
    return code, capsys.readouterr().out


def put(vault: Path, name: str, text: str) -> Path:
    path = vault / "04-Permanent" / f"{name}.md"
    path.write_text(text, encoding="utf-8")
    return path


# ------------------------------------------------------------------ extract_idea (pure)


def test_idea_section_is_the_body():
    text = (
        "---\ngithub: idea\n---\n# T\n\nIntro.\n\n## Idea\n\nDo it.\n\n### Sub\n\nMore.\n\n"
        "## Notes\n\nNo.\n"
    )
    assert extract_idea(text) == "Do it.\n\n### Sub\n\nMore."


def test_first_paragraph_when_there_is_no_idea_section():
    text = "---\ngithub: idea\n---\n# T\n\n<!-- hi -->\n\nFirst line\nsecond line.\n\nOther.\n"
    assert extract_idea(text) == "First line\nsecond line."


def test_no_body_at_all():
    assert extract_idea("---\ngithub: idea\n---\n# T\n") == ""
    assert extract_idea("---\ngithub: idea\n---\n# T\n\n## Idea\n\n## Next\n\nx\n") == ""


@pytest.mark.parametrize(
    "heading",
    [
        "## Idea",
        "##\tIdea",
        "##   Idea   ",
        "## Idea ##",
        "## Idea ## ",
        "## Idea#",
        "##  IDEA  #  ",
    ],
)
def test_heading_forms_that_open_the_idea_section(heading: str):
    assert extract_idea(f"# T\n\n{heading}\n\nBody.\n\n## Next\n\nx\n") == "Body."


@pytest.mark.parametrize(
    "not_heading", ["##Idea", "####### Idea", " ## Idea", "## Ideas", "## Idea x", "#"]
)
def test_lines_that_are_not_the_idea_heading(not_heading: str):
    assert extract_idea(f"# T\n\nFirst.\n\n{not_heading}\n\nBody.\n") == "First."


def test_heading_levels_end_the_idea_section():
    text = "## Idea\n\nA\n\n### Sub\n\nB\n\n# Top\n\nC\n"
    assert extract_idea(text) == "A\n\n### Sub\n\nB"
    assert extract_idea("## Idea\n\nA\n\n#\tTop ##\n\nC\n") == "A"


@pytest.mark.parametrize("filler", [" ", "\t", "#", " #", "# ", "a #"])
def test_heading_regex_is_linear_on_adversarial_lines(filler: str):
    start = time.perf_counter()
    extract_idea("# " + filler * 10_000 + "\n")
    extract_idea("## Idea\n\n## " + filler * 10_000 + "\n")
    extract_idea("##" + filler * 10_000)
    assert time.perf_counter() - start < 0.1


# ------------------------------------------------------------------ the command


def test_dry_run_lists_and_writes_nothing(
    standard_dir: Path, vault: Path, capsys: pytest.CaptureFixture[str]
):
    note = put(vault, "Better retries", idea())
    before = note.read_text(encoding="utf-8")
    fake = FakeGithub()
    code, out = run(standard_dir, vault, fake, capsys)
    assert code == 0
    assert "would create: ai-helper: Better retries" in out
    assert out.strip().splitlines()[-1] == "dry run: would create 1, refused 0, skipped 0"
    assert note.read_text(encoding="utf-8") == before
    assert fake.methods == {"GET"}
    assert not fake.issues


def test_execute_creates_the_issue_adds_it_to_the_project_and_writes_back(
    standard_dir: Path, vault: Path, capsys: pytest.CaptureFixture[str]
):
    note = put(vault, "Better retries", idea())
    fake = FakeGithub()
    code, out = run(standard_dir, vault, fake, capsys, "--execute")
    assert code == 0
    assert out.strip().splitlines()[-1] == "created 1, refused 0, skipped 0"
    (issue,) = fake.issues
    assert issue["path"] == f"/repos/{OWNER}/ai-helper/issues"
    assert issue["title"] == "Better retries"
    assert issue["body"].startswith("Retry with backoff.")
    assert issue["body"].endswith("\n\n---\n\nFrom the vault")
    assert "04-Permanent" not in issue["body"]
    assert "Better retries.md" not in issue["body"]
    assert [a["projectId"] for a in fake.added] == ["PVT_1"]
    assert [a["contentId"] for a in fake.added] == ["I_7"]
    text = note.read_text(encoding="utf-8")
    assert f"issue: {ISSUE_URL}\n" in text
    assert "github: issued\n" in text
    assert "github: idea" not in text
    assert "mine: yes\n" in text  # his other properties survive
    assert text.endswith("# Better retries\n\n## Idea\n\nRetry with backoff.\n")


def test_second_run_does_not_create_again(
    standard_dir: Path, vault: Path, capsys: pytest.CaptureFixture[str]
):
    put(vault, "Better retries", idea())
    run(standard_dir, vault, FakeGithub(), capsys, "--execute")
    fake = FakeGithub()
    code, out = run(standard_dir, vault, fake, capsys, "--execute")
    assert code == 0
    assert out.strip().splitlines()[-1] == "created 0, refused 0, skipped 0"
    assert not fake.issues


def test_a_note_that_already_has_an_issue_is_left_alone(
    standard_dir: Path, vault: Path, capsys: pytest.CaptureFixture[str]
):
    text = idea().replace("mine: yes", "issue: https://github.com/x/y/issues/1")
    note = put(vault, "Done already", text)
    fake = FakeGithub()
    code, out = run(standard_dir, vault, fake, capsys, "--execute")
    assert code == 0
    assert out.strip().splitlines()[-1] == "created 0, refused 0, skipped 1"
    assert not fake.issues
    assert note.read_text(encoding="utf-8") == text


def test_only_github_idea_notes_with_a_repo_count(
    standard_dir: Path, vault: Path, capsys: pytest.CaptureFixture[str]
):
    put(vault, "Issued", idea().replace("github: idea", "github: issued"))
    put(vault, "Plain", "---\ntype: permanent\n---\n# Plain\n\nNothing.\n")
    put(vault, "No frontmatter", "# Just text\n")
    put(vault, "Broken", "---\n: : [\n---\nbody\n")
    put(vault, "No repo", "---\ngithub: idea\n---\n# No repo\n")
    (vault / ".obsidian").mkdir()
    (vault / ".obsidian" / "stray.md").write_text(idea(), encoding="utf-8")
    fake = FakeGithub()
    code, out = run(standard_dir, vault, fake, capsys, "--execute")
    assert code == 0
    assert not fake.issues
    assert "No repo" in out  # said why it was skipped
    assert out.strip().splitlines()[-1] == "created 0, refused 0, skipped 1"


def test_public_target_naming_a_private_repo_is_refused(
    standard_dir: Path, vault: Path, capsys: pytest.CaptureFixture[str]
):
    text = idea(body="# Leaky\n\n## Idea\n\nPort the parser from Secret-Lab, then reuse it.\n")
    note = put(vault, "Leaky", text)
    fake = FakeGithub()
    code, out = run(standard_dir, vault, fake, capsys, "--execute")
    assert code == 0
    assert "refused: Leaky: names the private repo secret-lab" in out
    assert out.strip().splitlines()[-1] == "created 0, refused 1, skipped 0"
    assert not fake.issues
    assert "POST" not in fake.methods
    assert note.read_text(encoding="utf-8") == text


def test_a_private_name_anywhere_in_the_note_refuses(
    standard_dir: Path, vault: Path, capsys: pytest.CaptureFixture[str]
):
    # Even outside the "## Idea" section that would be sent.
    text = idea(body="# Leaky\n\n## Idea\n\nFine.\n\n## Notes\n\nSee [[secret-lab]].\n")
    put(vault, "Leaky", text)
    _, out = run(standard_dir, vault, FakeGithub(), capsys)
    assert out.strip().splitlines()[-1] == "dry run: would create 0, refused 1, skipped 0"


def test_a_longer_word_is_not_a_private_name(
    standard_dir: Path, vault: Path, capsys: pytest.CaptureFixture[str]
):
    put(vault, "Fine", idea(body="# Fine\n\n## Idea\n\nThe secret-labs idea.\n"))
    put(vault, "Fine2", idea(body="# Fine2\n\n## Idea\n\nMy-secret-lab and secret-labs.\n"))
    _, out = run(standard_dir, vault, FakeGithub(), capsys)
    assert out.strip().splitlines()[-1] == "dry run: would create 2, refused 0, skipped 0"


def test_private_target_may_name_private_repos(
    standard_dir: Path, vault: Path, capsys: pytest.CaptureFixture[str]
):
    put(vault, "Inner", idea(repo="secret-lab", body="# Inner\n\n## Idea\n\nUse secret-lab.\n"))
    _, out = run(standard_dir, vault, FakeGithub(), capsys)
    assert "would create: secret-lab: Inner" in out


def test_unknown_repo_is_refused(
    standard_dir: Path, vault: Path, capsys: pytest.CaptureFixture[str]
):
    put(vault, "Lost", idea(repo="no-such-repo"))
    fake = FakeGithub()
    _, out = run(standard_dir, vault, fake, capsys, "--execute")
    assert "refused: Lost: no-such-repo is not in the inventory" in out
    assert not fake.issues


def test_missing_project_still_creates_the_issue_and_writes_back(
    standard_dir: Path, vault: Path, capsys: pytest.CaptureFixture[str]
):
    note = put(vault, "Better retries", idea())
    code, out = run(standard_dir, vault, FakeGithub(project=False), capsys, "--execute")
    assert code == 0
    assert "not added to the Engineering project" in out
    assert f"issue: {ISSUE_URL}\n" in note.read_text(encoding="utf-8")


def test_failing_project_add_still_writes_back(
    standard_dir: Path, vault: Path, capsys: pytest.CaptureFixture[str]
):
    note = put(vault, "Better retries", idea())
    code, out = run(standard_dir, vault, FakeGithub(project_add_fails=True), capsys, "--execute")
    assert code == 0
    assert "not added to the Engineering project" in out
    assert "github: issued\n" in note.read_text(encoding="utf-8")


def test_dry_run_cannot_write_to_github(
    standard_dir: Path, vault: Path, capsys: pytest.CaptureFixture[str]
):
    put(vault, "Better retries", idea())
    fake = FakeGithub()
    gh = Gh(OWNER, fake, read_only=False)  # even a write-capable Gh is locked by a dry run
    main(["ideas", "--standard-dir", str(standard_dir), "--vault", str(vault)], gh)
    assert fake.methods == {"GET"}
    assert gh.read_only is True


def test_missing_vault_is_an_error(
    standard_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    code = main(
        ["ideas", "--standard-dir", str(standard_dir), "--vault", str(tmp_path / "nope")],
        Gh(OWNER, FakeGithub()),
    )
    assert code == 2
    assert "no such vault" in capsys.readouterr().err


def test_the_title_is_scanned_too(
    standard_dir: Path, vault: Path, capsys: pytest.CaptureFixture[str]
):
    # The note title is the file name; it is sent as the issue title but is not in the text.
    put(vault, "Port of Secret-Lab", idea(body="# Port\n\n## Idea\n\nClean text.\n"))
    fake = FakeGithub()
    _, out = run(standard_dir, vault, fake, capsys, "--execute")
    assert "refused: Port of Secret-Lab: names the private repo secret-lab" in out
    assert not fake.issues


def test_the_body_that_is_sent_is_scanned_too(
    standard_dir: Path,
    vault: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
):
    # Whatever issue_body() returns is covered, even if it is not literally in the note text.
    def leaky(text: str) -> str:
        return "Clean text. secret-lab"

    monkeypatch.setattr(ideas, "issue_body", leaky)
    put(vault, "Clean", idea(body="# Clean\n\n## Idea\n\nClean text.\n"))
    fake = FakeGithub()
    _, out = run(standard_dir, vault, fake, capsys, "--execute")
    assert "refused: Clean: names the private repo secret-lab" in out
    assert not fake.issues


def test_target_missing_from_the_live_listing_is_refused(
    standard_dir: Path, vault: Path, capsys: pytest.CaptureFixture[str]
):
    put(vault, "Better retries", idea())
    fake = FakeGithub(listed=[{"name": "secret-lab", "private": True}])
    _, out = run(standard_dir, vault, fake, capsys, "--execute")
    assert "refused: Better retries: ai-helper was not found on GitHub" in out
    assert not fake.issues


def test_a_private_target_missing_from_the_listing_is_refused_too(
    standard_dir: Path, vault: Path, capsys: pytest.CaptureFixture[str]
):
    put(vault, "Inner", idea(repo="secret-lab", body="# Inner\n\n## Idea\n\nx\n"))
    fake = FakeGithub(listed=[{"name": "ai-helper", "private": False}])
    _, out = run(standard_dir, vault, fake, capsys, "--execute")
    assert "refused: Inner: secret-lab was not found on GitHub" in out
    assert not fake.issues


def test_target_is_found_by_its_target_name_case_insensitively(
    standard_dir: Path, vault: Path, capsys: pytest.CaptureFixture[str]
):
    # The inventory lists old-widget with target widget; GitHub already says Widget.
    put(vault, "Widget idea", idea(repo="old-widget"))
    fake = FakeGithub(listed=[{"name": "Widget", "private": False}, {"name": "x", "private": True}])
    _, out = run(standard_dir, vault, fake, capsys)
    assert "would create: old-widget: Widget idea" in out


def test_live_public_overrides_an_inventory_that_says_private(
    standard_dir: Path, vault: Path, capsys: pytest.CaptureFixture[str]
):
    # secret-lab is private in the inventory but public on GitHub: the stricter case applies.
    put(vault, "Inner", idea(repo="secret-lab", body="# Inner\n\n## Idea\n\nUse secret-lab.\n"))
    fake = FakeGithub(listed=[{"name": "secret-lab", "private": False}])
    _, out = run(standard_dir, vault, fake, capsys)
    assert "refused: Inner: names the private repo secret-lab" in out


class FailingIssues(FakeGithub):
    """Like `FakeGithub`, but creating an issue fails with a server error."""

    def __call__(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        path = next(a for a in args if a.startswith("/") or a == "graphql")
        if path.endswith("/issues"):
            return subprocess.CompletedProcess(args, 1, "", "gh: Server Error (HTTP 500)\nretry\n")
        return super().__call__(args)


class MalformedIssues(FakeGithub):
    """Like `FakeGithub`, but the created issue has no URL."""

    def __call__(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        path = next(a for a in args if a.startswith("/") or a == "graphql")
        if path.endswith("/issues"):
            return subprocess.CompletedProcess(args, 0, "{}", "")
        return super().__call__(args)


def test_note_that_is_not_utf8_is_skipped(
    standard_dir: Path, vault: Path, capsys: pytest.CaptureFixture[str]
):
    (vault / "04-Permanent" / "binary.md").write_bytes(b"github: idea\n\xff\xfe\x00")
    put(vault, "Better retries", idea())
    code, out = run(standard_dir, vault, FakeGithub(), capsys)
    assert code == 0
    assert out.strip().splitlines()[-1] == "dry run: would create 1, refused 0, skipped 0"


def test_failed_issue_creation_is_reported_and_the_note_is_untouched(
    standard_dir: Path, vault: Path, capsys: pytest.CaptureFixture[str]
):
    note = put(vault, "Better retries", idea())
    before = note.read_text(encoding="utf-8")
    code, out = run(standard_dir, vault, FailingIssues(), capsys, "--execute")
    assert code == 1
    assert "FAILED Better retries: gh: Server Error (HTTP 500)" in out
    assert note.read_text(encoding="utf-8") == before


def test_an_answer_without_an_issue_url_is_a_failure(
    standard_dir: Path, vault: Path, capsys: pytest.CaptureFixture[str]
):
    note = put(vault, "Better retries", idea())
    before = note.read_text(encoding="utf-8")
    code, out = run(standard_dir, vault, MalformedIssues(), capsys, "--execute")
    assert code == 1
    assert "FAILED Better retries:" in out
    assert note.read_text(encoding="utf-8") == before


def test_note_that_cannot_be_written_back_is_reported(
    standard_dir: Path,
    vault: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
):
    put(vault, "Better retries", idea())

    def refuse(self: Path, data: bytes) -> int:
        raise PermissionError("read-only vault")

    monkeypatch.setattr(Path, "write_bytes", refuse)
    code, out = run(standard_dir, vault, FakeGithub(), capsys, "--execute")
    assert code == 1
    assert f"issue {ISSUE_URL} exists but the note was not updated" in out


def test_github_listing_failure_is_exit_1(
    standard_dir: Path,
    vault: Path,
    capsys: pytest.CaptureFixture[str],
):
    def broken(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 1, "", "gh: Server Error (HTTP 500)\n")

    put(vault, "Better retries", idea())
    argv = ["ideas", "--standard-dir", str(standard_dir), "--vault", str(vault)]
    assert main(argv, Gh(OWNER, broken)) == 1
    assert "cannot read GitHub" in capsys.readouterr().err
