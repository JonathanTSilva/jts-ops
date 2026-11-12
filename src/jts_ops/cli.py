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
from jts_ops.new import (
    DEFAULT_TEMPLATE,
    LICENSES,
    STACKS,
    STATUSES,
    VISIBILITIES,
    Cmd,
    NewRepoError,
    default_cmd,
    new_repo,
)
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

    new = commands.add_parser(
        "new",
        help="create a compliant repo: render the template, lock, commit, create on GitHub, "
        "apply (dry run unless --execute)",
    )
    new.add_argument("name", help="repository name")
    new.add_argument("--kind", required=True, help="kind from the standard (for example product)")
    new.add_argument("--stack", required=True, choices=STACKS)
    new.add_argument("--visibility", required=True, choices=VISIBILITIES)
    new.add_argument(
        "--license", required=True, choices=LICENSES, help="company licenses are never set here"
    )
    new.add_argument("--description", required=True, help="one-line description")
    new.add_argument("--status", default="wip", choices=STATUSES)
    new.add_argument("--release", action="store_true", help="cut releases with release-please")
    new.add_argument("--standard-dir", type=Path, default=None, help=standard_dir_help)
    new.add_argument(
        "--projects-dir", type=Path, default=Path("~/Projects"), help="parent of the new repo dir"
    )
    new.add_argument(
        "--template", default=DEFAULT_TEMPLATE, help="Copier template (git URL or local path)"
    )
    new.add_argument("--vcs-ref", default=None, help="template git ref (for example HEAD)")
    new.add_argument(
        "--render-only",
        action="store_true",
        help="stop after the first local commit, before creating the GitHub repo",
    )
    new.add_argument(
        "--execute", action="store_true", help="do it; without it only the steps are listed"
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


def _codeql_state(gh: Gh, repo: str) -> str:
    """Current CodeQL default-setup state, or "unavailable" when GitHub says 403/404."""
    try:
        data = gh.api(f"/repos/{gh.owner}/{repo}/code-scanning/default-setup")
    except GhError as err:
        if err.status in (403, 404):
            return "unavailable"
        raise
    setup: dict[str, Any] = data or {}
    return str(setup.get("state", "not-configured"))


def run_apply(
    args: argparse.Namespace,
    gh: Gh | None = None,
    extra_entries: Sequence[InventoryEntry] = (),
) -> int:
    """Apply the standard. `extra_entries` stand in for repos not yet in an inventory file."""
    standard_dir: Path = args.standard_dir or default_standard_dir()
    std = load_standard(standard_dir)
    inventory = load_inventory(standard_dir)
    for extra in extra_entries:
        inventory.setdefault(extra.name, extra)
    gh = gh or Gh(std.owner)
    gh.read_only = not args.execute  # a dry run cannot write, whatever the code path
    only: set[str] | None = set(args.repo) if args.repo else None
    try:
        listed_repos = list_repos(gh)
    except GhError as err:
        print(f"jts-ops: cannot list repos: {err.message}", file=sys.stderr)
        return 1
    if only is not None:
        missing = sorted(only - {r["name"] for r in listed_repos})
        if missing:
            print(f"jts-ops: no such repo: {', '.join(missing)}", file=sys.stderr)
            return 2

    total = 0
    failed = 0  # failed changes plus repos whose state could not be read
    failed_changes = 0
    for listed in listed_repos:
        name: str = listed["name"]
        if only is not None and name not in only:
            continue
        entry = inventory.get(name)
        try:
            state = fetch_state(gh, listed)
            rulesets = _existing_rulesets(gh, name) if ruleset_eligible(state, entry, std) else None
            codeql = (
                _codeql_state(gh, name)
                if entry is not None
                and entry.profile == "active"
                and not state.archived
                and not state.private
                else None
            )
            changes, skips = plan_changes(state, entry, std, rulesets, codeql)
        except GhError as err:
            reason = (err.message.splitlines() or ["unknown error"])[0]
            changes, skips = [], [f"could not read repo state: {reason}"]
            if args.execute:
                failed += 1
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
                failed_changes += 1
                print(f"  FAILED {change.kind}: {reason}", file=sys.stderr)
    if args.execute:
        print(f"applied {total - failed_changes} of {total} changes; {failed} failures")
    else:
        print(f"dry run: {total} changes planned, nothing written (pass --execute to apply)")
    return 1 if failed else 0


def run_new(args: argparse.Namespace, gh: Gh | None = None, run: Cmd = default_cmd) -> int:
    standard_dir: Path = args.standard_dir or default_standard_dir()
    std = load_standard(standard_dir)
    gh = gh or Gh(std.owner)

    def apply(entry: InventoryEntry) -> int:
        namespace = argparse.Namespace(standard_dir=standard_dir, repo=[entry.name], execute=True)
        return run_apply(namespace, gh, [entry])

    try:
        new_repo(
            args.name,
            kind=args.kind,
            stack=args.stack,
            visibility=args.visibility,
            license=args.license,
            description=args.description,
            std=std,
            gh=gh,
            projects_dir=args.projects_dir.expanduser(),
            template=args.template,
            execute=args.execute,
            status=args.status,
            has_release=args.release,
            vcs_ref=args.vcs_ref,
            render_only=args.render_only,
            run=run,
            apply=apply,
            on_step=lambda step: print(f"  {step}"),
        )
    except NewRepoError as err:
        print(f"jts-ops: {err}", file=sys.stderr)
        return 2
    if not args.execute:
        print("dry run: nothing was run or written (pass --execute to do it)")
    return 0


def main(argv: Sequence[str] | None = None, gh: Gh | None = None, run: Cmd = default_cmd) -> int:
    """Entry point for the `jts-ops` console script."""
    args = build_parser().parse_args(argv)
    if args.command == "new":
        return run_new(args, gh, run)
    if args.command == "audit":
        return run_audit(args, gh)
    if args.command == "apply":
        return run_apply(args, gh)
    return 2  # unreachable: the subparser is required


def console_main() -> None:
    raise SystemExit(main())
