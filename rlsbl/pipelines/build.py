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

from ..utils import run
from .. import effects


def build_npm_assets(dir_path: str, version: str, dist_dir: str) -> list[str]:
    """Pack an npm tarball into *dist_dir* and return the list of .tgz paths."""
    effects.makedirs(dist_dir, exist_ok=True)
    run("npm", ["pack", "--pack-destination", dist_dir], cwd=dir_path)
    return sorted(glob.glob(os.path.join(dist_dir, "*.tgz")))


def build_pypi_assets(dir_path: str, version: str, dist_dir: str) -> list[str]:
    """Build sdist and wheel into *dist_dir* and return the list of artifact paths."""
    effects.makedirs(dist_dir, exist_ok=True)
    run("uv", ["build", "--out-dir", dist_dir], env=os.environ, cwd=dir_path)
    return sorted(glob.glob(os.path.join(dist_dir, "*")))


def build_go_assets(dir_path: str, version: str, dist_dir: str) -> list[str]:
    """Build Go binaries into *dist_dir*.

    Uses goreleaser for cross-compilation when available, falling back
    to ``go build`` (host platform only) otherwise.
    """
    effects.makedirs(dist_dir, exist_ok=True)

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
                effects.copy_file(fentry.path, dest)
                artifacts.append(dest)
    return sorted(artifacts)
