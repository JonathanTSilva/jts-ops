# jts-ops

🧠 Audit, apply and sync the GitHub standard across all of Jonathan's repos

<!-- badges:start -->
[![Status](https://img.shields.io/badge/status-wip-yellow?style=flat-square)](https://www.repostatus.org/#wip)
[![CI](https://img.shields.io/github/actions/workflow/status/JonathanTSilva/jts-ops/ci.yml?branch=main&style=flat-square&label=ci)](https://github.com/JonathanTSilva/jts-ops/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/JonathanTSilva/jts-ops?style=flat-square)](https://github.com/JonathanTSilva/jts-ops/releases)
[![Scorecard](https://img.shields.io/ossf-scorecard/github.com/JonathanTSilva/jts-ops?style=flat-square&label=scorecard)](https://scorecard.dev/viewer/?uri=github.com/JonathanTSilva/jts-ops)
[![License](https://img.shields.io/badge/license-MIT-blue?style=flat-square)](LICENSE)
[![Codespaces](https://img.shields.io/badge/codespaces-open-181717?style=flat-square&logo=github)](https://codespaces.new/JonathanTSilva/jts-ops)
<!-- badges:end -->

## What it is

🧠 Audit, apply and sync the GitHub standard across all of Jonathan's repos

It is a `jts` repository, currently marked `wip`, set up from the shared standard in [`.github`](https://github.com/JonathanTSilva/.github).

## Quick start

```sh
git clone https://github.com/JonathanTSilva/jts-ops.git
cd jts-ops
uv sync
uv run pytest
```

Or open it in a dev container or GitHub Codespaces.

## Usage

Usage notes live here as the project grows. Agents and contributors: see [AGENTS.md](AGENTS.md) for commands and conventions.

## Docs

The conventions this repository follows are in the [handbook](https://github.com/JonathanTSilva/.github/blob/main/handbook/README.md), including [repo anatomy](https://github.com/JonathanTSilva/.github/blob/main/handbook/repo-anatomy.md) and [branches and commits](https://github.com/JonathanTSilva/.github/blob/main/handbook/branches-and-commits.md).

## Contributing

See the [contributing guide](https://github.com/JonathanTSilva/.github/blob/main/CONTRIBUTING.md). Branches are named `<type>/<issue>-<slug>`, commits and PR titles follow Conventional Commits, and PRs are squash-merged.

## License

MIT. See [LICENSE](LICENSE).
