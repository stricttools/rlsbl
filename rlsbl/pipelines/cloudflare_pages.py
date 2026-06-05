"""Cloudflare Pages pipeline -- deploys documentation via selfdoc deploy."""

import subprocess
import sys

from .base import BasePipeline
from ..utils import require_tool


class CloudflarePagesPipeline(BasePipeline):
    """Pipeline that deploys to Cloudflare Pages via the selfdoc CLI.

    Requires ``selfdoc`` on PATH and CF_ACCOUNT_ID + CF_PAGES_API_TOKEN
    environment variables when publishing locally.
    """

    def publish(self, dir_path: str, version: str, ctx) -> None:
        if not self.local:
            print(f"  Skipping pipeline '{self.name}' local publish (config: local=false)")
            return

        if not require_tool("selfdoc", fatal=False):
            print(
                f"Error: pipeline '{self.name}' requires 'selfdoc' on PATH",
                file=sys.stderr,
            )
            sys.exit(1)

        try:
            subprocess.run(
                ["selfdoc", "deploy"],
                cwd=dir_path,
                check=True,
                timeout=300,
            )
            print(f"Deployed to Cloudflare Pages: {version}")
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"selfdoc deploy failed: {exc}") from exc

    def required_env_vars(self) -> list[str]:
        if self.local:
            return ["CF_ACCOUNT_ID", "CF_PAGES_API_TOKEN"]
        return []
