# Target-support axes: the inventory behind the protocol migration

The release-target registry (`rlsbl/targets/__init__.py`) is meant to be the
single authority for what each target supports. Before this migration, that
authority was duplicated as target-name string literals scattered through
conditionals all over the package, plus a hand-declared `capabilities`
frozenset that had already drifted from the methods the targets actually
implement.

This file is the work list that drove the migration. It is an inventory, not a
specification: the live, line-accurate listing is produced by

    uv run python scripts/sweep_target_name_literals.py            # table
    uv run python scripts/sweep_target_name_literals.py --markdown # per-file
    uv run python scripts/sweep_target_name_literals.py --json     # machine

which parses every module under `rlsbl/` and reports target-name literals in
comparisons, membership tests, subscripts, `match` cases, and hand-listed
collection displays. Deliberately no counts are quoted here: the sweep output
is the enumeration, and a number typed into prose goes stale the moment a site
is migrated.

The permanent enforcement is `tests/test_target_literal_guard.py`, which
asserts that such literals occur only inside an allowlisted module set.

## The assignment rule

- **Behaviour invoked per target** becomes a protocol method: a default on
  `BaseTarget`, overridden by the targets that implement it.
- **Aggregate sets consumed elsewhere** become registry-derived structures,
  computed by iterating `TARGETS` and asking each target a question. Never
  hand-listed.

## Axes

### Yank dispatch — protocol method

`rlsbl/commands/yank.py` branched on `npm` / `go` / `pypi` to pick a
registry-removal routine, and fell through to a stderr line for anything else.

Migrated to `BaseTarget.yank()`, overridden by `NpmTarget` (npm deprecate),
`GoTarget` (a `retract` directive in `go.mod`), and `PypiTarget` (the
human-in-the-loop checklist, PyPI having no yank API). The base implementation
returns an explicit `unsupported` outcome naming the target.

### Built-in test runner — protocol method

`rlsbl/testing.py::run_project_tests` branched on `pypi` / `go` / `npm` /
`maven` and **silently returned success** for every other target. The
`test-suite` check (`rlsbl/checks/quality.py`) and `test-suite-workspace`
(`rlsbl/checks/workspace.py`) each carried their own copy of the recognized
set to steer around that silence.

Migrated to `BaseTarget.run_tests()`, returning a `SuiteRunOutcome` whose status
is `PASSED`, `FAILED` or `SKIPPED`. The base returns `SKIPPED` with a message
naming the target, so an unsupported target now produces a visible skip line in
the release step summary instead of a silent pass. The recognized set is
derived: `targets_with_builtin_tests()`.

### Library lint dispatch — parallel taxonomy, same no-silent-success discipline

`rlsbl/lint/__init__.py` dispatches on a **language** taxonomy
(`python`, `go`, `npm`, `maven`) that is deliberately NOT the target taxonomy:
`python` is not `pypi`, and one language may back several targets. The
taxonomies stay distinct.

What the two shared was the silent fallthrough: `_create_linter` and
`_create_import_scanner` returned `None` for an unhandled language and the
caller quietly did nothing. Consolidated into a single `LANGUAGES` table
(`rlsbl/lint/languages.py`) that declares, per language, its detection
manifests, linter factories, import-scanner availability and default exclude
patterns. `_detect_languages`, `_create_linter`, `_create_import_scanner` and
`_DEFAULT_EXCLUDE_PATTERNS` all derive from it, and a detected language with no
import scanner is reported as an explicit skip rather than silently dropped.

The target-side question ("does this target get library lint at all?") is a
separate, registry-derived set: `targets_with_library_lint()`.

### Name registries and normalization — registry-derived

- `rlsbl/registry.py::_REGISTRY_DISPATCH` hand-mapped `npm` / `pypi` / `go` to
  latest-version query functions. Now derived from the targets that implement
  `query_latest_version()`.
- `rlsbl/checks/project.py::check_name_consistency` built a `normalizers` dict
  keyed by target name. Now `target.normalize_package_name()`, a protocol
  method whose base implementation is `str.lower`.
- `rlsbl/commands/claim_name.py` hard-coded `("npm", "pypi")` as the claimable
  registries and branched on the name twice more (token requirement, publish
  routine). Now `BaseTarget.claim_placeholder()` plus
  `BaseTarget.claim_token_env_vars`, with the supported set derived.

### Check skip-sets — derived from protocol properties

`rlsbl/checks/__init__.py::CHECK_TARGETS` and several in-check `supported = {...}
& target_names` sets hand-listed which targets a check applies to. Each axis now
answers from the target:

| Axis | Protocol surface | How the target answers |
| --- | --- | --- |
| import analysis (`deps-*`, `dead-modules`, `dead-modules-stale`) | `supports_import_analysis` | implements `find_dead_modules` |
| circular-import analysis (`circular-deps`) | `supports_circular_dep_analysis` | implements `find_circular_dependencies` |
| lockfile floor policing (`dep-floors`) | `supports_dep_floors` | declared: a fact about the manifest format |
| library boundary lint (`library-lint`) | `lint_language` | names a language in the lint table |
| built-in test suite (`test-suite`) | `has_builtin_test_runner` | implements `run_tests` |
| CI workflow inlining (`workspace-ci-synced`) | `provides_ci_templates` | ships `ci.yml.tpl` |
| shared uv environment (`workspace-unbuildable`, the workspace test sync) | `shares_workspace_environment` | declared: a fact about the ecosystem |

The per-target detectors moved with the sets: `find_dead_modules` and
`find_circular_dependencies` are protocol methods, and each returns its own
user-facing explanation instead of the check spelling one out per target name.

A single-target check (`ruff-lint`, `maven-central-metadata`,
`dunder-version-missing`) no longer restates its scope in its own body either:
`check_scope_skip_reason` reads it from `CHECK_TARGETS`, which stays the one
place a check's scope is written down.

`MATRIX_COLUMNS` was already derived from `TARGETS.keys()` and stays that way.

**One scope question was deliberately left open.** `FlutterTarget` extends
`DartTarget`, so it inherits the Dart import analysers and the derived sets
would have pulled Flutter members into `deps-unused`, `deps-undeclared` and
`deps-dev-in-lib` -- three error-severity checks that have never applied to
them. Flutter explicitly drops both methods back to the base so the migration
changes no scope; whether Flutter should be in that set is a decision on its
own.

### Companion tags — existing protocol method

`rlsbl/checks/workspace.py::go-companion-tags` tested `e.name == "go"` to decide
whether a member needs companion tags, duplicating `GoTarget.companion_tags()`.
It now asks the method.

### Detection — declared files feed the base default

`detection_files` was already declared per target but most targets still
re-implemented `detect()` as a hand-written `os.path.exists` over the same
filenames. `BaseTarget.detect()` now consumes `detection_files`; targets that
inspect file *content* to decide (flutter vs dart, native-android, native-ios,
swift-apple, spec, pgdesign, plain) keep their overrides.

`PlainTarget`'s "some other target owns this directory" manifest list was
hand-maintained and had drifted (it still names `Cargo.toml` for a target that
no longer exists). It is now derived from every registered target's
`detection_files` plus a small declared extras set for manifests that belong to
no current target.

## Capability derivation

The `capabilities: frozenset[str]` attribute is deleted. Each axis it encoded is
now answered by asking the target directly:

| Former capability | Replacement | Kind |
| --- | --- | --- |
| `publication_probe` | `supports_publication_probe` | introspection: does the class override `BaseTarget.publication_probe`? |
| `read_name` | `supports_read_name` | introspection: overrides `read_name`? |
| `read_metadata` | `supports_read_metadata` | introspection: overrides `read_metadata`? |
| `dev_install` | `supports_dev_install` | behavioural: does `dev_install_command()` yield a spec for any mode? |
| `ci_templates` | `provides_ci_templates` | behavioural: does the target's template directory ship `ci.yml.tpl`? |
| `publish`, `build_assets` | *(nothing)* | no target ever declared them and no code ever read them; the pipelines doc claimed otherwise and has been corrected |

The declared frozensets had drifted from the code in two places. Eight targets
carried a `read_metadata` override that only restated the empty default with an
ecosystem-specific docstring; under the derivation each would have claimed
metadata support for a target that reads none, so the no-op overrides are
deleted and not overriding is the honest statement. And `swift-apple` inherits a
`dev_install_command` it did not declare (its specs resolve to none, so the
derived answer is still "no").

`getattr(target, "capabilities", default)` and `.get(axis, default)` are banned
in the replacement. Four reader sites decide whether to run a publication probe:

| Site | What a silent default would cost |
| --- | --- |
| `rlsbl/pipelines/base.py` (pre-publish probe) | publishing over a version the registry already serves |
| `rlsbl/commands/release/execute.py` (post-publish verification) | a probeable target dropped out of the verification set without a word |
| `rlsbl/evidence_gate.py` (registry evidence source) | "no evidence" for a target that can answer |
| `rlsbl/commands/yank.py` (yank's publication status) | a published version treated as unprobeable |

The first two used exactly the banned `getattr(..., frozenset())` form before
this change. `tests/test_target_capability_derivation.py` pins all four: by AST
(no defaulted read of the axis remains in any of them) and by behaviour for the
two that can be exercised directly.

## Known blind spots of the sweep and the guard

- **Indirection.** A name bound to a module constant and compared later
  (`NPM = "npm"` … `if n == NPM`) is invisible to an AST scan of literals.
- **Computed names.** f-strings, `str.format`, and names arriving from config
  are invisible.
- **Non-Python surfaces.** JSON config schemas, YAML workflow templates and the
  scaffold templates are not scanned.
- **Coincidental collisions.** `"flutter"` as a *pubspec.yaml key* in
  `targets/dart.py` and `targets/flutter.py` reads identically to the target
  name and is reported; it is a content test, not a feature-support conditional.
  Similar collisions are possible for any short target name.
- **Sparse displays.** A dict or set display whose target-name elements fall
  below the density threshold is not reported as a hand-listed aggregate.
