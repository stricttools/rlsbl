"""Check context types for the strictcli check system."""

from dataclasses import dataclass
from pathlib import Path


@dataclass
class ProjectCheckContext:
    """Check context for single-project checks."""

    project_root: Path


@dataclass
class WorkspaceCheckContext(ProjectCheckContext):
    """Check context for monorepo/workspace checks. Extends ProjectCheckContext."""

    workspace_root: Path
    projects: list  # list of project dicts from workspace.toml
    graph: object  # WorkspaceGraph instance (typed as object to avoid circular imports)
