# Migrate rlsbl health checks to strictcli's check system

## Context

strictcli v0.8.0 shipped a first-class check system. rlsbl has three separate check subsystems that should migrate to it:

1. **doctor** (11 checks, decorator-based registry, `--check <name>`, `--fix`): version consistency, name consistency, license, description, local-tag, remote-tag, github-release, branch-sync, changelog, library-lint, stale lock
2. **changelog validate** (7 checks, hard-coded dict): hashes_resolve, in_range, coverage, no_orphans, schema, batch_size_commits, batch_size_entries
3. **monorepo lint** (2-5 checks, inline): unregistered projects, stale entries, router exists, workflows synced, project targets

## Migration plan

### Step 1: Rename `rlsbl check` to `rlsbl check-availability`

The existing `check` command (checks name availability on registries) conflicts with strictcli's reserved `check` command. Rename it and add a deprecated alias.

### Step 2: Create `.strictcli/checks.toml`

Declare all ~20 checks with tags:
- `release` -- the 11 doctor checks
- `changelog` -- the 7 changelog validation checks
- `workspace` -- the monorepo checks
- `pre-push` -- checks that should run before pushing (changelog coverage, etc.)

### Step 3: Migrate check implementations

Convert each check function to `@app.check("name")` decorated functions returning `CheckResult`. The doctor's `(status, message)` tuples become `CheckResult(status, message, details)`. The changelog validate's `(bool, list[str])` tuples become `CheckResult("pass"/"fail", summary, details)`.

### Step 4: Create RlsblCheckContext

```python
class RlsblCheckContext:
    project_root: Path
    tag: str | None
    version: str | None
    workspace: dict | None
```

The doctor's ad-hoc argument passing (`needs_tag`, `needs_version`) is replaced by a single context object with all fields.

### Step 5: Update hook scripts

Replace `rlsbl pre-push-check` in `.git/hooks/pre-push` with `rlsbl check --tag pre-push`.

### Step 6: Deprecate old commands

- `rlsbl doctor` -> prints migration guidance, suggests `rlsbl check --tag release`
- `rlsbl changelog validate` -> suggests `rlsbl check --tag changelog`
- `rlsbl monorepo lint` -> suggests `rlsbl check --tag workspace`

### Step 7: Remove old code

After migration is stable, remove `commands/doctor.py`, inline monorepo lint, and the `CHECK_REGISTRY` pattern.

## Blocked on

- strictcli's `--fix` mechanism (deferred) -- doctor's `--fix` is a key feature. Can migrate checks without --fix first, add fix support when strictcli implements it.

## Effort

Medium. ~20 check functions to convert, plus TOML authoring, context design, hook updates, and command deprecation.
