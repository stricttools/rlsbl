"""Standalone build_assets functions for each ecosystem that compile platform-specific artifacts and return sorted output file paths for upload.

Each function takes a project directory, version, and dist directory,
builds the appropriate artifacts, and returns a sorted list of output
file paths. These are self-contained -- they read manifest files directly
instead of depending on target classes.
"""

import glob
import os
import shutil
import subprocess

import tomlkit

from ..utils import run


def build_npm_assets(dir_path: str, version: str, dist_dir: str) -> list[str]:
    """Pack an npm tarball into *dist_dir* and return the list of .tgz paths."""
    os.makedirs(dist_dir, exist_ok=True)
    run("npm", ["pack", "--pack-destination", dist_dir], cwd=dir_path)
    return sorted(glob.glob(os.path.join(dist_dir, "*.tgz")))


def build_pypi_assets(dir_path: str, version: str, dist_dir: str) -> list[str]:
    """Build sdist and wheel into *dist_dir* and return the list of artifact paths."""
    os.makedirs(dist_dir, exist_ok=True)
    run("uv", ["build", "--out-dir", dist_dir], env=os.environ, cwd=dir_path)
    return sorted(glob.glob(os.path.join(dist_dir, "*")))


def build_go_assets(dir_path: str, version: str, dist_dir: str) -> list[str]:
    """Build Go binaries into *dist_dir*.

    Uses goreleaser for cross-compilation when available, falling back
    to ``go build`` (host platform only) otherwise.
    """
    os.makedirs(dist_dir, exist_ok=True)

    if shutil.which("goreleaser"):
        try:
            return _build_go_with_goreleaser(dir_path, dist_dir)
        except (subprocess.CalledProcessError, OSError) as exc:
            print(f"Warning: goreleaser failed ({exc}), falling back to go build.")
    else:
        print("goreleaser not found, building for host platform only.")

    # Fallback: host-only build
    run("go", ["build", "-o", dist_dir + "/", "./..."], cwd=dir_path)
    return sorted(glob.glob(os.path.join(dist_dir, "*")))


def _build_go_with_goreleaser(dir_path: str, dist_dir: str) -> list[str]:
    """Run goreleaser build and collect cross-compiled binaries into *dist_dir*."""
    run(
        "goreleaser",
        ["build", "--snapshot", "--clean"],
        cwd=dir_path,
    )
    goreleaser_dist = os.path.join(dir_path, "dist")
    artifacts = []
    for direntry in sorted(os.scandir(goreleaser_dist), key=lambda e: e.name):
        if not direntry.is_dir():
            continue
        for fentry in sorted(os.scandir(direntry.path), key=lambda e: e.name):
            if fentry.is_file() and not fentry.name.startswith("."):
                dest = os.path.join(dist_dir, f"{direntry.name}__{fentry.name}")
                shutil.copy2(fentry.path, dest)
                artifacts.append(dest)
    return sorted(artifacts)


def _read_cargo_name(dir_path: str) -> str:
    """Read the package name from Cargo.toml."""
    cargo_path = os.path.join(dir_path, "Cargo.toml")
    with open(cargo_path, "r", encoding="utf-8") as f:
        doc = tomlkit.parse(f.read())
    pkg = doc.get("package", {})
    name = pkg.get("name")
    return str(name) if name is not None else ""


def build_cargo_assets(dir_path: str, version: str, dist_dir: str) -> list[str]:
    """Build Rust binary in release mode and copy to *dist_dir*."""
    os.makedirs(dist_dir, exist_ok=True)
    run("cargo", ["build", "--release"], cwd=dir_path)

    name = _read_cargo_name(dir_path)
    target_release = os.path.join(dir_path, "target", "release", name)
    if os.path.isfile(target_release):
        shutil.copy2(target_release, dist_dir)

    return sorted(glob.glob(os.path.join(dist_dir, "*")))
