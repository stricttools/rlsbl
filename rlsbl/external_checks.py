"""External check providers: config-declared subprocess checks.

Projects declare external checks in ``.rlsbl/config.json`` under the
``external_checks`` key.  Every entry declares a ``kind``:

- ``"freeform"``: an opaque shell ``command``.  rlsbl does not understand
  its scope -- the command runs verbatim through a shell.
- ``"structured"``: a known ``tool`` (mypy, ruff-check, ruff-format) plus an
  explicit ``paths`` list.  rlsbl composes the argv itself (no shell), routes
  the timeout through the configured budget, and emits an additional
  competing-scope guard check that hard-errors if the tool's own config file
  carries scope that would silently override or narrow ``paths``.

The mandatory ``kind`` marker exists so that unmanaged scope (a freeform shell
command whose target directories rlsbl cannot see) is always a visible,
deliberate declaration rather than an accident.

External checks are registered via a strictcli check provider
(``app.register_check_provider``).  The provider reads the project config at
materialization time (keyed on cwd) and returns a list of check specs.
strictcli handles memoization and re-materialization when the cwd changes.
"""

import configparser
import os
import re
import shutil
import subprocess
import tomllib

from strictcli import error_check_spec

from .utils import detect_uv_workspace_root, get_check_timeout, get_last_version_tag


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

# Structured-check adapters: tool name -> (binary, subcommand tokens).  The
# composed argv is ``uv run [group flags] <binary> <subcommand...> <paths...>``.
_STRUCTURED_ADAPTERS = {
    "mypy": ("mypy", []),
    "ruff-check": ("ruff", ["check"]),
    "ruff-format": ("ruff", ["format", "--check"]),
}

# Keys valid on every entry regardless of kind.
_COMMON_KEYS = {"name", "tag", "kind", "depends_on", "cwd"}
# Kind-specific additional keys.
_STRUCTURED_KEYS = {"tool", "paths"}
_FREEFORM_KEYS = {"command"}


class ExternalCheckError(Exception):
    """Raised when external check config is invalid."""


def _guard_name(entry_name):
    """Name of the competing-scope guard emitted for a structured entry."""
    return f"{entry_name}-scope-guard"


def validate_external_checks(config, *, project_root=None):
    """Validate the ``external_checks`` section of a project config.

    Every entry must declare ``kind``: ``"structured"`` or ``"freeform"``.

    Common required keys: ``name``, ``tag``, ``kind``.
    Structured entries additionally require ``tool`` (one of mypy, ruff-check,
    ruff-format) and ``paths`` (a non-empty list of strings); ``command`` is
    forbidden.  Freeform entries require ``command``; ``tool``/``paths`` are
    forbidden.  ``depends_on`` and ``cwd`` are optional on both kinds.

    Any unrecognized key on an entry is a hard error.

    Binary-existence is validated eagerly (at registration time):
    - freeform: the command's first token must resolve on PATH / at an
      absolute path.
    - structured: only ``uv`` must resolve on PATH (the tools live in the
      project venv, invoked via ``uv run``).

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
        if kind not in ("structured", "freeform"):
            raise ExternalCheckError(
                f"external_checks[{i}].kind '{kind}' is invalid: must be "
                f"'structured' (tool + paths, rlsbl-composed argv) or "
                f"'freeform' (opaque shell command)"
            )

        # Reject unknown keys -- these pass silently otherwise and hide typos
        # or cross-kind misuse (e.g. 'command' on a structured entry).
        allowed = _COMMON_KEYS | (
            _STRUCTURED_KEYS if kind == "structured" else _FREEFORM_KEYS
        )
        unknown = set(entry) - allowed
        if unknown:
            raise ExternalCheckError(
                f"external_checks[{i}] ('{entry['name']}', kind={kind}) has "
                f"unknown key(s): {', '.join(sorted(unknown))}. "
                f"Allowed keys: {', '.join(sorted(allowed))}"
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

        if kind == "structured":
            _validate_structured_entry(i, entry)
        else:
            _validate_freeform_entry(i, entry)

    return ext_checks


def _validate_structured_entry(i, entry):
    """Validate the tool/paths fields of a structured entry and probe ``uv``."""
    name = entry["name"]
    tool = entry.get("tool")
    if not isinstance(tool, str) or not tool.strip():
        raise ExternalCheckError(
            f"external_checks[{i}] ('{name}'): structured entries require a "
            f"non-empty 'tool' string"
        )
    if tool not in _STRUCTURED_ADAPTERS:
        valid = ", ".join(sorted(_STRUCTURED_ADAPTERS))
        raise ExternalCheckError(
            f"external_checks[{i}] ('{name}'): unknown tool '{tool}'. "
            f"Valid structured tools: {valid}. For anything else, use a "
            f"freeform entry (kind='freeform', command=...)."
        )

    paths = entry.get("paths")
    if not isinstance(paths, list) or not paths:
        raise ExternalCheckError(
            f"external_checks[{i}] ('{name}'): structured entries require a "
            f"non-empty 'paths' list"
        )
    for j, p in enumerate(paths):
        if not isinstance(p, str) or not p.strip():
            raise ExternalCheckError(
                f"external_checks[{i}] ('{name}').paths[{j}] must be a "
                f"non-empty string"
            )

    # Structured checks invoke tools via ``uv run``; only uv must be present.
    if shutil.which("uv") is None:
        raise ExternalCheckError(
            f"external_checks[{i}] ('{name}'): 'uv' not found on PATH -- "
            f"structured checks run tools via 'uv run'"
        )


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
# uv-run group/extra resolution (generalized from testing.py's pytest probe)
# ---------------------------------------------------------------------------


def _probe_tool_location(project_dir, tool_binary):
    """Detect where *tool_binary* is declared in a project's pyproject.toml.

    Generalizes the pytest-specific probe in ``testing.py`` to any tool.
    Checks, in order:
    1. ``[dependency-groups].*`` -- any group declaring the tool
    2. ``[project.optional-dependencies].*`` -- any extra declaring the tool
    3. ``[tool.uv].dev-dependencies`` -- uv legacy dev deps

    Returns ``(source_type, name)`` on match, else ``None``.  ``source_type``
    is one of ``"dependency-group"``, ``"optional-dep"``, ``"uv-dev"``.
    """
    pyproject_path = os.path.join(project_dir, "pyproject.toml")
    if not os.path.isfile(pyproject_path):
        return None
    try:
        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None

    # A requirement string like "ruff>=0.15.20" or "mypy[extra]" -- the package
    # name is the leading run of name characters before any version/extra spec.
    name_re = re.compile(rf"^{re.escape(tool_binary)}(\[|[<>=!~; ]|$)")

    def _declares(entries):
        for entry in entries:
            if isinstance(entry, str) and name_re.match(entry.strip()):
                return True
        return False

    dep_groups = data.get("dependency-groups", {})
    for group_name, entries in dep_groups.items():
        if _declares(entries):
            return ("dependency-group", group_name)

    opt_deps = data.get("project", {}).get("optional-dependencies", {})
    for extra_name, entries in opt_deps.items():
        if _declares(entries):
            return ("optional-dep", extra_name)

    uv_dev_deps = data.get("tool", {}).get("uv", {}).get("dev-dependencies", [])
    if _declares(uv_dev_deps):
        return ("uv-dev", "dev")

    return None


def _resolve_tool_group_flags(project_dir, tool_binary):
    """Return the ``uv run`` group/extra flags needed to reach *tool_binary*.

    Degrades to ``[]`` (plain ``uv run``) when the tool lives in the default
    ``dev`` group, in uv's legacy dev-dependencies, in a uv workspace venv, or
    cannot be located -- so the common case reproduces bare ``uv run <tool>``.
    Non-default dependency groups yield ``["--group", name]`` and optional
    extras yield ``["--extra", name]``.
    """
    if detect_uv_workspace_root(project_dir) is not None:
        return []
    location = _probe_tool_location(project_dir, tool_binary)
    if location is None:
        return []
    source_type, name = location
    if source_type == "dependency-group":
        return [] if name == "dev" else ["--group", name]
    if source_type == "optional-dep":
        return ["--extra", name]
    return []  # uv-dev: synced by default


def _compose_structured_argv(tool, paths, project_dir):
    """Compose the shell-free argv for a structured tool invocation."""
    binary, subcommand = _STRUCTURED_ADAPTERS[tool]
    group_flags = _resolve_tool_group_flags(project_dir, binary)
    return ["uv", "run", *group_flags, binary, *subcommand, *paths]


# ---------------------------------------------------------------------------
# Check functions
# ---------------------------------------------------------------------------


#: Attribute the resolved release-context env is memoized on, so N external
#: checks in one run mean ONE `git describe`, not N.
_ENV_CACHE_ATTR = "_rlsbl_external_check_env"


def _release_context_env(ctx):
    """Return the subprocess env for an external check: os.environ + RLSBL_*.

    Injected (see docs/configuration.md for the availability matrix):

    - ``RLSBL_PROJECT_ROOT`` -- the resolved project root. An entry with a
      ``cwd`` override otherwise has no way to find it.
    - ``RLSBL_LAST_TAG`` -- the project's last release tag, resolved through
      the same per-project tag glob the changelog layer uses (so it is
      monorepo-correct). The EMPTY STRING when no tag exists, so a check can
      tell "no baseline yet" from "not injected".
    - ``RLSBL_UNRELEASED_RANGE`` -- ``<last_tag>..HEAD``, or ``HEAD`` on a
      first release.

    Computed once per check run and memoized on the context object.
    """
    cached = getattr(ctx, _ENV_CACHE_ATTR, None)
    if cached is not None:
        return cached

    from .changelog.resolve import _unreleased_range
    from .checks._common import _resolve_tag_glob

    project_root = str(ctx.project_root)
    tag_glob = _resolve_tag_glob(ctx)
    try:
        last_tag = get_last_version_tag(tag_glob, cwd=project_root)
    except Exception:
        # A repo we cannot interrogate (shallow clone, not a git repo) still
        # gets the project root; the tag pair is simply absent-as-empty.
        last_tag = None
        unreleased_range = "HEAD"
    else:
        unreleased_range = _unreleased_range(tag_glob, cwd=project_root)

    env = dict(os.environ)
    env["RLSBL_PROJECT_ROOT"] = project_root
    env["RLSBL_LAST_TAG"] = last_tag or ""
    env["RLSBL_UNRELEASED_RANGE"] = unreleased_range
    try:
        setattr(ctx, _ENV_CACHE_ATTR, env)
    except AttributeError:
        pass  # a slotted/frozen context simply recomputes
    return env


def _resolve_cwd(ctx, cwd):
    """Resolve an entry's cwd against the project root."""
    if cwd is None:
        return str(ctx.project_root)
    if not os.path.isabs(cwd):
        return os.path.join(str(ctx.project_root), cwd)
    return cwd


def _report_subprocess_result(reporter, result, name):
    """Turn a completed subprocess into a pass/fail check result."""
    if result.returncode == 0:
        stdout = (result.stdout or "").strip()
        msg = stdout[:200] if stdout else "passed"
        return reporter.passed(msg)

    stderr = (result.stderr or "").strip()
    stdout = (result.stdout or "").strip()
    output = stderr or stdout or f"exit code {result.returncode}"
    if stdout:
        for line in stdout.splitlines()[:20]:
            reporter.error(line)
    if stderr:
        for line in stderr.splitlines()[:20]:
            reporter.error(line)
    if not stdout and not stderr:
        reporter.error(f"exit code {result.returncode}")
    return reporter.found(
        f"external check '{name}' failed (exit {result.returncode}): "
        + output.splitlines()[0][:200]
    )


def _resolve_check_budget(ctx):
    """Resolve the subprocess budget for an external check at RUN time.

    Read from the live ``ctx.config`` rather than bound when the check spec is
    built.  The provider materializes specs from a fresh on-disk config read,
    so a budget bound there could never see ``--check-timeout`` (which
    ``apply_timeout_overrides`` writes into the release's in-memory config).
    Resolving here is also what every built-in check does
    (``get_check_timeout(ctx.config)``), so one precedence chain -- flag >
    ``check_timeout`` config key > shipped default -- governs all of them.
    """
    return get_check_timeout(getattr(ctx, "config", None))


def _make_external_check_fn(command, cwd, name):
    """Build a check function that runs a freeform *command* through a shell.

    The returned function has the ``(ctx, reporter)`` signature expected by
    strictcli's check system.  The timeout is resolved per run from the live
    context (see :func:`_resolve_check_budget`).  The subprocess env carries
    the release context (see :func:`_release_context_env`).
    """
    def _run_external_check(ctx, reporter):
        check_cwd = _resolve_cwd(ctx, cwd)
        budget = _resolve_check_budget(ctx)
        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=check_cwd,
                capture_output=True,
                text=True,
                timeout=budget,
                env=_release_context_env(ctx),
            )
        except subprocess.TimeoutExpired:
            msg = f"external check '{name}' timed out after {budget}s"
            reporter.error(msg)
            return reporter.found(msg)
        except OSError as exc:
            reporter.error(f"external check '{name}' failed to execute: {exc}")
            return reporter.found(f"external check '{name}' failed to execute: {exc}")

        return _report_subprocess_result(reporter, result, name)

    return _run_external_check


def _make_structured_check_fn(tool, paths, cwd, name):
    """Build a check function that runs a structured tool via composed argv.

    No shell: the argv is a list, composed as ``uv run [group flags] <binary>
    <subcommand...> <paths...>``.  The timeout is resolved per run from the
    live context (see :func:`_resolve_check_budget`), and the subprocess env
    carries the release context (see :func:`_release_context_env`).
    """
    def _run_structured_check(ctx, reporter):
        check_cwd = _resolve_cwd(ctx, cwd)
        budget = _resolve_check_budget(ctx)
        argv = _compose_structured_argv(tool, paths, check_cwd)
        try:
            result = subprocess.run(
                argv,
                cwd=check_cwd,
                capture_output=True,
                text=True,
                timeout=budget,
                env=_release_context_env(ctx),
            )
        except subprocess.TimeoutExpired:
            msg = f"external check '{name}' timed out after {budget}s"
            reporter.error(msg)
            return reporter.found(msg)
        except OSError as exc:
            reporter.error(f"external check '{name}' failed to execute: {exc}")
            return reporter.found(f"external check '{name}' failed to execute: {exc}")

        return _report_subprocess_result(reporter, result, name)

    return _run_structured_check


# ---------------------------------------------------------------------------
# Competing-scope guards (pure config checks emitted per structured entry)
# ---------------------------------------------------------------------------


def _load_toml(path):
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None


def _mypy_scope_conflicts(root):
    """Return a list of (source, key) where mypy config carries scope.

    mypy's ``files``/``packages``/``modules`` config keys are silently
    OVERRIDDEN by CLI paths -- a scope declared there is dead but misleading.
    Checks pyproject ``[tool.mypy]``, ``mypy.ini``, ``.mypy.ini`` and setup.cfg
    ``[mypy]``.
    """
    scope_keys = ("files", "packages", "modules")
    conflicts = []

    pyproject = _load_toml(os.path.join(root, "pyproject.toml"))
    if pyproject is not None:
        mypy_tbl = pyproject.get("tool", {}).get("mypy", {})
        for key in scope_keys:
            if key in mypy_tbl:
                conflicts.append(("pyproject.toml [tool.mypy]", key))

    for ini_name in ("mypy.ini", ".mypy.ini"):
        ini_path = os.path.join(root, ini_name)
        if os.path.isfile(ini_path):
            parser = configparser.ConfigParser()
            try:
                parser.read(ini_path)
            except configparser.Error:
                continue
            if parser.has_section("mypy"):
                for key in scope_keys:
                    if parser.has_option("mypy", key):
                        conflicts.append((f"{ini_name} [mypy]", key))

    setup_cfg = os.path.join(root, "setup.cfg")
    if os.path.isfile(setup_cfg):
        parser = configparser.ConfigParser()
        try:
            parser.read(setup_cfg)
        except configparser.Error:
            parser = None
        if parser is not None and parser.has_section("mypy"):
            for key in scope_keys:
                if parser.has_option("mypy", key):
                    conflicts.append(("setup.cfg [mypy]", key))

    return conflicts


def _ruff_scope_conflicts(root):
    """Return a list of (source, key) where ruff config narrows scope.

    ruff's ``include``/``extend-include`` config keys silently NARROW the
    directories passed explicitly on the CLI (confirmed on ruff 0.15.20).
    ``exclude``/``extend-exclude``/``force-exclude`` are exempt: they are
    bypassed by explicit paths (loud over-inclusion, not silent under-scoping).
    Checks pyproject ``[tool.ruff]``, ``ruff.toml`` and ``.ruff.toml``.
    """
    scope_keys = ("include", "extend-include")
    conflicts = []

    pyproject = _load_toml(os.path.join(root, "pyproject.toml"))
    if pyproject is not None:
        ruff_tbl = pyproject.get("tool", {}).get("ruff", {})
        for key in scope_keys:
            if key in ruff_tbl:
                conflicts.append(("pyproject.toml [tool.ruff]", key))

    for toml_name in ("ruff.toml", ".ruff.toml"):
        toml_path = os.path.join(root, toml_name)
        if os.path.isfile(toml_path):
            data = _load_toml(toml_path)
            if data is not None:
                for key in scope_keys:
                    if key in data:
                        conflicts.append((toml_name, key))

    return conflicts


def _make_scope_guard_fn(tool, cwd, name):
    """Build the pure competing-scope guard check function for a tool."""
    binary, _ = _STRUCTURED_ADAPTERS[tool]

    def _run_scope_guard(ctx, reporter):
        root = _resolve_cwd(ctx, cwd)
        if binary == "mypy":
            conflicts = _mypy_scope_conflicts(root)
            explanation = (
                "mypy CLI paths silently OVERRIDE config scope; scope must "
                "live only in the structured external_checks entry's 'paths'"
            )
        else:  # ruff
            conflicts = _ruff_scope_conflicts(root)
            explanation = (
                "ruff config include/extend-include silently NARROWS the "
                "explicitly-passed directories; scope must live only in the "
                "structured external_checks entry's 'paths'"
            )
        if not conflicts:
            return reporter.passed("no competing scope in tool config")
        for source, key in conflicts:
            reporter.error(f"{source}: '{key}' competes with structured scope")
        return reporter.found(
            f"external check '{name}': competing scope in tool config "
            f"({explanation})"
        )

    return _run_scope_guard


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


def _entry_specs(entry):
    """Build the check spec(s) for a single validated external-check entry.

    Freeform entries yield one impure subprocess check.  Structured entries
    yield the impure tool check plus a pure/fast competing-scope guard check.

    No timeout is bound here: the subprocess budget is resolved per run from
    the live check context (see :func:`_resolve_check_budget`).
    """
    name = entry["name"]
    tag = entry["tag"]
    depends_on = entry.get("depends_on", [])
    cwd = entry.get("cwd")
    specs = []

    if entry["kind"] == "structured":
        tool = entry["tool"]
        paths = entry["paths"]
        specs.append(error_check_spec(
            name=name,
            tags=[tag],
            fast=False,
            pure=False,
            needs_network=False,
            depends_on=depends_on,
            impl=_make_structured_check_fn(tool, paths, cwd, name),
        ))
        # Competing-scope guard: pure + fast, no depends_on so it partitions
        # cleanly into the pure set even when the tool check depends on impure
        # built-ins.
        specs.append(error_check_spec(
            name=_guard_name(name),
            tags=[tag],
            fast=True,
            pure=True,
            needs_network=False,
            depends_on=[],
            impl=_make_scope_guard_fn(tool, cwd, name),
        ))
    else:
        command = entry["command"]
        specs.append(error_check_spec(
            name=name,
            tags=[tag],
            fast=False,
            pure=False,
            needs_network=False,
            depends_on=depends_on,
            impl=_make_external_check_fn(command, cwd, name),
        ))

    return specs


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


def run_external_preflight_checks(app, ctx, config, *, tag_expr="preflight"):
    """Run ONLY the config-declared external checks matching *tag_expr*.

    Used when the pre-release hook is customized: built-in preflight checks
    (test-suite, lint, maven-central-metadata) are the hook's responsibility
    and must be skipped, but config-declared external checks (and their
    structured scope guards) must still run.

    Selection mechanism: each external check is selected by its exact name
    intersected with *tag_expr* (strictcli's ``run_checks`` ANDs ``name_glob``
    with ``tag_expr``).  This runs exactly the config-declared external checks
    that carry the preflight tag and never selects a built-in check.

    The check provider must have already been registered on *app*
    (via ``app.register_check_provider``).

    Returns ``(results, exit_code)`` where ``exit_code`` is non-zero if any
    external check failed.
    """
    ext_checks = validate_external_checks(config)
    # Each structured entry also contributes a scope-guard check that must run.
    select_names = []
    for entry in ext_checks:
        select_names.append(entry["name"])
        if entry["kind"] == "structured":
            select_names.append(_guard_name(entry["name"]))

    all_results = []
    seen = set()
    worst_exit = 0
    for name in select_names:
        if name in seen:
            continue
        results, _impure_listed, exit_code = app.run_checks(
            ctx, tag_expr=tag_expr, name_glob=name,
        )
        for r in results:
            if r.name not in seen:
                all_results.append(r)
                seen.add(r.name)
        if exit_code != 0:
            worst_exit = exit_code
    return all_results, worst_exit
