"""Command line entry point."""

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

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
    audit.add_argument(
        "--standard-dir",
        type=Path,
        default=None,
        help=f"directory with standard.yml and inventory.yml (default: ${STANDARD_DIR_ENV}, "
        "else ~/Projects/.github)",
    )
    audit.add_argument(
        "--public-only", action="store_true", help="leave private repos out of every output"
    )
    audit.add_argument("--json", type=Path, default=None, help="write the JSON report here")
    audit.add_argument("--markdown", type=Path, default=None, help="write the Markdown report here")
    audit.add_argument("--repo", default=None, help="audit only this repo (current name)")
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


def main(argv: Sequence[str] | None = None, gh: Gh | None = None) -> int:
    """Entry point for the `jts-ops` console script."""
    args = build_parser().parse_args(argv)
    if args.command == "audit":
        return run_audit(args, gh)
    return 2  # unreachable: the subparser is required


def console_main() -> None:
    raise SystemExit(main())
