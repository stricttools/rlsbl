## Release workflow

This project uses [rlsbl](https://github.com/smm-h/rlsbl) for release orchestration.

- Run `rlsbl release init` to scaffold `.rlsbl/releases/unreleased.toml`
- Edit the release file: set bump type (patch/minor/major)
- Run `rlsbl release run --watch --yes` to execute
- CI handles publishing automatically via the publish workflow
- Never publish manually -- always use `rlsbl release run`
- Use `rlsbl release run --dry-run` to preview without making changes
- Global flags `--dry-run`, `--yes`, `--quiet` are available on all commands

## Changelog

CHANGELOG.md is generated from JSONL entries -- never edit it by hand.

- After each commit: `rlsbl changelog add --commits <hash> --description "What changed" --type feature|fix|breaking`
- For internal changes: `rlsbl changelog add --commits <hash> --no-user-facing`
- Generate CHANGELOG.md: `rlsbl changelog generate`
- Validate: `rlsbl check --tag changelog`

## Development

- Use `rlsbl dev install` for local editable installs (picks the right tool per target: `uv tool install -e` for pypi, `npm link` for npm, `go install` for go, etc.)

## CI and scaffolding

- `rlsbl scaffold` generates CI workflows and git hooks
- `rlsbl scaffold --update` picks up new templates via three-way merge
- Pre-push hook enforces JSONL commit coverage (blocks push until all commits are covered)

## Checks

- `rlsbl check --all` -- run all project checks
- `rlsbl check --tag changelog` -- changelog validation
- `rlsbl check --tag project` -- project structure checks
- `rlsbl status` -- version, branch, last tag, changelog coverage
