"""Check context types for the strictcli check system."""

from dataclasses import dataclass, field

from .context import ProjectContext


@dataclass
class WorkspaceCheckContext(ProjectContext):
    """Check context for monorepo/workspace checks. Extends ProjectContext."""

    projects: list = field(default_factory=list)  # list of WorkspaceProject from workspace.toml
    graph: object = field(default=None)  # WorkspaceGraph instance (typed as object to avoid circular imports)
    releasables: list = field(default_factory=list)  # list of Releasable instances
