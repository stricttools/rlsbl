"""Local editable overlays for `rlsbl dev sync`, installing sibling project checkouts as editable packages without committing path dependencies.

Overlays sibling checkouts (e.g. ../strictcli/python) onto the project's
locked environment without committing machine-local [tool.uv.sources] path
dependencies. Driven by a git-invisible TOML file at the project root
(dev-sources.toml.local-only -- the scaffold gitignore ignores *.local-only).

Why a wrapper is required (verified against uv 0.9.17):

- `uv pip install -e ../x` alone is wiped by the next `uv sync`: exact sync
  reinstalls the locked registry wheel even at equal versions.
- `uv sync --inexact --no-install-package <name>` preserves a pre-existing
  editable install; neither flag has an env-var equivalent.
- Bare `uv run` auto-syncs (and wipes overlays) unless UV_NO_SYNC=1 is set,
  hence the hard gate below.
"""

import os
import subprocess
import sys
import tomllib

from ..overlay_state import (
    MalformedSentinelError,
    OVERLAY_HEALTHY,
    OVERLAY_MISSING,
    OVERLAY_WIPED,
    SENTINEL_FILENAME,
    _normalize,
    classify_overlay,
    inspect_installed,
    load_sentinel,
)
from ..utils import require_tool

OVERRIDES_FILENAME = "dev-sources.toml.local-only"

_FILE_FORMAT_HOWTO = """\
Create {filename} at the project root (gitignored fleet-wide via the
*.local-only pattern) with one [[overlay]] block per local checkout:

    [[overlay]]
    package = "strictcli"          # distribution name, as uv knows it
    path = "../strictcli/python"   # absolute, or relative to the project root
"""

_UV_NO_SYNC_ERROR = """\
Error: UV_NO_SYNC is not set to 1 in the environment.

`rlsbl dev sync` refuses to run without it: any bare `uv run` auto-syncs the
environment, which reinstalls the locked registry wheels over the editable
overlays this command creates -- silently undoing them. With UV_NO_SYNC=1,
`uv run` skips that auto-sync and the overlays survive.

Set it permanently, then re-run:
    shell profile (~/.bashrc / ~/.zshrc):   export UV_NO_SYNC=1
    direnv (add to the project's .envrc):   export UV_NO_SYNC=1

(A bare `uv sync` still reverts overlays harmlessly; re-run
`rlsbl dev sync` to restore them.)
"""


def _fail(message):
    print(message, file=sys.stderr)
    return None


def _load_overlays(project_root):
    """Parse and validate the overlay file. Returns a list of
    {"package": str, "path": str (absolute), "version": str | None} dicts,
    or None after printing a hard error. Never a silent no-op."""
    file_path = os.path.join(project_root, OVERRIDES_FILENAME)
    howto = _FILE_FORMAT_HOWTO.format(filename=OVERRIDES_FILENAME)

    if not os.path.isfile(file_path):
        return _fail(
            f"Error: no {OVERRIDES_FILENAME} found in {project_root}.\n\n"
            "rlsbl dev sync overlays local editable checkouts onto this "
            "project's environment.\n" + howto
        )

    with open(file_path, "rb") as f:
        try:
            data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            return _fail(f"Error: invalid TOML in {OVERRIDES_FILENAME}: {e}")

    unknown_top = sorted(set(data) - {"overlay"})
    if unknown_top:
        return _fail(
            f"Error: unknown key(s) in {OVERRIDES_FILENAME}: "
            f"{', '.join(unknown_top)}. Only [[overlay]] blocks are allowed.\n\n"
            + howto
        )

    entries = data.get("overlay")
    if not entries:
        return _fail(
            f"Error: {OVERRIDES_FILENAME} declares no overlays.\n\n" + howto
        )
    if not isinstance(entries, list):
        return _fail(
            f"Error: 'overlay' in {OVERRIDES_FILENAME} must be an array of "
            "tables ([[overlay]] blocks).\n\n" + howto
        )

    overlays = []
    for i, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            return _fail(f"Error: [[overlay]] entry #{i} is not a table.\n\n" + howto)
        unknown = sorted(set(entry) - {"package", "path"})
        if unknown:
            return _fail(
                f"Error: unknown key(s) in [[overlay]] entry #{i}: "
                f"{', '.join(unknown)}. Allowed keys: package, path."
            )
        package = entry.get("package")
        path = entry.get("path")
        if not package or not isinstance(package, str):
            return _fail(
                f"Error: [[overlay]] entry #{i} is missing the 'package' key "
                "(the distribution name, as uv knows it)."
            )
        if not path or not isinstance(path, str):
            return _fail(
                f"Error: [[overlay]] entry #{i} ('{package}') is missing the "
                "'path' key (absolute, or relative to the project root)."
            )

        abs_path = path if os.path.isabs(path) else os.path.abspath(
            os.path.join(project_root, path)
        )
        if not os.path.isdir(abs_path):
            return _fail(
                f"Error: [[overlay]] entry '{package}': path does not exist: "
                f"{path} (resolved to {abs_path})."
            )
        pyproject_path = os.path.join(abs_path, "pyproject.toml")
        if not os.path.isfile(pyproject_path):
            return _fail(
                f"Error: [[overlay]] entry '{package}': {abs_path} is not an "
                "installable project (no pyproject.toml)."
            )

        with open(pyproject_path, "rb") as f:
            try:
                pyproject = tomllib.load(f)
            except tomllib.TOMLDecodeError as e:
                return _fail(
                    f"Error: [[overlay]] entry '{package}': invalid TOML in "
                    f"{pyproject_path}: {e}"
                )
        project_table = pyproject.get("project") or {}
        declared_name = project_table.get("name")
        if not declared_name:
            # Without a declared static name, the entry's 'package' cannot be
            # verified against what uv will actually install, so the
            # --no-install-package exclusion cannot be trusted to protect the
            # overlay. Hard error, never a silent skip of the guard.
            return _fail(
                f"Error: [[overlay]] entry '{package}': {pyproject_path} "
                "declares no [project].name. The overlay checkout must "
                "declare a static [project].name (PEP 621) matching the "
                "entry's 'package', or the sync exclusion cannot be verified "
                "to protect the overlay."
            )
        if _normalize(declared_name) != _normalize(package):
            # A mismatched name means --no-install-package would not match
            # and the next sync would silently wipe the overlay.
            return _fail(
                f"Error: [[overlay]] entry '{package}' does not match the "
                f"checkout's [project].name '{declared_name}' in "
                f"{pyproject_path}. The names must match (PEP 503 "
                "normalization applies), or the sync exclusion will not "
                "protect the overlay."
            )

        overlays.append(
            {
                "package": package,
                "path": abs_path,
                "version": project_table.get("version"),
            }
        )

    return overlays


def _write_sentinel(project_root, overlays):
    """Atomically record the intended overlay state after a successful sync.

    Writes SENTINEL_FILENAME alongside the overrides file (gitignored via the
    *.local-only pattern), capturing per overlaid package: its distribution
    name, the editable checkout path, and the overlaid version (read from the
    checkout's pyproject at sync time). The drift check and `rlsbl dev status`
    read this to detect when a later bare `uv sync`/`uv run` reinstalled the
    locked registry wheel over the overlay -- a silent wipe that would run the
    consuming project's tests against stale RELEASED dependency code.

    Atomic write (tmp + os.replace) per the codebase convention for shared
    state. *overlays* is the list returned by ``_load_overlays``.
    """
    import tomlkit

    doc = tomlkit.document()
    aot = tomlkit.aot()
    for overlay in overlays:
        table = tomlkit.table()
        table["package"] = overlay["package"]
        table["path"] = overlay["path"]
        # version is None for dynamic-version checkouts; record "" so the
        # round-trip is total (an absent key would be ambiguous with a
        # truncated/malformed sentinel).
        table["version"] = overlay.get("version") or ""
        aot.append(table)
    doc["overlay"] = aot

    target = os.path.join(str(project_root), SENTINEL_FILENAME)
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(tomlkit.dumps(doc))
    os.replace(tmp, target)


def run_status(project_root):
    """Entry point for `rlsbl dev status`. Prints the declared overlays and
    their actual venv state, then returns a process exit code: 1 if any
    declared overlay was wiped or is missing (scriptable), 0 otherwise --
    including when no overlays are declared (no sentinel)."""
    project_root = str(project_root)
    try:
        sentinel = load_sentinel(project_root)
    except MalformedSentinelError as e:
        print(str(e), file=sys.stderr)
        return 1
    if sentinel is None:
        print(
            f"No dev overlays declared (no {SENTINEL_FILENAME}). "
            "`rlsbl dev sync` writes it after overlaying local checkouts."
        )
        return 0
    if not sentinel:
        print(f"{SENTINEL_FILENAME} records no overlays.")
        return 0

    drifted = 0
    print(f"Dev overlays declared in {SENTINEL_FILENAME}:")
    for entry in sentinel:
        installed = inspect_installed(project_root, entry["package"])
        state, detail = classify_overlay(entry, installed)
        marker = {
            OVERLAY_HEALTHY: "ok",
            OVERLAY_WIPED: "WIPED",
            OVERLAY_MISSING: "MISSING",
        }[state]
        print(f"  [{marker}] {detail}")
        if state != OVERLAY_HEALTHY:
            drifted += 1

    if drifted:
        print(
            f"\n{drifted} of {len(sentinel)} overlay(s) drifted. Re-run "
            "`rlsbl dev sync` to restore editable installs.",
            file=sys.stderr,
        )
        return 1
    print(f"\nAll {len(sentinel)} overlay(s) intact.")
    return 0


def run_sync(project_root):
    """Entry point for `rlsbl dev sync`. Returns a process exit code."""
    project_root = str(project_root)

    # Hard gate: without UV_NO_SYNC=1, any bare `uv run` silently reinstalls
    # registry wheels over the overlays created below.
    if os.environ.get("UV_NO_SYNC") != "1":
        print(_UV_NO_SYNC_ERROR, file=sys.stderr)
        return 1

    # Overlays live at the sub-project root; a monorepo workspace root has no
    # environment of its own to overlay.
    if os.path.isdir(os.path.join(project_root, ".rlsbl-monorepo")):
        print(
            "Error: `rlsbl dev sync` must run inside a sub-project, not at "
            "the monorepo workspace root. cd into the sub-project (its "
            f"{OVERRIDES_FILENAME} lives at the sub-project root) and re-run.",
            file=sys.stderr,
        )
        return 1

    overlays = _load_overlays(project_root)
    if overlays is None:
        return 1

    if require_tool("uv", purpose="for rlsbl dev sync", fatal=False) is None:
        print("Error: uv not on PATH (required for rlsbl dev sync).", file=sys.stderr)
        return 1

    # Strip VIRTUAL_ENV so both commands deterministically target THIS
    # project's environment: `uv pip` prefers an active VIRTUAL_ENV over the
    # project's .venv (while `uv sync` ignores it), so a leaked venv from the
    # calling shell would silently split the two steps across environments.
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}

    # One sync invocation carrying ALL exclusions: --inexact keeps packages
    # uv did not install, --no-install-package skips the locked registry
    # wheel for each overlaid package.
    sync_cmd = ["uv", "sync", "--inexact"]
    for overlay in overlays:
        sync_cmd += ["--no-install-package", overlay["package"]]
    print(f"Syncing environment (excluding {len(overlays)} overlaid package(s))...")
    result = subprocess.run(sync_cmd, cwd=project_root, env=env)
    if result.returncode != 0:
        print(
            f"Error: uv sync failed (exit {result.returncode}); "
            "no overlays were installed.",
            file=sys.stderr,
        )
        return 1

    # Editable install per entry. Re-installing on every run is deliberate:
    # it picks up new transitive dependencies of the overlaid checkouts.
    for overlay in overlays:
        package = overlay["package"]
        version = overlay["version"] or "(dynamic version)"
        print(f"Overlaying {package} {version} from {overlay['path']}")
        result = subprocess.run(
            ["uv", "pip", "install", "-e", overlay["path"]],
            cwd=project_root,
            env=env,
        )
        if result.returncode != 0:
            print(
                f"Error: editable install of '{package}' from "
                f"{overlay['path']} failed (exit {result.returncode}).",
                file=sys.stderr,
            )
            return 1

    # Record the intended overlay state so the `dev-overlay-drift` check and
    # `rlsbl dev status` can later detect a silent wipe by a bare uv sync/run.
    _write_sentinel(project_root, overlays)

    print(
        f"Overlaid {len(overlays)} package(s). Note: a bare `uv sync` reverts "
        "overlays; re-run `rlsbl dev sync` to restore them (check with "
        "`rlsbl dev status`)."
    )
    return 0
