"""Inline publish logic for monorepo projects: workflow parsing and YAML emission."""

from __future__ import annotations

import yaml


def parse_publish_workflow(path: str) -> dict:
    """Parse a GitHub Actions publish workflow file.

    Reads the YAML file at *path*, validates it has a ``jobs:`` key, and
    returns a dict with the top-level keys that matter for inline publish
    generation.

    Returns a dict with keys:
        jobs       -- the ``jobs`` mapping from the workflow
        permissions -- workflow-level ``permissions`` mapping, or None
        env        -- workflow-level ``env`` mapping, or None
        name       -- workflow ``name`` string, or None
    """
    with open(path) as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict) or "jobs" not in data:
        raise ValueError(f"Workflow file {path} is missing a 'jobs' key")

    return {
        "jobs": data["jobs"],
        "permissions": data.get("permissions"),
        "env": data.get("env"),
        "name": data.get("name"),
    }


class _LiteralBlockDumper(yaml.SafeDumper):
    """SafeDumper subclass that emits multi-line strings as YAML literal blocks."""


def _literal_str_representer(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    """Represent multi-line strings with ``|`` literal block style."""
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_LiteralBlockDumper.add_representer(str, _literal_str_representer)


def emit_workflow(workflow_dict: dict) -> str:
    """Emit a workflow dict as a YAML string.

    Uses literal block style (``|``) for multi-line strings and preserves
    key order.  The custom representer is registered on a private Dumper
    subclass so the global ``yaml`` state is never modified.
    """
    return yaml.dump(
        workflow_dict,
        Dumper=_LiteralBlockDumper,
        default_flow_style=False,
        sort_keys=False,
    )
