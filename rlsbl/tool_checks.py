"""How rlsbl invokes a checking tool, and the three built-ins that do it.

Three built-in checks -- ``lint``, ``format`` and ``type-check`` -- run a
Python tool over a project-declared path list, through the project's own
environment.  They are configured in ``.rlsbl/config.json``::

    "checks": {
      "lint":       {"paths": ["mypackage", "tests", "scripts", "docs"]},
      "format":     {"paths": ["mypackage", "tests", "scripts", "docs"]},
      "type-check": {"paths": ["mypackage", "tests", "docs"]}
    }

A check with no entry skips.  ``paths`` is required when an entry is present;
``cwd`` is optional and resolves against the project root.

Why the paths are declared rather than inferred
-----------------------------------------------

Both tools read scope from their own config files, and both do it in a way
that silently disagrees with an explicit CLI path list:

* mypy's ``files`` / ``packages`` / ``modules`` are OVERRIDDEN by CLI paths --
  a scope declared there is dead but reads as authoritative.
* ruff's ``include`` / ``extend-include`` silently NARROW the directories
  passed on the command line (measured on ruff 0.15.20).

So each tool check is paired with a competing-scope guard check
(``lint-scope-guard`` and friends) that hard-errors when the tool's own config
carries scope.  The guards are pure and fast, so they run in a preview while
the tool checks themselves are listed.

``exclude`` / ``extend-exclude`` / ``force-exclude`` are deliberately exempt:
an explicit path bypasses them, which produces loud over-inclusion rather than
silent under-scoping.

Invocation
----------

``uv run [--group G | --extra E] <binary> [subcommand...] <paths...>``, with no
shell, in the project directory, with the release context in the environment.
The group/extra flags are resolved by reading where the project declares the
tool, so the common case reproduces a bare ``uv run <tool>``.
"""

import configparser
import os
import re
import tomllib
from collections import namedtuple

from . import effects
from .utils import (
    detect_uv_workspace_root,
    get_check_timeout,
)

#: Config key holding the per-check blocks.
CONFIG_KEY = "checks"

#: Keys a check block may carry.
_ENTRY_KEYS = {"paths", "cwd"}

ToolSpec = namedtuple("ToolSpec", ("binary", "subcommand", "scope_family"))

#: The three path-capable built-ins, by check name.
TOOL_CHECKS = {
    "lint": ToolSpec("ruff", ("check",), "ruff"),
    "format": ToolSpec("ruff", ("format", "--check"), "ruff"),
    "type-check": ToolSpec("mypy", (), "mypy"),
}


def guard_name(check_name):
    """Name of the competing-scope guard paired with *check_name*."""
    return f"{check_name}-scope-guard"


class ToolCheckConfigError(Exception):
    """Raised when the ``checks`` config block is invalid."""


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def validate_tool_checks_config(config):
    """Validate the ``checks`` section of a project config.

    Returns ``{check_name: entry}`` for the declared checks, or ``{}`` when
    the key is absent.  Every violation is a hard error: an unknown check
    name, a missing or empty ``paths``, a non-string path, an unknown key.
    """
    block = (config or {}).get(CONFIG_KEY)
    if block is None:
        return {}
    if not isinstance(block, dict):
        raise ToolCheckConfigError(
            f"{CONFIG_KEY} must be a map of check name -> settings, got "
            f"{type(block).__name__}"
        )

    declared = {}
    for name, entry in block.items():
        if name not in TOOL_CHECKS:
            valid = ", ".join(sorted(TOOL_CHECKS))
            raise ToolCheckConfigError(
                f"{CONFIG_KEY}.{name} is not a path-capable built-in check. "
                f"Valid names: {valid}."
            )
        if not isinstance(entry, dict):
            raise ToolCheckConfigError(
                f"{CONFIG_KEY}.{name} must be a map, got "
                f"{type(entry).__name__}"
            )
        unknown = set(entry) - _ENTRY_KEYS
        if unknown:
            raise ToolCheckConfigError(
                f"{CONFIG_KEY}.{name} has unknown key(s): "
                f"{', '.join(sorted(unknown))}. Allowed keys: "
                f"{', '.join(sorted(_ENTRY_KEYS))}"
            )
        paths = entry.get("paths")
        if not isinstance(paths, list) or not paths:
            raise ToolCheckConfigError(
                f"{CONFIG_KEY}.{name}.paths must be a non-empty list of "
                f"paths -- the scope is declared, never inferred"
            )
        for i, p in enumerate(paths):
            if not isinstance(p, str) or not p.strip():
                raise ToolCheckConfigError(
                    f"{CONFIG_KEY}.{name}.paths[{i}] must be a non-empty string"
                )
        cwd = entry.get("cwd")
        if cwd is not None and not isinstance(cwd, str):
            raise ToolCheckConfigError(
                f"{CONFIG_KEY}.{name}.cwd must be a string, got "
                f"{type(cwd).__name__}"
            )
        declared[name] = entry
    return declared


def declared_entry(config, check_name):
    """The validated entry for *check_name*, or None when it is not declared."""
    return validate_tool_checks_config(config).get(check_name)


# ---------------------------------------------------------------------------
# Invocation shape
# ---------------------------------------------------------------------------


def _load_toml(path):
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None


def probe_tool_location(project_dir, tool_binary):
    """Detect where *tool_binary* is declared in a project's pyproject.toml.

    Checks, in order:

    1. ``[dependency-groups].*`` -- any group declaring the tool
    2. ``[project.optional-dependencies].*`` -- any extra declaring the tool
    3. ``[tool.uv].dev-dependencies`` -- uv legacy dev deps

    Returns ``(source_type, name)`` on match, else ``None``.  ``source_type``
    is one of ``"dependency-group"``, ``"optional-dep"``, ``"uv-dev"``.
    """
    data = _load_toml(os.path.join(project_dir, "pyproject.toml"))
    if data is None:
        return None

    # A requirement string like "ruff>=0.15.20" or "mypy[extra]" -- the package
    # name is the leading run of name characters before any version/extra spec.
    name_re = re.compile(rf"^{re.escape(tool_binary)}(\[|[<>=!~; ]|$)")

    def _declares(entries):
        for entry in entries or []:
            if isinstance(entry, str) and name_re.match(entry.strip()):
                return True
        return False

    for group_name, entries in (data.get("dependency-groups") or {}).items():
        if _declares(entries):
            return ("dependency-group", group_name)

    opt_deps = (data.get("project") or {}).get("optional-dependencies") or {}
    for extra_name, entries in opt_deps.items():
        if _declares(entries):
            return ("optional-dep", extra_name)

    uv_dev_deps = (
        (data.get("tool") or {}).get("uv") or {}
    ).get("dev-dependencies") or []
    if _declares(uv_dev_deps):
        return ("uv-dev", "dev")

    return None


def resolve_tool_group_flags(project_dir, tool_binary):
    """Return the ``uv run`` group/extra flags needed to reach *tool_binary*.

    Degrades to ``[]`` (plain ``uv run``) when the tool lives in the default
    ``dev`` group, in uv's legacy dev-dependencies, in a uv workspace venv, or
    cannot be located -- so the common case reproduces bare ``uv run <tool>``.
    Non-default dependency groups yield ``["--group", name]`` and optional
    extras yield ``["--extra", name]``.
    """
    if detect_uv_workspace_root(project_dir) is not None:
        return []
    location = probe_tool_location(project_dir, tool_binary)
    if location is None:
        return []
    source_type, name = location
    if source_type == "dependency-group":
        return [] if name == "dev" else ["--group", name]
    if source_type == "optional-dep":
        return ["--extra", name]
    return []  # uv-dev: synced by default


def compose_argv(check_name, paths, project_dir):
    """Compose the shell-free argv for one tool check."""
    spec = TOOL_CHECKS[check_name]
    group_flags = resolve_tool_group_flags(project_dir, spec.binary)
    return ["uv", "run", *group_flags, spec.binary, *spec.subcommand, *paths]


# ---------------------------------------------------------------------------
# Subprocess context shared with the config-declared external checks
# ---------------------------------------------------------------------------


#: Attribute the resolved release-context env is memoized on, so N checks in
#: one run mean ONE ledger read, not N.
_ENV_CACHE_ATTR = "_rlsbl_external_check_env"


def release_context_env(ctx):
    """Return the subprocess env for a check: os.environ + RLSBL_*.

    Injected (see docs/configuration.md for the availability matrix):

    - ``RLSBL_PROJECT_ROOT`` -- the resolved project root. An entry with a
      ``cwd`` override otherwise has no way to find it.
    - ``RLSBL_LAST_TAG`` -- the tag name of the release the LEDGER anchors this
      checkout to, translated into the project's own tag scheme (so it is
      monorepo-correct). The EMPTY STRING when the ledger records no release
      this checkout contains, so a check can tell "no baseline yet" from "not
      injected".
    - ``RLSBL_UNRELEASED_RANGE`` -- ``<candidate_sha>..HEAD``, or ``HEAD`` when
      there is no such release.

    The version is SELECTED from the ledger and only then translated into a
    tag; the tag namespace no longer decides which release is the baseline.
    The range is expressed as the anchor commit rather than the tag, so a check
    receives a range that resolves even when the tag was deleted or moved.

    Computed once per check run and memoized on the context object.
    """
    cached = getattr(ctx, _ENV_CACHE_ATTR, None)
    if cached is not None:
        return cached

    from .ledger import range_anchor, tag_for_version
    from .checks._common import _resolve_ledger_dir, _resolve_tag_glob

    project_root = str(ctx.project_root)
    tag_glob = _resolve_tag_glob(ctx)
    # ``RLSBL_LAST_TAG=""`` is a SIGNAL, not a fallback: it states that this
    # project has no release in this history, and a check reading it takes the
    # first-release branch. Only the genuine no-release case may produce it --
    # every other failure mode of the ledger read (a tag disagreeing with an
    # anchor, an ancestry git cannot decide, an archive with no anchor) is a
    # LedgerError that propagates. A truncated history used to be flattened
    # into the empty string here, which made a shallow clone look brand-new to
    # every check; the ledger refuses to answer instead.
    anchor = range_anchor(_resolve_ledger_dir(ctx), tag_glob=tag_glob,
                          cwd=project_root)

    env = dict(os.environ)
    env["RLSBL_PROJECT_ROOT"] = project_root
    if anchor is None:
        env["RLSBL_LAST_TAG"] = ""
        env["RLSBL_UNRELEASED_RANGE"] = "HEAD"
    else:
        env["RLSBL_LAST_TAG"] = tag_for_version(tag_glob, anchor.version)
        env["RLSBL_UNRELEASED_RANGE"] = f"{anchor.candidate_sha}..HEAD"
    try:
        setattr(ctx, _ENV_CACHE_ATTR, env)
    except AttributeError:
        pass  # a slotted/frozen context simply recomputes
    return env


def resolve_cwd(ctx, cwd):
    """Resolve a check's declared cwd against the project root."""
    if cwd is None:
        return str(ctx.project_root)
    if not os.path.isabs(cwd):
        return os.path.join(str(ctx.project_root), cwd)
    return cwd


def report_subprocess_result(reporter, result, name):
    """Turn a completed subprocess into a pass/fail check result.

    Every line handed to the reporter goes through ``reportable_lines``: the
    reporter rejects empty problem text with an exception that propagates out
    of the whole check run, and real linters separate their findings with blank
    lines, so unfiltered output turned a lint failure into an unattributed
    internal error.
    """
    from .checks._common import reportable_lines, summary_line

    if result.returncode == 0:
        stdout = (result.stdout or "").strip()
        msg = summary_line(stdout, fallback="passed") if stdout else "passed"
        return reporter.passed(msg)

    exit_text = f"exit code {result.returncode}"
    stdout_lines = reportable_lines(result.stdout, limit=20)
    stderr_lines = reportable_lines(result.stderr, limit=20)
    for line in stdout_lines:
        reporter.error(line)
    for line in stderr_lines:
        reporter.error(line)
    if not stdout_lines and not stderr_lines:
        reporter.error(exit_text)
    first = (stderr_lines or stdout_lines or [exit_text])[0]
    return reporter.found(
        f"check '{name}' failed (exit {result.returncode}): " + first[:200]
    )


def resolve_check_budget(ctx):
    """Resolve the subprocess budget for a check at RUN time.

    Read from the live ``ctx.config`` rather than bound when the check spec is
    built, so ``--check-timeout`` (which the release writes into its in-memory
    config) is honored.  One precedence chain -- flag > ``check_timeout``
    config key > shipped default -- governs every check.
    """
    return get_check_timeout(getattr(ctx, "config", None))


def run_tool_check(ctx, reporter, check_name):
    """Run one declared tool check, or skip when it is not configured."""
    import subprocess

    entry = declared_entry(ctx.config, check_name)
    if entry is None:
        return reporter.skipped(
            f"not configured (declare .rlsbl/config.json "
            f'"{CONFIG_KEY}.{check_name}.paths")'
        )
    check_cwd = resolve_cwd(ctx, entry.get("cwd"))
    budget = resolve_check_budget(ctx)
    argv = compose_argv(check_name, entry["paths"], check_cwd)
    try:
        result = effects.run(
            argv,
            cwd=check_cwd,
            capture_output=True,
            text=True,
            timeout=budget,
            env=release_context_env(ctx),
        )
    except subprocess.TimeoutExpired:
        msg = f"check '{check_name}' timed out after {budget}s"
        reporter.error(msg)
        return reporter.found(msg)
    except OSError as exc:
        msg = f"check '{check_name}' failed to execute: {exc}"
        reporter.error(msg)
        return reporter.found(msg)

    return report_subprocess_result(reporter, result, check_name)


# ---------------------------------------------------------------------------
# Competing-scope guards
# ---------------------------------------------------------------------------


def mypy_scope_conflicts(root):
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


def ruff_scope_conflicts(root):
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


_SCOPE_EXPLANATION = {
    "mypy": (
        "mypy CLI paths silently OVERRIDE config scope; scope must live only "
        "in the check's declared 'paths'"
    ),
    "ruff": (
        "ruff config include/extend-include silently NARROWS the explicitly-"
        "passed directories; scope must live only in the check's declared "
        "'paths'"
    ),
}


def scope_conflicts(check_name, root):
    """Competing-scope findings for *check_name*'s tool under *root*."""
    family = TOOL_CHECKS[check_name].scope_family
    if family == "mypy":
        return mypy_scope_conflicts(root)
    return ruff_scope_conflicts(root)


def run_scope_guard(ctx, reporter, check_name):
    """Run the competing-scope guard paired with *check_name*."""
    entry = declared_entry(ctx.config, check_name)
    if entry is None:
        return reporter.skipped(f"{check_name} is not configured")
    root = resolve_cwd(ctx, entry.get("cwd"))
    conflicts = scope_conflicts(check_name, root)
    if not conflicts:
        return reporter.passed("no competing scope in tool config")
    for source, key in conflicts:
        reporter.error(f"{source}: '{key}' competes with declared scope")
    return reporter.found(
        f"check '{check_name}': competing scope in tool config "
        f"({_SCOPE_EXPLANATION[TOOL_CHECKS[check_name].scope_family]})"
    )
