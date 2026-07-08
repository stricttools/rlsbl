# Scaffold: per-target CI workflows omit `working-directory` for path-based subdir targets

## Summary

In a **standalone** (non-monorepo) repo whose `.rlsbl/config.json` declares a
**path-based subdir target** (e.g. `"targets": ["go", {"name":"pypi","path":"pypi/"}]`),
`rlsbl scaffold` generates a per-target CI workflow (`.github/workflows/ci-pypi.yml`)
whose job runs its build commands at the **repo root**, not in the target's subdirectory.
Because the target's `pyproject.toml`/`uv.lock` live in `pypi/`, CI fails immediately, and
the publish gate (which waits for CI to pass) then refuses to publish. The merged
`publish.yml` handles this correctly; the per-target CI path does not.

This blocks the first (and every) release for any standalone repo that uses a subdir
target — a Go binary distributed with npm/PyPI wrapper subdirs is the canonical case.

Confirmed against rlsbl **0.101.1** (`pyproject.toml` version).

## Reproduction

1. Standalone repo, `.rlsbl/config.json`:
   ```json
   { "targets": ["go", {"name":"pypi","path":"pypi/"}],
     "pipelines": { "go": {"type":"go","local":true,"install_paths":["."]},
                    "pypi": {"type":"pypi","local":false} } }
   ```
   with `pypi/pyproject.toml` and `pypi/uv.lock` present.
2. `rlsbl scaffold` → generates `.github/workflows/ci-pypi.yml` with:
   ```yaml
   jobs:
     test:
       steps:
         - uses: actions/checkout@v6
         - uses: astral-sh/setup-uv@v7
         - run: uv python install ${{ matrix.python-version }}
         - run: uv sync --locked        # runs at repo root
         - run: uv run python -c "import <pkg>"
   ```
3. On CI, `uv sync --locked` fails: `error: No pyproject.toml found in current directory
   or any parent directory` (exit 2). CI red → publish gate refuses to publish.

Manual workaround that fixes it (added to the generated file by hand):
```yaml
  test:
    defaults:
      run:
        working-directory: ./pypi/
```
`uses:` steps (checkout/setup-uv) correctly ignore `working-directory`; only `run:` steps
honor it, which is exactly what's needed.

## Root cause

The subdir path is known at scaffold time but is never threaded into the CI render path.

- **CI template is path-agnostic:** `rlsbl/templates/pypi/ci.yml.tpl:16-33` has no
  `working-directory`, no `defaults` block, and no path variable.
- **Publish gets the path via structural post-render injection**, NOT from the template:
  `rlsbl/commands/init_cmd.py:1744-1757` (`_generate_merged_publish`, defined at 1648)
  reads `target_path = target_paths.get(target_name, ".")` and, when `!= "."`, injects
  `defaults.run.working-directory` into every job (and calls
  `_rewrite_action_paths_for_jobs`). The publish template
  (`rlsbl/templates/pypi/publish.yml.tpl:21-37`) itself has no working-directory either.
- **The CI generation path never does this.** In `run_cmd_multi`
  (`rlsbl/commands/init_cmd.py:2001`), the CI block (`:2062-2116`) renders CI templates as
  raw strings via `plan_mappings` → `process_template` (pure `{{var}}` substitution,
  `:501-551`), with no structural YAML step. `target_paths` is built at `:2023` and passed
  to `_generate_merged_publish` (`:2123`) and `_finalize_scaffold` (`:2168`), but is
  **never passed into the CI plan generation**; the `ci_vars` dict (`:2104`) has no
  path/working-directory key.
- Target layer corroboration: `rlsbl/targets/pypi.py:334-399` — `template_vars` exposes no
  path/working-directory variable, and `template_mappings` returns the CI template rendered
  verbatim.

There is already a proven analog on the **monorepo** CI path:
`rlsbl/commands/monorepo/sync.py:47-60` `_inject_working_directory(doc, path)` (parses the
CI YAML, adds `defaults.run.working-directory` per job, only-if-absent) plus
`_rewrite_version_file_inputs` (`:63-84`). The standalone `run_cmd_multi` CI path has no
equivalent.

## Blast radius

Generic to `run_cmd_multi`'s CI path: every subdir (`path != "."`) target whose CI has
`run:` steps is broken. Confirmed by inspecting each CI template's run steps:

| Target | CI template | Broken as a subdir target? |
|---|---|---|
| pypi | `templates/pypi/ci.yml.tpl` | Yes (confirmed empirically) |
| npm | `templates/npm/ci.yml.tpl` (+ yarn/pnpm) | Yes if a test script exists; wrapper packages with no test script are skipped (`init_cmd.py:2086-2090` `_is_npm_wrapper`) |
| cargo | `templates/cargo/ci.yml.tpl` | Yes |
| hex | `templates/hex/ci.yml.tpl` | Yes |
| deno | `templates/deno/ci.yml.tpl` | Yes |
| maven | `templates/maven/ci.yml.tpl` | Yes |
| swift | `templates/swift/ci.yml.tpl` | Yes |
| zig | `templates/zig/ci.yml.tpl` | Yes |
| dart | `templates/dart/ci.yml.tpl` | Yes |
| flutter | `templates/flutter/ci.yml.tpl` | Yes |
| docker | `templates/docker/ci.yml.tpl` | Only `uses:` steps, no `run:` — build-context path is a separate concern |
| go | `templates/go/ci.yml.tpl` | Unaffected (root-level; injection skipped for `"."`) |

**Secondary issue in the same class:** setup actions with a version-file input
(`actions/setup-go` `go-version-file`, `setup-node` `node-version-file`, `setup-python`
`python-version-file`) resolve relative to the repo root, NOT `working-directory`. Any
subdir target CI using such an input also needs path-prefixing (the monorepo path already
handles this via `_rewrite_version_file_inputs`). The pypi CI uses a matrix
`uv python install` (no version-file), so pypi needs only the working-directory fix; other
targets may need both.

## Proposed fix

The fix belongs in the **render code**, not the templates (mirroring publish), because the
CI templates are shared/path-agnostic and the subdir is only known at scaffold time.

In `run_cmd_multi`'s CI block (`rlsbl/commands/init_cmd.py:2062-2116`): for each CI mapping
whose target has `target_path = target_paths.get(target, ".")` with `target_path != "."`,
structurally inject `defaults.run.working-directory: <target_path>` into every job of the
rendered CI YAML before it is planned/written. Reuse the existing
`_inject_working_directory(doc, path)` (and `_rewrite_version_file_inputs` for
version-file-input targets) from `rlsbl/commands/monorepo/sync.py` — identical semantics to
the publish injection at `init_cmd.py:1747-1757`.

### Options

1. **Reuse the monorepo helpers in the standalone CI path (recommended).**
   - Pros: one proven implementation for both CI paths; minimal new code; matches publish
     behavior exactly.
   - Cons: requires transforming the rendered CI content inside/around `plan_mappings`.
2. **Build CI content via a structural YAML helper analogous to
   `_generate_merged_publish` + `_plan_merged_publish`.**
   - Pros: fully structural, consistent with the publish generator.
   - Cons: more code; larger change to the CI generation flow.

### Important design constraint (both options)

CI flows through `plan_mappings`, which pre-renders content AND three-way-merges against
stored bases in `.rlsbl/bases/`. The injected `working-directory` must ALSO be written into
the **saved merge base**, or subsequent `rlsbl scaffold` runs will produce spurious
conflicts on the CI file. This base-consistency is the one non-trivial part of the fix.

## Affected files

- `rlsbl/commands/init_cmd.py` — CI generation block `:2062-2116` (fix site); reference
  injection at `:1744-1757`; `target_paths` at `:2023`.
- `rlsbl/commands/monorepo/sync.py:47-84` — `_inject_working_directory` /
  `_rewrite_version_file_inputs` to reuse.
- Per-target CI templates under `rlsbl/templates/*/ci.yml.tpl` — no change needed if fixed
  in render code (confirm none rely on root-relative paths another way).

## Tests

No regression coverage exists:
- `tests/test_scaffold_per_target_ci.py` exercises only root-path targets and asserts file
  existence, never `working-directory`.
- `tests/test_subdirectory_targets.py` / `tests/test_multi_target.py` cover version I/O and
  path resolution, never scaffold CI output.
- All `working-directory` assertions are publish/monorepo only
  (`tests/test_publish_inline.py`, `tests/test_monorepo_sync.py`).

Add a red-green test to `tests/test_scaffold_per_target_ci.py` (it already imports
`run_cmd_multi` and has the `mock_git_repo` fixture): config with
`["go", {"name":"pypi","path":"pypi/"}]`, place `pyproject.toml`/`uv.lock` under `pypi/`,
run `run_cmd_multi`, parse `.github/workflows/ci-pypi.yml`, assert
`jobs.test.defaults.run.working-directory == "pypi"` and that `ci-go.yml` has none. Fails
on current code, passes after the fix. Consider a parametrized variant across the other
subdir-broken targets.

## Effort estimate

Small–medium. Core injection is a few lines reusing existing helpers; the real work is
routing the rendered CI content through a structural transform within `plan_mappings` and
keeping the saved `.rlsbl/bases/` copy consistent so re-scaffolds don't conflict, plus the
regression test(s). Roughly half a day including tests and verifying re-scaffold idempotency.
