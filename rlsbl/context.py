"""Project context for general command use."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .workspace import WorkspaceProject


@dataclass
class ProjectContext:
    """Context object carrying project root, optional workspace root, and loaded config."""

    project_root: Path
    workspace_root: Path | None
    config: dict
    project: WorkspaceProject | None = field(default=None)


def create_context(
    root: Path,
    workspace_root: Path | None = None,
    project: WorkspaceProject | None = None,
) -> ProjectContext:
    """Create a ProjectContext, loading config from .rlsbl/config.json.

    Returns an empty dict for config if the file doesn't exist.
    """
    config_path = root / ".rlsbl" / "config.json"
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except FileNotFoundError:
        config = {}
    return ProjectContext(
        project_root=root,
        workspace_root=workspace_root,
        config=config,
        project=project,
    )
