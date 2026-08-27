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
from .base import BaseTarget

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
    """

    name: str
    doc: str
    answer: Callable[[object], object]


def _prop(name: str):
    """Answer an axis by reading the target property of the same name."""
    return lambda target: getattr(target, name)


TARGET_AXES: tuple[TargetAxis, ...] = (
    # --- identity and versioning ---
    TargetAxis("ecosystem", "Ecosystem label shown in listings.", lambda t: t.ecosystem),
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
    ),
    TargetAxis(
        "version_file",
        "File that holds the version, or null when the target inherits one.",
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
        lambda t: t.dev_install_command("."),
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

# Name prefixes that mark a support surface on the protocol. Every attribute on
# ``BaseTarget`` whose name starts with one of these must have an axis, so a new
# support question cannot be added to the protocol and left out of the matrix.
SUPPORT_PREFIXES: tuple[str, ...] = ("supports_", "provides_", "has_", "shares_")


def declared_support_surfaces(cls=BaseTarget) -> frozenset[str]:
    """Public attributes of *cls* whose names mark a support axis."""
    return frozenset(
        name
        for name in dir(cls)
        if not name.startswith("_") and name.startswith(SUPPORT_PREFIXES)
    )


def assert_axis_inventory_is_complete(cls=BaseTarget, axes=TARGET_AXES) -> None:
    """Every support surface the protocol declares must have an axis.

    This is the "a new axis must cover every target" direction: adding a
    ``supports_*`` property to the base class without adding it here is an
    error at import time, not a column that quietly never appears.
    """
    axis_names = {axis.name for axis in axes}
    missing = sorted(declared_support_surfaces(cls) - axis_names)
    if missing:
        raise RuntimeError(
            f"support surfaces on {cls.__name__} with no axis in TARGET_AXES: "
            f"{', '.join(missing)}. Add a TargetAxis for each in "
            f"rlsbl/targets/introspect.py, then regenerate the matrix with "
            f"`{MATRIX_REGEN_COMMAND}`."
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
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(fresh)
    os.replace(tmp, path)
    return True


# Completeness runs on import, in both directions: no registered target may be
# unable to answer an axis, and no support surface on the protocol may be
# missing one.
assert_axis_inventory_is_complete()
assert_every_target_answers_every_axis()
