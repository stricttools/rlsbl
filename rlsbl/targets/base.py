"""Base class for release targets providing shared defaults for version reading, writing, detection, scaffolding, and publish configuration."""

import os
from typing import ClassVar


class TemplateVars(dict):
    """Dict subclass that auto-generates namespaced ``{target}.{key}`` entries.

    On construction, for every key in *base_dict*, an additional entry
    ``"{target_name}.{key}"`` is stored so templates can reference
    target-specific values like ``{{pypi.minRequiredPython}}``.

    Post-construction mutations (``tv["newkey"] = val``) produce bare-only
    keys -- this is correct for non-target-specific additions like ``year``
    or ``repoName`` that callers add after the target returns its vars.
    """

    def __init__(self, target_name: str, base_dict: dict | None = None):
        if base_dict is None:
            base_dict = {}
        super().__init__(base_dict)
        self._target_name = target_name
        for key, value in base_dict.items():
            ns_key = f"{target_name}.{key}"
            super().__setitem__(ns_key, value)


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
    BUILD_TIMEOUT_DEFAULT: ClassVar[int] = 120

    @property
    def name(self):
        """Target registry name. Subclasses must override."""
        return "base"

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
        return TemplateVars(self.name, {})

    def template_mappings(self, ctx):
        return []

    def shared_template_mappings(self, ctx):
        mappings = [
            {"template": "CHANGELOG.md.tpl", "target": "CHANGELOG.md"},
            {"template": "gitignore.tpl", "target": ".gitignore"},
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
        if not ctx or not hasattr(ctx, "config") or not ctx.config:
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

    def _resolve_build_timeout(self, config):
        """Resolve build timeout through 4-level precedence.

        1. ``RLSBL_BUILD_TIMEOUT_{TARGET_NAME}`` env var
        2. ``RLSBL_BUILD_TIMEOUT`` env var
        3. ``config["build_timeout"]`` (int or dict with target-name / "default" keys)
        4. ``self.BUILD_TIMEOUT_DEFAULT`` class variable
        """
        # Level 1: target-specific env var
        env_specific = os.environ.get(f"RLSBL_BUILD_TIMEOUT_{self.name.upper()}")
        if env_specific is not None:
            return int(env_specific)

        # Level 2: generic env var
        env_generic = os.environ.get("RLSBL_BUILD_TIMEOUT")
        if env_generic is not None:
            return int(env_generic)

        # Level 3: config file
        if config is not None:
            bt = config.get("build_timeout")
            if bt is not None:
                if isinstance(bt, int):
                    return bt
                if isinstance(bt, dict):
                    return bt.get(self.name, bt.get("default", self.BUILD_TIMEOUT_DEFAULT))

        # Level 4: class default
        return self.BUILD_TIMEOUT_DEFAULT

    def build(self, dir_path, version, *, config=None):
        pass

    def companion_tags(self, name, version, path=None):
        """Return additional tags to create alongside the primary release tag.

        Ecosystems that require extra tags (e.g. Go module proxy tags)
        override this to return a list of tag strings.  The default
        implementation returns no companion tags.

        Args:
            name: the releasable or project name.
            version: the version being released (without ``v`` prefix).
            path: workspace-relative path to the package directory, or
                None for standalone projects.

        Returns:
            List of tag strings to create alongside the primary tag.
        """
        return []

    def format_version(self, version):
        """Format a semver version for this target's ecosystem.

        The default implementation returns the version unchanged (identity).
        This is correct for npm, Go, Cargo, Deno, plain, and most targets
        where semver is used directly.

        Targets with different version conventions (e.g. PyPI's PEP 440)
        override this to translate from semver to the ecosystem format.
        """
        return version

    def publication_probe(self, dir_path, version, ctx=None):
        """Probe the registry to determine if a specific version is published.

        Returns a PublicationProbeResult with one of three statuses:
            PUBLISHED: the version exists on the registry.
            UNPUBLISHED: the version does not exist on the registry.
            UNPROBEABLE: this target cannot probe (no API, no name, etc.).

        The default implementation returns UNPROBEABLE. Targets with registry
        APIs (npm, pypi, go, cargo) override this to query the registry.
        """
        from ..publication_probe import PublicationProbeResult, PublicationStatus
        return PublicationProbeResult(
            status=PublicationStatus.UNPROBEABLE,
            registry=self.name,
            version=version,
            message=f"target '{self.name}' does not support publication probing",
        )

    def dev_install_command(self, project_dir):
        """Specs for local install via `rlsbl dev install`, keyed by mode.

        Subclasses override to return spec dicts for the "global" and/or
        "venv" modes. See the protocol docstring for the spec format.
        Default returns {"global": None, "venv": None} (unsupported).
        """
        return {"global": None, "venv": None}
