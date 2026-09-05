"""Target introspection -- the axis inventory, the completeness assertions, and the committed support-matrix artifact every docs table is rendered from.

The release-target protocol is the single authority for what each target
supports. This module is where that authority is *enumerated*: one
``TargetAxis`` per support axis, each naming how the target answers it, and a
generator that asks every registered target every axis and serializes the
answers to a committed JSON file.

Two things rest on the enumeration being complete:

- **The artifact.** ``rlsbl/data/support-matrix.json`` is generated from here
  and committed. The docs directives read that file instead of importing
  rlsbl, so rendering the documentation no longer needs the package installed
  in the docs environment -- an import path that once broke a release when the
  selfdoc environment lost its rlsbl overlay.
- **The completeness assertions**, which run at import time. A registered
  target that cannot answer an axis is an error, and a support surface added
  to ``BaseTarget`` without a matching axis is an error too. Neither can be
  discovered later by a reader noticing a blank cell.
"""

import json
import os
from dataclasses import dataclass
from typing import Callable

from . import TARGETS
from .base import NOT_A_PROJECT_DIR, BaseTarget

# The artifact's own schema version. Bumped when the document shape changes,
# so a stale reader fails loudly instead of reading a renamed section as absent.
MATRIX_FORMAT_VERSION = 1

# Repo-relative location of the committed artifact. It sits under
# ``rlsbl/data/`` beside ``checks.toml``: that directory is the established
# home for shipped data files the docs read, and it ships inside the wheel.
MATRIX_RELPATH = os.path.join("rlsbl", "data", "support-matrix.json")

# The command that regenerates it, quoted in the freshness check's error.
MATRIX_REGEN_COMMAND = "uv run python scripts/generate_support_matrix.py"

# Placeholder strings fed to the format methods so the artifact records the
# SHAPE of each target's answer rather than one project's rendered values.
_NAME_PLACEHOLDER = "{name}"
_VERSION_PLACEHOLDER = "{version}"
_PATH_PLACEHOLDER = "{path}"

# A version with a pre-release segment, so a target whose ecosystem translates
# semver (PyPI's PEP 440) shows a different answer from the identity default.
_SAMPLE_VERSION = "1.2.3-rc.1"


@dataclass(frozen=True)
class TargetAxis:
    """One support axis, and how a target answers it.

    Attributes:
        name: the axis identifier, used as the key in the artifact.
        doc: one line saying what the axis means and how it is answered.
        answer: called with a target, returns a JSON-serializable answer.
        attr: the ``BaseTarget`` attribute this axis reads, when it is not
            spelled the same as the axis. The completeness assertion walks the
            protocol's attributes and looks each one up here, so an axis whose
            name differs from its source (``build_timeout_default`` reads
            ``BUILD_TIMEOUT_DEFAULT``) must say so or its source reads as
            unclassified.
    """

    name: str
    doc: str
    answer: Callable[[object], object]
    attr: str = ""

    @property
    def source_attr(self) -> str:
        """The protocol attribute this axis answers from."""
        return self.attr or self.name


def _prop(name: str):
    """Answer an axis by reading the target property of the same name."""
    return lambda target: getattr(target, name)


TARGET_AXES: tuple[TargetAxis, ...] = (
    # --- identity and versioning ---
    TargetAxis("ecosystem", "Human-readable name of the registry or platform, rendered in the docs support matrix.", lambda t: t.ecosystem),
    TargetAxis(
        "auto_detectable",
        "Whether detection runs without configuration: yes, no, or conditional.",
        lambda t: t.auto_detectable,
    ),
    TargetAxis(
        "detection_files",
        "Filenames whose presence in a directory declares this target.",
        lambda t: list(t.detection_files),
    ),
    TargetAxis(
        "content_based_detection",
        "Whether detection inspects file content (the target overrides detect).",
        lambda t: type(t).detect is not BaseTarget.detect,
        attr="detect",
    ),
    TargetAxis(
        "version_file",
        "File that holds the version, or null when the filename is "
        "per-project and cannot be stated statically.",
        lambda t: t.version_file(),
    ),
    TargetAxis(
        "tag_format",
        "Standalone release tag pattern.",
        lambda t: t.tag_format(_VERSION_PLACEHOLDER),
    ),
    TargetAxis(
        "monorepo_tag_format",
        "Monorepo release tag pattern.",
        lambda t: t.monorepo_tag_format(
            _NAME_PLACEHOLDER, _VERSION_PLACEHOLDER, _PATH_PLACEHOLDER
        ),
    ),
    TargetAxis(
        "monorepo_tag_glob",
        "Glob matching every monorepo version tag for a package.",
        lambda t: t.monorepo_tag_glob(_NAME_PLACEHOLDER, _PATH_PLACEHOLDER),
    ),
    TargetAxis(
        "companion_tags",
        "Extra tags created alongside the primary release tag.",
        lambda t: t.companion_tags(
            _NAME_PLACEHOLDER, _VERSION_PLACEHOLDER, _PATH_PLACEHOLDER
        ),
    ),
    TargetAxis(
        "format_version",
        f"How the ecosystem spells the semver version {_SAMPLE_VERSION}.",
        lambda t: t.format_version(_SAMPLE_VERSION),
    ),
    TargetAxis(
        "registry_display_name",
        "How this target's registry is spelled in user-facing output.",
        lambda t: t.registry_display_name,
    ),
    TargetAxis(
        "build_timeout_default",
        "Seconds allowed for this target's build before it is a timeout.",
        lambda t: t.BUILD_TIMEOUT_DEFAULT,
        attr="BUILD_TIMEOUT_DEFAULT",
    ),
    TargetAxis(
        "project_init_hint",
        "What a user is told to run to create a project of this target.",
        lambda t: t.get_project_init_hint(),
        attr="get_project_init_hint",
    ),
    # --- publishing authorization ---
    TargetAxis(
        "publisher_binds_to_repository",
        "Whether publishing is authorized for a REPOSITORY rather than for the "
        "package, so moving the code requires re-authorizing.",
        _prop("publisher_binds_to_repository"),
    ),
    TargetAxis(
        "publisher_setup_url",
        "Where a repository-bound publisher is registered; empty when none is.",
        _prop("publisher_setup_url"),
    ),
    TargetAxis(
        "consumed_by_repository_url",
        "Whether consumers resolve this target by repository URL and git tag "
        "rather than from a registry, so a monorepo member of this kind "
        "requires a standalone mirror to be consumable at all.",
        _prop("consumed_by_repository_url"),
    ),
    TargetAxis(
        "mirror_identity_files",
        "Manifests naming the repository the package lives in, which the "
        "mirror's scaffold commit rewrites to the mirror's own identity.",
        lambda t: list(t.mirror_identity_files),
    ),
    TargetAxis(
        "release_materialization_policy",
        "Whether a reconcile may recreate a released version's missing refs "
        "unconditionally, or must refuse when a recorded identity transition "
        "puts that version under a different published identity.",
        _prop("release_materialization_policy"),
    ),
    # --- manifest reading ---
    TargetAxis(
        "supports_read_name",
        "Reads a package name from its manifest (overrides read_name).",
        _prop("supports_read_name"),
    ),
    TargetAxis(
        "supports_read_metadata",
        "Reads license and description from its manifest (overrides read_metadata).",
        _prop("supports_read_metadata"),
    ),
    # --- registries ---
    TargetAxis(
        "supports_publication_probe",
        "Can ask its registry whether a version is published.",
        _prop("supports_publication_probe"),
    ),
    TargetAxis(
        "supports_cached_registry_probe",
        "Has a second, registry-side publication probe, because its primary "
        "one answers from somewhere other than the registry.",
        _prop("supports_cached_registry_probe"),
    ),
    TargetAxis(
        "supports_version_query",
        "Its registry answers a latest-version query.",
        _prop("supports_version_query"),
    ),
    TargetAxis(
        "supports_name_claim",
        "A name can be claimed on its registry by publishing a placeholder.",
        _prop("supports_name_claim"),
    ),
    TargetAxis(
        "claim_token_env_vars",
        "Environment variables, any one of which authenticates a name claim.",
        lambda t: list(t.claim_token_env_vars),
    ),
    TargetAxis(
        "supports_yank",
        "Its registry offers a removal action for a published version.",
        _prop("supports_yank"),
    ),
    # --- scaffolding and local development ---
    TargetAxis(
        "provides_ci_templates",
        "Ships ci.yml.tpl, so scaffold can generate a CI workflow.",
        _prop("provides_ci_templates"),
    ),
    TargetAxis(
        "supports_dev_install",
        "rlsbl dev install has something to run for this target.",
        _prop("supports_dev_install"),
    ),
    TargetAxis(
        "dev_install_command",
        "The local-install specs, keyed by mode (global, venv).",
        # NOT_A_PROJECT_DIR, never ".": the matrix records what a target's
        # install specs ARE, and a cell that changes with the operator's cwd
        # is not a per-target fact. See the constant for the whole reason.
        lambda t: t.dev_install_command(NOT_A_PROJECT_DIR),
    ),
    # --- source analysis, lint and tests ---
    TargetAxis(
        "supports_import_analysis",
        "rlsbl can read its sources to follow imports (overrides find_dead_modules).",
        _prop("supports_import_analysis"),
    ),
    TargetAxis(
        "supports_circular_dep_analysis",
        "Import-cycle detection is meaningful for this ecosystem.",
        _prop("supports_circular_dep_analysis"),
    ),
    TargetAxis(
        "supports_dep_floors",
        "Its manifest states dependency floors a lockfile can resolve ahead of.",
        _prop("supports_dep_floors"),
    ),
    TargetAxis(
        "lint_language",
        "Which library-lint language its sources are written in, or null.",
        lambda t: t.lint_language,
    ),
    TargetAxis(
        "has_builtin_test_runner",
        "Ships a built-in test runner (overrides run_tests).",
        _prop("has_builtin_test_runner"),
    ),
    TargetAxis(
        "shares_workspace_environment",
        "Workspace members of this target resolve into ONE shared environment.",
        _prop("shares_workspace_environment"),
    ),
)

AXIS_NAMES: tuple[str, ...] = tuple(axis.name for axis in TARGET_AXES)

# Every PUBLIC attribute of the protocol that is deliberately NOT an axis, each
# with the one line saying why. This is the whole exclusion list: the
# completeness assertion walks ``BaseTarget``'s public attributes and demands
# that each one is either an axis's source or named here.
#
# The polarity used to be the other way round -- only names starting with
# ``supports_``/``provides_``/``has_``/``shares_`` were policed -- so a
# per-target FACT named anything else (``lint_language`` historically, and the
# two ``publisher_*`` properties later) could be added to the protocol and never
# reach the matrix, with nothing failing. Naming what is excluded rather than
# guessing what is included cannot miss a fact by how it was spelled.
#
# Almost every entry is an OPERATION: something a target DOES, whose per-target
# fact (can it do it at all?) is an axis of its own.
NON_AXIS_ATTRIBUTES: dict[str, str] = {
    "name": "the target's own identity -- the matrix's row key, not a cell.",
    "build": "operation: builds the artifact.",
    "check_project_exists": "operation: asks a directory whether it holds this "
                            "kind of project (the fact is detection_files).",
    "claim_placeholder": "operation: publishes a placeholder to claim a name "
                         "(the fact is supports_name_claim).",
    "expected_refs": "operation: assembles ONE version's full ref set from a "
                     "version and a repository context. The per-target facts "
                     "it composes -- tag_format, monorepo_tag_format, "
                     "companion_tags -- are each already an axis; the assembly "
                     "is per-version, not per-target, so it has no cell.",
    "find_circular_dependencies": "operation: scans sources for import cycles "
                                  "(the fact is supports_circular_dep_analysis).",
    "find_dead_modules": "operation: scans sources for unreachable modules "
                         "(the fact is supports_import_analysis).",
    "normalize_package_name": "operation: folds one name into the form this "
                              "registry compares by; a function of its input.",
    "cached_registry_probe": "operation: asks the registry itself about one "
                             "version, for a target whose primary probe does "
                             "not (the fact is "
                             "supports_cached_registry_probe).",
    "publication_probe": "operation: asks the registry about one version "
                         "(the fact is supports_publication_probe).",
    "query_latest_version": "operation: asks the registry for the latest "
                            "version (the fact is supports_version_query).",
    "read_metadata": "operation: reads one project's manifest "
                     "(the fact is supports_read_metadata).",
    "read_name": "operation: reads one project's manifest "
                 "(the fact is supports_read_name).",
    "rewrite_mirror_identity": "operation: rewrites one clone's identity "
                               "manifests onto a mirror's repository identity "
                               "(the fact is mirror_identity_files).",
    "run_tests": "operation: runs one project's tests "
                 "(the fact is has_builtin_test_runner).",
    "shared_template_dir": "operation: resolves the shared scaffold templates "
                           "(the fact is provides_ci_templates).",
    "shared_template_mappings": "operation: builds one project's shared "
                                "template-to-file mappings.",
    "template_dir": "operation: resolves this target's scaffold templates "
                    "(the fact is provides_ci_templates).",
    "template_mappings": "operation: builds one project's target-specific "
                         "template-to-file mappings.",
    "template_vars": "operation: extracts one project's scaffold variables.",
    "write_version": "operation: writes a version into one project's files "
                     "(the fact is version_file).",
    "yank": "operation: removes a published version (the fact is supports_yank).",
}


def public_attributes(cls=BaseTarget) -> frozenset[str]:
    """Every public attribute of *cls* -- the set that must be classified."""
    return frozenset(name for name in dir(cls) if not name.startswith("_"))


def axis_source_attributes(axes=TARGET_AXES) -> frozenset[str]:
    """The protocol attributes the axes read."""
    return frozenset(axis.source_attr for axis in axes)


def unclassified_attributes(
    cls=BaseTarget, axes=TARGET_AXES, excluded=None,
) -> frozenset[str]:
    """Public attributes of *cls* that are neither an axis source nor excluded."""
    if excluded is None:
        excluded = NON_AXIS_ATTRIBUTES
    return public_attributes(cls) - axis_source_attributes(axes) - set(excluded)


def assert_axis_inventory_is_complete(
    cls=BaseTarget, axes=TARGET_AXES, excluded=None,
) -> None:
    """Every public attribute of the protocol is an axis or an excluded operation.

    This is the "a new fact must reach the matrix" direction, and it is stated
    by exclusion so that no naming convention can hide one: adding anything
    public to the base class without either giving it an axis or excluding it
    with a reason is an error at import time, not a column that quietly never
    appears. An exclusion naming an attribute that no longer exists is an error
    too -- a stale exclusion is an unpoliced surface waiting to be re-added.
    """
    if excluded is None:
        excluded = NON_AXIS_ATTRIBUTES
    unclassified = sorted(unclassified_attributes(cls, axes, excluded))
    if unclassified:
        raise RuntimeError(
            f"public attributes on {cls.__name__} that are neither a matrix "
            f"axis nor an excluded operation: {', '.join(unclassified)}. Add a "
            f"TargetAxis for each fact and regenerate the matrix with "
            f"`{MATRIX_REGEN_COMMAND}`, or add it to NON_AXIS_ATTRIBUTES in "
            f"rlsbl/targets/introspect.py with the one line saying why it is "
            f"not a per-target fact."
        )
    stale = sorted(set(excluded) - public_attributes(cls))
    if stale:
        raise RuntimeError(
            f"NON_AXIS_ATTRIBUTES names attributes {cls.__name__} does not "
            f"have: {', '.join(stale)}. Remove them -- an exclusion that "
            f"matches nothing polices nothing."
        )
    missing_reason = sorted(name for name, why in excluded.items() if not why.strip())
    if missing_reason:
        raise RuntimeError(
            f"NON_AXIS_ATTRIBUTES entries with no justification: "
            f"{', '.join(missing_reason)}. Every exclusion states why the "
            f"attribute is not a per-target fact."
        )


def target_axis_answers(registry=None, axes=TARGET_AXES) -> dict:
    """Ask every registered target every axis.

    Returns ``{target_name: {axis_name: answer}}``. Raises naming the target
    and the axis when a target cannot answer one -- a registered target that
    does not implement the whole protocol is a hard error, never a blank cell.
    """
    if registry is None:
        registry = TARGETS

    answers: dict[str, dict] = {}
    for target_name in sorted(registry):
        target = registry[target_name]
        row: dict = {}
        for axis in axes:
            try:
                value = axis.answer(target)
            except Exception as exc:
                raise RuntimeError(
                    f"target '{target_name}' cannot answer the "
                    f"'{axis.name}' support axis: {type(exc).__name__}: {exc}"
                ) from exc
            try:
                json.dumps(value)
            except TypeError as exc:
                raise RuntimeError(
                    f"target '{target_name}' answered the '{axis.name}' axis "
                    f"with a value the matrix cannot serialize: {value!r}"
                ) from exc
            row[axis.name] = value
        answers[target_name] = row
    return answers


def assert_every_target_answers_every_axis(registry=None, axes=TARGET_AXES) -> None:
    """Completeness in the target direction: no registered target may be short."""
    target_axis_answers(registry=registry, axes=axes)


# ---------------------------------------------------------------------------
# The rendered tables the docs directives consume
# ---------------------------------------------------------------------------

# Support columns and the property each one asks. The table used to read a
# hand-declared ``capabilities`` frozenset, which had drifted from the code:
# several targets implemented read_metadata without declaring it, and the
# swift-apple row's dev-install cell was blank for the opposite reason.
SUPPORT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("read_name", "supports_read_name"),
    ("read_metadata", "supports_read_metadata"),
    ("ci_templates", "provides_ci_templates"),
)

HEADERS = [
    "Name", "Ecosystem", "Detection files", "Version file", "Auto-detectable",
    "Tag format", "Monorepo tag format",
    *(column for column, _prop_name in SUPPORT_COLUMNS),
    "dev_install",
]

# Targets whose detection is decided by file content rather than by a
# filename, with the phrasing the table shows for them.
_DETECTION_OVERRIDES = {
    "plain": "VERSION (conditional)",
    "flutter": "pubspec.yaml (flutter)",
}


def _format_dev_install(specs) -> str:
    """Format the dev_install_command answer into a compact string."""
    parts = []
    for mode in ("global", "venv"):
        spec = specs.get(mode)
        if spec is not None:
            tool = spec["tool"]
            args = " ".join(spec["args"])
            parts.append(f"{mode}: {tool} {args}")

    return ", ".join(parts)


def generate_target_table_data(answers=None) -> tuple[list[str], list[list[str]]]:
    """Generate raw data for a markdown table of all release targets.

    Returns ``(headers, rows)`` where each row has one cell per header, sorted
    alphabetically by target name. Every cell comes from the axis answers, so
    the table cannot describe a target differently from the matrix.
    """
    if answers is None:
        answers = target_axis_answers()

    rows: list[list[str]] = []

    for target_name in sorted(answers):
        row_answers = answers[target_name]

        detection = _DETECTION_OVERRIDES.get(target_name)
        if detection is None:
            files = row_answers["detection_files"]
            detection = ", ".join(files) if files else "---"

        version_file = row_answers["version_file"] or "---"
        tag_format = row_answers["tag_format"] or "---"

        support_cells = [
            "✓" if row_answers[prop_name] else ""
            for _column, prop_name in SUPPORT_COLUMNS
        ]

        rows.append([
            target_name,
            row_answers["ecosystem"],
            detection,
            version_file,
            row_answers["auto_detectable"],
            tag_format,
            row_answers["monorepo_tag_format"],
            *support_cells,
            _format_dev_install(row_answers["dev_install_command"]),
        ])

    return list(HEADERS), rows


# ---------------------------------------------------------------------------
# The committed artifact
# ---------------------------------------------------------------------------


def _check_scopes() -> dict:
    """The check-vs-target scope map, in the artifact's shape."""
    from ..checks import CHECK_EXCLUDED_TARGETS, CHECK_TARGETS, MATRIX_COLUMNS

    scopes: dict[str, dict] = {}
    for check_name, targets in CHECK_TARGETS.items():
        if targets is None:
            entry = {"kind": "universal", "targets": list(MATRIX_COLUMNS)}
        elif isinstance(targets, str):
            entry = {"kind": targets, "targets": []}
        else:
            entry = {"kind": "targets", "targets": sorted(targets)}
        excluded = CHECK_EXCLUDED_TARGETS.get(check_name)
        if excluded:
            entry["excluded"] = dict(sorted(excluded.items()))
        scopes[check_name] = entry
    return scopes


def build_matrix() -> dict:
    """Build the whole support matrix as a plain JSON-serializable document.

    Three registries feed it: the release targets (every axis above), the
    check-vs-target scope map, and the publish pipelines. Each also
    contributes the exact ``headers``/``rows`` its docs table renders, so the
    directives are pure renderers with no derivation of their own.
    """
    from ..checks import MATRIX_COLUMNS, generate_feature_matrix_data
    from ..pipelines.introspect import generate_pipeline_table_data

    answers = target_axis_answers()
    target_headers, target_rows = generate_target_table_data(answers)
    matrix_headers, matrix_rows = generate_feature_matrix_data()
    pipeline_headers, pipeline_rows = generate_pipeline_table_data()

    return {
        "format_version": MATRIX_FORMAT_VERSION,
        "generated_by": "rlsbl.targets.introspect.build_matrix",
        "regenerate_with": MATRIX_REGEN_COMMAND,
        "axes": [
            {"name": axis.name, "doc": axis.doc} for axis in TARGET_AXES
        ],
        "targets": answers,
        "target_order": list(MATRIX_COLUMNS),
        "checks": _check_scopes(),
        "tables": {
            "axes": {
                "headers": ["Axis", "What it says about a target"],
                "rows": [[axis.name, axis.doc] for axis in TARGET_AXES],
            },
            "targets": {"headers": target_headers, "rows": target_rows},
            "feature_matrix": {"headers": matrix_headers, "rows": matrix_rows},
            "pipelines": {"headers": pipeline_headers, "rows": pipeline_rows},
        },
    }


def render_matrix() -> str:
    """Serialize the matrix deterministically.

    Sorted keys, a fixed indent and a trailing newline: two runs on the same
    registry produce byte-identical text, which is what makes the freshness
    check a regenerate-and-compare rather than a structural diff.
    """
    return (
        json.dumps(build_matrix(), indent=2, sort_keys=True, ensure_ascii=False)
        + "\n"
    )


def matrix_path(project_root: str) -> str:
    """Absolute path of the committed artifact inside *project_root*."""
    return os.path.join(str(project_root), MATRIX_RELPATH)


def write_matrix(project_root: str) -> bool:
    """Write the artifact into *project_root*. Returns True when it changed."""
    path = matrix_path(project_root)
    fresh = render_matrix()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            if f.read() == fresh:
                return False
    from .. import effects

    effects.makedirs(os.path.dirname(path), exist_ok=True)
    effects.atomic_write_text(path, fresh)
    return True


# Completeness runs on import, in both directions: no registered target may be
# unable to answer an axis, and no support surface on the protocol may be
# missing one.
assert_axis_inventory_is_complete()
assert_every_target_answers_every_axis()
