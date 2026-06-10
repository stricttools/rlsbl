"""npm pipeline implementation that publishes packages to the npm registry, authenticating via NPM_TOKEN and handling scoped package access."""

import os
import re
import subprocess

from .base import TokenPipeline
from ..utils import run


class NpmPipeline(TokenPipeline):
    """Pipeline that publishes to npm with provenance attestation."""

    _default_token_var = "NPM_TOKEN"

    def template_dir(self) -> str | None:
        return os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "templates", "npm"
        )

    def template_mappings(self, ctx) -> list[dict[str, str]]:
        # Detect package manager to select the right publish template
        pm = self._detect_package_manager(ctx)
        if pm == "pnpm":
            publish_template = "publish-pnpm.yml.tpl"
        elif pm == "yarn":
            publish_template = "publish-yarn.yml.tpl"
        else:
            publish_template = "publish.yml.tpl"
        return [
            {"template": publish_template, "target": ".github/workflows/publish.yml"},
        ]

    def _detect_package_manager(self, ctx) -> str:
        """Detect the package manager by checking for lock files."""
        project_root = str(ctx.project_root) if ctx and hasattr(ctx, "project_root") else "."
        current = os.path.abspath(project_root)
        while True:
            for lockfile, pm in [
                ("pnpm-lock.yaml", "pnpm"),
                ("yarn.lock", "yarn"),
                ("package-lock.json", "npm"),
            ]:
                if os.path.exists(os.path.join(current, lockfile)):
                    return pm
            if os.path.isdir(os.path.join(current, ".git")):
                break
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
        return "npm"

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
