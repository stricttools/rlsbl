"""npm pipeline -- publishes to npm registry using NPM_TOKEN."""

import os
import subprocess

from .base import TokenPipeline
from ..utils import run


class NpmPipeline(TokenPipeline):
    """Pipeline that publishes to npm with provenance attestation."""

    _default_token_var = "NPM_TOKEN"

    def build_assets(self, dir_path: str, version: str, dist_dir: str, ctx) -> list[str]:
        from .build import build_npm_assets
        return build_npm_assets(dir_path, version, dist_dir)

    def _publish_command(self, dir_path: str, version: str, token: str) -> None:
        try:
            run("npm", ["publish", "--provenance", "--access", "public"], env={
                **os.environ,
                "NPM_TOKEN": token,
            })
            print(f"Published to npm: {version}")
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"npm publish failed: {exc}") from exc
