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


def read_release_file(path: str) -> ReleaseConfig:
    """Read and validate a release TOML file.

    Raises FileNotFoundError if the file doesn't exist.
    Raises ValueError for schema/validation failures.
    """
    with open(path, "r", encoding="utf-8") as f:
        data = tomlkit.load(f)

    # --- bump ---
    if "bump" not in data:
        raise ValueError("missing required field: bump")
    bump = data["bump"]
    if not isinstance(bump, str) or bump not in VALID_BUMP_TYPES:
        raise ValueError(
            f"bump must be set in unreleased.toml: got {bump!r} (must be one of {VALID_BUMP_TYPES})"
        )

    # --- include ---
    if "include" not in data:
        raise ValueError("missing required field: include")
    include = data["include"]
    if not isinstance(include, list) or not all(isinstance(s, str) for s in include):
        raise ValueError("include must be a list of strings")

    # --- exclude ---
    if "exclude" not in data:
        raise ValueError("missing required field: exclude")
    exclude = data["exclude"]
    if not isinstance(exclude, list) or not all(isinstance(s, str) for s in exclude):
        raise ValueError("exclude must be a list of strings")

    # --- include ∩ exclude must be empty ---
    overlap = set(include) & set(exclude)
    if overlap:
        raise ValueError(
            f"targets appear in both include and exclude: {sorted(overlap)}"
        )

    # --- targets section ---
    targets_raw = data.get("targets", {})
    targets = {}
    if targets_raw:
        if not isinstance(targets_raw, dict):
            raise ValueError("targets must be a table of per-target configurations")
        include_set = set(include)
        for name, cfg in targets_raw.items():
            if name not in include_set:
                raise ValueError(
                    f"target config for {name!r} but it is not in include"
                )
            if not isinstance(cfg, dict):
                raise ValueError(f"target config for {name!r} must be a table")
            # Validate known fields
            for key, value in cfg.items():
                if key == "mode":
                    if value not in VALID_TARGET_MODES:
                        raise ValueError(
                            f"invalid mode for target {name!r}: {value!r} "
                            f"(must be one of {VALID_TARGET_MODES})"
                        )
                else:
                    raise ValueError(
                        f"unknown field {key!r} in target config for {name!r}"
                    )
            targets[name] = dict(cfg)

    # Flutter targets require a mode field in their per-target config
    for name in include:
        if name.startswith("flutter-"):
            if name not in targets or "mode" not in targets[name]:
                raise ValueError(
                    f"Flutter target {name!r} requires a [targets.{name}] section "
                    f"with mode = \"ota\" or mode = \"build\""
                )

    # Both flutter-ios and flutter-android must have the same mode when both present
    flutter_names = [n for n in include if n.startswith("flutter-")]
    if len(flutter_names) >= 2:
        modes = {n: targets[n]["mode"] for n in flutter_names}
        unique_modes = set(modes.values())
        if len(unique_modes) > 1:
            mode_list = ", ".join(f"{n}={m!r}" for n, m in sorted(modes.items()))
            raise ValueError(
                f"All Flutter targets must have the same mode, "
                f"but got: {mode_list}"
            )

    # --- description (required) ---
    if "description" not in data:
        raise ValueError("missing required field: description")
    description = data["description"]
    if not isinstance(description, str):
        raise ValueError("description must be a string")
    if not description.strip():
        raise ValueError("description must be set in unreleased.toml (a short summary of this release)")

    # --- context (optional) ---
    context = data.get("context", "")
    if not isinstance(context, str):
        raise ValueError("context must be a string")

    return ReleaseConfig(
        bump=bump,
        include=list(include),
        exclude=list(exclude),
        targets=targets,
        description=description.strip(),
        context=context.strip(),
    )


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

        # --- bump ---
        if "bump" not in pkg_data:
            raise ValueError(f"[packages.{pkg_name}] missing required field: bump")
        bump = pkg_data["bump"]
        if not isinstance(bump, str) or bump not in VALID_BUMP_TYPES:
            raise ValueError(
                f"[packages.{pkg_name}] invalid bump value: {bump!r} "
                f"(must be one of {VALID_BUMP_TYPES})"
            )

        # --- include ---
        if "include" not in pkg_data:
            raise ValueError(f"[packages.{pkg_name}] missing required field: include")
        include = pkg_data["include"]
        if not isinstance(include, list) or not all(isinstance(s, str) for s in include):
            raise ValueError(f"[packages.{pkg_name}] include must be a list of strings")

        # --- exclude ---
        if "exclude" not in pkg_data:
            raise ValueError(f"[packages.{pkg_name}] missing required field: exclude")
        exclude = pkg_data["exclude"]
        if not isinstance(exclude, list) or not all(isinstance(s, str) for s in exclude):
            raise ValueError(f"[packages.{pkg_name}] exclude must be a list of strings")

        # --- include/exclude overlap ---
        overlap = set(include) & set(exclude)
        if overlap:
            raise ValueError(
                f"[packages.{pkg_name}] targets appear in both include and exclude: "
                f"{sorted(overlap)}"
            )

        # --- targets section (optional) ---
        targets_raw = pkg_data.get("targets", {})
        targets = {}
        if targets_raw:
            if not isinstance(targets_raw, dict):
                raise ValueError(
                    f"[packages.{pkg_name}] targets must be a table"
                )
            include_set = set(include)
            for tname, tcfg in targets_raw.items():
                if tname not in include_set:
                    raise ValueError(
                        f"[packages.{pkg_name}] target config for {tname!r} "
                        "but it is not in include"
                    )
                if not isinstance(tcfg, dict):
                    raise ValueError(
                        f"[packages.{pkg_name}] target config for {tname!r} "
                        "must be a table"
                    )
                for key, value in tcfg.items():
                    if key == "mode":
                        if value not in VALID_TARGET_MODES:
                            raise ValueError(
                                f"[packages.{pkg_name}] invalid mode for target "
                                f"{tname!r}: {value!r} "
                                f"(must be one of {VALID_TARGET_MODES})"
                            )
                    else:
                        raise ValueError(
                            f"[packages.{pkg_name}] unknown field {key!r} "
                            f"in target config for {tname!r}"
                        )
                targets[tname] = dict(tcfg)

        # --- description (required) ---
        if "description" not in pkg_data:
            raise ValueError(f"[packages.{pkg_name}] missing required field: description")
        description = pkg_data["description"]
        if not isinstance(description, str):
            raise ValueError(f"[packages.{pkg_name}] description must be a string")
        if not description.strip():
            raise ValueError(f"[packages.{pkg_name}] description must be set (a short summary of this release)")

        # --- context (optional) ---
        context = pkg_data.get("context", "")
        if not isinstance(context, str):
            raise ValueError(f"[packages.{pkg_name}] context must be a string")

        packages[pkg_name] = ReleaseConfig(
            bump=bump,
            include=list(include),
            exclude=list(exclude),
            targets=targets,
            description=description.strip(),
            context=context.strip(),
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
