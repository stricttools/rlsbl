"""Base class for release targets providing shared defaults for version reading, writing, detection, scaffolding, and publish configuration."""

import os


class BaseTarget:
    """Concrete base providing defaults for optional Protocol methods."""

    def version_file(self):
        return None

    def tag_format(self, version):
        return f"v{version}"

    def monorepo_tag_format(self, name, version, path=None):
        return f"{name}@v{version}"

    def monorepo_tag_glob(self, name, path=None):
        return f"{name}@v*"

    def template_dir(self):
        return None

    def shared_template_dir(self):
        templates = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "templates", "shared"
        )
        return templates

    def read_name(self, dir_path, ctx):
        return None

    def read_metadata(self, dir_path):
        return {}

    def template_vars(self, dir_path, ctx):
        return {}

    def template_mappings(self, ctx):
        return []

    def shared_template_mappings(self, ctx):
        return [
            {"template": "CHANGELOG.md.tpl", "target": "CHANGELOG.md"},
            {"template": "gitignore.tpl", "target": ".gitignore"},
            {"template": "hooks/pre-checks.sh.tpl", "target": ".rlsbl/hooks/pre-checks.sh"},
            {"template": "hooks/pre-release.sh.tpl", "target": ".rlsbl/hooks/pre-release.sh"},
            {"template": "hooks/post-release.sh.tpl", "target": ".rlsbl/hooks/post-release.sh"},
            {"template": "lint/python.toml.tpl", "target": ".rlsbl/lint/python.toml"},
            {"template": "lint/go.toml.tpl", "target": ".rlsbl/lint/go.toml"},
            {"template": "lint/npm.toml.tpl", "target": ".rlsbl/lint/npm.toml"},
            {"template": "changes/unreleased.jsonl.tpl", "target": ".rlsbl/changes/unreleased.jsonl"},
        ]

    def check_project_exists(self, dir_path):
        return self.detect(dir_path)

    def get_project_init_hint(self):
        return ""

    def write_version(self, dir_path, version, ctx):
        """Write a new version to the target's version file(s).

        Returns a list of relative file paths (relative to dir_path) that
        were modified. Subclasses must override this method and return the
        actual paths written.
        """
        return []

    def build(self, dir_path, version):
        pass

    def publish(self, dir_path, version, ctx):
        pass

    def build_assets(self, dir_path, version, dist_dir, ctx):
        raise NotImplementedError(
            f"Asset builds not supported for target '{self.name}'"
        )

    def dev_install_command(self, project_dir):
        """Specs for local install via `rlsbl dev install`, keyed by mode.

        Subclasses override to return spec dicts for the "global" and/or
        "venv" modes. See the protocol docstring for the spec format.
        Default returns {"global": None, "venv": None} (unsupported).
        """
        return {"global": None, "venv": None}
