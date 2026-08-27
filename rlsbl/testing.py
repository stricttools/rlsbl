"""Shared test-running logic that auto-detects project types and invokes the correct test runner (pytest, go test, npm test) for releases and checks.

Extracted from the release pipeline so it can be reused by other commands
(e.g., pre-push checks, CI, standalone test invocations).
"""

import json
import os
import subprocess
import sys
import tomllib

from .errors import ConfigError
from .overlay_state import (
    MalformedSentinelError,
    OverlayModeConflictError,
    active_overlays,
)
from .utils import detect_uv_workspace_root, get_check_timeout, require_tool
from . import effects

# Shared remediation hint appended to every "command timed out" failure message
# so agents know which knob controls the budget. Kept identical across all sites.
CHECK_TIMEOUT_HINT = (
    "(budget configurable: the check_timeout key in .rlsbl/config.json "
    "— the check still hard-fails on real hangs)"
)

# We invoke pytest as ``python -P -m pytest`` rather than ``python -m pytest``.
# ``python -m`` prepends the current working directory to sys.path[0], and since
# tests run with cwd set to the target project's directory, a flat-layout module
# there that shadows a stdlib name (e.g. a package-root html.py shadowing stdlib
# ``html``) would break unrelated imports during pytest startup (plugin loading,
# e.g. pytest-playwright -> slugify -> ``from html.entities import ...``).
# ``-P`` (PYTHONSAFEPATH) keeps interpreter resolution intact (the reason we use
# ``python -m`` at all) while suppressing the CWD injection. It does NOT remove
# site-packages, so the project's own package and pytest remain importable.
# Requires Python 3.11+, which is the floor for all rlsbl-managed projects.


def _overlay_exclusions(overlays: list[dict] | None) -> list[str]:
    """Return the ``--no-install-package <pkg>`` arguments that keep declared
    dev overlays out of a sync, or ``[]`` in registry mode.

    Paired with ``--inexact`` (which stops the sync removing packages uv did
    not install), this is the same pair ``rlsbl dev sync`` and the sandboxed
    test runner use. Without it an exact sync reinstalls the locked registry
    wheel over the editable checkout, with no output saying so.
    """
    args: list[str] = []
    for overlay in overlays or []:
        args += ["--no-install-package", overlay["package"]]
    return args


def collect_active_overlays(project_dirs) -> list[dict]:
    """Union the active dev overlays declared by *project_dirs*.

    A workspace's members share one environment, so a sync at the workspace
    root must exclude every member's overlaid packages, not just one project's.
    Raises :class:`OverlayModeConflictError` when two projects overlay the same
    package from different checkouts -- one shared environment cannot hold both,
    and picking either silently would wipe the other.
    """
    merged: dict[str, str] = {}
    for project_dir in project_dirs:
        for overlay in active_overlays(project_dir) or []:
            package, path = overlay["package"], overlay["path"]
            existing = merged.get(package)
            if existing is not None and existing != path:
                raise OverlayModeConflictError(
                    f"Error: workspace projects overlay '{package}' from two "
                    f"different checkouts ({existing} and {path}); one shared "
                    "environment cannot hold both. Reconcile the "
                    "dev-sources.toml.local-only files and re-run "
                    "`rlsbl dev sync`."
                )
            merged[package] = path
    return [{"package": p, "path": merged[p]} for p in sorted(merged)]


def _sync_selector_args(run_cmd: list[str]) -> list[str]:
    """Extract the ``--group``/``--extra`` selectors from a resolved ``uv run``
    invocation, so an explicit ``uv sync`` installs exactly what the suite runs
    with. Reading them back off the command keeps one source for the choice."""
    selectors: list[str] = []
    for i, token in enumerate(run_cmd):
        if token in ("--group", "--extra") and i + 1 < len(run_cmd):
            selectors += [token, run_cmd[i + 1]]
    return selectors


def sync_workspace(
    workspace_root: str,
    *,
    verbose: bool = False,
    check_timeout: int = 120,
    overlays: list[dict] | None = None,
) -> bool:
    """Run uv sync --all-packages at the workspace root.

    In overlay mode (*overlays* non-empty) the sync additionally carries
    ``--inexact`` and one ``--no-install-package`` per overlaid package, so a
    workspace whose environment holds editable sibling checkouts is synced
    without wiping them.

    Returns True on success, False on failure.
    """
    if not require_tool("uv", fatal=False):
        print(
            "Error: uv is not installed; cannot sync the workspace.",
            file=sys.stderr,
        )
        return False

    if not os.path.exists(os.path.join(workspace_root, "pyproject.toml")):
        return True

    sync_cmd = ["uv", "sync", "--all-packages"]
    if not verbose:
        sync_cmd.append("--quiet")
    if overlays:
        sync_cmd.append("--inexact")
        sync_cmd += _overlay_exclusions(overlays)
    try:
        result = effects.run(sync_cmd, cwd=workspace_root, timeout=check_timeout)
    except subprocess.TimeoutExpired:
        print(f"Error: command timed out after {check_timeout}s: {sync_cmd} {CHECK_TIMEOUT_HINT}", file=sys.stderr)
        return False
    if result.returncode != 0:
        print("Error: uv sync failed.", file=sys.stderr)
        return False
    return True


def _probe_pytest_location(project_dir: str) -> tuple[str, str] | None:
    """Detect where pytest is declared in a project's pyproject.toml.

    Checks in order:
    1. [dependency-groups].* -- any group containing a pytest entry
    2. [project.optional-dependencies].* -- any extra containing pytest
    3. [tool.uv].dev-dependencies -- uv legacy dev deps

    Returns (source_type, group_name) on match, or None if not found.
    source_type is one of "dependency-group", "optional-dep", "uv-dev".
    """
    pyproject_path = os.path.join(project_dir, "pyproject.toml")
    if not os.path.isfile(pyproject_path):
        return None

    try:
        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None

    # 1. [dependency-groups]
    dep_groups = data.get("dependency-groups", {})
    for group_name, entries in dep_groups.items():
        for entry in entries:
            if isinstance(entry, str) and "pytest" in entry.lower():
                return ("dependency-group", group_name)

    # 2. [project.optional-dependencies]
    opt_deps = data.get("project", {}).get("optional-dependencies", {})
    for extra_name, entries in opt_deps.items():
        for entry in entries:
            if isinstance(entry, str) and "pytest" in entry.lower():
                return ("optional-dep", extra_name)

    # 3. [tool.uv].dev-dependencies
    uv_dev_deps = data.get("tool", {}).get("uv", {}).get("dev-dependencies", [])
    for entry in uv_dev_deps:
        if isinstance(entry, str) and "pytest" in entry.lower():
            return ("uv-dev", "dev")

    return None


def _resolve_pytest_invocation(
    project_dir: str, workspace_root: str | None
) -> list[str]:
    """Build the pytest command for a project based on its environment.

    For workspace members, returns plain ``uv run python -P -m pytest`` (the
    workspace venv has everything). For standalone projects, probes
    pyproject.toml to determine the correct uv flags.

    Raises ConfigError if pytest is not declared anywhere in pyproject.toml.
    """
    uv_ws_root = detect_uv_workspace_root(project_dir)
    if uv_ws_root is not None:
        return ["uv", "run", "python", "-P", "-m", "pytest"]

    location = _probe_pytest_location(project_dir)
    if location is None:
        raise ConfigError(
            f"pytest is not declared in {project_dir}/pyproject.toml. "
            "Add it to [dependency-groups].dev or [project.optional-dependencies].test."
        )

    source_type, name = location
    if source_type == "dependency-group":
        if name == "dev":
            return ["uv", "run", "python", "-P", "-m", "pytest"]
        return ["uv", "run", "--group", name, "python", "-P", "-m", "pytest"]
    elif source_type == "optional-dep":
        return ["uv", "run", "--extra", name, "python", "-P", "-m", "pytest"]
    else:
        # uv-dev: uv syncs dev deps by default
        return ["uv", "run", "python", "-P", "-m", "pytest"]


def _pytest_marker_args(config: dict) -> list[str]:
    """Return ``["-m", <markers>]`` when a ``test.pypi.markers`` string is
    configured, else ``[]``.

    Reads the per-target test block via ``config.get("test", {}).get("pypi", {})``
    so future per-target options (go tags, npm script selection) slot into the
    same shape. An absent section/key -- or an empty/falsy markers value --
    yields no arguments, keeping the pytest invocation byte-identical to the
    no-config case. Structural validation (unknown keys, non-string/empty
    markers) is enforced upstream by ``config.validate_test_config``.
    """
    markers = (config or {}).get("test", {}).get("pypi", {}).get("markers")
    if markers:
        return ["-m", markers]
    return []


def resolve_test_timeout(config: dict | None, check_timeout: int | None) -> int:
    """Resolve the subprocess budget for a test run.

    An explicit *check_timeout* from the caller wins; otherwise the project's
    configured check timeout applies.
    """
    if check_timeout is not None:
        return check_timeout
    return get_check_timeout(config)


def run_project_tests(
    target_name: str,
    *,
    project_dir: str | None = None,
    workspace_root: str | None = None,
    skip_sync: bool = False,
    config: dict | None = None,
):
    """Run the built-in test suite for the given target.

    Args:
        target_name: registry/target identifier (e.g., "pypi", "go", "npm").
        project_dir: working directory for subprocess calls. None means cwd.
        workspace_root: uv workspace root for monorepos. When set, uv sync
            runs here instead of at project_dir.
        skip_sync: if True, skip the uv sync step (caller already synced).
        config: project config dict. Used to read uv_sync_verbose for pypi.

    Returns a ``SuiteRunOutcome``. Does NOT call sys.exit -- that is the
    caller's responsibility.

    The dispatch is the target protocol. This function used to be a chain of
    name comparisons ending in ``return True``, so a target with no runner --
    or a name that was not a target at all -- reported a PASSING test step for
    a suite that never ran. A target that cannot run tests now answers SKIPPED
    naming itself, and the caller renders that as a visible skip line.

    No dry-run parameter: the test suite reaches this through the impure
    ``test-suite`` check, which the check framework already lists rather than
    runs under ``--dry-run``. The parameter this function used to carry was
    never passed by any caller, and a second, hand-rolled skip would be a
    second place for the two answers to disagree.
    """
    from .targets import TARGETS
    from .targets.outcomes import SuiteRunOutcome, SuiteRunStatus

    target = TARGETS.get(target_name)
    if target is None:
        return SuiteRunOutcome(
            status=SuiteRunStatus.SKIPPED,
            message=f"'{target_name}' is not a registered release target",
        )

    if not target.has_builtin_test_runner:
        return target.run_tests(
            project_dir=project_dir,
            workspace_root=workspace_root,
            skip_sync=skip_sync,
            config=config,
        )

    print("Running tests...")
    return target.run_tests(
        project_dir=project_dir,
        workspace_root=workspace_root,
        skip_sync=skip_sync,
        config=config,
    )


def _run_pypi_tests(
    *,
    project_dir: str | None,
    workspace_root: str | None = None,
    skip_sync: bool = False,
    config: dict,
    check_timeout: int = 120,
) -> bool:
    """Run Python tests via uv or bare pytest.

    For workspace members: syncs at workspace root, then runs
    ``uv run python -P -m pytest``. For standalone projects: skips sync
    (``uv run`` handles it), uses ``_resolve_pytest_invocation`` to build the
    correct command. Falls back to ``python -P -m pytest`` when uv is not installed.

    When the project declares dev overlays and the sentinel agrees, every uv
    invocation here is overlay-preserving: the sync excludes the overlaid
    packages and ``uv run`` is given ``--no-sync``. Otherwise this runner would
    reinstall the locked registry wheels over the editable checkouts -- testing
    released code while destroying the state ``dev-overlay-drift`` then fails
    on. A declaration and a sentinel that disagree are a hard error, since
    neither mode is then true.
    """
    uv_verbose = config.get("uv_sync_verbose", False)
    effective_dir = project_dir or "."
    marker_args = _pytest_marker_args(config)

    try:
        overlays = active_overlays(effective_dir)
    except (OverlayModeConflictError, MalformedSentinelError) as e:
        print(str(e), file=sys.stderr)
        return False

    if require_tool("uv", fatal=False):
        is_workspace_member = (
            workspace_root is not None
            and detect_uv_workspace_root(effective_dir) is not None
        )
        # `uv run` auto-syncs (exactly) before running, which would undo the
        # overlay-preserving sync between the two commands. The flag scopes the
        # suppression to this one invocation; UV_NO_SYNC=1 in the environment
        # would be inherited by the test process and by every `uv run` a
        # fixture spawns -- the same reason the sandboxed runner uses the flag.
        run_args = ["--no-sync"] if overlays else []

        if is_workspace_member:
            # Workspace member: sync at workspace root, run uv run python -P -m pytest
            if not skip_sync:
                if not sync_workspace(
                    workspace_root,
                    verbose=uv_verbose,
                    check_timeout=check_timeout,
                    overlays=overlays,
                ):
                    return False
            cmd = ["uv", "run"] + run_args + ["python", "-P", "-m", "pytest"]
        else:
            # Standalone: uv run handles sync; resolve the right invocation
            cmd = _resolve_pytest_invocation(effective_dir, workspace_root)
            if overlays:
                # Standalone normally leans on `uv run`'s auto-sync, which is
                # exact and would wipe the overlays. Sync explicitly instead,
                # with the same groups/extras the suite runs with.
                sync_cmd = ["uv", "sync", "--inexact"]
                if not uv_verbose:
                    sync_cmd.append("--quiet")
                sync_cmd += _sync_selector_args(cmd) + _overlay_exclusions(overlays)
                try:
                    sync_result = effects.run(
                        sync_cmd, cwd=project_dir, timeout=check_timeout
                    )
                except subprocess.TimeoutExpired:
                    print(f"Error: command timed out after {check_timeout}s: {sync_cmd} {CHECK_TIMEOUT_HINT}", file=sys.stderr)
                    return False
                if sync_result.returncode != 0:
                    print(
                        "Error: overlay-preserving uv sync failed "
                        f"(exit {sync_result.returncode}); tests were not run.",
                        file=sys.stderr,
                    )
                    return False
                cmd = cmd[:2] + run_args + cmd[2:]

        cmd = cmd + marker_args
        try:
            result = effects.run(cmd, cwd=project_dir, timeout=check_timeout)
        except subprocess.TimeoutExpired:
            print(f"Error: command timed out after {check_timeout}s: {cmd} {CHECK_TIMEOUT_HINT}", file=sys.stderr)
            return False
    elif require_tool("pytest", fatal=False):
        fallback_cmd = ["python", "-P", "-m", "pytest"] + marker_args
        try:
            result = effects.run(fallback_cmd, cwd=project_dir, timeout=check_timeout)
        except subprocess.TimeoutExpired:
            print(f"Error: command timed out after {check_timeout}s: {fallback_cmd} {CHECK_TIMEOUT_HINT}", file=sys.stderr)
            return False
    else:
        print(
            "Error: neither uv nor pytest is installed; cannot run tests.",
            file=sys.stderr,
        )
        return False

    return result.returncode == 0


def _run_go_tests(*, project_dir: str | None, check_timeout: int = 120) -> bool:
    """Run Go tests."""
    cmd = ["go", "test", "./...", "-race", "-short", "-count=1"]
    try:
        result = effects.run(cmd, cwd=project_dir, timeout=check_timeout)
    except subprocess.TimeoutExpired:
        print(f"Error: command timed out after {check_timeout}s: {cmd} {CHECK_TIMEOUT_HINT}", file=sys.stderr)
        return False
    return result.returncode == 0


def _run_maven_tests(*, project_dir: str | None, check_timeout: int = 120) -> bool:
    """Run Maven/Gradle tests.

    Prefers ./gradlew test if gradlew exists, otherwise falls back to mvn test
    if pom.xml exists.
    """
    effective_dir = project_dir or "."
    gradlew = os.path.join(effective_dir, "gradlew")
    if os.path.exists(gradlew):
        cmd = ["./gradlew", "test"]
        try:
            result = effects.run(cmd, cwd=project_dir, timeout=check_timeout)
        except subprocess.TimeoutExpired:
            print(f"Error: command timed out after {check_timeout}s: {cmd} {CHECK_TIMEOUT_HINT}", file=sys.stderr)
            return False
        except FileNotFoundError:
            print("Error: gradle (./gradlew) is not runnable; cannot run gradle tests.", file=sys.stderr)
            return False
        return result.returncode == 0

    pom_path = os.path.join(effective_dir, "pom.xml")
    if os.path.exists(pom_path):
        cmd = ["mvn", "test"]
        try:
            result = effects.run(cmd, cwd=project_dir, timeout=check_timeout)
        except subprocess.TimeoutExpired:
            print(f"Error: command timed out after {check_timeout}s: {cmd} {CHECK_TIMEOUT_HINT}", file=sys.stderr)
            return False
        except FileNotFoundError:
            print("Error: mvn (Maven) is not installed; cannot run maven tests.", file=sys.stderr)
            return False
        return result.returncode == 0

    print(
        "Error: no gradlew or pom.xml found; cannot run maven/gradle tests.",
        file=sys.stderr,
    )
    return False


def _run_npm_tests(*, project_dir: str | None, check_timeout: int = 120) -> bool:
    """Run npm tests if a test script is defined in package.json."""
    pkg_path = os.path.join(project_dir, "package.json") if project_dir else "package.json"
    if not os.path.exists(pkg_path):
        print(
            f"Error: no package.json found at {pkg_path}; cannot run npm tests.",
            file=sys.stderr,
        )
        return False

    try:
        with open(pkg_path, "r", encoding="utf-8") as f:
            pkg = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"Error: could not read {pkg_path}: {exc}", file=sys.stderr)
        return False

    if not pkg.get("scripts", {}).get("test"):
        # Nothing declared to run is a legitimate pass.
        print("No test script in package.json, skipping tests.")
        return True

    npm_cmd = ["npm", "test"]
    try:
        result = effects.run(npm_cmd, cwd=project_dir, timeout=check_timeout)
    except subprocess.TimeoutExpired:
        print(f"Error: command timed out after {check_timeout}s: {npm_cmd} {CHECK_TIMEOUT_HINT}", file=sys.stderr)
        return False
    except FileNotFoundError:
        print("Error: npm is not installed; cannot run npm tests.", file=sys.stderr)
        return False
    return result.returncode == 0
