"""Toolchain-backed Go project introspection via ``go list``, classifying projects as library or binary and resolving module paths and import dependencies.

Single source of truth for project-level Go classification (library vs
binary, entry-point location). Hand-rolled main.go globbing misdetects
real layouts: entry files not named main.go (cmd/x/cli.go), _test.go
files in package main dirs, and broken-root layouts (a root ``package
main`` file with no ``func main`` next to a real main under cmd/). The
go toolchain handles all of these correctly, so it is the source of
truth -- callers must never fall back to file scanning.
"""

import os
import shutil
import subprocess
from dataclasses import dataclass

from .errors import RlsblError

_GO_LIST_TIMEOUT = 60


class GoIntrospectError(RlsblError):
    """Go project introspection failed (missing toolchain, missing go.mod,
    go list failure, or an unresolvable main-package layout)."""


@dataclass(frozen=True)
class GoPackage:
    """A Go package as reported by ``go list``."""

    name: str
    import_path: str
    rel_dir: str  # relative to the project root: "." or "./cmd/x"


def list_packages(project_dir: str) -> list[GoPackage]:
    """Enumerate all packages in the Go module rooted at ``project_dir``.

    Runs ``go list -e -f ... ./...``. The ``-e`` flag reports packages
    even when their imports don't resolve (no network or go.sum needed);
    error pseudo-packages (empty package name) are skipped.

    Raises GoIntrospectError if the go binary is missing, ``project_dir``
    has no go.mod, or ``go list`` fails. Never silently returns an empty
    list for those conditions -- callers decide fatality.
    """
    if shutil.which("go") is None:
        raise GoIntrospectError(
            "'go' not found on PATH: the Go toolchain is required to "
            f"introspect the Go project at {project_dir}"
        )
    if not os.path.isfile(os.path.join(project_dir, "go.mod")):
        raise GoIntrospectError(
            f"no go.mod found in {project_dir}: cannot introspect packages "
            "in a module-less directory"
        )
    result = subprocess.run(
        ["go", "list", "-e", "-f", "{{.Name}}\t{{.ImportPath}}\t{{.Dir}}", "./..."],
        cwd=project_dir,
        capture_output=True,
        text=True,
        timeout=_GO_LIST_TIMEOUT,
    )
    if result.returncode != 0:
        raise GoIntrospectError(
            f"'go list ./...' failed in {project_dir} "
            f"(exit {result.returncode}):\n{result.stderr.strip()}"
        )

    root = os.path.realpath(project_dir)
    packages: list[GoPackage] = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        name, import_path, pkg_dir = parts
        if not name:
            # -e emits error pseudo-packages with an empty Name; skip them.
            continue
        rel = os.path.relpath(os.path.realpath(pkg_dir), root)
        rel_dir = "." if rel == "." else "./" + rel.replace(os.sep, "/")
        packages.append(GoPackage(name=name, import_path=import_path, rel_dir=rel_dir))
    return packages


def list_main_packages(project_dir: str) -> list[GoPackage]:
    """Enumerate ``package main`` packages (binaries) in a project."""
    return [p for p in list_packages(project_dir) if p.name == "main"]


def go_pipeline_install_paths(config: dict) -> list[str] | None:
    """Read ``install_paths`` declared on the go-type pipeline entry.

    Returns the declared list, or None when no go pipeline declares one.
    Raises GoIntrospectError if the declaration is not a non-empty list
    of strings.
    """
    for name, entry in (config.get("pipelines") or {}).items():
        if not isinstance(entry, dict) or entry.get("type") != "go":
            continue
        paths = entry.get("install_paths")
        if paths is None:
            continue
        if (
            not isinstance(paths, list)
            or not paths
            or not all(isinstance(p, str) and p for p in paths)
        ):
            raise GoIntrospectError(
                f"pipeline '{name}'.install_paths must be a non-empty list of "
                f"strings (e.g. [\"./cmd/x\"]), got {paths!r}"
            )
        return list(paths)
    return None


def _normalize_rel(path: str) -> str:
    """Normalize a declared install path to the helper's rel_dir form."""
    norm = os.path.normpath(path).replace(os.sep, "/")
    return "." if norm == "." else "./" + norm.lstrip("./")


def validate_install_paths(project_dir: str, paths: list[str]) -> list[str]:
    """Validate that every declared install path is a main package.

    Returns the normalized paths. Raises GoIntrospectError naming the
    offending path and listing what ``go list`` actually detected.
    """
    mains = list_main_packages(project_dir)
    main_dirs = {p.rel_dir for p in mains}
    normalized = [_normalize_rel(p) for p in paths]
    for original, norm in zip(paths, normalized):
        if norm not in main_dirs:
            raise GoIntrospectError(
                f"declared install path '{original}' is not a main package. "
                f"{describe_main_packages(mains)}"
            )
    return normalized


def describe_main_packages(mains: list[GoPackage]) -> str:
    """Human-readable summary of detected main packages for error messages."""
    if not mains:
        return "go list detected no main packages in this project."
    listing = ", ".join(f'"{p.rel_dir}"' for p in mains)
    return f"go list detected main packages at: {listing}"


def resolve_main_package_dir(project_dir: str, config: dict) -> str:
    """Resolve THE main package dir for single-binary concerns
    (goreleaser ``main:``, version.go placement).

    Resolution order:
    1. ``install_paths`` declared on the go pipeline: must contain exactly
       one path, which must be a main package.
    2. No declaration: exactly one detected main package is used.

    Anything else (zero mains, multiple mains without a declaration,
    multiple declared paths) is a hard error listing what ``go list``
    detected, so fixing the config is a copy-paste.
    """
    declared = go_pipeline_install_paths(config)
    if declared is not None:
        normalized = validate_install_paths(project_dir, declared)
        if len(normalized) != 1:
            raise GoIntrospectError(
                "install_paths declares multiple main packages, but this "
                "operation (goreleaser main / version.go placement) needs "
                f"exactly one: {declared!r}"
            )
        return normalized[0]

    mains = list_main_packages(project_dir)
    if len(mains) == 1:
        return mains[0].rel_dir
    if not mains:
        raise GoIntrospectError(
            f"no main packages found in {project_dir}. "
            f"{describe_main_packages(mains)}"
        )
    raise GoIntrospectError(
        f"multiple main packages found in {project_dir} and no "
        "install_paths declared on the go pipeline in .rlsbl/config.json. "
        f"{describe_main_packages(mains)} "
        'Declare e.g. "install_paths": ["./cmd/x"] to disambiguate.'
    )
