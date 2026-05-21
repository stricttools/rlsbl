"""Layer configuration loading and validation for monorepo architectural rules.

Reads the [layers] section from workspace.toml and provides utilities for
resolving package-to-layer assignments and validating that every workspace
project is assigned to exactly one layer.
"""

import os
import tomllib
from dataclasses import dataclass, field
from fnmatch import fnmatch

from .workspace import WORKSPACE_DIR, WORKSPACE_FILE


@dataclass
class LayerConfig:
    order: list[str]  # layer names, bottom-to-top
    assignments: dict[str, list[str]]  # layer name -> list of glob patterns
    unrestricted: list[str] = field(default_factory=list)  # exempt packages
    forbidden_targets: list[str] = field(default_factory=list)  # nothing may depend on
    allow: list[dict[str, str]] = field(default_factory=list)  # explicit cross-layer allowances


def load_layer_config(root: str) -> LayerConfig | None:
    """Read the [layers] section from workspace.toml.

    Returns None if the file has no [layers] section.
    Raises ValueError on invalid structure.
    Raises FileNotFoundError if workspace.toml doesn't exist.
    """
    path = os.path.join(root, WORKSPACE_DIR, WORKSPACE_FILE)
    with open(path, "rb") as f:
        data = tomllib.load(f)

    if "layers" not in data:
        return None

    layers = data["layers"]

    # -- order --
    if "order" not in layers:
        raise ValueError("[layers] missing required 'order' key")
    order = layers["order"]
    if not isinstance(order, list) or len(order) == 0:
        raise ValueError("[layers.order] must be a non-empty list of strings")
    for i, item in enumerate(order):
        if not isinstance(item, str):
            raise ValueError(f"[layers.order][{i}] must be a string, got {type(item).__name__}")

    order_set = set(order)

    # -- assignments --
    assignments_raw = layers.get("assignments", {})
    if not isinstance(assignments_raw, dict):
        raise ValueError("[layers.assignments] must be a table")
    assignments: dict[str, list[str]] = {}
    for key, patterns in assignments_raw.items():
        if key not in order_set:
            raise ValueError(
                f"[layers.assignments] key '{key}' is not in [layers.order]"
            )
        if not isinstance(patterns, list):
            raise ValueError(
                f"[layers.assignments.{key}] must be a list of strings"
            )
        for j, pat in enumerate(patterns):
            if not isinstance(pat, str):
                raise ValueError(
                    f"[layers.assignments.{key}][{j}] must be a string, "
                    f"got {type(pat).__name__}"
                )
        assignments[key] = list(patterns)

    # -- overrides --
    overrides = layers.get("overrides", {})
    if not isinstance(overrides, dict):
        raise ValueError("[layers.overrides] must be a table")

    unrestricted = _validate_string_list(overrides, "unrestricted", "[layers.overrides.unrestricted]")
    forbidden_targets = _validate_string_list(overrides, "forbidden_targets", "[layers.overrides.forbidden_targets]")

    # -- overrides.allow --
    allow_raw = overrides.get("allow", [])
    if not isinstance(allow_raw, list):
        raise ValueError("[layers.overrides.allow] must be a list of tables")
    allow: list[dict[str, str]] = []
    for i, entry in enumerate(allow_raw):
        if not isinstance(entry, dict):
            raise ValueError(f"[layers.overrides.allow][{i}] must be a table")
        for required_key in ("source", "target"):
            if required_key not in entry:
                raise ValueError(
                    f"[layers.overrides.allow][{i}] missing required key '{required_key}'"
                )
            if not isinstance(entry[required_key], str):
                raise ValueError(
                    f"[layers.overrides.allow][{i}].{required_key} must be a string"
                )
        allow.append({"source": entry["source"], "target": entry["target"]})

    return LayerConfig(
        order=list(order),
        assignments=assignments,
        unrestricted=unrestricted,
        forbidden_targets=forbidden_targets,
        allow=allow,
    )


def _validate_string_list(parent: dict, key: str, context: str) -> list[str]:
    """Validate and return an optional list-of-strings field."""
    raw = parent.get(key, [])
    if not isinstance(raw, list):
        raise ValueError(f"{context} must be a list of strings")
    for i, item in enumerate(raw):
        if not isinstance(item, str):
            raise ValueError(f"{context}[{i}] must be a string, got {type(item).__name__}")
    return list(raw)


def resolve_package_layer(name: str, config: LayerConfig) -> str | None:
    """Resolve which layer a package belongs to based on assignment globs.

    Returns the layer name if matched, None if unassigned.
    """
    for layer, patterns in config.assignments.items():
        for pattern in patterns:
            if fnmatch(name, pattern):
                return layer
    return None


def check_layer_violations(
    projects: list[dict], config: LayerConfig, graph
) -> list[str]:
    """Check dependency edges for layer violations.

    Returns a list of violation strings (empty means all good).
    The *graph* argument is a WorkspaceGraph instance.
    """
    # First validate assignments -- if there are errors, return them immediately
    assignment_errors = validate_layer_assignments(projects, config)
    if assignment_errors:
        return assignment_errors

    layer_index = {name: i for i, name in enumerate(config.order)}
    violations = []

    for proj in projects:
        source = proj["name"]

        # Unrestricted packages: skip all checks as source
        if any(fnmatch(source, pat) for pat in config.unrestricted):
            continue

        source_layer = resolve_package_layer(source, config)
        if source_layer is None:
            continue  # unassigned -- already caught by validate_layer_assignments

        for dep in graph.dependencies(source):
            target = dep.name

            # Forbidden targets: anything depending on these is a violation
            if any(fnmatch(target, pat) for pat in config.forbidden_targets):
                violations.append(
                    f"'{source}' depends on forbidden target '{target}'"
                )
                continue

            # Explicit allows: skip the layer check for matching edges
            if _is_explicitly_allowed(source, target, config.allow):
                continue

            target_layer = resolve_package_layer(target, config)
            if target_layer is None:
                continue  # unassigned target -- already caught above

            source_idx = layer_index[source_layer]
            target_idx = layer_index[target_layer]

            # Higher index = higher layer.  Higher depending on lower is OK.
            # Same layer is OK.  Lower depending on higher is a violation.
            if source_idx < target_idx:
                violations.append(
                    f"'{source}' ({source_layer}) depends on '{target}' "
                    f"({target_layer}) -- lower layer cannot depend on higher"
                )

    return violations


def _is_explicitly_allowed(
    source: str, target: str, allow: list[dict[str, str]]
) -> bool:
    """Check if an edge is explicitly allowed by an override rule."""
    for entry in allow:
        if fnmatch(source, entry["source"]) and fnmatch(target, entry["target"]):
            return True
    return False


def validate_layer_assignments(
    projects: list[dict], config: LayerConfig
) -> list[str]:
    """Validate that every project is assigned to exactly one layer.

    Returns a list of error messages (empty means valid).
    """
    errors = []
    for proj in projects:
        name = proj["name"]
        matched_layers = []
        for layer, patterns in config.assignments.items():
            for pattern in patterns:
                if fnmatch(name, pattern):
                    matched_layers.append(layer)
                    break  # one match per layer is enough
        if len(matched_layers) == 0:
            errors.append(f"Package '{name}' is not assigned to any layer")
        elif len(matched_layers) > 1:
            layers_str = ", ".join(matched_layers)
            errors.append(
                f"Package '{name}' matches multiple layers: {layers_str}"
            )
    return errors
