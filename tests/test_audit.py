import json
from dataclasses import replace
from typing import Any

import pytest

from jts_ops.audit import Finding, audit_repo, build_report
from jts_ops.inventory import InventoryEntry
from jts_ops.standard import Standard
from jts_ops.state import RepoState

BANNER = "<!-- archive-banner:start -->\n> Archived.\n<!-- archive-banner:end -->\n"


def good_state(**overrides: Any) -> RepoState:
    """A fully compliant state for the `ai-helper` fixture entry."""
    base = RepoState(
        name="ai-helper",
        private=False,
        archived=False,
        description="A helper",
        topics=("kind-ai", "status-active", "python"),
        has_wiki=False,
        has_discussions=False,
        allow_squash_merge=True,
        allow_merge_commit=False,
        allow_rebase_merge=False,
        delete_branch_on_merge=True,
        default_branch="main",
        license_spdx="MIT",
        files=frozenset({"README.md", "LICENSE", "AGENTS.md", ".claude/settings.json"}),
        readme="# ai-helper\n",
        ci_conclusion="success",
        labels=("Type: Bug", "type: docs", "extra"),
    )
    return replace(base, **overrides)


def entry(inv: dict[str, InventoryEntry], **overrides: Any) -> InventoryEntry:
    return replace(inv["ai-helper"], **overrides)


def checks(findings: list[Finding]) -> dict[str, Finding]:
    return {f.check: f for f in findings}


def test_compliant_repo_has_no_findings(std: Standard, inv: dict[str, InventoryEntry]):
    assert audit_repo(good_state(), inv["ai-helper"], std) == []


def test_inventory_missing(std: Standard):
    findings = audit_repo(good_state(name="stray"), None, std)
    assert [(f.check, f.severity) for f in findings] == [("inventory.missing", "error")]
    assert findings[0].repo == "stray"


def test_name_convention_uses_target_kind_against_current_name(
    std: Standard, inv: dict[str, InventoryEntry]
):
    state = good_state(name="not-an-ai-name")
    found = checks(audit_repo(state, inv["ai-helper"], std))
    assert found["name.convention"].severity == "error"
    assert "ai-" in found["name.convention"].message
    ok = checks(audit_repo(good_state(), inv["ai-helper"], std))
    assert "name.convention" not in ok


def test_unmigrated_name_is_flagged(std: Standard, inv: dict[str, InventoryEntry]):
    # Current name `old-widget` is valid on its own but the target kind is `product`
    # with target `widget`; a current name of a different kind's shape must show up.
    entry_ai = entry(inv, name="old-widget", target="ai-widget")
    found = checks(audit_repo(good_state(name="old-widget"), entry_ai, std))
    assert found["name.convention"].severity == "error"


def test_description_emoji(std: Standard, inv: dict[str, InventoryEntry]):
    found = checks(audit_repo(good_state(description="no emoji here"), inv["ai-helper"], std))
    assert found["description.emoji"].severity == "warning"
    assert "A" in found["description.emoji"].message


def test_topics_kind(std: Standard, inv: dict[str, InventoryEntry]):
    state = good_state(topics=("status-active",))
    found = checks(audit_repo(state, inv["ai-helper"], std))
    assert found["topics.kind"].severity == "warning"
    assert "kind-ai" in found["topics.kind"].message
    assert "topics.status" not in found


def test_topics_status(std: Standard, inv: dict[str, InventoryEntry]):
    state = good_state(topics=("kind-ai",))
    found = checks(audit_repo(state, inv["ai-helper"], std))
    assert found["topics.status"].severity == "warning"
    assert "status-active" in found["topics.status"].message
    assert "topics.kind" not in found


def test_wiki_enabled(std: Standard, inv: dict[str, InventoryEntry]):
    found = checks(audit_repo(good_state(has_wiki=True), inv["ai-helper"], std))
    assert found["wiki.enabled"].severity == "warning"


def test_license_missing_public(std: Standard, inv: dict[str, InventoryEntry]):
    found = checks(audit_repo(good_state(license_spdx=None), inv["ai-helper"], std))
    assert found["license.missing"].severity == "error"


def test_license_missing_is_fine_for_company_license(std: Standard, inv: dict[str, InventoryEntry]):
    company = entry(inv, license="company")
    found = checks(audit_repo(good_state(license_spdx=None), company, std))
    assert "license.missing" not in found


def test_license_missing_ignored_for_private(std: Standard, inv: dict[str, InventoryEntry]):
    found = checks(audit_repo(good_state(license_spdx=None, private=True), inv["ai-helper"], std))
    assert "license.missing" not in found


def test_files_required(std: Standard, inv: dict[str, InventoryEntry]):
    state = good_state(files=frozenset({"README.md"}))
    findings = [f for f in audit_repo(state, inv["ai-helper"], std) if f.check == "files.required"]
    assert {f.severity for f in findings} == {"error"}
    assert sorted(f.message for f in findings) == [
        "missing .claude/settings.json",
        "missing AGENTS.md",
        "missing LICENSE",
    ]


def test_copier_answers_required_except_for_template_kind(
    std: Standard, inv: dict[str, InventoryEntry]
):
    needs_answers = replace(
        std,
        required_files={
            **std.required_files,
            "active": (*std.required_files["active"], ".copier-answers.yml"),
        },
    )
    messages = [
        f.message
        for f in audit_repo(good_state(), inv["ai-helper"], needs_answers)
        if f.check == "files.required"
    ]
    assert messages == ["missing .copier-answers.yml"]
    template = entry(inv, kind="template")
    assert [
        f for f in audit_repo(good_state(), template, needs_answers) if f.check == "files.required"
    ] == []


def test_settings_merge(std: Standard, inv: dict[str, InventoryEntry]):
    state = good_state(allow_merge_commit=True, delete_branch_on_merge=False)
    found = checks(audit_repo(state, inv["ai-helper"], std))
    assert found["settings.merge"].severity == "warning"
    assert "merge commit" in found["settings.merge"].message
    assert "delete branch" in found["settings.merge"].message
    squash_off = checks(audit_repo(good_state(allow_squash_merge=False), inv["ai-helper"], std))
    assert "squash" in squash_off["settings.merge"].message


def test_ci_failing(std: Standard, inv: dict[str, InventoryEntry]):
    found = checks(audit_repo(good_state(ci_conclusion="failure"), inv["ai-helper"], std))
    assert found["ci.failing"].severity == "error"
    for conclusion in ("success", None, "cancelled"):
        ok = checks(audit_repo(good_state(ci_conclusion=conclusion), inv["ai-helper"], std))
        assert "ci.failing" not in ok


def test_ci_failing_ignored_for_archived_profile(std: Standard, inv: dict[str, InventoryEntry]):
    state = good_state(
        topics=("kind-ai", "status-archived"),
        archived=True,
        readme=BANNER,
        ci_conclusion="failure",
    )
    found = checks(audit_repo(state, archived_entry(inv, fate="archive"), std))
    assert "ci.failing" not in found


def archived_entry(inv: dict[str, InventoryEntry], **overrides: Any) -> InventoryEntry:
    return entry(inv, profile="archived", status="archived", **overrides)


def test_archived_flag_is_info_while_fate_is_archive(std: Standard, inv: dict[str, InventoryEntry]):
    state = good_state(topics=("kind-ai", "status-archived"), readme=BANNER)
    found = checks(audit_repo(state, archived_entry(inv, fate="archive"), std))
    assert found["archived.flag"].severity == "info"


def test_archived_flag_is_error_when_fate_is_not_archive(
    std: Standard, inv: dict[str, InventoryEntry]
):
    state = good_state(topics=("kind-ai", "status-archived"), readme=BANNER)
    found = checks(audit_repo(state, archived_entry(inv, fate="keep"), std))
    assert found["archived.flag"].severity == "error"


def test_archived_flag_absent_once_archived(std: Standard, inv: dict[str, InventoryEntry]):
    state = good_state(topics=("kind-ai", "status-archived"), readme=BANNER, archived=True)
    found = checks(audit_repo(state, archived_entry(inv, fate="archive"), std))
    assert "archived.flag" not in found


def test_archived_banner(std: Standard, inv: dict[str, InventoryEntry]):
    state = good_state(topics=("kind-ai", "status-archived"), archived=True, readme="# Hi\n")
    found = checks(audit_repo(state, archived_entry(inv, fate="archive"), std))
    assert found["archived.banner"].severity == "warning"
    with_banner = good_state(
        topics=("kind-ai", "status-archived"), archived=True, readme=BANNER + "# Hi\n"
    )
    ok = checks(audit_repo(with_banner, archived_entry(inv, fate="archive"), std))
    assert "archived.banner" not in ok


def test_archived_profile_skips_active_only_checks(std: Standard, inv: dict[str, InventoryEntry]):
    state = good_state(
        topics=("kind-ai", "status-archived"),
        archived=True,
        readme=BANNER,
        files=frozenset(),
        labels=(),
        allow_merge_commit=True,
    )
    found = checks(audit_repo(state, archived_entry(inv, fate="archive"), std))
    assert not {"files.required", "labels.missing", "settings.merge"} & found.keys()


def test_labels_missing_case_insensitive(std: Standard, inv: dict[str, InventoryEntry]):
    # "Type: Bug" matches "type: bug" case-insensitively; "type: docs" is absent here.
    state = good_state(labels=("Type: Bug",))
    found = checks(audit_repo(state, inv["ai-helper"], std))
    assert found["labels.missing"].severity == "warning"
    assert "type: docs" in found["labels.missing"].message
    assert "type: bug" not in found["labels.missing"].message


def test_exempt_profile_skips_files_and_settings(std: Standard, inv: dict[str, InventoryEntry]):
    vault = entry(
        inv, name="jts-vault-example", target="jts-vault-example", profile="exempt", kind="jts"
    )
    state = good_state(
        name="jts-vault-example",
        files=frozenset({"README.md"}),
        has_wiki=True,
        allow_merge_commit=True,
        license_spdx=None,
        ci_conclusion="failure",
        labels=(),
    )
    found = checks(audit_repo(state, vault, std))
    assert set(found) <= {"inventory.missing", "description.emoji", "topics.kind", "topics.status"}
    assert "files.required" not in found


def test_special_profile_runs_only_its_checks(std: Standard, inv: dict[str, InventoryEntry]):
    special = entry(
        inv, name="ExampleOwner", target="ExampleOwner", kind="special", profile="special"
    )
    state = good_state(
        name="ExampleOwner",
        description="no emoji",
        topics=(),
        has_wiki=True,
        license_spdx=None,
        files=frozenset(),
        ci_conclusion="failure",
    )
    assert set(checks(audit_repo(state, special, std))) == {"wiki.enabled", "license.missing"}


def test_empty_repo_no_crash(std: Standard, inv: dict[str, InventoryEntry]):
    state = good_state(files=None, readme=None)
    findings = audit_repo(state, inv["ai-helper"], std)
    found = checks(findings)
    assert found["files.required"].severity == "info"
    archived = audit_repo(state, archived_entry(inv), std)
    assert checks(archived)["archived.banner"].severity == "warning"


def test_findings_sorted_by_check_id(std: Standard, inv: dict[str, InventoryEntry]):
    state = good_state(has_wiki=True, license_spdx=None, ci_conclusion="failure", topics=())
    ids = [f.check for f in audit_repo(state, inv["ai-helper"], std)]
    assert ids == sorted(ids)
    assert len(ids) >= 4


def result(
    state: RepoState, ent: InventoryEntry | None, findings: list[Finding]
) -> tuple[RepoState, InventoryEntry | None, list[Finding]]:
    return (state, ent, findings)


def three_results(
    std: Standard, inv: dict[str, InventoryEntry]
) -> list[tuple[RepoState, InventoryEntry | None, list[Finding]]]:
    out: list[tuple[RepoState, InventoryEntry | None, list[Finding]]] = []
    for name, bad in (("ai-zeta", False), ("ai-alpha", False), ("ai-mid", True)):
        state = good_state(name=name, description="A helper", license_spdx=None if bad else "MIT")
        ent = entry(inv, name=name, target=name)
        out.append(result(state, ent, audit_repo(state, ent, std)))
    return out


def test_percent_rounding(std: Standard, inv: dict[str, InventoryEntry]):
    report = build_report(three_results(std, inv), public_only=False)
    assert report.summary["total"] == 3
    assert report.summary["compliant"] == 2
    assert report.summary["percent"] == 67
    assert report.summary["ci_total"] == 3
    assert report.summary["ci_green"] == 3


def test_percent_rounds_half_up(std: Standard, inv: dict[str, InventoryEntry]):
    results: list[tuple[RepoState, InventoryEntry | None, list[Finding]]] = []
    for i in range(8):
        state = good_state(name=f"ai-r{i}", license_spdx=None if i else "MIT")
        ent = entry(inv, name=f"ai-r{i}", target=f"ai-r{i}")
        results.append(result(state, ent, audit_repo(state, ent, std)))
    # 1 of 8 compliant is 12.5%, which rounds half up to 13.
    assert build_report(results, public_only=False).summary["percent"] == 13


def test_empty_report(std: Standard):
    report = build_report([], public_only=False)
    assert report.summary == {
        "total": 0,
        "compliant": 0,
        "percent": 0,
        "ci_green": 0,
        "ci_total": 0,
    }


def test_report_is_sorted_and_deterministic(std: Standard, inv: dict[str, InventoryEntry]):
    report = build_report(three_results(std, inv), public_only=False)
    assert [r["name"] for r in report.repos] == ["ai-alpha", "ai-mid", "ai-zeta"]
    mid = report.repos[1]
    assert mid["compliant"] is False
    assert [f["check"] for f in mid["findings"]] == ["license.missing"]
    data = json.loads(report.to_json())
    assert data["summary"]["percent"] == 67
    assert data["generated_at"] == report.generated_at


def test_public_only_excludes_private(std: Standard, inv: dict[str, InventoryEntry]):
    results = three_results(std, inv)
    secret_state = good_state(name="ai-secret-ledger", private=True, license_spdx=None)
    secret_entry = entry(inv, name="ai-secret-ledger", target="secret-vault", visibility="private")
    results.append(result(secret_state, secret_entry, audit_repo(secret_state, secret_entry, std)))
    results.append(result(good_state(name="ai-stray-private", private=True), None, []))

    report = build_report(results, public_only=True)
    for text in (report.to_json(), report.to_markdown()):
        assert "ai-secret-ledger" not in text
        assert "secret-vault" not in text
        assert "ai-stray-private" not in text
    assert report.summary["total"] == 3
    assert report.summary["compliant"] == 2
    assert report.summary["percent"] == 67

    full = build_report(results, public_only=False)
    assert full.summary["total"] == 5
    assert "ai-secret-ledger" in full.to_json()


def test_markdown_lists_findings(std: Standard, inv: dict[str, InventoryEntry]):
    markdown = build_report(three_results(std, inv), public_only=False).to_markdown()
    assert markdown.startswith("# GitHub standard audit")
    assert "67%" in markdown
    assert "license.missing" in markdown
    assert markdown.index("ai-alpha") < markdown.index("ai-mid") < markdown.index("ai-zeta")


def test_markdown_escapes_pipes(std: Standard, inv: dict[str, InventoryEntry]):
    state = good_state(name="mid")
    finding = Finding("mid", "fetch.error", "error", "bad | thing")
    markdown = build_report(
        [result(state, entry(inv, name="mid", target="mid"), [finding])], public_only=False
    ).to_markdown()
    assert "bad \\| thing" in markdown


@pytest.mark.parametrize("severity", ["error", "warning", "info"])
def test_only_errors_break_compliance(std: Standard, inv: dict[str, InventoryEntry], severity: str):
    state = good_state()
    finding = Finding(state.name, "x.y", severity, "m")  # pyright: ignore[reportArgumentType]
    report = build_report([result(state, inv["ai-helper"], [finding])], public_only=False)
    assert report.repos[0]["compliant"] is (severity != "error")


def test_visibility_match_has_no_finding(std: Standard, inv: dict[str, InventoryEntry]):
    public = entry(inv, visibility="public", fate="keep")
    assert "visibility.mismatch" not in checks(audit_repo(good_state(), public, std))
    secret = entry(inv, visibility="private", fate="keep")
    private_state = good_state(private=True, license_spdx=None)
    assert "visibility.mismatch" not in checks(audit_repo(private_state, secret, std))


def test_visibility_mismatch_is_error(std: Standard, inv: dict[str, InventoryEntry]):
    expects_public = entry(inv, visibility="public", fate="keep")
    found = checks(audit_repo(good_state(private=True, license_spdx=None), expects_public, std))
    assert found["visibility.mismatch"].severity == "error"
    assert "private" in found["visibility.mismatch"].message
    assert "public" in found["visibility.mismatch"].message


def test_visibility_mismatch_skipped_for_publish_fate(
    std: Standard, inv: dict[str, InventoryEntry]
):
    pending = entry(inv, visibility="public", fate="publish")
    found = checks(audit_repo(good_state(private=True, license_spdx=None), pending, std))
    assert "visibility.mismatch" not in found


def test_visibility_checked_for_every_profile(std: Standard, inv: dict[str, InventoryEntry]):
    vault = entry(
        inv,
        name="jts-vault-example",
        target="jts-vault-example",
        profile="exempt",
        kind="jts",
        visibility="public",
        fate="keep",
    )
    state = good_state(name="jts-vault-example", private=True, license_spdx=None)
    assert "visibility.mismatch" in checks(audit_repo(state, vault, std))
