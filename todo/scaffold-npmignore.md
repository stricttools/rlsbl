# Scaffold .npmignore for npm targets

## Problem

When `rlsbl scaffold` sets up an npm target, it creates `.github/workflows/publish.yml` for npm publishing but does NOT create a `.npmignore` file. This means `npm publish` includes the entire project tree — Python source, tests, todo files, .rlsbl/ metadata, .github/ workflows, etc.

A consumer published their first npm package and it contained 392 files including server Python code, test files, and internal todo documents.

## Proposed solution

When scaffolding an npm target (or when `--update` runs on a project with an npm target), create a `.npmignore` file with sensible defaults:

```
# Exclude non-npm content
server/
tests/
todo/
e2e/
scripts/
docs/
.rlsbl/
.github/
.venv/
__pycache__/
*.py
*.pyc
*.toml
*.lock
```

The file should be **user-owned** (scaffold creates once, never overwrites on `--update`), same as `LICENSE` and `CHANGELOG.md`.

## Alternative

Instead of `.npmignore`, scaffold a `files` whitelist in `package.json`. This is stricter (only listed files are published) but more fragile (new files must be explicitly added).

`.npmignore` is the safer default since it's exclusion-based.

## Affected files

- `rlsbl/templates/npmignore` — new template
- `rlsbl/scaffold.py` (or equivalent) — create .npmignore when target includes npm

## Effort

Small — one new template file + a few lines in scaffold logic.
