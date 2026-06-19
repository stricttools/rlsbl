"""Project context for general command use."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .workspace import Releasable, WorkspaceProject


@dataclass
class ProjectContext:
    """Context object carrying project root, optional workspace root, and loaded config."""

    project_root: Path
    workspace_root: Path | None
    config: dict
    project: WorkspaceProject | None = field(default=None)
    push_stdin: str | None = field(default=None)
    releasable: "Releasable | None" = field(default=None)


def create_context(
    root: Path,
    workspace_root: Path | None = None,
    project: WorkspaceProject | None = None,
) -> ProjectContext:
    """Create a ProjectContext, loading config via read_project_config().

    Uses the merged view (publish.json + config.json with conflict detection).
    Returns an empty dict for config if neither file exists.
    """
    from .config import read_project_config

    config = read_project_config(root)
    return ProjectContext(
        project_root=root,
        workspace_root=workspace_root,
        config=config,
        project=project,
    )
