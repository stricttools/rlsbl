"""External check providers: config-declared subprocess checks.

Projects declare external checks in ``.rlsbl/config.json`` under the
``external_checks`` key.  Every entry declares ``kind = "freeform"``: an
opaque shell ``command``.  rlsbl does not understand its scope -- the command
runs verbatim through a shell.

The mandatory ``kind`` marker exists so that unmanaged scope (a freeform shell
command whose target directories rlsbl cannot see) is always a visible,
deliberate declaration rather than an accident.

``kind = "structured"`` is RETIRED.  It named a known tool plus a path list
and had rlsbl compose the argv; that invocation shape is now three built-in
checks (``lint``, ``format``, ``type-check``) configured under the top-level
``checks`` key -- see :mod:`rlsbl.tool_checks`.  A config that still declares
it is a hard error naming the replacement.

External checks are registered via a strictcli check provider
(``app.register_check_provider``).  The provider reads the project config at
materialization time (keyed on cwd) and returns a list of check specs.
strictcli handles memoization and re-materialization when the cwd changes.
"""

import os
import re
import shutil
import subprocess

from strictcli import error_check_spec

from .tool_checks import (
    release_context_env,
    report_subprocess_result,
    resolve_check_budget,
    resolve_cwd,
)
from . import effects


# Leading environment-assignment pattern (``VAR=value``).  Shell-legal as a
# command prefix, but our binary-existence check would misread it as the
# command name.  We require the explicit ``env`` prefix form instead.
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# Valid external-check name charset.  Matches strictcli's own check-name
# pattern (``_IDENTIFIER_RE`` in strictcli, enforced on every checks.toml
# check name): lowercase letter, then lowercase letters / digits / hyphens.
# Critically, this charset excludes fnmatch metacharacters (``*?[]``); a name
# like ``test-*`` would otherwise glob-match a built-in check (e.g.
# ``test-suite``) in the name-selection path used by
# ``run_external_preflight_checks``.
_CHECK_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")

# The keys a freeform entry may carry.  There is one kind, so there is one set.
_ALLOWED_KEYS = {"name", "tag", "kind", "depends_on", "cwd", "command"}

# The retired structured tools, mapped to the built-in check that replaced
# each one.  Kept only to make the migration error say where to go.
_RETIRED_TOOL_REPLACEMENT = {
    "mypy": "type-check",
    "ruff-check": "lint",
    "ruff-format": "format",
}


class ExternalCheckError(Exception):
    """Raised when external check config is invalid."""


def _retired_structured_message(index, entry):
    """The hard error a retired ``kind = "structured"`` entry gets."""
    name = entry.get("name", "?")
    tool = entry.get("tool")
    paths = entry.get("paths")
    replacement = _RETIRED_TOOL_REPLACEMENT.get(tool)
    if replacement is None:
        target = "one of: lint, format, type-check"
        example = '"checks": {"lint": {"paths": ["src", "tests"]}}'
    else:
        target = f'"{replacement}"'
        rendered = ", ".join(f'"{p}"' for p in (paths or ["src", "tests"]))
        example = f'"checks": {{"{replacement}": {{"paths": [{rendered}]}}}}'
    return (
        f"external_checks[{index}] ('{name}'): kind \"structured\" was "
        f"removed. The path-list tool invocation is now a BUILT-IN check "
        f"configured under the top-level \"checks\" key. Move this entry to "
        f"{target} and delete it from external_checks:\n"
        f"  {example}\n"
        f"Tool mapping: mypy -> type-check, ruff-check -> lint, "
        f"ruff-format -> format. The competing-scope guard is now "
        f"<check>-scope-guard and is registered automatically."
    )


def validate_external_checks(config, *, project_root=None):
    """Validate the ``external_checks`` section of a project config.

    Every entry must declare ``kind = "freeform"`` plus ``name``, ``tag`` and
    ``command``; ``depends_on`` and ``cwd`` are optional.  Any unrecognized
    key is a hard error, and so is the retired ``kind = "structured"`` (the
    message names the built-in check that replaced it).

    Binary existence is validated eagerly, at registration time: the command's
    first token must resolve on PATH or at an absolute path.

    Returns the validated list of external check dicts, or an empty list if
    the key is absent.
    """
    ext_checks = config.get("external_checks")
    if ext_checks is None:
        return []
    if not isinstance(ext_checks, list):
        raise ExternalCheckError(
            f"external_checks must be a list, got {type(ext_checks).__name__}"
        )

    seen_names = set()
    for i, entry in enumerate(ext_checks):
        if not isinstance(entry, dict):
            raise ExternalCheckError(
                f"external_checks[{i}] must be a dict, "
                f"got {type(entry).__name__}"
            )

        # Common required scalars: name, tag, kind.
        for key in ("name", "tag", "kind"):
            if key not in entry:
                raise ExternalCheckError(
                    f"external_checks[{i}] is missing required key '{key}'"
                )
            val = entry[key]
            if not isinstance(val, str) or not val.strip():
                raise ExternalCheckError(
                    f"external_checks[{i}].{key} must be a non-empty string"
                )

        kind = entry["kind"]
        if kind == "structured":
            raise ExternalCheckError(_retired_structured_message(i, entry))
        if kind != "freeform":
            raise ExternalCheckError(
                f"external_checks[{i}].kind '{kind}' is invalid: the only "
                f"kind is 'freeform' (an opaque shell command). A tool run "
                f"over a declared path list is a built-in check now -- "
                f"configure lint / format / type-check under the top-level "
                f"\"checks\" key."
            )

        # Reject unknown keys -- these pass silently otherwise and hide typos.
        unknown = set(entry) - _ALLOWED_KEYS
        if unknown:
            raise ExternalCheckError(
                f"external_checks[{i}] ('{entry['name']}') has "
                f"unknown key(s): {', '.join(sorted(unknown))}. "
                f"Allowed keys: {', '.join(sorted(_ALLOWED_KEYS))}"
            )

        name = entry["name"]
        if not _CHECK_NAME_RE.match(name):
            raise ExternalCheckError(
                f"external_checks[{i}].name '{name}' is not a valid check name "
                f"(must match [a-z][a-z0-9-]*): lowercase letters, digits, and "
                f"hyphens only, starting with a letter. This charset excludes "
                f"glob metacharacters (*?[]), which would otherwise let a name "
                f"pattern-match a built-in check during name-based selection."
            )
        if name in seen_names:
            raise ExternalCheckError(
                f"external_checks: duplicate name '{name}'"
            )
        seen_names.add(name)

        # Optional: depends_on
        depends_on = entry.get("depends_on")
        if depends_on is not None:
            if not isinstance(depends_on, list):
                raise ExternalCheckError(
                    f"external_checks[{i}].depends_on must be a list, "
                    f"got {type(depends_on).__name__}"
                )
            for j, dep in enumerate(depends_on):
                if not isinstance(dep, str) or not dep.strip():
                    raise ExternalCheckError(
                        f"external_checks[{i}].depends_on[{j}] must be "
                        f"a non-empty string"
                    )

        # Optional: cwd
        cwd = entry.get("cwd")
        if cwd is not None and not isinstance(cwd, str):
            raise ExternalCheckError(
                f"external_checks[{i}].cwd must be a string, "
                f"got {type(cwd).__name__}"
            )

        _validate_freeform_entry(i, entry)

    return ext_checks


def _validate_freeform_entry(i, entry):
    """Validate the command field of a freeform entry and probe its binary."""
    name = entry["name"]
    command = entry.get("command")
    if not isinstance(command, str) or not command.strip():
        raise ExternalCheckError(
            f"external_checks[{i}] ('{name}'): freeform entries require a "
            f"non-empty 'command' string"
        )

    first_token = command.split()[0]
    if _ENV_ASSIGN_RE.match(first_token):
        raise ExternalCheckError(
            f"external_checks[{i}] ('{name}'): environment assignments in "
            f"external check commands must use the env prefix: "
            f"env VAR=1 cmd args"
        )
    binary = first_token
    if os.path.isabs(binary):
        if not os.path.isfile(binary):
            raise ExternalCheckError(
                f"external_checks[{i}] ('{name}'): "
                f"command binary not found: {binary}"
            )
    else:
        if shutil.which(binary) is None:
            raise ExternalCheckError(
                f"external_checks[{i}] ('{name}'): "
                f"command binary not found on PATH: {binary}"
            )


# ---------------------------------------------------------------------------
# Check functions
# ---------------------------------------------------------------------------


def _make_external_check_fn(command, cwd, name):
    """Build a check function that runs a freeform *command* through a shell.

    The returned function has the ``(ctx, reporter)`` signature expected by
    strictcli's check system.  The timeout is resolved per run from the live
    context (see :func:`_resolve_check_budget`).  The subprocess env carries
    the release context (see :func:`_release_context_env`).
    """
    def _run_external_check(ctx, reporter):
        check_cwd = resolve_cwd(ctx, cwd)
        budget = resolve_check_budget(ctx)
        try:
            result = effects.run(
                command,
                shell=True,
                cwd=check_cwd,
                capture_output=True,
                text=True,
                timeout=budget,
                env=release_context_env(ctx),
            )
        except subprocess.TimeoutExpired:
            msg = f"external check '{name}' timed out after {budget}s"
            reporter.error(msg)
            return reporter.found(msg)
        except OSError as exc:
            reporter.error(f"external check '{name}' failed to execute: {exc}")
            return reporter.found(f"external check '{name}' failed to execute: {exc}")

        return report_subprocess_result(reporter, result, name)

    return _run_external_check


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


def _entry_specs(entry):
    """Build the check spec for a single validated external-check entry.

    One impure subprocess check per entry.  No timeout is bound here: the
    budget is resolved per run from the live check context (see
    :func:`resolve_check_budget`).
    """
    return [error_check_spec(
        name=entry["name"],
        tags=[entry["tag"]],
        fast=False,
        pure=False,
        needs_network=False,
        depends_on=entry.get("depends_on", []),
        impl=_make_external_check_fn(
            entry["command"], entry.get("cwd"), entry["name"],
        ),
    )]


def make_external_check_provider(config_reader):
    """Build a check provider that reads external checks from config.

    ``config_reader`` is a callable that returns the project config dict
    for the current working directory.  The provider is called lazily by
    strictcli at materialization time (memoized by cwd).

    Returns a provider function suitable for ``app.register_check_provider()``.
    """
    def _provider():
        try:
            config = config_reader()
        except Exception:
            # Config unreadable (e.g. no .rlsbl/ in cwd) -> no external checks
            return []

        try:
            ext_checks = validate_external_checks(config)
        except ExternalCheckError as exc:
            # Surface config errors as a hard error during materialization
            raise ValueError(
                f"external checks config error: {exc}"
            ) from exc

        specs = []
        for entry in ext_checks:
            specs.extend(_entry_specs(entry))
        return specs

    return _provider


def run_external_preflight_checks(app, ctx, config, *, tag_expr="preflight",
                                  pure_only=False):
    """Run ONLY the config-declared external checks matching *tag_expr*.

    Used when the pre-release hook is customized: built-in preflight checks
    (test-suite, lint, maven-central-metadata) are the hook's responsibility
    and must be skipped, but config-declared external checks must still run.

    Selection mechanism: each external check is selected by its exact name
    intersected with *tag_expr* (strictcli's ``run_checks`` ANDs ``name_glob``
    with ``tag_expr``).  This runs exactly the config-declared external checks
    that carry the preflight tag and never selects a built-in check.

    ``pure_only`` is the rehearsal's partition, and it is what makes this
    entry point usable under ``--dry-run``: pure checks EXECUTE and report
    real findings, while impure ones are listed rather than run.  The
    non-customized-hook branch has always previewed that way; passing this
    through is what stops the customized-hook branch from being the one place
    a preview silently skips a check it could have run.

    The check provider must have already been registered on *app*
    (via ``app.register_check_provider``).

    Returns ``(results, impure_listed, exit_code)``: the executed checks, the
    names withheld by the purity partition (empty unless *pure_only*), and a
    non-zero exit code if any executed check failed.
    """
    select_names = [
        entry["name"] for entry in validate_external_checks(config)
    ]

    all_results = []
    all_listed = []
    seen = set()
    worst_exit = 0
    for name in select_names:
        if name in seen:
            continue
        results, impure_listed, exit_code = app.run_checks(
            ctx, tag_expr=tag_expr, name_glob=name, pure_only=pure_only,
        )
        for r in results:
            if r.name not in seen:
                all_results.append(r)
                seen.add(r.name)
        for listed in impure_listed:
            if listed not in seen:
                all_listed.append(listed)
                seen.add(listed)
        if exit_code != 0:
            worst_exit = exit_code
    return all_results, all_listed, worst_exit
