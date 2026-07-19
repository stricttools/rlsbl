"""PyPI pipeline implementation that builds and publishes Python packages to PyPI, authenticating via PYPI_TOKEN or TWINE_PASSWORD credentials."""

import os
import re
import subprocess
import sys
import tomllib

from .base import TokenPipeline
from ..utils import run


def normalize_module_name(dist_name: str) -> str:
    """Normalize a PyPI distribution name to an importable module name.

    Lowercases and collapses runs of ``-``, ``_``, and ``.`` into a single
    underscore (the standard import-name form of a distribution name).
    """
    return re.sub(r"[-_.]+", "_", dist_name.strip().lower())


def launcher_module_name(subdir: str) -> str:
    """Resolve the launcher module dir name from the target's pyproject.toml.

    The module directory is named after the distribution's normalized name
    so hatchling's default package auto-detection finds it. When the
    manifest is absent (scaffold hard-errors on this separately) or has no
    name, falls back to a neutral placeholder so callers that only need a
    non-``publish`` path (e.g. publish-template resolution) do not crash.
    """
    pyproject = os.path.join(subdir, "pyproject.toml") if subdir != "." else "pyproject.toml"
    try:
        with open(pyproject, "rb") as f:
            data = tomllib.load(f)
        name = (data.get("project") or {}).get("name")
        if name:
            return normalize_module_name(name)
    except (OSError, tomllib.TOMLDecodeError):
        pass
    return "launcher_pkg"


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
        # Launcher artifact: wrapper-package that downloads a binary
        # from a GitHub Release on first run (no pip postinstall hook).
        # In addition to the publish workflow, emit the first-run launcher
        # module. Its directory must match the (normalized) distribution
        # name so hatchling's auto-detection includes it in the wheel with
        # no extra build config.
        if self.config.get("artifact") == "launcher":
            subdir = self._linked_target_subdir(ctx)
            module = launcher_module_name(subdir)
            rel = f"{module}/__init__.py"
            target = rel if subdir == "." else f"{subdir}/{rel}"
            return [
                {"template": "publish-launcher.yml.tpl",
                 "target": ".github/workflows/publish.yml"},
                {"template": "shim-launcher.py.tpl",
                 "target": target},
            ]
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

        if not self.probe_before_publish(dir_path, version, ctx):
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
            run("uv", ["publish", "--check-url", "https://pypi.org/simple/"], env={
                **os.environ,
                "UV_PUBLISH_TOKEN": token,
            }, cwd=dir_path)
            print(f"Published to PyPI: {version}")
        except subprocess.CalledProcessError as exc:
            if self.is_already_published_error(exc):
                print(f"  PyPI: version already exists, treating as success")
                return
            raise RuntimeError(f"PyPI publish failed: {exc}") from exc

    def required_env_vars(self) -> list[str]:
        if not self.local:
            return []
        config_token_var = self.config.get("token_var")
        if config_token_var:
            return [config_token_var]
        # Both are accepted; report the primary one
        return ["PYPI_TOKEN"]
