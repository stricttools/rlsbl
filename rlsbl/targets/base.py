"""Base class for release targets providing shared defaults for version reading, writing, detection, scaffolding, and publish configuration."""

import os
from typing import ClassVar


class BaseTarget:
    """Concrete base providing defaults for optional Protocol methods.

    Subclasses should override ``detection_files`` with the filenames whose
    existence in a directory indicates a project of that type.  The tuple is
    used both by the target's own ``detect()`` method and by
    ``checks.PROJECT_MANIFESTS`` (derived automatically from the registry).
    """

    detection_files: ClassVar[tuple[str, ...]] = ()
    capabilities: ClassVar[frozenset[str]] = frozenset()
    ecosystem: ClassVar[str] = ""
    auto_detectable: ClassVar[str] = "yes"

    def version_file(self, dir_path=None):
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
        mappings = [
            {"template": "CHANGELOG.md.tpl", "target": "CHANGELOG.md"},
            {"template": "gitignore.tpl", "target": ".gitignore"},
            {"template": "hooks/pre-checks.sh.tpl", "target": ".rlsbl/hooks/pre-checks.sh"},
            {"template": "hooks/pre-release.sh.tpl", "target": ".rlsbl/hooks/pre-release.sh"},
            {"template": "hooks/post-release.sh.tpl", "target": ".rlsbl/hooks/post-release.sh"},
            {"template": "changes/unreleased.jsonl.tpl", "target": ".rlsbl/changes/unreleased.jsonl"},
        ]
        mappings.extend(self._lint_config_mappings(ctx))
        return mappings

    def _lint_config_mappings(self, ctx):
        """Return lint config mappings filtered by declared targets.

        If no targets are configured, all 3 lint configs are included
        for backward compatibility with unconfigured projects.
        """
        all_lint = [
            ("pypi", {"template": "lint/python.toml.tpl", "target": ".rlsbl/lint/python.toml"}),
            ("npm", {"template": "lint/npm.toml.tpl", "target": ".rlsbl/lint/npm.toml"}),
            ("go", {"template": "lint/go.toml.tpl", "target": ".rlsbl/lint/go.toml"}),
        ]
        targets = self._extract_target_names(ctx)
        if not targets:
            return [mapping for _, mapping in all_lint]
        return [mapping for target, mapping in all_lint if target in targets]

    @staticmethod
    def _extract_target_names(ctx):
        """Extract target name strings from ctx.config["targets"].

        Returns a set of target names, or an empty set if targets
        is not configured or ctx is unavailable.
        """
        if not ctx or not ctx.config:
            return set()
        raw = ctx.config.get("targets")
        if not raw or not isinstance(raw, list):
            return set()
        names = set()
        for entry in raw:
            if isinstance(entry, str):
                names.add(entry)
            elif isinstance(entry, dict):
                name = entry.get("name")
                if name:
                    names.add(name)
        return names

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

    def dev_install_command(self, project_dir):
        """Specs for local install via `rlsbl dev install`, keyed by mode.

        Subclasses override to return spec dicts for the "global" and/or
        "venv" modes. See the protocol docstring for the spec format.
        Default returns {"global": None, "venv": None} (unsupported).
        """
        return {"global": None, "venv": None}
