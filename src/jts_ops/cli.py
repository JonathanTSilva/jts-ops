"""Command line entry point."""

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from jts_ops.apply import RULESET_NAME, execute, plan_changes, ruleset_eligible
from jts_ops.audit import Finding, Result, audit_repo, build_report
from jts_ops.gh import Gh, GhError, fetch_state, list_repos
from jts_ops.inventory import InventoryEntry, load_inventory
from jts_ops.standard import Standard, load_standard
from jts_ops.state import RepoState

STANDARD_DIR_ENV = "JTS_STANDARD_DIR"


def default_standard_dir() -> Path:
    return Path(os.environ.get(STANDARD_DIR_ENV, "~/Projects/.github")).expanduser()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jts-ops")
    commands = parser.add_subparsers(dest="command", required=True)
    audit = commands.add_parser("audit", help="check every repo against the standard")
    standard_dir_help = (
        f"directory with standard.yml and inventory.yml (default: ${STANDARD_DIR_ENV}, "
        "else ~/Projects/.github)"
    )
    audit.add_argument("--standard-dir", type=Path, default=None, help=standard_dir_help)
    audit.add_argument(
        "--public-only", action="store_true", help="leave private repos out of every output"
    )
    audit.add_argument("--json", type=Path, default=None, help="write the JSON report here")
    audit.add_argument("--markdown", type=Path, default=None, help="write the Markdown report here")
    audit.add_argument("--repo", default=None, help="audit only this repo (current name)")

    apply = commands.add_parser(
        "apply",
        help="bring repo settings, topics, descriptions, labels and security in line "
        "with the standard (dry run unless --execute)",
    )
    apply.add_argument("--standard-dir", type=Path, default=None, help=standard_dir_help)
    apply.add_argument(
        "--repo",
        action="append",
        default=None,
        metavar="NAME",
        help="limit to this repo (current name); may be repeated",
    )
    apply.add_argument(
        "--execute", action="store_true", help="make the changes; without it nothing is written"
    )
    return parser


def _fetch_failed_state(listed: dict[str, object]) -> RepoState:
    """Placeholder for a repo whose state could not be fetched; only identity is real."""
    return RepoState(
        name=str(listed["name"]),
        private=bool(listed.get("private", True)),
        archived=bool(listed.get("archived", False)),
        description="",
        topics=(),
        has_wiki=False,
        has_discussions=False,
        allow_squash_merge=False,
        allow_merge_commit=False,
        allow_rebase_merge=False,
        delete_branch_on_merge=False,
        default_branch="",
        license_spdx=None,
        files=None,
        readme=None,
        ci_conclusion=None,
        labels=(),
    )


def collect(
    gh: Gh,
    std: Standard,
    inventory: dict[str, InventoryEntry],
    public_only: bool,
    only_repo: str | None,
) -> list[Result]:
    """Fetch and audit repos. A repo that cannot be fetched gets one `fetch.error` finding."""
    results: list[Result] = []
    for listed in list_repos(gh):
        name: str = listed["name"]
        if only_repo is not None and name != only_repo:
            continue
        # Private repos are not even fetched when the output must not mention them.
        if public_only and listed.get("private", True):
            continue
        entry = inventory.get(name)
        try:
            state = fetch_state(gh, listed)
        except GhError as err:
            reason = (err.message.splitlines() or ["unknown error"])[0]
            finding = Finding(name, "fetch.error", "error", f"could not fetch repo state: {reason}")
            results.append((_fetch_failed_state(listed), entry, [finding]))
            continue
        results.append((state, entry, audit_repo(state, entry, std)))
    return results


def run_audit(args: argparse.Namespace, gh: Gh | None = None) -> int:
    standard_dir: Path = args.standard_dir or default_standard_dir()
    std = load_standard(standard_dir)
    inventory = load_inventory(standard_dir)
    gh = gh or Gh(std.owner)
    try:
        results = collect(gh, std, inventory, args.public_only, args.repo)
    except GhError as err:
        print(f"jts-ops: cannot list repos: {err.message}", file=sys.stderr)
        return 1
    report = build_report(results, public_only=args.public_only)
    if args.json:
        args.json.write_text(report.to_json(), encoding="utf-8")
    if args.markdown:
        args.markdown.write_text(report.to_markdown(), encoding="utf-8")
    s = report.summary
    print(
        f"audited {s['total']} repos: {s['compliant']} compliant ({s['percent']}%), "
        f"CI green {s['ci_green']}/{s['ci_total']}"
    )
    return 0  # the audit reports, it does not gate


def _existing_rulesets(gh: Gh, repo: str) -> list[dict[str, Any]]:
    """Full ruleset objects (the list endpoint omits the rules) for the repo's rulesets."""
    base = f"/repos/{gh.owner}/{repo}/rulesets"
    summaries: list[dict[str, Any]] = gh.api(base, paginate=True)
    return [gh.api(f"{base}/{r['id']}") for r in summaries if r.get("name") == RULESET_NAME]


def run_apply(args: argparse.Namespace, gh: Gh | None = None) -> int:
    standard_dir: Path = args.standard_dir or default_standard_dir()
    std = load_standard(standard_dir)
    inventory = load_inventory(standard_dir)
    gh = gh or Gh(std.owner)
    only: set[str] | None = set(args.repo) if args.repo else None
    try:
        listed_repos = list_repos(gh)
    except GhError as err:
        print(f"jts-ops: cannot list repos: {err.message}", file=sys.stderr)
        return 1
    if only is not None:
        for missing in sorted(only - {r["name"] for r in listed_repos}):
            print(f"jts-ops: no such repo: {missing}", file=sys.stderr)

    total = 0
    failed = 0
    for listed in listed_repos:
        name: str = listed["name"]
        if only is not None and name not in only:
            continue
        entry = inventory.get(name)
        try:
            state = fetch_state(gh, listed)
            rulesets = _existing_rulesets(gh, name) if ruleset_eligible(state, entry, std) else None
            changes, skips = plan_changes(state, entry, std, rulesets)
        except GhError as err:
            reason = (err.message.splitlines() or ["unknown error"])[0]
            changes, skips = [], [f"could not read repo state: {reason}"]
        if not changes and not skips:
            continue
        print(name)
        for change in changes:
            print(f"  {change.kind}: {change.reason}")
        for skip in skips:
            print(f"  {skip}" if skip.startswith("extra (kept)") else f"  skip: {skip}")
        total += len(changes)
        if args.execute and changes:
            for change, reason in execute(changes, gh):
                failed += 1
                print(f"  FAILED {change.kind}: {reason}", file=sys.stderr)
    if args.execute:
        print(f"applied {total - failed} of {total} changes")
    else:
        print(f"dry run: {total} changes planned, nothing written (pass --execute to apply)")
    return 1 if failed else 0


def main(argv: Sequence[str] | None = None, gh: Gh | None = None) -> int:
    """Entry point for the `jts-ops` console script."""
    args = build_parser().parse_args(argv)
    if args.command == "audit":
        return run_audit(args, gh)
    if args.command == "apply":
        return run_apply(args, gh)
    return 2  # unreachable: the subparser is required


def console_main() -> None:
    raise SystemExit(main())
