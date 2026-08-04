"""Cloudflare Pages pipeline that deploys generated documentation sites to Cloudflare Pages by invoking selfdoc deploy as a local step."""

import subprocess
import sys

from .base import BasePipeline
from ..utils import require_tool
from .. import effects


class CloudflarePagesPipeline(BasePipeline):
    """Pipeline that deploys to Cloudflare Pages via the selfdoc CLI.

    Requires ``selfdoc`` on PATH and CF_ACCOUNT_ID + CF_PAGES_API_TOKEN
    environment variables when publishing locally.
    """

    def publish(self, dir_path: str, version: str, ctx) -> None:
        """Deploy to Cloudflare Pages via ``selfdoc deploy``."""
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
            effects.run(
                # `selfdoc deploy` declares itself `consequential` -- it makes
                # a Cloudflare Pages deployment live, or force-pushes gh-pages.
                # The approval was already taken one level up: this runs only
                # inside `rlsbl release run`, which is itself consequential and
                # has already asked. Without the flag the child hard-errors on
                # the release runner's non-interactive stdin, or re-asks the
                # same question on a TTY. selfdoc's `gen` and `check` are NOT
                # consequential and stay bare (see validate.py).
                ["selfdoc", "deploy", "--approve-consequential"],
                cwd=dir_path,
                check=True,
                timeout=300,
            )
            print(f"Deployed to Cloudflare Pages: {version}")
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"selfdoc deploy failed: {exc}") from exc

    def required_env_vars(self) -> list[str]:
        """Return Cloudflare account and API token vars when local."""
        if self.local:
            return ["CF_ACCOUNT_ID", "CF_PAGES_API_TOKEN"]
        return []
