"""Check context types for the strictcli check system."""

from dataclasses import dataclass

from .context import ProjectContext


@dataclass
class WorkspaceCheckContext(ProjectContext):
    """Check context for monorepo/workspace checks. Extends ProjectContext."""

    projects: list  # list of project dicts from workspace.toml
    graph: object  # WorkspaceGraph instance (typed as object to avoid circular imports)
