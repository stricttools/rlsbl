"""Claim a package name on npm or PyPI by publishing a minimal placeholder package with version 0.0.0 and an empty description."""

import json
import os
import subprocess
import sys
import tempfile
from .. import effects


def run_cmd(target, args, flags):
    """Claim a package name on a registry by publishing a minimal placeholder.

    Checks availability first via check-name, then publishes a version 0.0.0
    placeholder package to reserve the name. Supports npm (via npm publish),
    and PyPI (via uv build + uv publish). Requires NPM_TOKEN for npm, or
    PYPI_TOKEN / UV_PUBLISH_TOKEN for PyPI.
    When --force-publish is passed, proceeds even if the name appears taken or
    the availability check returns an ambiguous status. That is a decision
    about the availability CHECK, deliberately not the framework's
    --approve-consequential, which only skips the confirmation prompt: skipping
    a prompt must never also override a "this name is taken" refusal.
    """

    if len(args) != 1:
        print("Expected exactly one package name.", file=sys.stderr)
        sys.exit(1)

    name = args[0]

    if target not in ("npm", "pypi"):
        print(f"Unsupported target: {target!r}. Must be 'npm' or 'pypi'.", file=sys.stderr)
        sys.exit(1)

    from rlsbl.commands.check import _check_single_name

    result = _check_single_name(name, target)
    status = result["status"]

    if status == "available":
        pass
    elif status == "taken":
        detail = result.get("note") or result.get("reason", "unknown reason")
        print(f"Name '{name}' appears taken on {target}: {detail}.", file=sys.stderr)
        if flags["force-publish"]:
            print("--force-publish passed, attempting publish anyway...")
        else:
            sys.exit(1)
    elif status == "error":
        error = result.get("error", "unknown error")
        print(f"Error checking '{name}' on {target}: {error}", file=sys.stderr)
        sys.exit(2)
    else:
        print(f"Ambiguous status '{status}' for '{name}' on {target}.", file=sys.stderr)
        if flags["force-publish"]:
            print("--force-publish passed, attempting publish anyway...")
        else:
            sys.exit(1)

    if target == "npm":
        if "NPM_TOKEN" not in os.environ:
            print("NPM_TOKEN environment variable is not set.", file=sys.stderr)
            sys.exit(1)
    elif target == "pypi":
        if "PYPI_TOKEN" not in os.environ and "UV_PUBLISH_TOKEN" not in os.environ:
            print("Neither PYPI_TOKEN nor UV_PUBLISH_TOKEN environment variable is set.", file=sys.stderr)
            sys.exit(1)

    # No hand-rolled preview and no hand-rolled prompt: `claim-name` declares
    # itself `consequential`, so strictcli asks for confirmation before
    # dispatch (--approve-consequential skips it), and under --dry-run every
    # write and every publish below is RECORDED instead of performed.  The
    # preview is therefore the real code path -- the incident this command is
    # named for (a "dry run" that published for real) cannot recur by a branch
    # being forgotten.

    tmpdir = tempfile.mkdtemp()
    try:
        if target == "npm":
            _claim_npm(name, tmpdir)
        elif target == "pypi":
            _claim_pypi(name, tmpdir)
    except subprocess.CalledProcessError as e:
        print(e.stderr, file=sys.stderr)
        sys.exit(1)
    finally:
        effects.rmtree(tmpdir)


def _claim_npm(name, tmpdir):
    package_json = {
        "name": name,
        "version": "0.0.0",
        "description": "Name reservation",
    }
    with effects.open_write(os.path.join(tmpdir, "package.json"), "w") as f:
        json.dump(package_json, f, indent=2)
        f.write("\n")

    effects.run(
        ["npm", "publish", "--access", "public"],
        grant="publish",
        resource=f"npm:{name}",
        cwd=tmpdir,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    if not effects.previewing():
        # A preview published nothing, so it must not claim it did; the
        # would-do log the framework prints is the preview's own report.
        print(f"Successfully claimed '{name}' on npm: https://www.npmjs.com/package/{name}")


def _claim_pypi(name, tmpdir):
    name_underscored = name.replace("-", "_")
    pkg_dir = os.path.join(tmpdir, name_underscored)
    effects.makedirs(pkg_dir)

    with effects.open_write(os.path.join(pkg_dir, "__init__.py"), "w") as f:
        pass

    pyproject_toml = f"""\
[project]
name = "{name}"
version = "0.0.0"
description = "Name reservation"
requires-python = ">=3.11"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
"""
    with effects.open_write(os.path.join(tmpdir, "pyproject.toml"), "w") as f:
        f.write(pyproject_toml)

    effects.run(
        ["uv", "build"],
        cwd=tmpdir,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )

    # The token rides the environment, never argv: it must not reach a process
    # listing, and it must not reach the would-do log a preview prints.
    token = os.environ.get("UV_PUBLISH_TOKEN") or os.environ["PYPI_TOKEN"]

    effects.run(
        ["uv", "publish"],
        grant="publish",
        resource=f"pypi:{name}",
        env={**os.environ, "UV_PUBLISH_TOKEN": token},
        cwd=tmpdir,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    if not effects.previewing():
        print(f"Successfully claimed '{name}' on PyPI: https://pypi.org/project/{name}/")
