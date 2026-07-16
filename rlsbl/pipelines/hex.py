"""Hex pipeline implementation that publishes Elixir and Erlang packages to hex.pm, authenticating via the HEX_API_KEY environment variable."""

import os
import subprocess
import sys

from .base import TokenPipeline
from ..utils import run


class HexPipeline(TokenPipeline):
    """Pipeline that publishes Elixir packages to hex.pm."""

    _default_token_var = "HEX_API_KEY"

    def template_dir(self) -> str | None:
        return os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "templates", "hex"
        )

    def template_mappings(self, ctx) -> list[dict[str, str]]:
        return [
            {"template": "publish.yml.tpl", "target": ".github/workflows/publish.yml"},
        ]

    def publish(self, dir_path: str, version: str, ctx) -> None:
        if not self.local:
            print(f"  Skipping pipeline '{self.name}' local publish (config: local=false)")
            return

        if not self.probe_before_publish(dir_path, version, ctx):
            return

        token = os.environ.get(self.token_var)
        if not token:
            print(
                f"Error: pipeline '{self.name}' requires {self.token_var} but it's not set",
                file=sys.stderr,
            )
            sys.exit(1)
        self._publish_command(dir_path, version, token)

    def _publish_command(self, dir_path: str, version: str, token: str) -> None:
        try:
            run("mix", ["hex.publish", "--yes"], env={
                **os.environ,
                "HEX_API_KEY": token,
            })
            print(f"Published to Hex: {version}")
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"mix hex.publish failed: {exc}") from exc
