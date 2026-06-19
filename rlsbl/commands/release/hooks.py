"""Hook helpers: content hashing, template hash lookup, hook emptiness check, hook runner.

Supports two levels of hooks in monorepo explicit mode:
- Per-releasable: ``.rlsbl-monorepo/releasables/{name}/hooks/``
- Per-package: ``.rlsbl/hooks/`` within each member package

Execution order during release:
1. Releasable pre-checks
2. Per-package pre-checks (alphabetical by package name)
3. Built-in tests (each member)
4. Per-package pre-release (alphabetical)
5. Releasable pre-release
"""

import hashlib
import os

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


def run_release_hook(hook_name, hook_path, project_dir, env, timeout):
    """Run a release hook script (pre-checks or pre-release).

    hook_name: human-readable name for error messages (e.g. "pre-checks").
    hook_path: absolute path to the shell script.
    project_dir: working directory for the hook.
    env: environment dict to pass to the subprocess.
    timeout: seconds before the hook is killed.

    Raises HookError on non-zero exit or timeout.
    """
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
                         *, project_dir=None):
    """Run hooks at both releasable and per-package levels.

    For ``pre-checks``: releasable first, then per-package (alphabetical).
    For ``pre-release``: per-package first (alphabetical), then releasable.
    For ``post-release``: releasable first, then per-package (alphabetical).

    Each per-package hook gets ``RLSBL_PACKAGE`` added to its env.

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

    Raises:
        HookError on first failure at any level.
    """
    hook_file = f"{hook_name}.sh"
    releasable_hook = get_releasable_hook_path(workspace_root, releasable_name, hook_file)

    # Sort member packages alphabetically by name
    sorted_members = sorted(member_packages, key=lambda p: p[0])

    if hook_name == "pre-release":
        # Per-package first, then releasable
        _run_per_package_hooks(hook_name, hook_file, sorted_members, hook_env, timeout, log)
        if os.path.exists(releasable_hook):
            cwd = project_dir or str(workspace_root)
            log(f"Running releasable {hook_name} hook...")
            run_release_hook(
                f"releasable {hook_name}",
                releasable_hook,
                cwd,
                hook_env,
                timeout,
            )
    else:
        # Releasable first, then per-package (pre-checks, post-release)
        if os.path.exists(releasable_hook):
            cwd = project_dir or str(workspace_root)
            log(f"Running releasable {hook_name} hook...")
            run_release_hook(
                f"releasable {hook_name}",
                releasable_hook,
                cwd,
                hook_env,
                timeout,
            )
        _run_per_package_hooks(hook_name, hook_file, sorted_members, hook_env, timeout, log)


def _run_per_package_hooks(hook_name, hook_file, sorted_members, hook_env, timeout, log):
    """Run a hook for each member package that has it, in alphabetical order.

    Adds ``RLSBL_PACKAGE`` to the env for each package.

    Args:
        hook_name: human-readable hook name (e.g. ``"pre-checks"``).
        hook_file: hook file name (e.g. ``"pre-checks.sh"``).
        sorted_members: list of (package_name, package_dir) tuples, already sorted.
        hook_env: base environment dict (without RLSBL_PACKAGE).
        timeout: seconds before a hook is killed.
        log: callable for logging messages.

    Raises:
        HookError on first failure.
    """
    for pkg_name, pkg_dir in sorted_members:
        pkg_hook = get_package_hook_path(pkg_dir, hook_file)
        if os.path.exists(pkg_hook):
            pkg_env = dict(hook_env)
            pkg_env["RLSBL_PACKAGE"] = pkg_name
            log(f"Running {hook_name} hook for package {pkg_name}...")
            run_release_hook(
                f"{hook_name} ({pkg_name})",
                pkg_hook,
                str(pkg_dir),
                pkg_env,
                timeout,
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


def is_releasable_hook_customized(workspace_root, releasable_name):
    """Check if a releasable's pre-release hook is customized (not scaffold boilerplate).

    The "effectively empty" check is at the releasable level: if the
    releasable's pre-release hook is customized, built-in tests/lint are
    skipped for the entire releasable.

    Args:
        workspace_root: path to the monorepo root.
        releasable_name: name of the releasable.

    Returns:
        True if the releasable has a customized pre-release hook, False otherwise.
    """
    hook_path = get_releasable_hook_path(workspace_root, releasable_name, "pre-release.sh")
    return not _is_hook_effectively_empty(hook_path)
