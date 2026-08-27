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

    lint_language: ClassVar[str | None] = None
    """Which library-lint language this target's sources are written in.

    The lint taxonomy (``python``, ``go``, ``npm``, ``maven`` -- see
    ``rlsbl.lint.languages``) is deliberately separate from the target
    taxonomy: ``pypi`` publishes ``python``, and one language can back several
    targets. This property is the single bridge between the two, replacing the
    hand-listed target sets the library-lint check used to carry.

    None means the target does not participate in library boundary lint.
    """

    @property
    def name(self):
        """Target registry name. Subclasses must override."""
        return "base"

    def detect(self, dir_path):
        """Return True when any declared ``detection_files`` entry exists here.

        This is the declared-manifest half of detection, and it is the whole
        story for a target whose presence is decided by a filename: npm by
        ``package.json``, Go by ``go.mod``, and so on. Those targets declare
        their filenames and inherit this method rather than restating the
        same ``os.path.exists`` call.

        Targets whose presence depends on file CONTENT -- Flutter and Dart
        sharing ``pubspec.yaml``, an Android application versus a Gradle
        library sharing ``build.gradle`` -- override this and inspect the
        file. A target that declares no detection files never auto-detects.
        """
        return any(
            os.path.exists(os.path.join(dir_path, filename))
            for filename in self.detection_files
        )

    def version_file(self, dir_path=None):
        """Return the relative path of the file that holds the project version."""
        return None

    def tag_format(self, version):
        """Return the git tag string for a standalone release version."""
        return f"v{version}"

    def monorepo_tag_format(self, name, version, path=None):
        """Return the git tag string for a monorepo package release."""
        return f"{name}@v{version}"

    def monorepo_tag_glob(self, name, path=None):
        """Return a glob pattern matching all version tags for a monorepo package."""
        return f"{name}@v*"

    def template_dir(self):
        """Return the path to this target's ecosystem-specific template directory."""
        return None

    def shared_template_dir(self):
        """Return the path to the shared template directory common to all targets."""
        templates = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "templates", "shared"
        )
        return templates

    def read_name(self, dir_path, ctx):
        """Read the project name from the target's manifest file."""
        return None

    def read_metadata(self, dir_path):
        """Read project metadata (license, description) from the manifest file."""
        return {}

    def template_vars(self, dir_path, ctx):
        """Return template variables extracted from the project for scaffold rendering."""
        return TemplateVars(self.name, {})

    def template_mappings(self, ctx):
        """Return the list of target-specific template-to-file mappings for scaffolding."""
        return []

    def shared_template_mappings(self, ctx):
        """Return template-to-file mappings shared across all targets."""
        mappings = [
            {"template": "CHANGELOG.md.tpl", "target": "CHANGELOG.md"},
            {"template": "gitignore.tpl", "target": ".gitignore"},
            {"template": "changes/unreleased.jsonl.tpl", "target": ".rlsbl/changes/unreleased.jsonl"},
        ]
        mappings.extend(self._lint_config_mappings(ctx))
        # Sandboxed test runner: emitted only for projects that declared the
        # test_sandbox config family (the stricttest floor's outer layer).
        from ..test_sandbox import runner_mapping

        runner = runner_mapping(getattr(ctx, "config", None))
        if runner is not None:
            mappings.append(runner)
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
        """Return True if the project's manifest file exists in dir_path."""
        return self.detect(dir_path)

    def get_project_init_hint(self):
        """Return a user-facing hint for initializing a project of this target type."""
        return ""

    def write_version(self, dir_path, version, ctx):
        """Write a new version to the target's version file(s).

        Returns a list of relative file paths (relative to dir_path) that
        were modified. Subclasses must override this method and return the
        actual paths written.
        """
        return []

    def _resolve_build_timeout(self, config):
        """Resolve the build timeout from config, then the shipped default.

        1. ``config["build_timeout"]`` -- an int, or a dict keyed by target
           name with an optional ``"default"`` entry
        2. ``self.BUILD_TIMEOUT_DEFAULT`` class variable

        There is deliberately no environment-variable layer: build budgets are
        declared in ``.rlsbl/config.json``, never picked up from the ambient
        environment.
        """
        if config is not None:
            bt = config.get("build_timeout")
            if bt is not None:
                if isinstance(bt, int) and not isinstance(bt, bool):
                    return bt
                if isinstance(bt, dict):
                    return bt.get(self.name, bt.get("default", self.BUILD_TIMEOUT_DEFAULT))

        return self.BUILD_TIMEOUT_DEFAULT

    def build(self, dir_path, version, *, config=None):
        """Build distributable artifacts for this target. No-op by default."""
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
        This is correct for npm, Go, Deno, plain, and most targets
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
        APIs (npm, pypi, go) override this to query the registry.
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

    def yank(self, project_dir, version, tag, *, reason=None, dry_run=False):
        """Remove a published version from this target's registry.

        Targets whose registry offers a removal action (npm's ``deprecate``,
        Go's ``retract`` directive, PyPI's manual yank) override this. The
        default answers UNSUPPORTED naming the target, so ``rlsbl release
        yank`` reports a target it cannot act on instead of passing over it.

        Returns a :class:`~.outcomes.YankOutcome`.
        """
        from .outcomes import YankOutcome, YankStatus

        return YankOutcome(
            status=YankStatus.UNSUPPORTED,
            message=f"target '{self.name}' has no registry-removal action",
        )
