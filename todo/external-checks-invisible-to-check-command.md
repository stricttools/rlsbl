# External checks never run via `rlsbl check --tag <tag>` / `--all`

## Context

Config-declared external checks (`external_checks` in a project's
`.rlsbl/config.json`) are registered on the strictcli app by
`_register_external_checks_from_config`, which is called from two places:

1. The release preflight (`commands/release/__init__.py`) calls it explicitly
   BEFORE `app.run_checks(tag_expr="preflight")` — external checks run there.
2. The check-context factory (`_check_context_factory` in `rlsbl/__init__.py`)
   calls it when the context is created.

## Problem

strictcli's auto-generated `check` command handler resolves the selected check
set (`_filter_checks(app._check_defs, ...)`) BEFORE it invokes
`app._check_context_factory()`. Registration path 2 therefore happens after
selection: external checks are silently absent from every
`rlsbl check --tag <tag>` and `rlsbl check --all` run — not even shown as
SKIP. A consumer wiring e.g. a `preflight`-tagged external check cannot
exercise it with `rlsbl check --tag preflight`; it only ever runs inside
`release run`. This is silent degradation: the check system reports PASS/green
while a declared check was never considered.

Reproduce: declare any valid external check with `tag: "preflight"`, run
`rlsbl check --tag preflight`, observe the check's name never appears in the
output.

## Secondary observation

`validate_external_checks` eagerly validates the command binary by taking
`command.split()[0]` and requiring it on PATH, while execution uses
`subprocess.run(shell=True)`. A shell-legal `VAR=1 cmd args` command passes at
runtime but fails registration ("command binary not found on PATH: VAR=1") —
and because registration is all-or-nothing, ONE such entry unregisters ALL
external checks with only a stderr warning (see also: the warning-and-continue
behavior in `_register_external_checks_from_config` contradicts the hard-error
philosophy). Consumers must spell it `env VAR=1 cmd args`. Either support the
env-prefix form in validation or document/hard-error it clearly at scaffold
time; and consider making a config error a hard error instead of a warning.

## Solutions

1. **Register external checks before selection in the check command path.**
   Call `_register_external_checks_from_config` eagerly at context-module
   import / app startup (config is already loadable there), or have strictcli
   invoke the context factory before `_filter_checks`. Pros: external checks
   become first-class in `check --tag`/`--all`/`--list`; matches the "one
   source of truth" story. Cons: strictcli-side change (factory-before-filter)
   touches the shared check runner; rlsbl-side change (eager registration)
   must handle running outside a project dir gracefully.
2. **Hard error instead:** if `external_checks` exists in config and the tag
   filter would have matched one, refuse `rlsbl check` with a message naming
   the limitation. Pros: no silent gap. Cons: keeps the feature gap.

Option 1 (eager registration in rlsbl, before the check command's selection)
is the correct fix; option 2 is a stopgap.

## Affected files

- `rlsbl/__init__.py` (`_check_context_factory`, `_register_external_checks_from_config`, app wiring)
- `rlsbl/external_checks.py` (`validate_external_checks` binary check; warning-vs-error policy)
- strictcli `App._register_check_command` (`_filter_checks` before `_check_context_factory()`) if fixed upstream
- Red-green: a test that declares an external check and asserts it appears in `check --tag <tag>` output

## Effort

Small-medium: the registration-ordering fix is a few lines plus tests; the
validation-policy decision (env prefix, warning vs hard error) is a design
call for the maintainer.
