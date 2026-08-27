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

    # There is no ``capabilities`` attribute. What a target supports is
    # answered per axis by asking the target -- ``supports_publication_probe``,
    # ``supports_read_name``, ``supports_read_metadata``,
    # ``supports_dev_install``, ``provides_ci_templates`` -- each derived from
    # the implementation rather than stored beside it. A declared set could
    # disagree with the code it described, and did.

    @property
    def supports_publication_probe(self) -> bool:
        """Whether ``publication_probe`` gives a real answer for this target."""
        ...

    @property
    def supports_read_name(self) -> bool:
        """Whether ``read_name`` reads a real name for this target."""
        ...

    @property
    def supports_read_metadata(self) -> bool:
        """Whether ``read_metadata`` reads real metadata for this target."""
        ...

    @property
    def supports_dev_install(self) -> bool:
        """Whether ``dev_install_command`` yields a spec for any mode."""
        ...

    @property
    def provides_ci_templates(self) -> bool:
        """Whether this target ships a CI workflow template to scaffold."""
        ...

    @property
    def supports_import_analysis(self) -> bool:
        """Whether rlsbl can read this target's sources to follow imports."""
        ...

    @property
    def supports_circular_dep_analysis(self) -> bool:
        """Whether cycle detection is meaningful for this target's ecosystem."""
        ...

    @property
    def has_builtin_test_runner(self) -> bool:
        """Whether this target ships a built-in test runner."""
        ...

    @property
    def supports_version_query(self) -> bool:
        """Whether this target's registry answers a latest-version query."""
        ...

    @property
    def supports_name_claim(self) -> bool:
        """Whether a name can be claimed on this target's registry."""
        ...

    @property
    def supports_yank(self) -> bool:
        """Whether this target's registry offers a removal action."""
        ...

    ecosystem: str
    """Ecosystem identifier (e.g. 'node', 'python', 'go', 'jvm')."""

    auto_detectable: str
    """Whether this target can be auto-detected: 'yes', 'no', or 'conditional'."""

    detection_files: tuple[str, ...]
    """Filenames whose presence in a directory indicates this kind of project.

    Consumed by the default ``detect()`` and by the derived
    ``checks.PROJECT_MANIFESTS`` set. Empty for a target that never
    auto-detects, or whose detection inspects file CONTENT instead.
    """

    lint_language: str | None
    """Which library-lint language this target's sources are written in.

    The lint taxonomy (``python``, ``go``, ``npm``, ``maven``) is deliberately
    separate from the target taxonomy; this is the single bridge between them.
    None means the target does not participate in library boundary lint.
    """

    shares_workspace_environment: bool
    """Whether workspace members of this target share ONE resolved environment."""

    supports_dep_floors: bool
    """Whether this target's manifest states dependency floors a lock resolves.

    Declared rather than introspected: it is a fact about the manifest format.
    """

    claim_token_env_vars: tuple[str, ...]
    """Environment variables, any one of which authenticates a name claim.

    Empty for a target that cannot claim names at all.
    """

    BUILD_TIMEOUT_DEFAULT: int
    """Seconds allowed for this target's build before it is a timeout."""

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

    def companion_tags(self, name: str, version: str, path: str | None = None) -> list[str]:
        """Return additional tags to create alongside the primary release tag.

        Ecosystems that require extra tags (e.g. Go module proxy tags)
        override this. The default returns no companion tags.
        """
        return []

    def format_version(self, version: str) -> str:
        """Translate a semver version into this ecosystem's version format.

        The default is the identity, which is correct for npm, Go, Deno and
        most others. PyPI overrides it for PEP 440.
        """
        return version

    # --- Registry identity and queries ---

    def normalize_package_name(self, raw_name: str) -> str:
        """Reduce a package name to the form this registry compares by.

        PyPI folds runs of ``-_.`` to a single hyphen, npm removes them, Go
        compares the last path segment. The default lowercases.
        """
        return raw_name.lower()

    def query_latest_version(self, name: str) -> dict:
        """Ask this target's registry for the latest published version.

        Returns a dict with ``status`` ``"found"`` (plus ``version``),
        ``"not_found"``, or ``"error"`` (plus ``message``). The default
        answers ``error`` naming the target, so "this ecosystem has no version
        API" is never mistaken for "the package is unpublished".
        """
        ...

    def claim_placeholder(self, name: str, tmpdir: str) -> str:
        """Publish a minimal placeholder package to reserve *name*.

        Targets whose registry accepts a publish override this; the default
        raises, and ``claimable_targets()`` derives the accepted set from
        exactly this method.
        """
        ...

    @property
    def registry_display_name(self) -> str:
        """How to spell this target's registry in user-facing output."""
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
                Zig, Swift).

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

    # --- Optional: Source analysis ---

    def find_dead_modules(
        self, root: str, *, exclude_dirs=None, suppress=frozenset(),
    ) -> list[tuple[str, str]]:
        """Find source files or packages nothing else references.

        Returns ``(path, reason)`` pairs, where *reason* is the
        ecosystem-specific explanation shown to the user. The default returns
        nothing: a target with no import scanner has no opinion, and
        ``supports_import_analysis`` is derived from this override.
        """
        return []

    def find_circular_dependencies(self, root: str, *, exclude_dirs=None) -> list[list[str]]:
        """Find import cycles within this target's sources.

        Returns a list of cycles, each a list of module identifiers. The
        default returns nothing, and ``supports_circular_dep_analysis`` is
        derived from this override.
        """
        return []

    # --- Optional: Test suite ---

    def run_tests(
        self,
        *,
        project_dir: str | None = None,
        workspace_root: str | None = None,
        skip_sync: bool = False,
        config: dict | None = None,
        check_timeout: int | None = None,
    ):
        """Run this target's built-in test suite.

        Returns a ``SuiteRunOutcome``. The default answers SKIPPED naming the
        target, so a project whose target ships no runner records a visible
        skip rather than a passing step for a suite that never ran.
        """
        ...

    # --- Optional: Registry removal ---

    def yank(self, project_dir: str, version: str, tag: str, *,
             reason: str | None = None, dry_run: bool = False):
        """Remove a published version from this target's registry.

        Returns a ``YankOutcome``. The default answers UNSUPPORTED naming the
        target, so ``rlsbl release yank`` reports a target it cannot act on
        instead of passing over it.
        """
        ...
