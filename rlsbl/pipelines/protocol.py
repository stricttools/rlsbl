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

    def publish(self, dir_path: str, version: str, ctx) -> None: ...
    def build_assets(self, dir_path: str, version: str, dist_dir: str, ctx) -> list[str]: ...
    def template_dir(self) -> str | None: ...
    def template_mappings(self, ctx) -> list[dict[str, str]]: ...
    def required_env_vars(self) -> list[str]: ...
