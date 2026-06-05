"""Release file reader and validator for file-based releases.

Instead of passing bump type on the CLI, the user creates
.rlsbl/releases/unreleased.toml describing the release.
This module reads and validates that file's internal consistency.
"""

import os
from dataclasses import dataclass, field

import tomlkit


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


def get_release_file_path(project_dir: str = ".") -> str:
    """Return the path to .rlsbl/releases/unreleased.toml relative to project_dir."""
    return os.path.join(project_dir, ".rlsbl", "releases", "unreleased.toml")


def _validate_release_config(data: dict, prefix: str = "") -> ReleaseConfig:
    """Validate release config fields from a parsed TOML dict.

    Shared validation for both single-project and batch (per-package) release
    configs. The prefix is prepended to all error messages -- empty string for
    single-project, "[packages.<name>] " for batch.

    Raises ValueError for schema/validation failures.
    Returns a ReleaseConfig on success.
    """
    def err(msg: str) -> ValueError:
        return ValueError(f"{prefix}{msg}")

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

    return ReleaseConfig(
        bump=bump,
        include=list(include),
        exclude=list(exclude),
        targets=targets,
        description=description.strip(),
        context=context.strip(),
    )


def read_release_file(path: str) -> ReleaseConfig:
    """Read and validate a release TOML file.

    Raises FileNotFoundError if the file doesn't exist.
    Raises ValueError for schema/validation failures.
    """
    with open(path, "r", encoding="utf-8") as f:
        data = tomlkit.load(f)

    return _validate_release_config(data)


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
    Raises ValueError for schema/validation failures.
    """
    with open(path, "r", encoding="utf-8") as f:
        data = tomlkit.load(f)

    if "packages" not in data:
        raise ValueError("missing required section: [packages]")

    packages_raw = data["packages"]
    if not isinstance(packages_raw, dict):
        raise ValueError("[packages] must be a table of package configurations")

    if not packages_raw:
        raise ValueError("[packages] is empty -- at least one package is required")

    packages = {}
    for pkg_name, pkg_data in packages_raw.items():
        if not isinstance(pkg_data, dict):
            raise ValueError(
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
    Raises ValueError for schema/validation failures.
    """
    with open(path, "r", encoding="utf-8") as f:
        data = tomlkit.load(f)

    # --- version ---
    if "version" not in data:
        raise ValueError("missing required field: version")
    version = data["version"]
    if not isinstance(version, str) or not version.strip():
        raise ValueError("version must be a non-empty string")

    # --- dispatch ---
    if "dispatch" not in data:
        raise ValueError("missing required field: dispatch")
    dispatch = data["dispatch"]
    if not isinstance(dispatch, list) or not all(isinstance(s, str) for s in dispatch):
        raise ValueError("dispatch must be a list of strings")
    if not dispatch:
        raise ValueError("dispatch must be non-empty")

    # --- ref ---
    if "ref" not in data:
        raise ValueError("missing required field: ref")
    ref = data["ref"]
    if not isinstance(ref, str) or not ref.strip():
        raise ValueError("ref must be set in retry.toml (e.g. a tag like v1.2.3 or a branch like main)")

    return RetryConfig(
        version=version.strip(),
        dispatch=list(dispatch),
        ref=ref.strip(),
    )
