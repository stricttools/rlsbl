# A path-scoped target on a releasable crashes `release run` and corrupts `release init`

## Severity

Blocker. A releasable that declares a `{"name": ..., "path": ...}` target cannot
be released at all: `rlsbl monorepo release run` raises an unhandled
`TypeError`, and `rlsbl monorepo release init` writes a release file that fails
its own schema validation. The config shape itself is fully sanctioned --
`config.schema.toml` models `TargetRef` as a union of a bare string and a
`{name, path}` record, `_parse_target_entry` implements it, and
`publish_inline.py::_render_root_publisher_jobs` documents it by example
(`e.g. {"name": "npm", "path": "npm"}`) and renders the correct
`defaults.run.working-directory` for it. Only the release path assumes every
entry is a string.

## Reproduction

Releasable config (`.rlsbl-monorepo/releasables/<rel>/config.json`):

```json
{
  "publish_mode": "ci",
  "targets": ["pypi", {"name": "npm", "path": "npm"}],
  "pipelines": {
    "pypi": {"type": "pypi", "local": false, "target": "pypi"},
    "npm":  {"type": "npm",  "local": false, "provenance": true, "target": "npm"}
  }
}
```

`rlsbl monorepo sync` handles this correctly and generates a working npm publish
job. Then:

```python
from rlsbl.targets import read_releasable_targets
set(read_releasable_targets(".rlsbl-monorepo/releasables/<rel>/config.json"))
# TypeError: cannot use 'dict' as a set element (unhashable type: 'dict')
```

## Bug 1 -- `validate_release_targets` raises `TypeError`

`rlsbl/commands/release/validate.py`, in the releasable branch:

```python
rel_targets = read_releasable_targets(rel_config_path)
if rel_targets is not None:
    detected = set(rel_targets)          # <-- dict entry is unhashable
```

`read_releasable_targets` returns the raw config list, so a record-form entry
reaches `set()` and blows up. This runs during release validation, so the
release aborts with a bare `TypeError` and no actionable message.

Fix: normalize to names first, e.g.
`detected = {e if isinstance(e, str) else e["name"] for e in rel_targets}` --
better, give `read_releasable_targets` (or a new sibling) a documented
name-only return and make every caller use it.

## Bug 2 -- `collect_releasable_targets` returns raw entries, so `release init` writes an invalid `include`

`rlsbl/targets/__init__.py::collect_releasable_targets` says in its docstring
"Returns a deduplicated list of target names", but the releasable branch returns
`list(rel_targets)` -- the raw entries, records included. The member-level
fallback branch just below it *does* return names (`e.name`), so the two
branches disagree about the contract.

That value flows into
`rlsbl/commands/monorepo/batch_release_init.py::_build_pkg_section` as
`target_names` and is written straight into the release file:

```python
pkg_table.add("include", target_names)
```

So `rlsbl monorepo release init` emits

```toml
include = ["pypi", {name = "npm", path = "npm"}]
```

which `rlsbl/release_file.py` then rejects: `include must be a list of strings`.
`_render_commented_section` has the same problem for zero-commit items.

Downstream, `batch_plan.py` does `registry = rc.include[0]` and
`TARGETS[registry]`, which would also fail if a record entry were ever first in
`include`.

Fix: make `collect_releasable_targets` honour its docstring and return names on
both branches. Check for other consumers that treat its result as names.

## Suggested test coverage

- A releasable whose `targets` contains a record entry: `release init` produces
  `include = ["pypi", "npm"]` (strings only) and the resulting file passes
  `release_file` validation.
- `validate_release_targets` accepts the same releasable without raising, and
  correctly reports a missing/extra target by *name*.
- `collect_releasable_targets` returns `["pypi", "npm"]`, not raw entries, for
  both the releasable-config branch and the member-detection fallback.

## Effort

Small -- two call sites plus a shared normalization helper, and the tests above.
The shape is already supported everywhere else; this is the release path
catching up.
