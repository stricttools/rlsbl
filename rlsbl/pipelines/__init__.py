"""Pipeline registry mapping type strings to pipeline classes for config-driven publish orchestration."""

from .protocol import Pipeline
from .npm import NpmPipeline
from .pypi import PypiPipeline
from .go import GoPipeline
from .cargo import CargoPipeline
from .deno import DenoPipeline
from .hex import HexPipeline
from .maven import MavenPipeline
from .docker import DockerPipeline
from .cloudflare_pages import CloudflarePagesPipeline

PIPELINE_TYPES: dict[str, type] = {
    "npm": NpmPipeline,
    "pypi": PypiPipeline,
    "go": GoPipeline,
    "cargo": CargoPipeline,
    "deno": DenoPipeline,
    "hex": HexPipeline,
    "maven": MavenPipeline,
    "docker": DockerPipeline,
    "cloudflare-pages": CloudflarePagesPipeline,
}


def load_pipelines(config: dict) -> dict[str, "Pipeline"]:
    """Instantiate pipelines from the project config's ``pipelines`` section.

    Each entry in ``config["pipelines"]`` is a dict with at least ``type``
    and ``local`` keys. The ``type`` is looked up in ``PIPELINE_TYPES`` to
    find the class, which is instantiated with the pipeline name, type,
    local flag, and full entry config.

    Returns a dict mapping pipeline names to pipeline instances.
    Returns an empty dict if no ``pipelines`` key exists in the config.
    """
    pipelines_config = config.get("pipelines")
    if not pipelines_config:
        return {}

    result = {}
    for name, entry in pipelines_config.items():
        pipeline_type = entry["type"]
        cls = PIPELINE_TYPES[pipeline_type]
        instance = cls(
            name=name,
            pipeline_type=pipeline_type,
            local=entry["local"],
            config=entry,
        )
        result[name] = instance
    return result


__all__ = ["Pipeline", "PIPELINE_TYPES", "load_pipelines"]
