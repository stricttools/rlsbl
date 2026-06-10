"""PyPI pipeline implementation that builds and publishes Python packages to PyPI, authenticating via PYPI_TOKEN or TWINE_PASSWORD credentials."""

import os
import subprocess
import sys

from .base import TokenPipeline
from ..utils import run


class PypiPipeline(TokenPipeline):
    """Pipeline that builds and publishes Python packages to PyPI.

    Supports dual-token resolution: tries PYPI_TOKEN first, falls back to
    TWINE_PASSWORD. When ``token_var`` is explicitly set in pipeline config,
    only that variable is consulted.
    """

    _default_token_var = "PYPI_TOKEN"

    def template_dir(self) -> str | None:
        return os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "templates", "pypi"
        )

    def template_mappings(self, ctx) -> list[dict[str, str]]:
        return [
            {"template": "publish.yml.tpl", "target": ".github/workflows/publish.yml"},
        ]

    def build_assets(self, dir_path: str, version: str, dist_dir: str, ctx) -> list[str]:
        from .build import build_pypi_assets
        return build_pypi_assets(dir_path, version, dist_dir)

    def publish(self, dir_path: str, version: str, ctx) -> None:
        if not self.local:
            print(f"  Skipping pipeline '{self.name}' local publish (config: local=false)")
            return

        # When token_var was explicitly set in config, use it directly
        config_token_var = self.config.get("token_var")
        if config_token_var:
            token = os.environ.get(config_token_var)
            if not token:
                print(
                    f"Error: pipeline '{self.name}' requires {config_token_var} "
                    f"but it's not set",
                    file=sys.stderr,
                )
                sys.exit(1)
        else:
            # Dual-token: try PYPI_TOKEN first, fall back to TWINE_PASSWORD
            token = os.environ.get("PYPI_TOKEN") or os.environ.get("TWINE_PASSWORD")
            if not token:
                print(
                    f"Error: pipeline '{self.name}' requires PYPI_TOKEN or "
                    f"TWINE_PASSWORD but neither is set",
                    file=sys.stderr,
                )
                sys.exit(1)

        self._publish_command(dir_path, version, token)

    def _publish_command(self, dir_path: str, version: str, token: str) -> None:
        try:
            run("uv", ["build"], env=os.environ, cwd=dir_path)
            run("uv", ["publish"], env={
                **os.environ,
                "UV_PUBLISH_TOKEN": token,
            }, cwd=dir_path)
            print(f"Published to PyPI: {version}")
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"PyPI publish failed: {exc}") from exc

    def required_env_vars(self) -> list[str]:
        if not self.local:
            return []
        config_token_var = self.config.get("token_var")
        if config_token_var:
            return [config_token_var]
        # Both are accepted; report the primary one
        return ["PYPI_TOKEN"]
