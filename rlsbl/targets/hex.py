"""Hex (Elixir/Erlang) release target that manages version tracking in mix.exs and scaffolds CI workflows for publishing to hex.pm."""

import os
import re

from .base import BaseTarget, TemplateVars
from .utils import _get_git_author
from ..errors import VersionError


class HexTarget(BaseTarget):
    """Release target for Hex/Elixir projects (mix.exs)."""

    detection_files = ("mix.exs",)
    capabilities = frozenset({"read_name", "ci_templates", "dev_install"})
    ecosystem = "Elixir / Hex"

    @property
    def name(self):
        return "hex"

    def detect(self, dir_path):
        return os.path.exists(os.path.join(dir_path, "mix.exs"))

    def read_name(self, dir_path, ctx):
        """Extract app name from mix.exs."""
        mix_path = os.path.join(dir_path, "mix.exs")
        if not os.path.exists(mix_path):
            return None
        with open(mix_path, "r", encoding="utf-8") as f:
            content = f.read()
        app_match = re.search(r'app:\s*:(\w+)', content)
        return app_match.group(1) if app_match else None

    def read_metadata(self, dir_path):
        """Hex metadata extraction not yet implemented."""
        return {}

    def read_version(self, dir_path):
        """Read the version from mix.exs."""
        mix_path = os.path.join(dir_path, "mix.exs")
        with open(mix_path, "r", encoding="utf-8") as f:
            content = f.read()
        match = re.search(r'version:\s*"([^"]+)"', content)
        if not match:
            raise VersionError(f"No version found in {mix_path}")
        return match.group(1)

    def write_version(self, dir_path, version, ctx):
        """Write a new version to mix.exs.

        Returns a list of relative file paths that were modified.
        """
        mix_path = os.path.join(dir_path, "mix.exs")
        with open(mix_path, "r", encoding="utf-8") as f:
            content = f.read()
        new_content = re.sub(
            r'(version:\s*)"[^"]+"',
            f'\\1"{version}"',
            content,
        )
        tmp_path = mix_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(new_content)
        os.replace(tmp_path, mix_path)
        return [self.version_file()]

    def version_file(self, dir_path=None):
        return "mix.exs"

    def tag_format(self, version):
        return f"v{version}"

    def template_dir(self):
        return os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "templates", "hex"
        )

    def template_vars(self, dir_path, ctx):
        """Extract template variables from mix.exs."""
        mix_path = os.path.join(dir_path, "mix.exs")
        with open(mix_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Extract app name
        app_match = re.search(r'app:\s*:(\w+)', content)
        app_name = app_match.group(1) if app_match else ""

        author = _get_git_author()

        return TemplateVars(self.name, {
            "name": app_name,
            "version": self.read_version(dir_path),
            "author": author,
            "publishSetup": "Requires HEX_API_KEY secret on GitHub (Settings > Secrets > Actions)",
        })

    def template_mappings(self, ctx):
        return [
            {"template": "ci.yml.tpl", "target": ".github/workflows/ci.yml"},
        ]

    def check_project_exists(self, dir_path):
        return os.path.exists(os.path.join(dir_path, "mix.exs"))

    def get_project_init_hint(self):
        return 'Run "mix new <project_name>" first'

    def dev_install_command(self, project_dir):
        # Elixir/Mix has no real distinction between a "global" install and a
        # local one: dependencies live in `deps/` next to mix.exs and are
        # compiled into `_build/`. `mix deps.get` fetches deps for both modes.
        # There is no portable equivalent of `pip uninstall` or `npm unlink`.
        spec = {
            "tool": "mix",
            "purpose": "for fetching Hex dependencies",
            "args": ["deps.get"],
            "uninstall_args_template": None,
        }
        return {"global": spec, "venv": spec}
