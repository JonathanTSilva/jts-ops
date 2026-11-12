# AGENTS.md

Instructions for AI coding agents working in this repository. This file is the single source; `CLAUDE.md` only imports it.

## Overview

jts-ops: 🧠 Audit, apply and sync the GitHub standard across all of Jonathan's repos

- Kind: `jts`
- Stack: Python (uv, ruff, pyright, pytest)
- Visibility: public
- Status: wip
- License: mit

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
