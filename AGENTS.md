# AGENTS.md

Instructions for AI coding agents working in this repository. This file is the single source; `CLAUDE.md` only imports it.

## Overview

jts-ops: 🧠 CLI that audits and applies the GitHub standard across all repositories

- Kind: `jts`
- Stack: Python (uv, ruff, pyright, pytest)
- Visibility: public
- Status: wip
- License: mit

## Module map

| Module | Does |
|---|---|
| `cli.py` | argparse entry point: `audit`, `apply`, `new`, `migrate`, `adopt`; prints, writes reports, sets exit codes |
| `standard.py` | Loads `standard.yml` and `labels.yml` into a `Standard` |
| `inventory.py` | Merges the public `inventory.yml` with the optional private inventory |
| `naming.py` | Repo name validation against the standard |
| `gh.py` | Read-side adapter over the `gh` CLI; `Gh` is read-only unless told otherwise |
| `state.py` | `RepoState`, the observed state of one repo |
| `audit.py` | Pure checks of a `RepoState` against the standard, plus JSON and Markdown reports |
| `apply.py` | `plan_changes` (pure) and `execute` (writes) for settings, topics, descriptions, labels and security |
| `migrate.py` | `plan_migration` (pure) and `execute_migration` for the irreversible rename, archive and local-move steps, plus the banner and license loaders |
| `new.py` | The `new` flow: render the template, lock, commit, create the repo, apply |
| `adopt.py` | The `adopt` flow: render the template onto an existing clone (skeleton off, README and any license kept), then issue, branch, commit, push and PR |

## Rules

- `Gh` is read-only unless `--execute` is passed. Never bypass `read_only` or call a write endpoint from a dry-run path.
- Tests never hit the network. They use the invented fixtures in `tests/fixtures/` and fake runners, never the real `gh`, GitHub or private inventory (the real-data tests skip when those are absent).
- Never name a private repository in code, tests, fixtures, docs or commit messages: this repository is public. Use invented names.
- Never run `apply`, `new` or `adopt` with `--execute` against the real account while developing.

## Setup

```sh
uv sync
pre-commit install --install-hooks
```

Or open the repository in a dev container or GitHub Codespaces, which runs the setup for you.

## Commands

| Task | Command |
|---|---|
| Test | `uv run pytest` |
| Lint | `uv run ruff check` and `uv run pyright` |
| Format | `uv run ruff format` |

CI runs the same checks through the shared workflows in [`.github`](https://github.com/JonathanTSilva/.github). The single required check is `ci`.

## Conventions

- Branches: `<type>/<issue>-<slug>`, for example `feat/12-add-parser`. See [branches and commits](https://github.com/JonathanTSilva/.github/blob/main/handbook/branches-and-commits.md).
- Commits and PR titles: Conventional Commits. Squash-merge only.
- Names: lowercase kebab-case. See [naming](https://github.com/JonathanTSilva/.github/blob/main/handbook/naming.md).
- Layout of this repo: [repo anatomy](https://github.com/JonathanTSilva/.github/blob/main/handbook/repo-anatomy.md).
- Releases: release-please opens a release PR from Conventional Commits on `main`. Nobody edits the changelog or tags by hand. See [releases](https://github.com/JonathanTSilva/.github/blob/main/handbook/releases.md).
- Labels and issues: [labels and issues](https://github.com/JonathanTSilva/.github/blob/main/handbook/labels-and-issues.md).
- Security: [security](https://github.com/JonathanTSilva/.github/blob/main/handbook/security.md). Pin third-party actions by full commit SHA with a version comment.
- AI assistance is disclosed in the PR template: [AI assistance](https://github.com/JonathanTSilva/.github/blob/main/handbook/ai.md).
- Whole handbook: [handbook](https://github.com/JonathanTSilva/.github/blob/main/handbook/README.md).

## Boundaries

- Never commit secrets, tokens, `.env` files or real data.
- Never push to `main`. Work on a branch and open a pull request.
- Never force-push.
- Do not rename, archive, delete or change the visibility of this repository.
- Do not edit `LICENSE`.
- Files rendered from the template (`.copier-answers.yml` and the standard files) change through `copier update`, not by hand.

## Model routing

Three subagents in `.claude/agents/` carry fixed models, so routing is built in.

| Agent | Model | Use for |
|---|---|---|
| `architect` | Opus | Thinking: designs, plans, trade-offs, reviews, anything ambiguous or irreversible |
| `builder` | Sonnet | Building: implementing a scoped task from a plan, writing tests, refactors |
| `mechanic` | Haiku | Mechanical work: precise, checklist-driven, repetitive edits with a verification command |

Rules:

1. Opus thinks. Design, plans, reviews, and anything that cannot be undone.
2. Sonnet builds. Give it the task text, the files it may touch and the done-check.
3. Haiku applies. Give it an exact checklist and a command that proves it is done. If that fails, it escalates to Sonnet and does not improvise.
4. Jonathan decides. Approvals and irreversible GitHub actions (rename, archive, visibility change, delete) wait for him.
