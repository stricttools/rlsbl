"""Base class for release targets providing shared defaults for version reading, writing, detection, scaffolding, and publish configuration."""

import os
from typing import ClassVar

# The scaffold template that makes a target's directory a source of CI
# workflows. Its presence is what ``provides_ci_templates`` answers from.
CI_TEMPLATE_FILENAME = "ci.yml.tpl"

# The closed vocabulary of ``release_materialization_policy``. A ref the ledger
# records but the remote does not carry may either be recreated unconditionally,
# or only when the version's published identity still matches the repository's
# current one.
MATERIALIZE_ALWAYS = "materialize"
MATERIALIZE_UNLESS_IDENTITY_CHANGED = "refuse-identity-transition"
MATERIALIZATION_POLICIES = (
    MATERIALIZE_ALWAYS,
    MATERIALIZE_UNLESS_IDENTITY_CHANGED,
)


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
    ecosystem: ClassVar[str] = ""
    auto_detectable: ClassVar[str] = "yes"
    BUILD_TIMEOUT_DEFAULT: ClassVar[int] = 120

    publisher_binds_to_repository: ClassVar[bool] = False
    """Whether this registry's publishing authorization names the REPOSITORY.

    False for a registry authenticated by a token the project carries: moving
    the code to another repository changes nothing about who may publish it.
    True where the authorization is a statement about a specific repository and
    workflow -- PyPI Trusted Publishing is the case that exists -- so a project
    that moves must be re-authorized before its next release, and rlsbl cannot
    do it (registering a publisher is an act on an external system).

    Declared rather than introspected: it is a fact about the registry's auth
    model, not something the target's methods reveal.
    """

    publisher_setup_url: ClassVar[str] = ""
    """Where a repository-bound publisher is registered. Empty when none is."""

    lint_language: ClassVar[str | None] = None
    """Which library-lint language this target's sources are written in.

    The lint taxonomy (``python``, ``go``, ``npm``, ``maven`` -- see
    ``rlsbl.lint.languages``) is deliberately separate from the target
    taxonomy: ``pypi`` publishes ``python``, and one language can back several
    targets. This property is the single bridge between the two, replacing the
    hand-listed target sets the library-lint check used to carry.

    None means the target does not participate in library boundary lint.
    """

    release_materialization_policy: ClassVar[str] = MATERIALIZE_ALWAYS
    """Whether a released version's MISSING refs may simply be recreated.

    ``rlsbl release reconcile`` materializes a ref the ledger records as
    released but the remote does not carry. For most ecosystems that is a pure
    repair: the tag names a version, and pushing it publishes nothing that was
    not already released.

    For Go it is not. A Go tag IS the module's published artifact -- the proxy
    resolves ``<module path>@<tag>`` and caches the result permanently -- so a
    tag pushed under a module path the repository has since CHANGED publishes
    an old version under the new identity, which was never released and can
    never be withdrawn. Go therefore declares
    :data:`MATERIALIZE_UNLESS_IDENTITY_CHANGED`, and the reconciler refuses to
    materialize any version whose identity a recorded ``go-module-path``
    transition places on the other side of the change.

    Declared rather than introspected: it is a fact about what publishing means
    in the ecosystem, not something the target's methods reveal.
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
        """Read project metadata (license, description) from the manifest file.

        The default is empty, and that is the right answer for every ecosystem
        whose manifest carries no license or description (Go modules, Swift
        packages, deno.json, Dockerfiles, ...). Those targets do NOT override
        this to return an empty dict of their own: not overriding it is what
        makes ``supports_read_metadata`` answer honestly.
        """
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

    # --- The full ref set of one released version ---

    def expected_refs(self, version, context):
        """Every git ref *version* owns: the primary tag, companions, aliases.

        THE single authority for the question. The release flow creates and
        pushes exactly this set, and the ``unpublished-refs`` check renders
        exactly this set against the repository and its remote -- one
        derivation, so a ref the release creates can never be a ref the check
        does not look for.

        *context* is a :class:`~rlsbl.targets.refs.RefContext` built by
        :func:`~rlsbl.targets.refs.ref_context`. Returns an
        :class:`~rlsbl.targets.refs.ExpectedRefs`.

        Not overridden by any target: the per-target facts it composes
        (``tag_format``, ``monorepo_tag_format``, ``companion_tags``) are the
        axes, and this is the assembly of them.
        """
        from .refs import ExpectedRefs, recorded_aliases

        primary = self._primary_ref(version, context)
        return ExpectedRefs(
            version=version,
            primary=primary,
            companions=self._companion_refs(version, context, primary),
            aliases=recorded_aliases(context, version),
        )

    def _primary_ref(self, version, context):
        """The one tag the release itself is named after.

        Three naming authorities, in precedence order: a releasable's declared
        ``tag_format``, a monorepo package's target-derived
        ``monorepo_tag_format``, and a standalone repository's ``tag_format``.
        """
        if context.primary_tag_format:
            return context.primary_tag_format.format(
                name=context.releasable_name or "", version=version,
            )
        if context.monorepo_name:
            return self.monorepo_tag_format(
                context.monorepo_name, version, path=context.project_path,
            )
        return self.tag_format(version)

    def _companion_refs(self, version, context, primary):
        """The extra tags this release's members' ecosystems require.

        Only a releasable release has members to ask, which is why
        ``member_package_paths`` being None -- rather than empty -- means "no
        companions", exactly as the release flow's own guard did.

        Two rules, both inherited from the collector this replaced:

        * A primary tag that is ALREADY Go-compatible (it contains ``/v``)
          suppresses companions entirely, so a Go-shaped release does not
          duplicate its own tag.
        * A publish-suppressed member (``publish_mode: "none"``) contributes
          nothing -- there is no proxy to satisfy for something never published.

        A member whose config cannot be resolved is a HARD ERROR, matching the
        version-sync plan: the two must agree on the member set, and silently
        skipping one here would tag a release the sync path would have refused.
        """
        if context.member_package_paths is None:
            return ()
        if "/v" in primary:
            return ()

        from . import TARGETS
        from ..member_context import resolve_member_context

        seen: set[str] = set()
        found: list[str] = []
        for pkg_path in context.member_package_paths:
            abs_pkg = os.path.join(context.repo_root, pkg_path)
            if not os.path.isdir(abs_pkg):
                continue
            member = resolve_member_context(
                abs_pkg, releasable_config_dir=context.releasable_config_dir,
            )
            if member.publish_mode == "none":
                continue
            for entry in member.targets or ():
                target = TARGETS.get(entry.name)
                if target is None:
                    continue
                for tag in target.companion_tags(entry.name, version, path=pkg_path):
                    if tag != primary and tag not in seen:
                        seen.add(tag)
                        found.append(tag)
        return tuple(found)

    def normalize_package_name(self, raw_name):
        """Reduce a package name to the form this registry compares by.

        Registries differ in what they consider "the same name": PyPI folds
        runs of ``-_.`` to a single hyphen (PEP 503), npm removes them
        entirely, Go compares the last path segment of a module path. A
        cross-target name-consistency check must ask each target rather than
        keep a dict keyed by target name.

        The default lowercases, which is the right answer for a registry with
        no normalization rules of its own.
        """
        return raw_name.lower()

    def query_latest_version(self, name):
        """Ask this target's registry for the latest published version.

        Returns a dict with ``status`` ``"found"`` (plus ``version``),
        ``"not_found"``, or ``"error"`` (plus ``message``) -- the shape
        ``rlsbl.registry`` has always used.

        The default answers ``error`` naming the target rather than returning
        None: a caller comparing a local version against "the registry" must
        never mistake "this ecosystem has no version API" for "the package is
        unpublished".
        """
        return {
            "status": "error",
            "message": f"Unknown registry: {self.name}",
        }

    def claim_placeholder(self, name, tmpdir):
        """Publish a minimal placeholder package to reserve *name*.

        Targets whose registry accepts a publish override this. The default
        raises: a target that cannot claim a name must not be reachable from
        ``rlsbl claim-name``, and ``claimable_targets()`` derives the
        command's accepted set from exactly this method.
        """
        raise NotImplementedError(
            f"target '{self.name}' cannot claim a name on its registry"
        )

    claim_token_env_vars: ClassVar[tuple[str, ...]] = ()
    """Environment variables, any one of which authenticates a name claim.

    Empty for a target that cannot claim names at all.
    """

    @property
    def registry_display_name(self):
        """How to spell this target's registry in user-facing output.

        Defaults to the target name, which is already right for npm and most
        others. PyPI capitalises and Go's index has a different name entirely,
        so they override. This replaced a display dict keyed by target name.
        """
        return self.name

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

    def cached_registry_probe(self, dir_path, version, ctx=None):
        """Ask the REGISTRY ITSELF whether a version is out in the world.

        A second probe, deliberately narrower than :meth:`publication_probe`.
        It exists because a target's primary probe does not have to ask the
        registry: Go's asks the git remote whether the version's tag exists,
        which is the right question for "did we tag this?" and the wrong one
        for "can anyone still download this?" -- ``proxy.golang.org`` caches a
        module version permanently the first time it is resolved, so a deleted
        tag reads as never-published while the proxy goes on serving it.

        THE CONTRACT IS TWO-VALUED, not three: PUBLISHED, or UNPROBEABLE.
        This probe only ever ADDS positive evidence. A registry that indexes
        lazily is absent-by-default for a version nobody has fetched yet, so
        its silence must never be reported as UNPUBLISHED -- that would let
        registry lag clear a destructive operation.

        The default returns UNPROBEABLE. The fact is
        ``supports_cached_registry_probe``.
        """
        from ..publication_probe import PublicationProbeResult, PublicationStatus
        return PublicationProbeResult(
            status=PublicationStatus.UNPROBEABLE,
            registry=self.name,
            version=version,
            message=(
                f"target '{self.name}' has no registry-side probe beyond its "
                f"primary one"
            ),
        )

    def dev_install_command(self, project_dir):
        """Specs for local install via `rlsbl dev install`, keyed by mode.

        Subclasses override to return spec dicts for the "global" and/or
        "venv" modes. See the protocol docstring for the spec format.
        Default returns {"global": None, "venv": None} (unsupported).
        """
        return {"global": None, "venv": None}

    # -- Derived capability answers -----------------------------------------
    #
    # These replaced a hand-declared ``capabilities: frozenset[str]``, which
    # had already drifted from the code it described: several targets
    # implemented ``read_metadata`` without declaring it, and two of the
    # strings it could hold (``publish``, ``build_assets``) were declared by
    # no target and read by nothing.
    #
    # Each answer is derived per axis, by whichever means is honest for that
    # axis: whether the class overrides a method, whether a method actually
    # yields anything, or whether the shipped templates contain the file. None
    # of them is a stored declaration that can disagree with the behaviour.
    #
    # There is deliberately no ``getattr(target, ..., default)`` form for any
    # of these. Four call sites decide whether to run a publication probe from
    # ``supports_publication_probe``, and a silent default at any of them would
    # answer "no evidence" for a target that can in fact answer.

    @property
    def supports_publication_probe(self):
        """Whether this target can ask its registry if a version is published."""
        return type(self).publication_probe is not BaseTarget.publication_probe

    @property
    def supports_cached_registry_probe(self):
        """Whether this target has a registry-side probe beyond its primary one."""
        return (
            type(self).cached_registry_probe
            is not BaseTarget.cached_registry_probe
        )

    @property
    def supports_read_name(self):
        """Whether this target can read a package name out of its manifest."""
        return type(self).read_name is not BaseTarget.read_name

    @property
    def supports_read_metadata(self):
        """Whether this target can read license/description from its manifest."""
        return type(self).read_metadata is not BaseTarget.read_metadata

    @property
    def supports_dev_install(self):
        """Whether ``rlsbl dev install`` has anything to run for this target.

        Behavioural rather than override-based: a subclass can inherit a
        ``dev_install_command`` whose specs resolve to nothing for it, and the
        honest answer there is "no".
        """
        specs = self.dev_install_command(".")
        return any(specs.get(mode) is not None for mode in ("global", "venv"))

    @property
    def provides_ci_templates(self):
        """Whether this target ships a CI workflow template.

        Answered from the template directory rather than declared: a target
        provides CI templates exactly when its template directory contains
        ``ci.yml.tpl``, which is the file the scaffold renders into
        ``.github/workflows/ci.yml``.
        """
        return self._has_template(CI_TEMPLATE_FILENAME)

    def _has_template(self, filename):
        """Whether this target's template directory ships *filename*."""
        directory = self.template_dir()
        if directory is None:
            return False
        return os.path.isfile(os.path.join(directory, filename))

    shares_workspace_environment: ClassVar[bool] = False
    """Whether workspace members of this target share ONE resolved environment.

    True for uv/Python, where every member of a uv workspace installs into a
    single ``.venv`` -- which is why a sync at the workspace root has to
    exclude every member's dev overlays at once, and why buildability is
    checked at the root rather than per member. False for ecosystems whose
    members each resolve independently.
    """

    supports_dep_floors: ClassVar[bool] = False
    """Whether this target's manifest states dependency floors a lock resolves.

    True for the ecosystems whose manifest declares minimum versions that a
    lockfile can silently resolve ahead of -- the condition the ``dep-floors``
    check exists to police. Declared rather than introspected: it is a fact
    about the manifest format, not about any method here.
    """

    @property
    def supports_import_analysis(self):
        """Whether rlsbl can read this target's sources to follow imports.

        Derived from the target implementing ``find_dead_modules``: the
        dead-module detectors and the workspace dependency checks
        (``deps-unused`` and friends) both rest on the same import scanners,
        so a target that can answer one can answer the others.
        """
        return type(self).find_dead_modules is not BaseTarget.find_dead_modules

    @property
    def supports_circular_dep_analysis(self):
        """Whether cycle detection is meaningful for this target's ecosystem.

        Derived from the ``find_circular_dependencies`` override. Go
        deliberately does not implement it: the compiler already rejects
        circular imports, so a checker would only ever agree with it.
        """
        return (
            type(self).find_circular_dependencies
            is not BaseTarget.find_circular_dependencies
        )

    def find_dead_modules(self, root, *, exclude_dirs=None, suppress=frozenset()):
        """Find source files or packages nothing else references.

        Returns a list of ``(path, reason)`` pairs, where *reason* is the
        ecosystem-specific explanation shown to the user ("not imported by any
        other module", "not reachable from any entry point", ...). The default
        returns nothing: a target with no import scanner has no opinion.

        Args:
            root: project root to scan.
            exclude_dirs: sibling directories to keep out of the scan.
            suppress: declared exclusions (legitimate non-entry points),
                threaded into the detector where the detector supports it so a
                listed file cannot keep other modules alive.
        """
        return []

    def find_circular_dependencies(self, root, *, exclude_dirs=None):
        """Find import cycles within this target's sources.

        Returns a list of cycles, each a list of module identifiers. The
        default returns nothing.
        """
        return []

    @property
    def supports_version_query(self):
        """Whether this target's registry can be asked for a latest version.

        Derived from the ``query_latest_version`` override.
        ``rlsbl.targets.targets_with_version_queries()`` is the set form.
        """
        return type(self).query_latest_version is not BaseTarget.query_latest_version

    @property
    def supports_name_claim(self):
        """Whether ``rlsbl claim-name`` can reserve a name on this registry.

        Derived from the ``claim_placeholder`` override.
        ``rlsbl.targets.claimable_targets()`` is the set form.
        """
        return type(self).claim_placeholder is not BaseTarget.claim_placeholder

    @property
    def supports_yank(self):
        """Whether this target's registry offers a removal action.

        Derived from the ``yank`` override. The base answers UNSUPPORTED, so a
        target that does not override it has nothing to run.
        """
        return type(self).yank is not BaseTarget.yank

    @property
    def has_builtin_test_runner(self):
        """Whether this target ships a built-in test runner.

        Derived from the override rather than declared, so the answer cannot
        drift from the method. Callers that need the SET of such targets ask
        ``rlsbl.targets.targets_with_builtin_tests()``.
        """
        return type(self).run_tests is not BaseTarget.run_tests

    def run_tests(
        self,
        *,
        project_dir=None,
        workspace_root=None,
        skip_sync=False,
        config=None,
        check_timeout=None,
    ):
        """Run this target's built-in test suite.

        Targets whose ecosystem has a standard test command (``uv run
        pytest``, ``go test``, ``npm test``, the Gradle/Maven test task)
        override this. The default answers SKIPPED naming the target.

        That default is the whole point of the method. The name chain this
        replaced ended in a bare ``return True``, so a release of a project
        whose target has no runner recorded a PASSING test step for a suite
        that never ran.

        Returns a :class:`~.outcomes.SuiteRunOutcome`.
        """
        from .outcomes import SuiteRunOutcome, SuiteRunStatus

        return SuiteRunOutcome(
            status=SuiteRunStatus.SKIPPED,
            message=f"target '{self.name}' has no built-in test runner",
        )

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
