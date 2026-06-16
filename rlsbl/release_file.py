"""Release file reader and validator for file-based releases.

Instead of passing bump type on the CLI, the user creates
.rlsbl/releases/unreleased.toml describing the release.
This module reads and validates that file's internal consistency.
"""

import os
import sys
from dataclasses import dataclass, field

import tomlkit

from .errors import ReleaseFileError


VALID_BUMP_TYPES = ("patch", "minor", "major")

VALID_TARGET_MODES = ("ota", "build")


@dataclass
class ReleaseConfig:
    bump: str  # "patch", "minor", "major"
    include: list[str]  # target names to release
    exclude: list[str]  # target names to skip
    targets: dict[str, dict] = field(default_factory=dict)  # per-target config
    description: str = ""  # short description of this release
    context: str = ""  # optional context explaining why these changes were made
    blog: bool = False


def get_release_file_path(project_dir: str = ".") -> str:
    """Return the path to .rlsbl/releases/unreleased.toml relative to project_dir."""
    return os.path.join(project_dir, ".rlsbl", "releases", "unreleased.toml")


def _validate_release_config(data: dict, prefix: str = "") -> ReleaseConfig:
    """Validate release config fields from a parsed TOML dict.

    Shared validation for both single-project and batch (per-package) release
    configs. The prefix is prepended to all error messages -- empty string for
    single-project, "[packages.<name>] " for batch.

    Raises ReleaseFileError for schema/validation failures.
    Returns a ReleaseConfig on success.
    """
    def err(msg: str) -> ReleaseFileError:
        return ReleaseFileError(f"{prefix}{msg}")

    # --- bump ---
    if "bump" not in data:
        raise err("missing required field: bump")
    bump = data["bump"]
    if not isinstance(bump, str) or bump not in VALID_BUMP_TYPES:
        raise err(
            f"bump must be set to a valid value: invalid bump {bump!r} "
            f"(must be one of {VALID_BUMP_TYPES})"
        )

    # --- include ---
    if "include" not in data:
        raise err("missing required field: include")
    include = data["include"]
    if not isinstance(include, list) or not all(isinstance(s, str) for s in include):
        raise err("include must be a list of strings")

    # --- exclude ---
    if "exclude" not in data:
        raise err("missing required field: exclude")
    exclude = data["exclude"]
    if not isinstance(exclude, list) or not all(isinstance(s, str) for s in exclude):
        raise err("exclude must be a list of strings")

    # --- include ∩ exclude must be empty ---
    overlap = set(include) & set(exclude)
    if overlap:
        raise err(
            f"targets appear in both include and exclude: {sorted(overlap)}"
        )

    # --- targets section ---
    targets_raw = data.get("targets", {})
    targets = {}
    if targets_raw:
        if not isinstance(targets_raw, dict):
            raise err("targets must be a table of per-target configurations")
        include_set = set(include)
        for name, cfg in targets_raw.items():
            if name not in include_set:
                raise err(
                    f"target config for {name!r} but it is not in include"
                )
            if not isinstance(cfg, dict):
                raise err(f"target config for {name!r} must be a table")
            # Validate known fields
            for key, value in cfg.items():
                if key == "mode":
                    if value not in VALID_TARGET_MODES:
                        raise err(
                            f"invalid mode for target {name!r}: {value!r} "
                            f"(must be one of {VALID_TARGET_MODES})"
                        )
                else:
                    raise err(
                        f"unknown field {key!r} in target config for {name!r}"
                    )
            targets[name] = dict(cfg)

    # Flutter target requires a mode field in its per-target config
    for name in include:
        if name == "flutter":
            if name not in targets or "mode" not in targets[name]:
                raise err(
                    f"Flutter target {name!r} requires a [targets.{name}] section "
                    f"with mode = \"ota\" or mode = \"build\""
                )

    # --- description (required) ---
    if "description" not in data:
        raise err("missing required field: description")
    description = data["description"]
    if not isinstance(description, str):
        raise err("description must be a string")
    if not description.strip():
        raise err("description must be set (a short summary of this release)")

    # --- context (optional) ---
    context = data.get("context", "")
    if not isinstance(context, str):
        raise err("context must be a string")

    # --- blog (optional) ---
    blog = data.get("blog", False)
    if not isinstance(blog, bool):
        raise err("blog must be a boolean")

    return ReleaseConfig(
        bump=bump,
        include=list(include),
        exclude=list(exclude),
        targets=targets,
        description=description.strip(),
        context=context.strip(),
        blog=blog,
    )


def read_release_file(path: str) -> ReleaseConfig:
    """Read and validate a release TOML file.

    Raises FileNotFoundError if the file doesn't exist.
    Raises ReleaseFileError for schema/validation failures.
    """
    with open(path, "r", encoding="utf-8") as f:
        data = tomlkit.load(f)

    return _validate_release_config(data)


def unfinalize_release_file(releases_dir: str, version: str) -> list[str]:
    """Reverse a release-file finalization: restore vX.Y.Z.toml to unreleased.toml.

    Inverse of the finalization step in `release run`, which renames
    unreleased.toml to vX.Y.Z.toml, chmods it read-only (0o444), and creates
    a fresh empty unreleased.toml.

    1. No-op (returns []) if the versioned file doesn't exist.
    2. If unreleased.toml exists with content that differs from the versioned
       file (finalization only ever writes an empty file, so anything else is
       user content), warns on stderr and skips -- nothing is deleted.
    3. Otherwise removes the fresh unreleased.toml, makes the versioned file
       writable, and renames it back to unreleased.toml.

    Returns the list of changed file paths (for committing).
    """
    versioned = os.path.join(releases_dir, f"v{version}.toml")
    unreleased = os.path.join(releases_dir, "unreleased.toml")

    if not os.path.isfile(versioned):
        return []

    if os.path.isfile(unreleased):
        with open(unreleased, "r", encoding="utf-8") as f:
            unreleased_content = f.read()
        if unreleased_content != "":
            with open(versioned, "r", encoding="utf-8") as f:
                versioned_content = f.read()
            if unreleased_content != versioned_content:
                print(
                    f"warning: {unreleased} has user content that differs "
                    f"from {versioned}; leaving both files in place. Restore "
                    f"the release file manually if needed.",
                    file=sys.stderr,
                )
                return []
        os.unlink(unreleased)

    os.chmod(versioned, 0o644)
    os.rename(versioned, unreleased)
    return [unreleased, versioned]


@dataclass
class BatchReleaseConfig:
    """Configuration from a batch release TOML file (monorepo)."""

    packages: dict[str, ReleaseConfig]  # package name -> config


def get_batch_release_file_path(workspace_root: str = ".") -> str:
    """Return the path to .rlsbl-monorepo/releases/unreleased.toml."""
    return os.path.join(workspace_root, ".rlsbl-monorepo", "releases", "unreleased.toml")


def read_batch_release_file(path: str) -> BatchReleaseConfig:
    """Read and validate a batch release TOML file.

    Expects [packages.<name>] sections, each with the same fields
    as a single ReleaseConfig (bump, include, exclude, optional targets).

    Raises FileNotFoundError if the file doesn't exist.
    Raises ReleaseFileError for schema/validation failures.
    """
    with open(path, "r", encoding="utf-8") as f:
        data = tomlkit.load(f)

    if "packages" not in data:
        raise ReleaseFileError("missing required section: [packages]")

    packages_raw = data["packages"]
    if not isinstance(packages_raw, dict):
        raise ReleaseFileError("[packages] must be a table of package configurations")

    if not packages_raw:
        raise ReleaseFileError("[packages] is empty -- at least one package is required")

    packages = {}
    for pkg_name, pkg_data in packages_raw.items():
        if not isinstance(pkg_data, dict):
            raise ReleaseFileError(
                f"[packages.{pkg_name}] must be a table"
            )

        packages[pkg_name] = _validate_release_config(
            pkg_data, prefix=f"[packages.{pkg_name}] "
        )

    return BatchReleaseConfig(packages=packages)


# ---------------------------------------------------------------------------
# Retry file
# ---------------------------------------------------------------------------


@dataclass
class RetryConfig:
    """Configuration from a retry TOML file (.rlsbl/releases/retry.toml)."""

    version: str  # version to retry (mandatory)
    dispatch: list[str]  # workflow filenames to dispatch, e.g. ["publish.yml"]
    ref: str  # git ref for CI dispatch, defaults to tag


def get_retry_file_path(project_dir: str = ".") -> str:
    """Return the path to .rlsbl/releases/retry.toml relative to project_dir."""
    return os.path.join(project_dir, ".rlsbl", "releases", "retry.toml")


def read_retry_file(path: str) -> RetryConfig:
    """Read and validate a retry TOML file.

    Raises FileNotFoundError if the file doesn't exist.
    Raises ReleaseFileError for schema/validation failures.
    """
    with open(path, "r", encoding="utf-8") as f:
        data = tomlkit.load(f)

    # --- version ---
    if "version" not in data:
        raise ReleaseFileError("missing required field: version")
    version = data["version"]
    if not isinstance(version, str) or not version.strip():
        raise ReleaseFileError("version must be a non-empty string")

    # --- dispatch ---
    if "dispatch" not in data:
        raise ReleaseFileError("missing required field: dispatch")
    dispatch = data["dispatch"]
    if not isinstance(dispatch, list) or not all(isinstance(s, str) for s in dispatch):
        raise ReleaseFileError("dispatch must be a list of strings")
    if not dispatch:
        raise ReleaseFileError("dispatch must be non-empty")

    # --- ref ---
    if "ref" not in data:
        raise ReleaseFileError("missing required field: ref")
    ref = data["ref"]
    if not isinstance(ref, str) or not ref.strip():
        raise ReleaseFileError("ref must be set in retry.toml (e.g. a tag like v1.2.3 or a branch like main)")

    return RetryConfig(
        version=version.strip(),
        dispatch=list(dispatch),
        ref=ref.strip(),
    )
