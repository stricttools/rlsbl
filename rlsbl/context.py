"""Project context for general command use."""

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ProjectContext:
    """Context object carrying project root, optional monorepo root, and loaded config."""

    project_root: Path
    monorepo_root: Path | None
    config: dict


def create_context(root: Path, monorepo_root: Path | None = None) -> ProjectContext:
    """Create a ProjectContext, loading config from .rlsbl/config.json.

    Returns an empty dict for config if the file doesn't exist.
    """
    config_path = root / ".rlsbl" / "config.json"
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except FileNotFoundError:
        config = {}
    return ProjectContext(project_root=root, monorepo_root=monorepo_root, config=config)
