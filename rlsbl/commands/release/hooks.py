"""Hook helpers: content hashing, template hash lookup, hook emptiness check, hook runner.

Supports two levels of hooks in monorepo explicit mode:
- Per-releasable: ``.rlsbl-monorepo/releasables/{name}/hooks/``
- Per-package: ``.rlsbl/hooks/`` within each member package

Hook sources (checked in order):
1. Config-driven: ``hooks`` key in project/releasable config.json
2. Script-based: shell scripts in ``.rlsbl/hooks/`` directories

Config-driven hooks use dual syntax (each entry is a string or structured object):
- String shorthand: ``"npm test"`` is equivalent to ``{"cmd": "npm test"}``
- Structured: ``{"cmd": "npm test", "dir": "./submodule", "env": {"NODE_ENV": "test"}}``

Execution order during release:
1. Releasable pre-checks
2. Per-package pre-checks (alphabetical by package name)
3. Built-in tests (each member)
4. Per-package pre-release (alphabetical)
5. Releasable pre-release
"""

import hashlib
import os
import sys

from .validate import HookError

# Lazily computed on first access via _get_pre_release_template_hashes().
_PRE_RELEASE_TEMPLATE_HASHES = None


def _compute_content_hash(content):
    """SHA-256 of content with trailing whitespace stripped."""
    return hashlib.sha256(content.rstrip().encode("utf-8")).hexdigest()


def _get_pre_release_template_hashes():
    """Return a frozenset of content hashes for known scaffold template versions of pre-release.sh.

    Currently there is only one version (the template has never changed),
    but using a set follows the same pattern as hook_hashes.py, making it
    easy to add historical versions later.
    """
    global _PRE_RELEASE_TEMPLATE_HASHES
    if _PRE_RELEASE_TEMPLATE_HASHES is not None:
        return _PRE_RELEASE_TEMPLATE_HASHES

    template_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "templates", "shared", "hooks", "pre-release.sh.tpl",
    )
    with open(template_path, "r", encoding="utf-8") as f:
        template_content = f.read()

    # V1: original scaffold template (before the comment was updated to
    # describe the override behavior).
    _V1 = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "# Project-specific pre-release checks.\n"
        "# Built-in checks (tests, lint) run automatically before this hook.\n"
        "# Add custom validation here, e.g.:\n"
        "#   - Check for uncommitted documentation\n"
        "#   - Verify external service connectivity\n"
        "#   - Run integration tests not covered by the test suite\n"
    )

    _PRE_RELEASE_TEMPLATE_HASHES = frozenset({
        _compute_content_hash(template_content),
        _compute_content_hash(_V1),
    })
    return _PRE_RELEASE_TEMPLATE_HASHES


def _is_hook_effectively_empty(hook_path):
    """Check if a pre-release hook file is effectively empty (matches scaffold template).

    Returns True (hook is boilerplate / not customized) when:
    - The hook file does not exist
    - The hook file's content hash matches a known scaffold template version

    Returns False (hook has been customized) when:
    - The hook exists and its content does not match any known template version
    """
    if not os.path.exists(hook_path):
        return True

    with open(hook_path, "r", encoding="utf-8") as f:
        hook_content = f.read()

    hook_hash = _compute_content_hash(hook_content)
    return hook_hash in _get_pre_release_template_hashes()


# ---------------------------------------------------------------------------
# Backward compatibility bridge
# ---------------------------------------------------------------------------


def warn_if_hook_needs_migration(config, hook_path):
    """Emit a warning if a customized pre-release.sh exists without config entries.

    This detects projects that still use the old script-based hook system
    without having migrated to config-driven hooks. The warning is
    informational only -- no auto-conversion is performed.

    Args:
        config: project config dict (from ``ctx.config``).
        hook_path: absolute path to the pre-release.sh script.

    Returns:
        True if a warning was emitted, False otherwise.
    """
    # If config already has hooks.pre_release entries (even empty list),
    # no migration needed -- the project is using the config system.
    if isinstance(config, dict):
        hooks_section = config.get("hooks")
        if isinstance(hooks_section, dict) and "pre_release" in hooks_section:
            return False

    # Check if the script exists and is customized (not matching template)
    if not _is_hook_effectively_empty(hook_path):
        print(
            "Warning: Customized hook script found at "
            f"{hook_path} but no hook config entries.\n"
            "Migrate to config-driven hooks by adding to .rlsbl/config.json:\n"
            '  "hooks": {"pre_release": ["<your commands here>"]}',
            file=sys.stderr,
        )
        return True

    return False


# ---------------------------------------------------------------------------
# Config-driven hooks
# ---------------------------------------------------------------------------

# Map hyphenated hook names (used in code) to underscore config keys.
_HOOK_CONFIG_KEY = {
    "pre-checks": "pre_checks",
    "pre-release": "pre_release",
    "post-release": "post_release",
}


def normalize_hook_entry(entry):
    """Convert a hook entry to structured dict form.

    String entries are shorthand for ``{"cmd": <string>}``.
    Dict entries are validated for required ``cmd`` field.

    Args:
        entry: a string or dict hook entry.

    Returns:
        A dict with at least ``cmd`` (str), and optionally ``dir`` (str)
        and ``env`` (dict).

    Raises:
        HookError: if entry is neither str nor dict, or if dict is missing ``cmd``.
    """
    if isinstance(entry, str):
        return {"cmd": entry}
    if isinstance(entry, dict):
        if "cmd" not in entry:
            raise HookError(
                f"Hook entry missing required 'cmd' field: {entry}"
            )
        # Validate types of optional fields
        if "dir" in entry and not isinstance(entry["dir"], str):
            raise HookError(
                f"Hook entry 'dir' must be a string: {entry}"
            )
        if "env" in entry and not isinstance(entry["env"], dict):
            raise HookError(
                f"Hook entry 'env' must be a dict: {entry}"
            )
        return dict(entry)
    raise HookError(
        f"Hook entry must be a string or dict, got {type(entry).__name__}: {entry}"
    )


def _get_config_hooks(hook_name, config):
    """Read hook entries from config for a given hook slot.

    Args:
        hook_name: hyphenated hook name (e.g. ``"pre-checks"``).
        config: project config dict.

    Returns:
        List of hook entries (strings or dicts), or None if the config
        has no entries for this hook slot.

    Raises:
        HookError: if the hook slot value is not a list.
    """
    if not isinstance(config, dict):
        return None
    hooks_section = config.get("hooks")
    if hooks_section is None or not isinstance(hooks_section, dict):
        return None
    config_key = _HOOK_CONFIG_KEY.get(hook_name, hook_name.replace("-", "_"))
    if config_key not in hooks_section:
        return None
    entries = hooks_section[config_key]
    if not isinstance(entries, list):
        raise HookError(
            f"hooks.{config_key} must be a list, got {type(entries).__name__}"
        )
    return entries


def run_config_hooks(hook_name, config, project_dir, env, timeout, *,
                     fatal=True, display_name=None):
    """Run config-driven hooks for a given hook slot.

    Reads ``config["hooks"][<hook_config_key>]``, normalizes each entry,
    and runs them in order via subprocess.

    Args:
        hook_name: canonical hyphenated hook name for config lookup
            (e.g. ``"pre-checks"``).
        config: project config dict containing a ``hooks`` key.
        project_dir: base working directory.
        env: environment dict to pass to subprocesses.
        timeout: seconds before a command is killed.
        fatal: if True (default), non-zero exit raises HookError.
            If False, logs a warning and continues (for post-release).
        display_name: human-readable name for error messages. If None,
            uses ``hook_name``.

    Returns:
        True if config had entries for this hook (even if empty list),
        False if config had no entries (caller should fall back to scripts).

    Raises:
        HookError: on non-zero exit or timeout (when fatal=True).
    """
    entries = _get_config_hooks(hook_name, config)
    if entries is None:
        return False

    if display_name is None:
        display_name = hook_name

    # Late-bind subprocess through the package namespace so tests can patch
    # rlsbl.commands.release.subprocess and the mock is visible here.
    from . import subprocess as _subprocess

    for entry in entries:
        normalized = normalize_hook_entry(entry)
        cmd = normalized["cmd"]

        # Compute working directory
        cwd = project_dir
        if "dir" in normalized:
            cwd = os.path.join(project_dir, normalized["dir"])

        # Merge env: base env + entry-specific env
        run_env = dict(env)
        if "env" in normalized:
            run_env.update(normalized["env"])

        try:
            _subprocess.run(
                ["bash", "-c", cmd], env=run_env, check=True,
                timeout=timeout, cwd=cwd,
            )
        except _subprocess.CalledProcessError as e:
            if fatal:
                raise HookError(
                    f"{display_name} hook command failed (exit {e.returncode}): {cmd}"
                ) from e
            print(
                f"Warning: {display_name} hook command failed "
                f"(exit {e.returncode}): {cmd}",
                file=sys.stderr,
            )
        except _subprocess.TimeoutExpired as e:
            if fatal:
                raise HookError(
                    f"{display_name} hook command timed out after {timeout}s: {cmd}"
                ) from e
            print(
                f"Warning: {display_name} hook command timed out "
                f"after {timeout}s: {cmd}",
                file=sys.stderr,
            )

    return True


def is_hook_customized(config, hook_path):
    """Check if the pre-release hook is customized, using config-first detection.

    Decision logic:
    1. If config has a non-empty ``hooks.pre_release`` commands list, the hook
       is considered customized (built-in tests and lint are skipped).
    2. If config has ``hooks.pre_release`` as an empty list, the hook is NOT
       customized (built-in tests and lint run).
    3. If config has no ``hooks`` section or no ``pre_release`` key, fall back
       to the script-file hash check (backward compat during migration):
       - Customized script file -> customized
       - Template/missing script file -> not customized

    Args:
        config: project config dict (from ``ctx.config``).
        hook_path: absolute path to the pre-release.sh script.

    Returns:
        True if the hook is customized (skip built-in tests/lint),
        False otherwise.
    """
    pre_release_cmds = _get_config_hooks("pre-release", config)
    if pre_release_cmds is not None:
        # Config has hooks.pre_release -- use it as the source of truth
        return len(pre_release_cmds) > 0

    # No config-based hooks section -- fall back to script hash check
    return not _is_hook_effectively_empty(hook_path)


def run_release_hook(hook_name, hook_path, project_dir, env, timeout,
                     *, config=None):
    """Run a release hook: config-driven if available, else script-based.

    When ``config`` is provided and has entries for the hook slot, runs
    config-driven hooks and ignores the script file. Otherwise falls back
    to the script at ``hook_path``.

    hook_name: human-readable name for error messages (e.g. "pre-checks",
        "releasable pre-checks", "pre-checks (mypkg)"). May include a
        prefix for display purposes; the canonical hook name for config
        lookup is extracted automatically.
    hook_path: absolute path to the shell script (fallback).
    project_dir: working directory for the hook.
    env: environment dict to pass to the subprocess.
    timeout: seconds before the hook is killed.
    config: project config dict (optional). If provided, checked for
        config-driven hook entries before falling back to script.

    Raises HookError on non-zero exit or timeout.
    """
    # Check config-driven hooks first
    if config is not None:
        # Extract canonical hook name for config lookup. Display names like
        # "releasable pre-checks" or "pre-checks (mypkg)" need to be mapped
        # to the base name "pre-checks" for config key resolution.
        canonical = hook_name
        for base_name in _HOOK_CONFIG_KEY:
            if base_name in hook_name:
                canonical = base_name
                break
        fatal = "post-release" not in hook_name
        if run_config_hooks(canonical, config, project_dir, env, timeout,
                            fatal=fatal, display_name=hook_name):
            return

    # Fall back to script-based hooks
    # Late-bind subprocess through the package namespace so tests can patch
    # rlsbl.commands.release.subprocess and the mock is visible here.
    from . import subprocess as _subprocess

    if not os.path.exists(hook_path):
        return

    hook_path = os.path.abspath(hook_path)
    try:
        _subprocess.run(
            ["bash", hook_path], env=env, check=True,
            timeout=timeout, cwd=project_dir,
        )
    except _subprocess.CalledProcessError as e:
        raise HookError(
            f"{hook_name} hook exited with code {e.returncode}."
        ) from e
    except _subprocess.TimeoutExpired as e:
        raise HookError(
            f"{hook_name} hook timed out after {timeout}s."
        ) from e


# ---------------------------------------------------------------------------
# Per-releasable hook resolution
# ---------------------------------------------------------------------------


def get_releasable_hook_path(workspace_root, releasable_name, hook_name):
    """Return the absolute path to a releasable-level hook script.

    Path: ``<workspace_root>/.rlsbl-monorepo/releasables/<name>/hooks/<hook_name>``

    Args:
        workspace_root: path to the monorepo root.
        releasable_name: name of the releasable.
        hook_name: hook file name (e.g. ``"pre-checks.sh"``).

    Returns:
        Absolute path string. The file may or may not exist on disk.
    """
    from ...workspace import get_releasable_dir
    return os.path.join(get_releasable_dir(str(workspace_root), releasable_name), "hooks", hook_name)


def get_package_hook_path(package_dir, hook_name):
    """Return the absolute path to a per-package hook script.

    Path: ``<package_dir>/.rlsbl/hooks/<hook_name>``

    Args:
        package_dir: absolute path to the package directory.
        hook_name: hook file name (e.g. ``"pre-checks.sh"``).

    Returns:
        Absolute path string. The file may or may not exist on disk.
    """
    return os.path.join(str(package_dir), ".rlsbl", "hooks", hook_name)


def build_hook_env(base_env, version, *, package_name=None, bump_type=None,
                   prev_version=None, description=None):
    """Build the environment dict for hook execution.

    Always sets ``RLSBL_VERSION``. When ``package_name`` is provided
    (per-package hooks), also sets ``RLSBL_PACKAGE``.

    Args:
        base_env: base environment dict (typically ``os.environ.copy()``).
        version: the new release version string.
        package_name: current package name (for per-package hooks), or None.
        bump_type: bump type string (patch/minor/major), or None.
        prev_version: previous version string, or None.
        description: release description string, or None.

    Returns:
        A new dict with hook-specific env vars added.
    """
    env = dict(base_env)
    env["RLSBL_VERSION"] = version
    if bump_type is not None:
        env["RLSBL_BUMP_TYPE"] = bump_type
    if prev_version is not None:
        env["RLSBL_PREV_VERSION"] = prev_version
    if description is not None:
        env["RLSBL_DESCRIPTION"] = description
    if package_name is not None:
        env["RLSBL_PACKAGE"] = package_name
    return env


def run_releasable_hooks(hook_name, workspace_root, releasable_name,
                         member_packages, hook_env, timeout, log,
                         *, project_dir=None, releasable_config=None,
                         package_configs=None):
    """Run hooks at both releasable and per-package levels.

    For ``pre-checks``: releasable first, then per-package (alphabetical).
    For ``pre-release``: per-package first (alphabetical), then releasable.
    For ``post-release``: releasable first, then per-package (alphabetical).

    Each per-package hook gets ``RLSBL_PACKAGE`` added to its env.

    Config-driven hooks take precedence over script files at each level
    independently. The releasable level uses ``releasable_config`` and
    per-package level uses ``package_configs``. If config has entries for
    a hook slot, scripts are ignored for that level.

    Args:
        hook_name: hook file name without extension context, e.g. ``"pre-checks"``.
        workspace_root: path to the monorepo root.
        releasable_name: name of the releasable.
        member_packages: list of (package_name, package_dir) tuples, sorted
            alphabetically by name.
        hook_env: base environment dict (without RLSBL_PACKAGE).
        timeout: seconds before a hook is killed.
        log: callable for logging messages.
        project_dir: fallback cwd for releasable-level hooks. If None,
            uses workspace_root.
        releasable_config: releasable-level config dict (optional).
        package_configs: dict mapping package_name to per-package config dict
            (optional).

    Raises:
        HookError on first failure at any level.
    """
    hook_file = f"{hook_name}.sh"
    releasable_hook = get_releasable_hook_path(workspace_root, releasable_name, hook_file)

    # Sort member packages alphabetically by name
    sorted_members = sorted(member_packages, key=lambda p: p[0])

    def _run_releasable_level():
        cwd = project_dir or str(workspace_root)
        log(f"Running releasable {hook_name} hook...")
        run_release_hook(
            f"releasable {hook_name}",
            releasable_hook,
            cwd,
            hook_env,
            timeout,
            config=releasable_config,
        )

    def _releasable_has_hooks():
        """Check if releasable level has any hook source (config or script)."""
        if releasable_config is not None:
            entries = _get_config_hooks(hook_name, releasable_config)
            if entries is not None:
                return True
        return os.path.exists(releasable_hook)

    if hook_name == "pre-release":
        # Per-package first, then releasable
        _run_per_package_hooks(hook_name, hook_file, sorted_members,
                               hook_env, timeout, log,
                               package_configs=package_configs)
        if _releasable_has_hooks():
            _run_releasable_level()
    else:
        # Releasable first, then per-package (pre-checks, post-release)
        if _releasable_has_hooks():
            _run_releasable_level()
        _run_per_package_hooks(hook_name, hook_file, sorted_members,
                               hook_env, timeout, log,
                               package_configs=package_configs)


def _run_per_package_hooks(hook_name, hook_file, sorted_members, hook_env,
                           timeout, log, *, package_configs=None):
    """Run a hook for each member package that has it, in alphabetical order.

    Adds ``RLSBL_PACKAGE`` to the env for each package.
    Config-driven hooks take precedence over script files per-package.

    Args:
        hook_name: human-readable hook name (e.g. ``"pre-checks"``).
        hook_file: hook file name (e.g. ``"pre-checks.sh"``).
        sorted_members: list of (package_name, package_dir) tuples, already sorted.
        hook_env: base environment dict (without RLSBL_PACKAGE).
        timeout: seconds before a hook is killed.
        log: callable for logging messages.
        package_configs: dict mapping package_name to per-package config dict
            (optional).

    Raises:
        HookError on first failure.
    """
    if package_configs is None:
        package_configs = {}

    for pkg_name, pkg_dir in sorted_members:
        pkg_hook = get_package_hook_path(pkg_dir, hook_file)
        pkg_config = package_configs.get(pkg_name)
        pkg_env = dict(hook_env)
        pkg_env["RLSBL_PACKAGE"] = pkg_name

        # Check if package has config-driven hooks or a script file
        has_config_hooks = (
            pkg_config is not None
            and _get_config_hooks(hook_name, pkg_config) is not None
        )
        has_script = os.path.exists(pkg_hook)

        if has_config_hooks or has_script:
            log(f"Running {hook_name} hook for package {pkg_name}...")
            run_release_hook(
                f"{hook_name} ({pkg_name})",
                pkg_hook,
                str(pkg_dir),
                pkg_env,
                timeout,
                config=pkg_config,
            )


def run_releasable_tests(member_packages, registry, flags, *, ctx, log):
    """Run built-in tests for each member package of a releasable.

    Iterates members alphabetically. Any failure raises HookError.

    Args:
        member_packages: list of (package_name, package_dir) tuples.
        registry: target/registry identifier (e.g. ``"pypi"``).
        flags: release flags dict.
        ctx: ProjectContext.
        log: callable for logging messages.

    Raises:
        HookError if any member's tests fail.
    """
    from .validate import _run_builtin_tests

    for pkg_name, pkg_dir in sorted(member_packages, key=lambda p: p[0]):
        log(f"Running tests for package {pkg_name}...")
        _run_builtin_tests(registry, flags, project_dir=str(pkg_dir), ctx=ctx)


def run_releasable_lint(member_packages, flags, *, ws_projects, log):
    """Run built-in lint for library members of a releasable.

    Only runs on members with ``library = true``.

    Args:
        member_packages: list of (package_name, package_dir) tuples.
        flags: release flags dict.
        ws_projects: list of WorkspaceProject instances (to check library flag).
        log: callable for logging messages.

    Raises:
        HookError if any member's lint fails.
    """
    from .validate import _run_builtin_lint

    # Build a lookup from name to library flag
    lib_lookup = {}
    for proj in ws_projects:
        lib_lookup[proj.name] = proj.library

    for pkg_name, pkg_dir in sorted(member_packages, key=lambda p: p[0]):
        is_library = lib_lookup.get(pkg_name, False)
        if is_library:
            log(f"Running lint for library package {pkg_name}...")
            _run_builtin_lint(flags, is_library=True, project_dir=str(pkg_dir))


def is_releasable_hook_customized(workspace_root, releasable_name, config=None):
    """Check if a releasable's pre-release hook is customized (not scaffold boilerplate).

    When ``config`` is provided, checks config-driven hooks first:
    1. If config has a non-empty ``hooks.pre_release`` list -> customized
    2. If config has an empty ``hooks.pre_release`` list -> not customized
    3. If config has no ``hooks`` section -> fall back to script hash check

    The "effectively empty" check is at the releasable level: if the
    releasable's pre-release hook is customized, built-in tests/lint are
    skipped for the entire releasable.

    Args:
        workspace_root: path to the monorepo root.
        releasable_name: name of the releasable.
        config: optional releasable-level config dict. When None, uses
            script-hash check only (backward compat).

    Returns:
        True if the releasable has a customized pre-release hook, False otherwise.
    """
    hook_path = get_releasable_hook_path(workspace_root, releasable_name, "pre-release.sh")
    if config is not None:
        return is_hook_customized(config, hook_path)
    return not _is_hook_effectively_empty(hook_path)
