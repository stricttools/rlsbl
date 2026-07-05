"""Release target protocol defining the formal interface that all target implementations must satisfy for detection, versioning, and scaffolding."""

from typing import Protocol, runtime_checkable


@runtime_checkable
class ReleaseTarget(Protocol):
    """Protocol defining a release target.

    Targets handle version management, scaffolding templates, and optionally
    build/publish steps for a specific ecosystem (npm, pypi, go, codehome, docs, etc.)
    """

    @property
    def name(self) -> str:
        """Unique identifier for this target (e.g. 'npm', 'pypi', 'codehome')."""
        ...

    capabilities: frozenset[str]
    """Set of capabilities this target supports (e.g. 'publish', 'build_assets', 'dev_install')."""

    ecosystem: str
    """Ecosystem identifier (e.g. 'node', 'python', 'go', 'jvm')."""

    auto_detectable: str
    """Whether this target can be auto-detected: 'yes', 'no', or 'conditional'."""

    def detect(self, dir_path: str) -> bool:
        """Check if this target is present/applicable in the given directory."""
        ...

    def read_version(self, dir_path: str) -> str:
        """Read the current version from the target's manifest file."""
        ...

    def read_name(self, dir_path: str, ctx) -> str | None:
        """Read the project's package name from the manifest, or None."""
        return None

    def read_metadata(self, dir_path: str) -> dict[str, str]:
        """Read optional metadata (license, description) from the manifest."""
        return {}

    def write_version(self, dir_path: str, version: str, ctx) -> None:
        """Write a new version to the target's manifest file (atomic)."""
        ...

    def version_file(self, dir_path: str | None = None) -> str | None:
        """Filename that holds the version (e.g. 'package.json'), or None if inherited.

        When dir_path is provided, implementations may resolve the filename
        dynamically (e.g. Deno choosing between deno.json and deno.jsonc).
        """
        ...

    def tag_format(self, version: str) -> str:
        """Format the git tag for a release. Returns f'v{version}' by default."""
        ...

    def monorepo_tag_format(self, name: str, version: str, path: str | None = None) -> str:
        """Format the git tag for a monorepo release. Default: f'{name}@v{version}'."""
        ...

    def monorepo_tag_glob(self, name: str, path: str | None = None) -> str:
        """Return a glob pattern matching all monorepo version tags. Default: f'{name}@v*'."""
        ...

    # --- Optional: Scaffold support ---

    def template_dir(self) -> str | None:
        """Absolute path to target-specific template directory, or None."""
        return None

    def shared_template_dir(self) -> str | None:
        """Absolute path to shared template directory, or None."""
        return None

    def template_vars(self, dir_path: str, ctx) -> dict[str, str]:
        """Extract template placeholder values from the project."""
        return {}

    def template_mappings(self, ctx) -> list[dict[str, str]]:
        """Target-specific template-to-output-path mappings."""
        return []

    def shared_template_mappings(self, ctx) -> list[dict[str, str]]:
        """Shared template-to-output-path mappings."""
        return []

    def check_project_exists(self, dir_path: str) -> bool:
        """Check if the target's project file exists (alias for detect)."""
        return self.detect(dir_path)

    def get_project_init_hint(self) -> str:
        """Human-readable hint for initializing a project for this target."""
        return ""

    # --- Optional: Publication probe ---

    def publication_probe(self, dir_path: str, version: str, ctx=None):
        """Probe the registry to determine if a specific version is published.

        Returns a PublicationProbeResult (PUBLISHED, UNPUBLISHED, or UNPROBEABLE).
        Default: UNPROBEABLE.
        """
        ...

    # --- Optional: Build ---

    def build(self, dir_path: str, version: str, *, config: dict | None = None) -> None:
        """Pre-publish build step (e.g. generate docs). No-op by default."""
        pass

    # --- Optional: Developer-mode local install ---

    def dev_install_command(self, project_dir: str) -> dict[str, dict | None]:
        """Return the local-install specs for this target, keyed by mode.

        Used by `rlsbl dev install` to install/uninstall the project for local
        development. Returns a dict with two keys:

            "global": spec for the global-install mode, where the project is
                installed as a globally-available tool or symlink (e.g.
                `uv tool install -e .`, `npm link`, `go install`). None if the
                target has no global-install concept.

            "venv": spec for the local/venv-install mode, where dependencies
                are fetched into the project's own environment without exposing
                a global CLI (e.g. `uv sync`, `npm install`). None for targets
                that have no separate local-environment concept (e.g. Go,
                Cargo, Zig, Swift).

        Each spec dict has the shape:
            {
                "tool": "uv",
                "args": ["tool", "install", "-e", "."],
                "uninstall_args_template": ["tool", "uninstall", "{name}"],
                "purpose": "for editable Python install",
            }

        Fields:
            tool: CLI tool that must be on PATH.
            args: argv passed to the tool to install.
            uninstall_args_template: argv list of templates passed to the tool
                to uninstall. Each entry may contain `{name}` (replaced with
                the project's package name) or `{dir}` (replaced with the
                project directory basename). None means uninstall is not
                supported for this mode of this target.
            purpose: human-readable string for the require_tool error message.

        The returned dict may also carry an optional top-level "reason" key:
        a human-readable explanation for why the modes are None (e.g. "Go
        library: nothing to install"). `rlsbl dev install` surfaces it in the
        skip message instead of the generic "not yet supported" line.
        """
        return {"global": None, "venv": None}
