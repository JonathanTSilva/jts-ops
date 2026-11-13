# jts-ops

🧠 CLI that audits and applies the GitHub standard across all repositories

<!-- badges:start -->
[![Status](https://img.shields.io/badge/status-wip-yellow?style=flat-square)](https://www.repostatus.org/#wip)
[![CI](https://img.shields.io/github/actions/workflow/status/JonathanTSilva/jts-ops/ci.yml?branch=main&style=flat-square&label=ci)](https://github.com/JonathanTSilva/jts-ops/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/JonathanTSilva/jts-ops?style=flat-square)](https://github.com/JonathanTSilva/jts-ops/releases)
[![Coverage](https://codecov.io/gh/JonathanTSilva/jts-ops/graph/badge.svg)](https://codecov.io/gh/JonathanTSilva/jts-ops)
[![Quality](https://sonarcloud.io/api/project_badges/measure?project=JonathanTSilva_jts-ops&metric=alert_status)](https://sonarcloud.io/summary/new_code?id=JonathanTSilva_jts-ops)
[![Scorecard](https://img.shields.io/ossf-scorecard/github.com/JonathanTSilva/jts-ops?style=flat-square&label=scorecard)](https://scorecard.dev/viewer/?uri=github.com/JonathanTSilva/jts-ops)
[![License](https://img.shields.io/badge/license-MIT-blue?style=flat-square)](LICENSE)
[![Codespaces](https://img.shields.io/badge/codespaces-open-181717?style=flat-square&logo=github)](https://codespaces.new/JonathanTSilva/jts-ops)
<!-- badges:end -->

## What it is

jts-ops reads the GitHub standard kept as data in the [`.github`](https://github.com/JonathanTSilva/.github) repository, plus an optional private inventory, and checks every repository of the account against it. `audit` reports what drifts, `apply` brings repository settings, topics, descriptions, labels and security in line, and `new` renders the [template](https://github.com/JonathanTSilva/template-repo) into a compliant new repository.

## Quick start

Install the command from Git:

```sh
uv tool install git+https://github.com/JonathanTSilva/jts-ops
```

Or run it from a clone without installing:

```sh
git clone https://github.com/JonathanTSilva/jts-ops.git
cd jts-ops
uv sync
uv run jts-ops --help
```

It needs the [GitHub CLI](https://cli.github.com) signed in (`gh auth login`) and a checkout of the standard at `~/Projects/.github`. `jts-ops new` also needs `uv` and `git` (and `npx` for the node stack). Or open the repository in a dev container or GitHub Codespaces.

## Usage

Audit every repo and write the reports:

```sh
jts-ops audit --json audit.json --markdown audit.md
jts-ops audit --repo my-repo        # one repo, by current name
jts-ops audit --public-only         # keep private repos out of every output
```

Apply the standard. This is a dry run that lists the planned changes:

```sh
jts-ops apply
jts-ops apply --repo my-repo --repo other-repo
jts-ops apply --execute             # make the changes
```

Create a compliant repo. Without `--execute` it only lists the steps:

```sh
jts-ops new my-tool --kind product --stack python --visibility public \
  --license mit --description "One line about it"
jts-ops new my-tool --kind product --stack python --visibility public \
  --license mit --description "One line about it" --render-only --execute
```

`--render-only` stops after the first local commit, before anything is created on GitHub.

### Dry-run guarantee

`audit` only reads. `apply` and `new` change nothing unless `--execute` is passed. The `gh` adapter is read-only by default and refuses every non-`GET` call, so a dry run cannot write even through a code path that forgot to check the flag.

### Configuration

| Setting | Default | Meaning |
|---|---|---|
| `JTS_STANDARD_DIR` or `--standard-dir` | `~/Projects/.github` | Directory with `standard.yml`, `labels.yml` and `inventory.yml` |
| `JTS_PRIVATE_INVENTORY` | `~/.config/jts-ops/inventory.private.yml` | Inventory of private repos, same schema as `inventory.yml` |
| `JTS_COPIER` | `copier` | Command used to render the template in `new` |

The public `inventory.yml` lists public repos only. Private repos live in the private inventory file, which never goes in a public repository. jts-ops merges both; a missing private file means public entries only, and a name in both files is an error.

## Vault notes

`jts-ops vault-sync [--vault ~/Projects/jts-brain] [--dry-run]` writes one note per repo into `02-Areas/Open Source/Repositories/` of an Obsidian vault and keeps it current. It only reads GitHub and only writes files in the vault; it never commits. It rewrites the properties it owns (type, repo, url, visibility, kind, status, stack, topics, last_release, ci, open_issues, archived) and the block between `<!-- jts-ops:start -->` and `<!-- jts-ops:end -->`. Everything else in a note is left alone, a renamed repo's note is moved to the new name, a repo whose name starts with `.` gets `dot-<rest>.md` (Obsidian hides dot files), and a file is written only when its content changed.

`jts-ops ideas [--vault ~/Projects/jts-brain] [--execute]` turns vault notes with `github: idea` and `repo: <name>` (and no `issue:`) into GitHub issues. It is a dry run unless `--execute` is given. The issue title is the note title; the body is the note's `## Idea` section or first paragraph plus a "From the vault" footer. It then writes `issue: <url>` and `github: issued` back into the note and adds the issue to the "Engineering" project when that exists. It refuses to create an issue in a public repo from a note whose text names a private repo.

## Automatic sync

`contrib/systemd` has a user timer that runs `jts-ops sync --notify` hourly. Install it:

```sh
cp contrib/systemd/* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now jts-ops-sync.timer
```

Verify it:

```sh
systemd-analyze --user verify contrib/systemd/jts-ops-sync.service contrib/systemd/jts-ops-sync.timer
systemctl --user list-timers | grep jts-ops
journalctl --user -u jts-ops-sync -n 20
```

## Docs

The conventions this repository follows are in the [handbook](https://github.com/JonathanTSilva/.github/blob/main/handbook/README.md), including [repo anatomy](https://github.com/JonathanTSilva/.github/blob/main/handbook/repo-anatomy.md) and [branches and commits](https://github.com/JonathanTSilva/.github/blob/main/handbook/branches-and-commits.md).

## Contributing

See the [contributing guide](https://github.com/JonathanTSilva/.github/blob/main/CONTRIBUTING.md). Branches are named `<type>/<issue>-<slug>`, commits and PR titles follow Conventional Commits, and PRs are squash-merged.

## License

MIT. See [LICENSE](LICENSE).
