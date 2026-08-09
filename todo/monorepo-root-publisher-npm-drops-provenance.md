# Monorepo root publisher drops `--provenance` from the generated npm publish job

## Context

A monorepo releasable can declare a path-scoped npm target plus an npm pipeline:

```json
{
  "targets": ["pypi", {"name": "npm", "path": "npm"}],
  "pipelines": {
    "pypi": {"type": "pypi", "local": false, "target": "pypi"},
    "npm":  {"type": "npm",  "local": false, "provenance": true, "target": "npm"}
  }
}
```

`rlsbl monorepo sync` renders the root publisher's jobs through
`rlsbl/commands/monorepo/publish_inline.py::_render_root_publisher_jobs`, which
correctly injects `defaults.run.working-directory: npm`, the tag-prefix `if:`,
`needs: gate` and `permissions.id-token: write`.

## Problem 1 -- `provenance: true` is silently ignored

The generated job publishes with:

```yaml
- run: npm publish --access public ${{ steps.dist-tag.outputs.tag }}
```

`--provenance` is missing even though the pipeline declares `provenance: true`,
and even though `permissions.id-token: write` (which exists only to make
provenance possible) *is* emitted.

Cause: `_render_root_publisher_jobs` builds its template vars from
`_build_project_template_vars(project_dir, root)` and calls
`_generate_merged_publish(targets, tvars, target_paths=target_paths)`. Neither
sets the `npm.provenance` template var, so the
`{{#if npm.provenance}}--provenance {{/if}}` block in
`rlsbl/templates/npm/publish.yml.tpl` renders empty.

The standalone scaffold path does it right --
`rlsbl/commands/init_cmd.py` sets `vars_dict["npm.provenance"] =
_npm_provenance_var(ctx.config)` in two places (around lines 1760 and 2861).
The monorepo root-publisher path never does.

This is a silent downgrade of a security posture the operator explicitly
configured, and config validation makes `provenance` *mandatory* for npm
pipelines precisely so the choice is deliberate -- then the renderer discards
it. Nothing warns.

Likely fix: have `_render_root_publisher_jobs` set `tvars["npm.provenance"]`
from the member's merged config (reuse `_npm_provenance_var`), and/or pass
`pipelines=` through to `_generate_merged_publish` so pipeline config keys reach
the templates on this path the same way they do for the scaffold. Worth auditing
whether any other pipeline config key (go `artifact`, launcher `wraps` /
`binary_source` / `download`) is dropped on the same path.

## Problem 2 -- a path-scoped target leaks to every member of the releasable

`detect_targets()` treats a releasable's `targets` list as authoritative for
every member (deliberately, so a per-package `targets: []` cannot erase it), and
`_parse_target_entry` resolves a relative `path` against *each member's*
directory. So `{"name": "npm", "path": "npm"}` on a releasable with N members
resolves to `<member>/npm` for all N.

Observed on a releasable with 27 members: one real npm package, 26 phantom ones.
Consequences:

- `rlsbl monorepo sync` prints 26 `Warning: target 'npm' path '.../<member>/npm'
  does not exist` lines, each followed by a 26-line
  `Warning: template_vars failed for target npm` + full `FileNotFoundError`
  traceback from `rlsbl/commands/monorepo/sync.py:480` -> `targets/npm.py:182`.
  Sync still succeeds and generates the right jobs, so this is pure noise -- but
  it is ~700 lines of tracebacks on every sync, which trains operators (and
  agents) to ignore sync output.
- `_sync_member_package_versions_plan` (`commands/release/execute.py`) is
  stricter: a member whose effective `publish_mode` is not `"none"` hits
  `check_project_exists(entry.path)` and raises
  `ConfigError: member '<m>' declares target 'npm' but its manifest does not
  exist at <m>/npm. Cannot sync version.` A releasable where every non-primary
  member is `publish_mode: "none"` survives; a single member that forgets its
  `publish_mode` override turns a path-scoped target on the releasable into a
  hard release abort, with an error that describes a target the member never
  declared.

A path-scoped target arguably belongs to one member, not to the whole
releasable. Options worth weighing: resolve a path-scoped entry only against the
releasable's primary/root member; let a member opt out of an inherited target
without being able to erase the list wholesale; or at minimum skip (silently,
once) members where the resolved manifest does not exist, instead of warning 26
times and then hard-erroring elsewhere.

`target.template_vars()` failing should probably also not print a traceback for
the "path does not exist" case that `detect_targets` already warned about.

## Effort

Problem 1: small -- one template var on one code path, plus a regression test
asserting `--provenance` appears in a monorepo root publisher's generated npm
job when the pipeline sets `provenance: true`.

Problem 2: medium -- needs a design decision about the scope of a path-scoped
target within a releasable before any code changes.
