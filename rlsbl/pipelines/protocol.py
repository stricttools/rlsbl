"""Pipeline protocol defining the formal interface that all pipeline implementations must satisfy for publishing, asset building, and scaffolding."""

from typing import Protocol, runtime_checkable


@runtime_checkable
class Pipeline(Protocol):
    """Protocol defining a release pipeline.

    Pipelines handle publishing, asset building, and CI template generation
    for a specific publish mechanism (e.g. pypi, npm, docker, cloudflare-pages).
    """

    name: str
    pipeline_type: str
    local: bool

    def publish(self, dir_path: str, version: str, ctx) -> None:
        """Publish the package at *version* from *dir_path*."""
        ...

    def build_assets(self, dir_path: str, version: str, dist_dir: str, ctx) -> list[str]:
        """Build release assets into *dist_dir* and return their paths."""
        ...

    def template_dir(self) -> str | None:
        """Return the directory containing CI workflow templates, or None."""
        ...

    def template_mappings(self, ctx) -> list[dict[str, str]]:
        """Return template-to-target path mappings for CI scaffold."""
        ...

    def required_env_vars(self) -> list[str]:
        """Return env var names required for local publishing."""
        ...

    def ci_secret_names(self) -> list[str]:
        """Return the repository secrets the CI publish job authenticates with."""
        ...
