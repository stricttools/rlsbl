"""Claim a package name on npm, PyPI, or crates.io by publishing a minimal placeholder package with version 0.0.0 and an empty description."""

import json
import os
import shutil
import subprocess
import sys
import tempfile


def _read_cargo_token():
    """Read the crates.io API token from ~/.cargo/credentials.toml.

    Returns the token string, or None if the file does not exist or
    has no token entry.
    """
    creds_path = os.path.expanduser("~/.cargo/credentials.toml")
    if not os.path.exists(creds_path):
        return None
    try:
        import tomllib
        with open(creds_path, "rb") as f:
            data = tomllib.load(f)
        registry = data.get("registry", {})
        return registry.get("token")
    except Exception:
        return None


def run_cmd(target, args, flags):
    """Claim a package name on a registry by publishing a minimal placeholder.

    Checks availability first via check-name, then publishes a version 0.0.0
    placeholder package to reserve the name. Supports npm (via npm publish),
    PyPI (via uv build + uv publish), and crates.io (via cargo publish).
    Requires NPM_TOKEN for npm, PYPI_TOKEN / UV_PUBLISH_TOKEN for PyPI, or
    ~/.cargo/credentials.toml for crates.io.
    When --yes is passed, proceeds even if the name appears taken or the
    availability check returns an ambiguous status.
    """

    if len(args) != 1:
        print("Expected exactly one package name.", file=sys.stderr)
        sys.exit(1)

    name = args[0]

    if target not in ("npm", "pypi", "crates"):
        print(f"Unsupported target: {target!r}. Must be 'npm', 'pypi', or 'crates'.", file=sys.stderr)
        sys.exit(1)

    from rlsbl.commands.check import _check_single_name

    result = _check_single_name(name, target)
    status = result["status"]

    if status == "available":
        pass
    elif status == "taken":
        detail = result.get("note") or result.get("reason", "unknown reason")
        print(f"Name '{name}' appears taken on {target}: {detail}.", file=sys.stderr)
        if flags["yes"]:
            print("--yes passed, attempting publish anyway...")
        else:
            sys.exit(1)
    elif status == "error":
        error = result.get("error", "unknown error")
        print(f"Error checking '{name}' on {target}: {error}", file=sys.stderr)
        sys.exit(2)
    else:
        print(f"Ambiguous status '{status}' for '{name}' on {target}.", file=sys.stderr)
        if flags["yes"]:
            print("--yes passed, attempting publish anyway...")
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
    elif target == "crates":
        token = _read_cargo_token()
        if not token:
            print(
                "No crates.io token found. Run `cargo login` first to store "
                "your token in ~/.cargo/credentials.toml.",
                file=sys.stderr,
            )
            sys.exit(1)
        # Permanence confirmation: crates.io names are permanent
        if not flags["yes"]:
            print(
                f"WARNING: crates.io names are PERMANENT and cannot be deleted or unpublished.\n"
                f"Claim '{name}'? [y/N] ",
                end="",
                flush=True,
            )
            answer = input().strip().lower()
            if answer not in ("y", "yes"):
                print("Aborted.", file=sys.stderr)
                sys.exit(1)

    tmpdir = tempfile.mkdtemp()
    try:
        if target == "npm":
            _claim_npm(name, tmpdir)
        elif target == "pypi":
            _claim_pypi(name, tmpdir)
        elif target == "crates":
            _claim_crates(name, tmpdir)
    except subprocess.CalledProcessError as e:
        print(e.stderr, file=sys.stderr)
        sys.exit(1)
    finally:
        shutil.rmtree(tmpdir)


def _claim_npm(name, tmpdir):
    package_json = {
        "name": name,
        "version": "0.0.0",
        "description": "Name reservation",
    }
    with open(os.path.join(tmpdir, "package.json"), "w") as f:
        json.dump(package_json, f, indent=2)
        f.write("\n")

    subprocess.run(
        ["npm", "publish", "--access", "public"],
        cwd=tmpdir,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    print(f"Successfully claimed '{name}' on npm: https://www.npmjs.com/package/{name}")


def _claim_pypi(name, tmpdir):
    name_underscored = name.replace("-", "_")
    pkg_dir = os.path.join(tmpdir, name_underscored)
    os.makedirs(pkg_dir)

    with open(os.path.join(pkg_dir, "__init__.py"), "w") as f:
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
    with open(os.path.join(tmpdir, "pyproject.toml"), "w") as f:
        f.write(pyproject_toml)

    subprocess.run(
        ["uv", "build"],
        cwd=tmpdir,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )

    token = os.environ.get("UV_PUBLISH_TOKEN") or os.environ["PYPI_TOKEN"]

    subprocess.run(
        ["uv", "publish", "--token", token],
        cwd=tmpdir,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    print(f"Successfully claimed '{name}' on PyPI: https://pypi.org/project/{name}/")


def _claim_crates(name, tmpdir):
    cargo_toml = f"""\
[package]
name = "{name}"
version = "0.0.0"
edition = "2021"
description = "Name placeholder"
license = "MIT"
"""
    with open(os.path.join(tmpdir, "Cargo.toml"), "w") as f:
        f.write(cargo_toml)

    src_dir = os.path.join(tmpdir, "src")
    os.makedirs(src_dir)
    with open(os.path.join(src_dir, "lib.rs"), "w") as f:
        f.write("//! Name placeholder crate.\n")

    subprocess.run(
        ["cargo", "publish", "--allow-dirty"],
        cwd=tmpdir,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    print(f"Successfully claimed '{name}' on crates.io: https://crates.io/crates/{name}")
