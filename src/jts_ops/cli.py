"""Command line entry point."""

import argparse
import contextlib
import datetime
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from jts_ops.adopt import TEMPLATE_REF, AdoptError, adopt_repo
from jts_ops.apply import RULESET_NAME, execute, plan_changes, ruleset_eligible
from jts_ops.audit import Finding, Result, audit_repo, build_report
from jts_ops.gh import Gh, GhError, fetch_state, list_repos
from jts_ops.inventory import InventoryEntry, load_inventory
from jts_ops.migrate import (
    Step,
    describe_step,
    execute_migration,
    load_banner,
    load_license_texts,
    plan_digest,
    plan_migration,
)
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
from jts_ops.sync import SyncAction, execute_sync, plan_sync, scan_local, summarize

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
    adopt = commands.add_parser(
        "adopt",
        help="render the template onto an existing repo's clone and open a PR "
        "(dry run unless --execute)",
    )
    adopt.add_argument("name", help="the repo's current name, as in the inventory")
    adopt.add_argument("--stack", required=True, choices=STACKS)
    adopt.add_argument("--standard-dir", type=Path, default=None, help=standard_dir_help)
    adopt.add_argument(
        "--projects-dir", type=Path, default=Path("~/Projects"), help="where local clones live"
    )
    adopt.add_argument(
        "--template", default=DEFAULT_TEMPLATE, help="Copier template (git URL or local path)"
    )
    adopt.add_argument(
        "--vcs-ref",
        default=TEMPLATE_REF,
        help=f"template git ref (default {TEMPLATE_REF})",
    )
    adopt.add_argument(
        "--render-only",
        action="store_true",
        help="run only the local steps (precheck, scratch branch, copier), leaving the changes "
        "uncommitted: no issue, no commit, no push; needs no --execute",
    )
    adopt.add_argument(
        "--execute", action="store_true", help="do it; without it only the steps are listed"
    )
    migrate = commands.add_parser(
        "migrate",
        help="plan and run the irreversible archiving and renaming steps "
        "(dry run unless --execute --plan FILE)",
    )
    migrate.add_argument("--standard-dir", type=Path, default=None, help=standard_dir_help)
    migrate.add_argument(
        "--repo",
        action="append",
        default=None,
        metavar="NAME",
        help="limit to this repo (current name); may be repeated",
    )
    migrate.add_argument("--plan-out", type=Path, default=None, help="write the plan here (JSON)")
    migrate.add_argument(
        "--execute",
        action="store_true",
        help="run the steps; requires --plan with the digest of a reviewed dry run",
    )
    migrate.add_argument("--plan", type=Path, default=None, help="the reviewed plan to execute")
    migrate.add_argument(
        "--projects-dir", type=Path, default=Path("~/Projects"), help="where local clones live"
    )
    migrate.add_argument(
        "--template-dir",
        type=Path,
        default=Path("~/Projects/template-repo"),
        help="template checkout the license texts are read from",
    )
    sync = commands.add_parser(
        "sync",
        help="mirror GitHub into the projects dir: clone new repos, archive and rename "
        "folders, fast-forward clean clones (dirty clones are only fetched)",
    )
    sync.add_argument("--standard-dir", type=Path, default=None, help=standard_dir_help)
    sync.add_argument(
        "--projects-dir", type=Path, default=Path("~/Projects"), help="where local clones live"
    )
    sync.add_argument(
        "--dry-run", action="store_true", help="list what would happen; run no git write"
    )
    sync.add_argument(
        "--notify",
        action="store_true",
        help="desktop notification when something failed or a dirty or orphaned clone exists",
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


def _load_plan(path: Path) -> tuple[str, list[Step]] | None:
    """The digest and steps of a saved plan, or None when the file is unusable or altered."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        steps = [Step(**row) for row in data["steps"]]
        digest = str(data["digest"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return (digest, steps) if plan_digest(steps) == digest else None


def run_migrate(args: argparse.Namespace, gh: Gh | None = None) -> int:
    if args.plan is not None and not args.execute:
        print("jts-ops: --plan only makes sense with --execute", file=sys.stderr)
        return 2
    if args.execute and args.plan is None:
        print("jts-ops: --execute requires --plan FILE from a reviewed dry run", file=sys.stderr)
        return 3
    plan_file: Path | None = args.plan
    approved: tuple[str, list[Step]] | None = None
    if args.execute and plan_file is not None:
        approved = _load_plan(plan_file)
        if approved is None:
            print("jts-ops: the plan file is unreadable or was altered", file=sys.stderr)
            return 3

    standard_dir: Path = args.standard_dir or default_standard_dir()
    std = load_standard(standard_dir)
    inventory = load_inventory(standard_dir)
    try:
        banner = load_banner(standard_dir)
        license_texts = load_license_texts(
            args.template_dir.expanduser(), datetime.date.today().year
        )
    except (OSError, ValueError) as err:
        print(f"jts-ops: {err}", file=sys.stderr)
        return 2
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

    steps: list[Step] = []
    problems = 0  # repos that could not be planned
    # Sorted, so the digest does not depend on the order the API happens to list repos in.
    for listed in sorted(listed_repos, key=lambda r: str(r["name"])):
        name: str = listed["name"]
        entry = inventory.get(name)
        if entry is None or (only is not None and name not in only):
            continue
        try:
            state = fetch_state(gh, listed)
            steps += plan_migration(state, entry, banner, license_texts, std)
        except (GhError, ValueError) as err:
            reason = (err.message if isinstance(err, GhError) else str(err)).splitlines()
            print(f"{name}\n  skip: cannot plan: {reason[0] if reason else 'unknown error'}")
            problems += 1
    digest = plan_digest(steps)

    if approved is not None and approved[0] != digest:
        print(
            "jts-ops: the plan no longer matches GitHub or the inventory; run a new dry run "
            f"and review it (approved {approved[0][:12]}, now {digest[:12]})",
            file=sys.stderr,
        )
        return 3

    if args.plan_out:
        rows = [{"repo": s.repo, "action": s.action, "payload": s.payload} for s in steps]
        args.plan_out.write_text(
            json.dumps({"digest": digest, "steps": rows}, indent=2) + "\n", encoding="utf-8"
        )
    last_repo = None
    for step in steps:
        if step.repo != last_repo:
            print(step.repo)
            last_repo = step.repo
        print(f"  {step.action}: {describe_step(step)}")
    print(f"{len(steps)} steps, digest {digest}")
    if not args.execute:
        print("dry run: nothing written (pass --execute --plan FILE to run it)")
        return 1 if problems else 0

    failures = 0
    for step, error in execute_migration(steps, gh, args.projects_dir.expanduser()):
        if error is not None:
            failures += 1
            print(f"  FAILED {step.repo} {step.action}: {error}", file=sys.stderr)
    print(f"ran {len(steps) - failures} of {len(steps)} steps; {failures} failures")
    return 1 if failures or problems else 0


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


def run_adopt(args: argparse.Namespace, gh: Gh | None = None, run: Cmd = default_cmd) -> int:
    standard_dir: Path = args.standard_dir or default_standard_dir()
    std = load_standard(standard_dir)
    inventory = load_inventory(standard_dir)
    # Real runs may write to GitHub (issue, PR); a dry run or a render-only run never does.
    gh = gh or Gh(std.owner, read_only=not args.execute or args.render_only)
    try:
        adopt_repo(
            args.name,
            stack=args.stack,
            std=std,
            inv=inventory,
            gh=gh,
            cmd=run,
            projects_dir=args.projects_dir.expanduser(),
            template=args.template,
            vcs_ref=args.vcs_ref,
            execute=args.execute,
            render_only=args.render_only,
            on_step=lambda step: print(f"  {step}"),
        )
    except AdoptError as err:
        print(f"jts-ops: {err}", file=sys.stderr)
        return 2
    if not (args.execute or args.render_only):
        print("dry run: nothing was run or written (pass --execute to do it)")
    return 0


SYNC_QUIET_KINDS = frozenset({"fetch", "fast-forward"})


def _notify(run: Cmd, body: str) -> None:
    with contextlib.suppress(OSError):  # no notify-send here; the summary was printed anyway
        run(["notify-send", "jts-ops sync", body], None)


def run_sync(args: argparse.Namespace, gh: Gh | None = None, run: Cmd = default_cmd) -> int:
    standard_dir: Path = args.standard_dir or default_standard_dir()
    std = load_standard(standard_dir)
    inventory = load_inventory(standard_dir)
    projects_dir: Path = args.projects_dir.expanduser()
    if not projects_dir.is_dir():
        print(f"jts-ops: no such projects dir: {projects_dir}", file=sys.stderr)
        return 2
    gh = gh or Gh(std.owner)  # read-only: sync never writes to GitHub
    try:
        remote = list_repos(gh)
    except GhError as err:
        print(f"jts-ops: cannot list repos: {err.message}", file=sys.stderr)
        if args.notify and not args.dry_run:
            _notify(run, f"cannot list repos: {err.message}")
        return 1
    actions = plan_sync(
        remote, scan_local(projects_dir, run), inventory, projects_dir, owner=std.owner
    )
    for action in actions:
        if action.kind not in SYNC_QUIET_KINDS:
            print(f"  {action.kind}: {action.repo}: {action.detail}")
    if args.dry_run:
        results: list[tuple[SyncAction, str | None]] = [(a, None) for a in actions]
    else:
        results = execute_sync(actions, run, projects_dir)
    failures = 0
    for action, error in results:
        if error is not None and error != "skipped: earlier step failed":
            failures += 1
            print(f"  FAILED {action.repo} {action.kind}: {error}", file=sys.stderr)
    summary = summarize(results, dry_run=args.dry_run)
    print(summary)
    flagged = any(
        a.kind == "orphaned" or (a.kind == "skip" and a.detail.startswith("dirty:"))
        for a in actions
    )
    if args.notify and not args.dry_run and (failures or flagged):
        _notify(run, summary)
    return 1 if failures else 0


def main(argv: Sequence[str] | None = None, gh: Gh | None = None, run: Cmd = default_cmd) -> int:
    """Entry point for the `jts-ops` console script."""
    args = build_parser().parse_args(argv)
    if args.command == "new":
        return run_new(args, gh, run)
    if args.command == "adopt":
        return run_adopt(args, gh, run)
    if args.command == "audit":
        return run_audit(args, gh)
    if args.command == "apply":
        return run_apply(args, gh)
    if args.command == "migrate":
        return run_migrate(args, gh)
    if args.command == "sync":
        return run_sync(args, gh, run)
    return 2  # unreachable: the subparser is required


def console_main() -> None:
    raise SystemExit(main())
