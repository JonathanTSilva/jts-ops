import argparse
import base64
import json
import subprocess
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from jts_ops.apply import (
    RELEASE_SECRET,
    RULESET_BODY,
    Change,
    ReleaseTokenError,
    ci_passes_release_token,
    execute,
    load_release_token,
    plan_changes,
)
from jts_ops.cli import main, run_apply
from jts_ops.gh import Gh, GhError, gh_secret_set
from jts_ops.inventory import InventoryEntry
from jts_ops.standard import Standard
from jts_ops.state import LabelInfo, RepoState

FIXTURES = Path(__file__).parent / "fixtures"
CI = ".github/workflows/ci.yml"


def make_state(**overrides: Any) -> RepoState:
    """A public `ai-helper` repo that already matches the standard except labels/security."""
    base = RepoState(
        name="ai-helper",
        private=False,
        archived=False,
        description="Example AI helper",
        topics=("kind-ai", "status-active", "python"),
        has_wiki=False,
        has_discussions=False,
        allow_squash_merge=True,
        allow_merge_commit=False,
        allow_rebase_merge=False,
        delete_branch_on_merge=True,
        default_branch="main",
        license_spdx="MIT",
        files=frozenset({"README.md", CI}),
        readme="# ai-helper\n",
        ci_conclusion="success",
        labels=("type: bug", "type: docs"),
        label_details=(
            LabelInfo("type: bug", "d22d48", "Something is broken"),
            LabelInfo("type: docs", "0075ca", "Documentation only"),
        ),
        squash_merge_commit_title="PR_TITLE",
        squash_merge_commit_message="COMMIT_MESSAGES",
    )
    return replace(base, **overrides)


def kinds(changes: list[Change]) -> list[str]:
    return [c.kind for c in changes]


def test_compliant_repo_only_has_idempotent_ensure_calls(
    std: Standard, inv: dict[str, InventoryEntry]
):
    changes, skips = plan_changes(make_state(), inv["ai-helper"], std, [])
    assert set(kinds(changes)) == {"security", "codeql-default-setup", "ruleset"}
    assert skips == []


def test_archived_repo_skipped(std: Standard, inv: dict[str, InventoryEntry]):
    changes, skips = plan_changes(make_state(archived=True), inv["ai-helper"], std)
    assert changes == []
    assert any("archived" in s for s in skips)


def test_missing_from_inventory_is_skipped(std: Standard):
    changes, skips = plan_changes(make_state(name="stray"), None, std)
    assert changes == []
    assert any("inventory" in s for s in skips)


def test_exempt_only_metadata(std: Standard, inv: dict[str, InventoryEntry]):
    state = make_state(
        name="hidden-notes", private=True, description="old", topics=(), labels=(), files=None
    )
    changes, _ = plan_changes(state, inv["hidden-notes"], std, [])
    assert set(kinds(changes)) == {"topics", "description"}


def test_label_case_insensitive(std: Standard, inv: dict[str, InventoryEntry]):
    state = make_state(
        labels=("Type: Bug", "type: docs"),
        label_details=(
            LabelInfo("Type: Bug", "d22d48", "Something is broken"),
            LabelInfo("type: docs", "0075ca", "Documentation only"),
        ),
    )
    changes, _ = plan_changes(state, inv["ai-helper"], std, [])
    updates = [c for c in changes if c.kind == "label-update"]
    assert len(updates) == 1
    assert updates[0].payload == {"name": "Type: Bug", "new_name": "type: bug"}
    assert "label-create" not in kinds(changes)


def test_label_create_update_and_extra_kept(std: Standard, inv: dict[str, InventoryEntry]):
    state = make_state(
        labels=("type: bug", "wontfix"),
        label_details=(
            LabelInfo("type: bug", "ffffff", "old text"),
            LabelInfo("wontfix", "000000", ""),
        ),
    )
    changes, skips = plan_changes(state, inv["ai-helper"], std, [])
    by_kind = {c.kind: c for c in changes if c.kind.startswith("label-")}
    assert by_kind["label-create"].payload["name"] == "type: docs"
    assert by_kind["label-update"].payload == {
        "name": "type: bug",
        "color": "d22d48",
        "description": "Something is broken",
    }
    assert "extra (kept): wontfix" in skips


def test_labels_never_deleted(std: Standard, inv: dict[str, InventoryEntry]):
    state = make_state(labels=("stale",), label_details=())
    changes, skips = plan_changes(state, inv["ai-helper"], std, [])
    assert all(c.kind != "label-delete" for c in changes)
    assert "extra (kept): stale" in skips

    calls: list[tuple[str, str]] = []

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append((args[args.index("--method") + 1], next(a for a in args if a.startswith("/"))))
        return subprocess.CompletedProcess(args, 0, "", "")

    execute(changes, Gh("ExampleOwner", runner, read_only=False))
    assert calls
    assert "DELETE" not in {method for method, _ in calls}


def test_topics_replace_stale_kind(std: Standard, inv: dict[str, InventoryEntry]):
    entry = replace(inv["ai-helper"], kind="research", status="wip")
    state = make_state(topics=("kind-learn", "linux", "status-active"))
    changes, _ = plan_changes(state, entry, std, [])
    topics = next(c for c in changes if c.kind == "topics")
    assert topics.payload == {"names": ["kind-research", "linux", "status-wip"]}


def test_settings_diff_and_discussions(std: Standard, inv: dict[str, InventoryEntry]):
    entry = replace(inv["ai-helper"], featured=True)
    state = make_state(allow_merge_commit=True, has_wiki=True)
    changes, _ = plan_changes(state, entry, std, [])
    payloads = [c.payload for c in changes if c.kind == "settings"]
    assert payloads == [{"allow_merge_commit": False, "has_wiki": False, "has_discussions": True}]


def test_squash_format_is_its_own_change_and_always_a_pair(
    std: Standard, inv: dict[str, InventoryEntry]
):
    pair = {
        "squash_merge_commit_title": "PR_TITLE",
        "squash_merge_commit_message": "COMMIT_MESSAGES",
    }
    for state in (
        make_state(squash_merge_commit_title=None, squash_merge_commit_message=None),
        make_state(squash_merge_commit_title="COMMIT_OR_PR_TITLE"),
        make_state(squash_merge_commit_message="PR_BODY"),
    ):
        changes, _ = plan_changes(state, inv["ai-helper"], std, [])
        payloads = [c.payload for c in changes if c.kind == "settings"]
        assert payloads == [pair]
    # Flags and format both off: two separate changes.
    state = make_state(has_wiki=True, squash_merge_commit_title=None)
    changes, _ = plan_changes(state, inv["ai-helper"], std, [])
    assert [c.payload for c in changes if c.kind == "settings"] == [{"has_wiki": False}, pair]


def test_special_profile_only_disables_wiki(std: Standard, inv: dict[str, InventoryEntry]):
    entry = inv["ExampleOwner"]
    state = make_state(
        name="ExampleOwner",
        has_wiki=True,
        allow_merge_commit=True,
        description=entry.description,
        topics=("kind-special", "status-active"),
    )
    changes, _ = plan_changes(state, entry, std, [])
    assert next(c for c in changes if c.kind == "settings").payload == {"has_wiki": False}
    assert {"label-create", "codeql-default-setup", "ruleset"}.isdisjoint(kinds(changes))
    assert "security" in kinds(changes)


def test_rename_is_deferred(std: Standard, inv: dict[str, InventoryEntry]):
    state = make_state(name="old-widget", topics=("kind-product", "status-wip"))
    entry = inv["old-widget"]
    changes, skips = plan_changes(state, entry, std, [])
    assert any("Plan 2" in s and "widget" in s for s in skips)
    assert all(c.repo == "old-widget" for c in changes)


def test_visibility_and_archive_are_deferred(std: Standard, inv: dict[str, InventoryEntry]):
    entry = replace(inv["ai-helper"], visibility="private", status="archived")
    _, skips = plan_changes(make_state(), entry, std, [])
    assert sum("Plan 2" in s for s in skips) == 2


def test_security_features_depend_on_visibility(std: Standard, inv: dict[str, InventoryEntry]):
    def features(state: RepoState) -> list[str]:
        changes, _ = plan_changes(state, inv["ai-helper"], std, [])
        return [c.payload["feature"] for c in changes if c.kind == "security"]

    assert features(make_state()) == [
        "vulnerability-alerts",
        "automated-security-fixes",
        "private-vulnerability-reporting",
        "secret-scanning",
    ]
    assert features(make_state(private=True)) == [
        "vulnerability-alerts",
        "automated-security-fixes",
    ]


def test_codeql_public_active_only(std: Standard, inv: dict[str, InventoryEntry]):
    entry = inv["ai-helper"]
    public, _ = plan_changes(make_state(), entry, std, [])
    assert [c.payload for c in public if c.kind == "codeql-default-setup"] == [
        {"state": "configured"}
    ]
    private, _ = plan_changes(
        make_state(private=True), replace(entry, visibility="private"), std, []
    )
    assert "codeql-default-setup" not in kinds(private)
    special, _ = plan_changes(make_state(name="ExampleOwner"), inv["ExampleOwner"], std, [])
    assert "codeql-default-setup" not in kinds(special)


def test_codeql_state_is_respected(std: Standard, inv: dict[str, InventoryEntry]):
    entry = inv["ai-helper"]
    for state, planned in (("configured", False), ("not-configured", True), (None, True)):
        changes, skips = plan_changes(make_state(), entry, std, [], state)
        assert ("codeql-default-setup" in kinds(changes)) is planned
        assert "codeql: not available" not in skips
    changes, skips = plan_changes(make_state(), entry, std, [], "unavailable")
    assert "codeql-default-setup" not in kinds(changes)
    assert "codeql: not available" in skips


def test_ruleset_requires_ci_file(std: Standard, inv: dict[str, InventoryEntry]):
    entry = inv["ai-helper"]
    changes, skips = plan_changes(make_state(files=frozenset({"README.md"})), entry, std, [])
    assert "ruleset" not in kinds(changes)
    assert "ruleset: no ci.yml yet" in skips

    changes, skips = plan_changes(make_state(), entry, std, [])
    rulesets = [c for c in changes if c.kind == "ruleset"]
    assert len(rulesets) == 1
    assert rulesets[0].payload["id"] is None
    body = rulesets[0].payload["body"]
    assert body["name"] == "main"
    assert body["conditions"]["ref_name"]["include"] == ["~DEFAULT_BRANCH"]
    checks = next(r for r in body["rules"] if r["type"] == "required_status_checks")
    assert checks["parameters"]["required_status_checks"] == [{"context": "ci"}]
    assert "no ci.yml yet" not in " ".join(skips)


def test_private_repo_no_ruleset_on_free(std: Standard, inv: dict[str, InventoryEntry]):
    entry = replace(inv["ai-helper"], visibility="private")
    changes, _ = plan_changes(make_state(private=True), entry, std, [])
    assert "ruleset" not in kinds(changes)
    pro = replace(std, plan="pro")
    changes, _ = plan_changes(make_state(private=True), entry, pro, [])
    assert "ruleset" in kinds(changes)


def server_ruleset(**overrides: Any) -> dict[str, Any]:
    """The standard ruleset as the server returns it, with extra server-added fields."""
    rules: list[dict[str, Any]] = json.loads(json.dumps(RULESET_BODY["rules"]))
    for rule in rules:
        rule["id"] = 7
        if rule["type"] == "required_status_checks":
            rule["parameters"]["required_status_checks"][0]["integration_id"] = 15368
            rule["parameters"]["do_not_enforce_on_create"] = False
    base = {
        **json.loads(json.dumps(RULESET_BODY)),
        "id": 42,
        "source_type": "Repository",
        "bypass_actors": [],
        "rules": rules,
    }
    return {**base, **overrides}


def test_matching_ruleset_is_left_alone_and_differing_is_updated(
    std: Standard, inv: dict[str, InventoryEntry]
):
    entry = inv["ai-helper"]
    changes, _ = plan_changes(make_state(), entry, std, [server_ruleset()])
    assert "ruleset" not in kinds(changes)

    stale = server_ruleset(rules=[r for r in server_ruleset()["rules"] if r["type"] != "deletion"])
    changes, _ = plan_changes(make_state(), entry, std, [stale])
    update = next(c for c in changes if c.kind == "ruleset")
    assert update.payload["id"] == 42

    # A ruleset with another name is not ours: a new one is created.
    changes, _ = plan_changes(make_state(), entry, std, [server_ruleset(name="other")])
    assert next(c for c in changes if c.kind == "ruleset").payload["id"] is None

    # Existing `main` is updated even without ci.yml; only creation needs it.
    stale_enforcement = server_ruleset(enforcement="disabled")
    changes, _ = plan_changes(make_state(files=frozenset()), entry, std, [stale_enforcement])
    assert next(c for c in changes if c.kind == "ruleset").payload["id"] == 42


# --- execute and CLI -------------------------------------------------------------------------


class FakeGithub:
    """Serves GETs for a one-repo account and records every call with its body."""

    def __init__(
        self,
        repo: dict[str, Any] | list[dict[str, Any]],
        fail: tuple[str, str] | None = None,
        codeql: str | int = "not-configured",
        broken: set[str] | None = None,
        ci_text: str = "jobs: {}\n",
        release_secret: bool = False,
    ) -> None:
        self.repos = repo if isinstance(repo, list) else [repo]
        self.fail = fail
        self.codeql = codeql
        self.broken = broken or set()
        self.ci_text = ci_text
        self.release_secret = release_secret
        self.calls: list[tuple[str, str, Any]] = []

    def __call__(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        method = args[args.index("--method") + 1]
        path = next(a for a in args if a.startswith("/"))
        body = None
        if "--input" in args:
            body = json.loads(Path(args[args.index("--input") + 1]).read_text(encoding="utf-8"))
        self.calls.append((method, path, body))

        def ok(data: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args, 0, json.dumps(data), "")

        if self.fail == (method, path):
            return subprocess.CompletedProcess(args, 1, "", "gh: Unprocessable (HTTP 422)\n")
        if method != "GET":
            return ok({})
        if path.startswith("/user/repos"):
            return ok([self.repos])
        if "/git/trees/" in path:
            if path.split("/")[3] in self.broken:
                return subprocess.CompletedProcess(args, 1, "", "gh: Server Error (HTTP 500)\n")
            return ok({"tree": [{"path": CI, "type": "blob"}]})
        if path.endswith("/readme"):
            return ok({"encoding": "base64", "content": "IyBoaQo="})
        if "/actions/workflows/ci.yml/runs" in path:
            return ok({"workflow_runs": []})
        if path.endswith("/labels"):
            return ok([[{"name": "Type: Bug", "color": "d22d48", "description": "x"}]])
        if path.endswith("/code-scanning/default-setup"):
            if isinstance(self.codeql, int):
                return subprocess.CompletedProcess(args, 1, "", f"gh: x (HTTP {self.codeql})\n")
            return ok({"state": self.codeql})
        if path.endswith("/rulesets"):
            no_rulesets: list[object] = []
            return ok([no_rulesets])
        if f"/contents/{CI}?" in path:
            encoded = base64.b64encode(self.ci_text.encode()).decode()
            return ok({"encoding": "base64", "content": encoded})
        if path.endswith(f"/actions/secrets/{RELEASE_SECRET}"):
            if not self.release_secret:
                return subprocess.CompletedProcess(args, 1, "", "gh: Not Found (HTTP 404)\n")
            return ok({"name": RELEASE_SECRET})
        raise AssertionError(path)


@pytest.fixture
def standard_dir(tmp_path: Path) -> Path:
    for name in ("standard.yml", "labels.yml", "inventory.yml"):
        (tmp_path / name).write_bytes((FIXTURES / "standard" / name).read_bytes())
    return tmp_path


@pytest.fixture(autouse=True)
def no_real_private_inventory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("JTS_PRIVATE_INVENTORY", str(tmp_path / "absent.yml"))


def helper_repo(name: str = "ai-helper") -> dict[str, Any]:
    data = json.loads((FIXTURES / "gh" / "repo.json").read_text(encoding="utf-8"))
    return {**data, "name": name, "private": False}


def test_dry_run_makes_no_writes(standard_dir: Path, capsys: pytest.CaptureFixture[str]):
    fake = FakeGithub(helper_repo())
    code = main(["apply", "--standard-dir", str(standard_dir)], Gh("ExampleOwner", fake))
    assert code == 0
    assert fake.calls
    assert {method for method, _, _ in fake.calls} == {"GET"}
    out = capsys.readouterr().out
    assert out.startswith("ai-helper\n")
    assert "  label-update: " in out
    assert "  security: enable vulnerability-alerts" in out
    assert "dry run" in out


def test_execute_sends_expected_writes(standard_dir: Path):
    fake = FakeGithub(helper_repo())
    code = main(
        ["apply", "--execute", "--standard-dir", str(standard_dir)], Gh("ExampleOwner", fake)
    )
    assert code == 0
    writes = [(m, p, b) for m, p, b in fake.calls if m != "GET"]
    base = "/repos/ExampleOwner/ai-helper"
    by_call = {(m, p): b for m, p, b in writes}
    assert by_call[("POST", f"{base}/labels")]["name"] == "type: docs"
    assert by_call[("PATCH", f"{base}/labels/Type%3A%20Bug")]["new_name"] == "type: bug"
    patches = [b for m, p, b in writes if m == "PATCH" and p == base]
    assert {
        "squash_merge_commit_title": "PR_TITLE",
        "squash_merge_commit_message": "COMMIT_MESSAGES",
    } in patches
    assert ("PUT", f"{base}/vulnerability-alerts") in by_call
    assert ("PUT", f"{base}/automated-security-fixes") in by_call
    assert ("PUT", f"{base}/private-vulnerability-reporting") in by_call
    assert by_call[("PATCH", f"{base}/code-scanning/default-setup")] == {"state": "configured"}
    assert by_call[("POST", f"{base}/rulesets")]["name"] == "main"
    assert {"PUT", "POST", "PATCH"} >= {m for m, _, _ in writes}
    secret = [
        b for m, p, b in writes if m == "PATCH" and p == base and "security_and_analysis" in b
    ]
    assert len(secret) == 1


def test_failed_change_does_not_stop_the_rest(
    standard_dir: Path, capsys: pytest.CaptureFixture[str]
):
    base = "/repos/ExampleOwner/ai-helper"
    fake = FakeGithub(helper_repo(), fail=("PATCH", f"{base}/code-scanning/default-setup"))
    code = main(
        ["apply", "--execute", "--standard-dir", str(standard_dir)], Gh("ExampleOwner", fake)
    )
    assert code == 1
    assert ("POST", f"{base}/rulesets") in {(m, p) for m, p, _ in fake.calls}
    assert "FAILED codeql-default-setup" in capsys.readouterr().err


def test_repo_filter(standard_dir: Path, capsys: pytest.CaptureFixture[str]):
    fake = FakeGithub(helper_repo())
    code = main(
        ["apply", "--repo", "other", "--standard-dir", str(standard_dir)], Gh("ExampleOwner", fake)
    )
    captured = capsys.readouterr()
    assert "no such repo: other" in captured.err
    assert captured.out == ""
    assert code == 2
    assert fake.calls == [("GET", "/user/repos?affiliation=owner&per_page=100", None)]


def test_unknown_repo_exits_2_with_execute_too(standard_dir: Path):
    fake = FakeGithub(helper_repo())
    args = ["apply", "--execute", "--repo", "nope", "--standard-dir", str(standard_dir)]
    assert main(args, Gh("ExampleOwner", fake)) == 2
    assert {m for m, _, _ in fake.calls} == {"GET"}


@pytest.mark.parametrize("codeql", ["configured", 404, 403])
def test_cli_reads_codeql_state(
    standard_dir: Path, capsys: pytest.CaptureFixture[str], codeql: str | int
):
    fake = FakeGithub(helper_repo(), codeql=codeql)
    main(["apply", "--standard-dir", str(standard_dir)], Gh("ExampleOwner", fake))
    out = capsys.readouterr().out
    assert "codeql-default-setup" not in out
    assert ("skip: codeql: not available" in out) is isinstance(codeql, int)
    assert any(p.endswith("/code-scanning/default-setup") for _, p, _ in fake.calls)


def test_cli_plans_codeql_when_not_configured(
    standard_dir: Path, capsys: pytest.CaptureFixture[str]
):
    fake = FakeGithub(helper_repo(), codeql="not-configured")
    main(["apply", "--standard-dir", str(standard_dir)], Gh("ExampleOwner", fake))
    assert "codeql-default-setup" in capsys.readouterr().out


def test_second_repo_still_processed_when_first_fails(
    standard_dir: Path, capsys: pytest.CaptureFixture[str]
):
    fake = FakeGithub([helper_repo("ai-broken"), helper_repo("ai-helper")], broken={"ai-broken"})
    code = main(
        ["apply", "--execute", "--standard-dir", str(standard_dir)], Gh("ExampleOwner", fake)
    )
    assert code == 1
    out = capsys.readouterr().out
    assert "could not read repo state" in out
    assert ("POST", "/repos/ExampleOwner/ai-helper/rulesets") in {(m, p) for m, p, _ in fake.calls}
    # In a dry run an unreadable repo is only reported.
    dry = FakeGithub([helper_repo("ai-broken"), helper_repo("ai-helper")], broken={"ai-broken"})
    assert main(["apply", "--standard-dir", str(standard_dir)], Gh("ExampleOwner", dry)) == 0


def test_execute_records_non_gh_errors():
    def not_json(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, "not json", "")

    first = Change("ai-helper", "topics", {"names": ["a"]}, "x")
    second = Change("ai-helper", "description", {"description": "d"}, "x")
    failures = execute([first, second], Gh("ExampleOwner", not_json, read_only=False))
    assert [c for c, _ in failures] == [first, second]


def test_dry_run_locks_even_a_writable_gh(standard_dir: Path):
    fake = FakeGithub(helper_repo())
    gh = Gh("ExampleOwner", fake, read_only=False)
    main(["apply", "--standard-dir", str(standard_dir)], gh)
    assert gh.read_only is True


# --- release-please token --------------------------------------------------------------------

TOKEN = "github_pat_example_value"
RELEASE_CI = """\
jobs:
  release:
    uses: ExampleOwner/.github/.github/workflows/release.yml@0123abcd # v1.2.4
    secrets:
      token: ${{ secrets.RELEASE_PLEASE_TOKEN }}
"""


class FakeSetter:
    """Records `gh secret set` calls instead of running them."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, str]] = []

    def __call__(self, owner: str, repo: str, name: str, value: str) -> None:
        self.calls.append((owner, repo, name, value))


def test_release_secret_missing_with_token_plans_a_change(
    std: Standard, inv: dict[str, InventoryEntry]
):
    changes, skips = plan_changes(
        make_state(), inv["ai-helper"], std, [], release_secret="missing", release_token=True
    )
    secret = [c for c in changes if c.kind == "release-secret"]
    assert len(secret) == 1
    assert secret[0].payload == {"name": RELEASE_SECRET}
    assert not [s for s in skips if RELEASE_SECRET in s]


def test_release_secret_missing_without_token_is_a_skip(
    std: Standard, inv: dict[str, InventoryEntry]
):
    changes, skips = plan_changes(
        make_state(), inv["ai-helper"], std, [], release_secret="missing", release_token=False
    )
    assert "release-secret" not in kinds(changes)
    hint = [s for s in skips if RELEASE_SECRET in s]
    assert len(hint) == 1
    assert "~/.config/jts-ops/release-please-token" in hint[0]


@pytest.mark.parametrize("state", ["present", None])
def test_release_secret_present_or_not_needed_plans_nothing(
    std: Standard, inv: dict[str, InventoryEntry], state: Any
):
    changes, skips = plan_changes(
        make_state(), inv["ai-helper"], std, [], release_secret=state, release_token=True
    )
    assert "release-secret" not in kinds(changes)
    assert not [s for s in skips if RELEASE_SECRET in s]


def test_execute_sets_the_release_secret_through_the_setter():
    fake = FakeGithub(helper_repo())
    setter = FakeSetter()
    change = Change("ai-helper", "release-secret", {"name": RELEASE_SECRET}, "set")
    failures = execute([change], Gh("ExampleOwner", fake, read_only=False), setter, TOKEN)
    assert failures == []
    assert setter.calls == [("ExampleOwner", "ai-helper", RELEASE_SECRET, TOKEN)]
    assert fake.calls == []  # no API call carries the token


def test_execute_release_secret_fails_cleanly_without_token_or_when_read_only():
    setter = FakeSetter()
    change = Change("ai-helper", "release-secret", {"name": RELEASE_SECRET}, "set")
    writable = Gh("ExampleOwner", FakeGithub(helper_repo()), read_only=False)
    [(_, reason)] = execute([change], writable, setter, None)
    assert "token" in reason
    read_only = Gh("ExampleOwner", FakeGithub(helper_repo()))
    [(_, reason)] = execute([change], read_only, setter, TOKEN)
    assert "read-only" in reason
    assert setter.calls == []


def test_load_release_token_prefers_the_environment(tmp_path: Path):
    path = tmp_path / "token"
    path.write_text("from-file\n")
    path.chmod(0o600)
    assert load_release_token({"RELEASE_PLEASE_TOKEN": f" {TOKEN}\n"}, path) == TOKEN
    assert load_release_token({"RELEASE_PLEASE_TOKEN": "  "}, path) == "from-file"
    assert load_release_token({}, path) == "from-file"
    assert load_release_token({}, tmp_path / "absent") is None


def test_load_release_token_refuses_a_file_others_can_read(tmp_path: Path):
    path = tmp_path / "token"
    path.write_text(TOKEN)
    path.chmod(0o644)
    with pytest.raises(ReleaseTokenError, match="chmod 600"):
        load_release_token({}, path)
    path.write_text("")
    path.chmod(0o600)
    assert load_release_token({}, path) is None


def test_gh_secret_set_sends_the_value_on_stdin(monkeypatch: pytest.MonkeyPatch):
    seen: list[tuple[list[str], str | None]] = []

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append((args, kwargs.get("input")))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    gh_secret_set("ExampleOwner", "ai-helper", RELEASE_SECRET, TOKEN)
    [(args, stdin)] = seen
    assert args == ["gh", "secret", "set", RELEASE_SECRET, "--repo", "ExampleOwner/ai-helper"]
    assert stdin == TOKEN
    assert not [a for a in args if TOKEN in a]


def test_gh_secret_set_raises_on_failure(monkeypatch: pytest.MonkeyPatch):
    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 1, "", "failed to set secret (HTTP 403)\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(GhError, match="403"):
        gh_secret_set("ExampleOwner", "ai-helper", RELEASE_SECRET, TOKEN)


def _apply_args(standard_dir: Path, execute: bool) -> argparse.Namespace:
    return argparse.Namespace(standard_dir=standard_dir, repo=None, execute=execute)


def test_cli_sets_a_missing_release_secret_from_the_token_file(
    standard_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    token_file = tmp_path / "release-token"
    token_file.write_text(TOKEN + "\n")
    token_file.chmod(0o600)
    monkeypatch.setenv("JTS_RELEASE_TOKEN_FILE", str(token_file))

    dry = FakeSetter()
    fake = FakeGithub(helper_repo(), ci_text=RELEASE_CI)
    assert (
        run_apply(_apply_args(standard_dir, False), Gh("ExampleOwner", fake), set_secret=dry) == 0
    )
    assert dry.calls == []
    assert f"  release-secret: set {RELEASE_SECRET}" in capsys.readouterr().out

    setter = FakeSetter()
    fake = FakeGithub(helper_repo(), ci_text=RELEASE_CI)
    code = run_apply(_apply_args(standard_dir, True), Gh("ExampleOwner", fake), set_secret=setter)
    assert code == 0
    assert setter.calls == [("ExampleOwner", "ai-helper", RELEASE_SECRET, TOKEN)]
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err


def test_cli_leaves_an_existing_or_unused_release_secret_alone(
    standard_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setenv("RELEASE_PLEASE_TOKEN", TOKEN)
    setter = FakeSetter()
    present = FakeGithub(helper_repo(), ci_text=RELEASE_CI, release_secret=True)
    run_apply(_apply_args(standard_dir, True), Gh("ExampleOwner", present), set_secret=setter)
    unused = FakeGithub(helper_repo())
    run_apply(_apply_args(standard_dir, True), Gh("ExampleOwner", unused), set_secret=setter)
    assert setter.calls == []
    assert not [p for _, p, _ in unused.calls if "/actions/secrets/" in p]
    assert "release-secret" not in capsys.readouterr().out


def test_cli_without_a_token_prints_a_hint_and_does_not_fail(
    standard_dir: Path, capsys: pytest.CaptureFixture[str]
):
    setter = FakeSetter()
    fake = FakeGithub(helper_repo(), ci_text=RELEASE_CI)
    code = run_apply(_apply_args(standard_dir, True), Gh("ExampleOwner", fake), set_secret=setter)
    assert code == 0
    assert setter.calls == []
    assert f"skip: {RELEASE_SECRET} missing" in capsys.readouterr().out


def test_cli_refuses_a_token_file_others_can_read(
    standard_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    token_file = tmp_path / "release-token"
    token_file.write_text(TOKEN)
    token_file.chmod(0o644)
    monkeypatch.setenv("JTS_RELEASE_TOKEN_FILE", str(token_file))
    fake = FakeGithub(helper_repo(), ci_text=RELEASE_CI)
    code = run_apply(
        _apply_args(standard_dir, True), Gh("ExampleOwner", fake), set_secret=FakeSetter()
    )
    assert code == 2
    err = capsys.readouterr().err
    assert "chmod 600" in err and TOKEN not in err
    assert fake.calls == []


def _routes(**status: int) -> Any:
    """A gh runner that answers ci.yml contents and the secret with the given HTTP statuses."""

    def run(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        path = next(a for a in args if a.startswith("/"))
        key = "secret" if "/actions/secrets/" in path else "contents"
        code = status.get(key, 200)
        if code != 200:
            return subprocess.CompletedProcess(args, 1, "", f"gh: x (HTTP {code})\n")
        if key == "contents":
            content = base64.b64encode(RELEASE_CI.encode()).decode()
            return subprocess.CompletedProcess(args, 0, json.dumps({"content": content}), "")
        return subprocess.CompletedProcess(args, 0, json.dumps({"name": RELEASE_SECRET}), "")

    return run


def test_release_secret_state_edge_cases(inv: dict[str, InventoryEntry]):
    from jts_ops.cli import _release_secret_state  # pyright: ignore[reportPrivateUsage]

    entry = inv["ai-helper"]
    ok = Gh("ExampleOwner", _routes())
    assert _release_secret_state(ok, make_state(), entry) == "present"
    assert _release_secret_state(ok, make_state(), None) is None
    assert _release_secret_state(ok, make_state(name="hidden-notes"), inv["hidden-notes"]) is None
    assert _release_secret_state(ok, make_state(archived=True), entry) is None
    assert _release_secret_state(ok, make_state(files=None), entry) is None
    assert _release_secret_state(ok, make_state(files=frozenset({"README.md"})), entry) is None
    gone = Gh("ExampleOwner", _routes(contents=404))
    assert _release_secret_state(gone, make_state(), entry) is None
    missing = Gh("ExampleOwner", _routes(secret=404))
    assert _release_secret_state(missing, make_state(), entry) == "missing"
    for broken in (_routes(contents=500), _routes(secret=403)):
        with pytest.raises(GhError):
            _release_secret_state(Gh("ExampleOwner", broken), make_state(), entry)


def test_execute_release_secret_needs_a_setter():
    change = Change("ai-helper", "release-secret", {"name": RELEASE_SECRET}, "set")
    gh = Gh("ExampleOwner", FakeGithub(helper_repo()), read_only=False)
    [(_, reason)] = execute([change], gh, None, TOKEN)
    assert "setter" in reason


def test_ci_passes_release_token_only_for_the_shared_release_workflow():
    assert ci_passes_release_token(RELEASE_CI, "ExampleOwner")
    assert not ci_passes_release_token(RELEASE_CI, "SomeoneElse")
    assert not ci_passes_release_token(
        "# token: ${{ secrets.RELEASE_PLEASE_TOKEN }}\n", "ExampleOwner"
    )
    other_job = """\
jobs:
  upload:
    runs-on: ubuntu-latest
    steps:
      - run: ./upload.sh
        env:
          TOKEN: ${{ secrets.RELEASE_PLEASE_TOKEN }}
"""
    assert not ci_passes_release_token(other_job, "ExampleOwner")
    inherit = RELEASE_CI.replace(
        "    secrets:\n      token: ${{ secrets.RELEASE_PLEASE_TOKEN }}\n", "    secrets: inherit\n"
    )
    assert not ci_passes_release_token(inherit, "ExampleOwner")
    other_workflow = RELEASE_CI.replace("release.yml@", "deploy.yml@")
    assert not ci_passes_release_token(other_workflow, "ExampleOwner")
    for broken in ("jobs: [", "just text", "jobs: []", "jobs:\n  x: 1\n", ""):
        assert not ci_passes_release_token(broken, "ExampleOwner")


def test_printed_release_texts_name_the_real_secret_and_file():
    from jts_ops.apply import (
        RELEASE_SETUP_HINT,
        RELEASE_SETUP_REASON,
        RELEASE_TOKEN_ENV,
        RELEASE_TOKEN_FILE,
    )

    assert RELEASE_SETUP_HINT.startswith(f"{RELEASE_SECRET} missing; export {RELEASE_TOKEN_ENV} ")
    assert RELEASE_TOKEN_FILE in RELEASE_SETUP_HINT
    assert RELEASE_SETUP_REASON.startswith(f"set {RELEASE_SECRET} ")
