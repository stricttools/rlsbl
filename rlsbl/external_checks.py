"""External check providers: config-declared subprocess checks.

Projects declare external checks in ``.rlsbl/config.json`` under the
``external_checks`` key.  Each entry specifies a name, command, tag,
optional depends_on, and optional cwd.  During ``rlsbl check --tag``
or the release preflight, external checks run as subprocess calls.
Non-zero exit = hard fail.  No bypass mechanism.

External checks are registered dynamically on the strictcli app when
a check context is created (they cannot be in checks.toml because
they are per-project, not per-tool).
"""

import os
import re
import shutil
import subprocess
import sys

from strictcli import CheckResult


# Leading environment-assignment pattern (``VAR=value``).  Shell-legal as a
# command prefix, but our binary-existence check would misread it as the
# command name.  We require the explicit ``env`` prefix form instead.
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


class ExternalCheckError(Exception):
    """Raised when external check config is invalid."""


def validate_external_checks(config, *, project_root=None):
    """Validate the ``external_checks`` section of a project config.

    Raises :class:`ExternalCheckError` if:
    - ``external_checks`` is present but not a list
    - An entry is missing ``name``, ``command``, or ``tag``
    - ``name`` is not a non-empty string
    - ``command`` is not a non-empty string
    - ``tag`` is not a non-empty string
    - ``depends_on`` is present but not a list of strings
    - ``cwd`` is present but not a string
    - The command binary (first token) is not found on PATH or at an
      absolute path -- checked eagerly so failures surface at registration
      time, not at run time.

    Returns the validated list of external check dicts, or an empty list
    if the key is absent.
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

        # Required fields
        for key in ("name", "command", "tag"):
            if key not in entry:
                raise ExternalCheckError(
                    f"external_checks[{i}] is missing required key '{key}'"
                )
            val = entry[key]
            if not isinstance(val, str) or not val.strip():
                raise ExternalCheckError(
                    f"external_checks[{i}].{key} must be a non-empty string"
                )

        name = entry["name"]
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

        # Eagerly validate command binary existence.
        # The command string is split by shell rules; the first token must
        # be findable.
        command = entry["command"]
        first_token = command.split()[0]
        # Reject leading environment assignments (``VAR=value cmd ...``).
        # They are shell-legal but would be misinterpreted as the binary
        # name; the ``env`` prefix form is the supported way to set env vars.
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

    return ext_checks


def _make_external_check_fn(command, cwd, name):
    """Build a check function that runs *command* as a subprocess.

    The returned function has the signature ``fn(ctx) -> CheckResult``
    expected by the strictcli check system.
    """
    def _run_external_check(ctx):
        check_cwd = cwd
        if check_cwd is None:
            check_cwd = str(ctx.project_root)
        elif not os.path.isabs(check_cwd):
            check_cwd = os.path.join(str(ctx.project_root), check_cwd)

        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=check_cwd,
                capture_output=True,
                text=True,
                timeout=300,
            )
        except subprocess.TimeoutExpired:
            return CheckResult(
                "fail",
                f"external check '{name}' timed out after 300s",
            )
        except OSError as exc:
            return CheckResult(
                "fail",
                f"external check '{name}' failed to execute: {exc}",
            )

        if result.returncode == 0:
            # Collect any stdout as the message (truncated)
            stdout = (result.stdout or "").strip()
            msg = stdout[:200] if stdout else "passed"
            return CheckResult("pass", msg)

        # Non-zero exit: hard fail
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        output = stderr or stdout or f"exit code {result.returncode}"
        # Include details lines for diagnostic output
        details = []
        if stdout:
            details.extend(stdout.splitlines()[:20])
        if stderr:
            details.extend(stderr.splitlines()[:20])
        return CheckResult(
            "fail",
            f"external check '{name}' failed (exit {result.returncode}): "
            + output.splitlines()[0][:200],
            details=details,
        )

    return _run_external_check


def register_external_checks(app, config):
    """Register external checks from *config* on *app*.

    Injects ``_CheckDef`` objects directly into ``app._check_defs`` with
    their ``impl`` already set, bypassing the checks.toml double-entry
    requirement (external checks are config-declared, not TOML-declared).

    Raises :class:`ExternalCheckError` on invalid config.
    """
    from strictcli import _CheckDef  # internal but stable dataclass

    ext_checks = validate_external_checks(config)
    if not ext_checks:
        return

    for entry in ext_checks:
        name = entry["name"]
        tag = entry["tag"]
        command = entry["command"]
        depends_on = entry.get("depends_on", [])
        cwd = entry.get("cwd")

        # Skip if already registered (idempotent for repeated context
        # creation within the same process)
        if name in app._check_defs:
            continue

        check_fn = _make_external_check_fn(command, cwd, name)

        check_def = _CheckDef(
            name=name,
            tags=[tag],
            severity="error",
            fast=False,
            pure=False,
            needs_network=False,
            depends_on=depends_on,
            scope="",
            impl=check_fn,
        )
        app._check_defs[name] = check_def


def run_external_preflight_checks(app, ctx, config, *, tag_expr="preflight"):
    """Run ONLY the config-declared external checks matching *tag_expr*.

    Used when the pre-release hook is customized: built-in preflight checks
    (test-suite, lint, maven-central-metadata) are the hook's responsibility
    and must be skipped, but config-declared external checks must still run.

    Selection mechanism: each external check is selected by its exact name
    intersected with *tag_expr* (strictcli's ``run_checks`` ANDs ``name_glob``
    with ``tag_expr``).  This runs exactly the config-declared external checks
    that carry the preflight tag and never selects a built-in check.

    Caveat: if a config-declared external check declares ``depends_on`` on a
    built-in preflight check, strictcli's dependency resolver pulls that
    built-in into the run.  Config-declared externals should not depend on
    built-ins when the hook is expected to own testing/linting.

    ``register_external_checks`` (via ``_register_external_checks_from_config``)
    must have already registered the checks on *app*.

    Returns ``(results, exit_code)`` where ``exit_code`` is non-zero if any
    external check failed.
    """
    ext_checks = validate_external_checks(config)
    all_results = []
    seen = set()
    worst_exit = 0
    for entry in ext_checks:
        name = entry["name"]
        if name in seen:
            continue
        results, exit_code = app.run_checks(
            ctx, tag_expr=tag_expr, name_glob=name,
        )
        for r in results:
            if r.name not in seen:
                all_results.append(r)
                seen.add(r.name)
        if exit_code != 0:
            worst_exit = exit_code
    return all_results, worst_exit
