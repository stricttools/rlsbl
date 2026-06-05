"""Hex pipeline -- publishes Elixir packages to hex.pm using HEX_API_KEY."""

import os
import subprocess

from .base import TokenPipeline
from ..utils import run


class HexPipeline(TokenPipeline):
    """Pipeline that publishes Elixir packages to hex.pm."""

    _default_token_var = "HEX_API_KEY"

    def _publish_command(self, dir_path: str, version: str, token: str) -> None:
        try:
            run("mix", ["hex.publish", "--yes"], env={
                **os.environ,
                "HEX_API_KEY": token,
            })
            print(f"Published to Hex: {version}")
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"mix hex.publish failed: {exc}") from exc
