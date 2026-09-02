"""Structural guard: target-name literals may not spread into new modules.

The release-target registry is the single authority for what each target
supports. Every place outside the targets package that tests a target NAME to
decide behaviour -- ``if name == "npm"``, ``{"pypi", "go"} & target_names``, a
dict keyed by target names used as a dispatch -- is a second copy of that
authority, free to drift from it. Several did, which is why the axes were
migrated onto the protocol.

This guard is a ratchet, not a clean-slate assertion. The migration converted
the axes it was scoped to; the remainder is recorded below as a baseline, and
the guard fails when a module introduces a NEW target-name conditional or adds
another instance of an existing one. Removing one is always allowed: the check
is that the current findings are a sub-multiset of the baseline.

Two module groups are exempt entirely:

- ``rlsbl/targets/`` -- the registry itself, where naming a target is the
  whole point;
- ``rlsbl/checks/__init__.py`` -- the check-to-target matrix, whose display
  ordering (``MATRIX_COLUMNS``) is asserted complete against ``TARGETS`` at
  import time and whose scope sets are derived from protocol properties.

Detection is AST-based (see ``scripts/sweep_target_name_literals.py``), so a
target name inside a docstring, an error message or a URL is not a finding
while ``if name == "go":`` is. The implementation is shared with that script
rather than restated here, so the guard and the inventory can never disagree
about what counts.

Known blind spots, all shared with the sweep:

- **Indirection.** ``NPM = "npm"`` bound at module level and compared later is
  invisible: the guard sees only literals.
- **Computed names.** f-strings, ``str.format``, names arriving from config or
  from ``argv`` are invisible.
- **Non-Python surfaces.** Config schemas, YAML workflow templates and the
  scaffold templates are not scanned at all.
- **Coincidental collisions.** ``"flutter"`` as a *pubspec.yaml key* reads
  exactly like the target name and is reported as a finding in the dart and
  flutter targets; both are inside the exempt package, but the same collision
  is possible for any short target name elsewhere. Several other taxonomies
  share spellings with target names and are equally invisible to the sweep:

  | Taxonomy | Where it collides |
  | --- | --- |
  | PIPELINE types (``npm``, ``go``, ``pypi``, ...) | ``rlsbl/pipelines/``, ``rlsbl/config.py``, ``rlsbl/commands/release/validate.py`` |
  | LINT LANGUAGES (``python``, ``go``, ``npm``, ``maven``) | ``rlsbl/go_introspect.py`` |
  | strictcli implementation LANGUAGES (``python``, ``go``, ``typescript``) | ``rlsbl/commands/release/validate.py`` |
  | LAUNCHER entry types (``npm``, ``pypi``) | ``rlsbl/checks/project.py`` |

  Baseline entries of that kind carry a comment saying so, per the
  instruction in ``test_no_new_module_tests_a_target_name``. An entry with no
  such comment is an un-migrated real target-name conditional.
- **Multiplicity within a fingerprint.** The baseline counts findings per
  (module, category, name-tuple). Replacing one ``if registry == "pypi"`` with
  a different ``if registry == "pypi"`` in the same module is invisible; only
  the total per fingerprint is pinned.
"""

import collections
import importlib.util
import os

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SWEEP_SCRIPT = os.path.join(REPO_ROOT, "scripts", "sweep_target_name_literals.py")

# Modules where naming a target is the authority itself, not a copy of it.
EXEMPT_PREFIXES = (
    "rlsbl/targets/",
    "rlsbl/checks/__init__.py",
)

# Every remaining target-name conditional outside the exempt modules, as of the
# protocol migration. The guard allows fewer, never more, and never a module
# that is not listed. Entries here are NOT approved designs -- they are the
# un-migrated remainder, recorded so it cannot grow.
LEGACY_BASELINE: dict[str, dict[tuple[str, tuple[str, ...]], int]] = {
    "rlsbl/checks/project.py": {
        # Spelling collision, not the target registry: both compare a
        # LAUNCHER entry's declared ``type`` (a config taxonomy of its own)
        # to decide which manifest keys that launcher kind must carry.
        ("compare", ('npm',)): 1,
        ("compare", ('pypi',)): 1,
    },
    "rlsbl/commands/check.py": {
        ("compare", ('go',)): 3,
        ("compare", ('npm',)): 3,
        ("compare", ('pypi',)): 6,
    },
    "rlsbl/commands/init_cmd.py": {
        ("compare", ('go',)): 3,
        ("compare", ('npm',)): 6,
        ("dispatch_key", ('go', 'npm', 'pypi')): 1,
    },
    "rlsbl/commands/monorepo/batch_release_init.py": {
        ("compare", ('flutter',)): 1,
    },
    "rlsbl/commands/release/phase_a.py": {
        # A real target-name conditional, NOT a collision: the ecosystem
        # keyword tagger picks between ensure_npm_keyword and
        # ensure_pypi_keyword from the plan step's payload ``kind``, which is
        # a target name (the planner indexes TARGETS with it). Un-migrated
        # remainder -- the tagger belongs on the protocol.
        ("compare", ('npm',)): 1,
    },
    "rlsbl/commands/release/validate.py": {
        # Spelling collision: the strictcli implementation LANGUAGE taxonomy
        # (python / go / typescript) picking the --dump-schema argv.
        ("compare", ('go',)): 1,
        # Spelling collision: an npm PIPELINE type declaring ``provenance``.
        ("compare", ('npm',)): 1,
    },
    "rlsbl/commands/release_init.py": {
        ("compare", ('flutter',)): 1,
    },
    "rlsbl/config.py": {
        ("compare", ('go',)): 1,
        ("compare", ('npm',)): 2,
        ("compare", ('pypi',)): 1,
    },
    "rlsbl/go_introspect.py": {
        ("compare", ('go',)): 2,
    },
    "rlsbl/pipelines/__init__.py": {
        ("dispatch_key", ('deno', 'docker', 'go', 'hex', 'maven', 'npm', 'pypi')): 1,
    },
    "rlsbl/pipelines/introspect.py": {
        ("dispatch_key", ('deno', 'docker', 'go', 'hex', 'maven', 'npm', 'pypi')): 1,
    },
    "rlsbl/publish_gate.py": {
        ("dispatch_key", ('deno', 'docker', 'go', 'hex', 'maven', 'npm', 'pypi', 'spec', 'swift', 'swift-apple', 'zig')): 1,
    },
    "rlsbl/release_file.py": {
        ("compare", ('flutter',)): 2,
    },
    "rlsbl/tagging.py": {
        ("compare", ('npm',)): 1,
        ("compare", ('pypi',)): 1,
    },
}


def _sweep():
    """Run the inventory sweep, sharing the script's implementation."""
    spec = importlib.util.spec_from_file_location("_sweep_tool", SWEEP_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.sweep(os.path.join(REPO_ROOT, "rlsbl"))


def _current():
    """Findings outside the exempt modules, as {module: Counter(fingerprint)}."""
    found = collections.defaultdict(collections.Counter)
    for f in _sweep():
        if f["file"].startswith(EXEMPT_PREFIXES):
            continue
        found[f["file"]][(f["category"], tuple(f["names"]))] += 1
    return found


CURRENT = _current()


def test_no_new_module_tests_a_target_name():
    """A module not in the baseline must not start naming targets."""
    unexpected = sorted(set(CURRENT) - set(LEGACY_BASELINE))
    assert not unexpected, (
        "these modules newly test a target NAME to decide behaviour:\n  "
        + "\n  ".join(unexpected)
        + "\n\nAsk the target instead: add a property or method to "
        "ReleaseTarget and derive the answer from the registry. If the "
        "literal is a PIPELINE type or a LINT LANGUAGE that merely shares a "
        "spelling with a target, say so in a comment and add the module to "
        "LEGACY_BASELINE with that reason."
    )


@pytest.mark.parametrize("module", sorted(LEGACY_BASELINE))
def test_no_module_grows_more_target_name_conditionals(module):
    """An existing site may be removed, never multiplied."""
    baseline = LEGACY_BASELINE[module]
    current = CURRENT.get(module, collections.Counter())

    grew = {
        fingerprint: (count, baseline.get(fingerprint, 0))
        for fingerprint, count in current.items()
        if count > baseline.get(fingerprint, 0)
    }
    assert not grew, (
        f"{module} gained target-name conditionals:\n  "
        + "\n  ".join(
            f"{fingerprint}: {now} now, {was} in the baseline"
            for fingerprint, (now, was) in sorted(grew.items())
        )
        + "\n\nAsk the target instead of testing its name."
    )


@pytest.mark.parametrize("module", sorted(LEGACY_BASELINE))
def test_the_baseline_does_not_outlive_what_it_records(module):
    """A module fully migrated must be deleted from the baseline.

    Otherwise the baseline slowly becomes a list of modules nobody has touched
    in years, and a regression into one of them goes unnoticed.
    """
    assert module in CURRENT, (
        f"{module} no longer has any target-name conditional -- delete its "
        f"LEGACY_BASELINE entry so a regression there fails again"
    )


def test_the_exempt_modules_are_the_registry_and_the_matrix():
    """The exemption list is deliberately tiny; widening it needs a reason."""
    assert EXEMPT_PREFIXES == ("rlsbl/targets/", "rlsbl/checks/__init__.py")


def test_the_sweep_finds_something_at_all():
    """A sweep that silently matched nothing would make this guard vacuous."""
    assert _sweep(), "the AST sweep produced no findings; it is broken"
