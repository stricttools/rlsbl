"""Cargo pipeline implementation that publishes Rust crates to crates.io, authenticating via the CARGO_REGISTRY_TOKEN environment variable."""

import os
import subprocess

from .base import TokenPipeline
from ..utils import run


class CargoPipeline(TokenPipeline):
    """Pipeline that publishes Rust crates to crates.io."""

    _default_token_var = "CARGO_REGISTRY_TOKEN"

    def template_dir(self) -> str | None:
        return os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "templates", "cargo"
        )

    def template_mappings(self, ctx) -> list[dict[str, str]]:
        return [
            {"template": "publish.yml.tpl", "target": ".github/workflows/publish.yml"},
        ]

    def build_assets(self, dir_path: str, version: str, dist_dir: str, ctx) -> list[str]:
        from .build import build_cargo_assets
        return build_cargo_assets(dir_path, version, dist_dir)

    def _publish_command(self, dir_path: str, version: str, token: str) -> None:
        try:
            run("cargo", ["publish"], env={
                **os.environ,
                "CARGO_REGISTRY_TOKEN": token,
            }, cwd=dir_path)
            print(f"Published to crates.io: {version}")
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"cargo publish failed: {exc}") from exc
