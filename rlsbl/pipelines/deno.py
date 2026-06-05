"""Deno pipeline -- publishes to JSR using DENO_TOKEN or JSR_TOKEN."""

import os
import subprocess
import sys

from .base import TokenPipeline
from ..utils import run


class DenoPipeline(TokenPipeline):
    """Pipeline that publishes Deno packages to JSR.

    Supports dual-token resolution: tries DENO_TOKEN first, falls back to
    JSR_TOKEN. When ``token_var`` is explicitly set in pipeline config,
    only that variable is consulted.
    """

    _default_token_var = "DENO_TOKEN"

    def template_dir(self) -> str | None:
        return os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "templates", "deno"
        )

    def template_mappings(self, ctx) -> list[dict[str, str]]:
        return [
            {"template": "publish.yml.tpl", "target": ".github/workflows/publish.yml"},
        ]

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
            # Dual-token: try DENO_TOKEN first, fall back to JSR_TOKEN
            token = os.environ.get("DENO_TOKEN") or os.environ.get("JSR_TOKEN")
            if not token:
                print(
                    f"Error: pipeline '{self.name}' requires DENO_TOKEN or "
                    f"JSR_TOKEN but neither is set",
                    file=sys.stderr,
                )
                sys.exit(1)

        self._publish_command(dir_path, version, token)

    def _publish_command(self, dir_path: str, version: str, token: str) -> None:
        try:
            run("deno", ["publish"], env={
                **os.environ,
                "DENO_TOKEN": token,
            })
            print(f"Published to JSR: {version}")
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"deno publish failed: {exc}") from exc

    def required_env_vars(self) -> list[str]:
        if not self.local:
            return []
        config_token_var = self.config.get("token_var")
        if config_token_var:
            return [config_token_var]
        # Both are accepted; report the primary one
        return ["DENO_TOKEN"]
