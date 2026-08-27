"""Per-axis conformance: detection.

Detection had two duplications of the registry. Targets declared
``detection_files`` and then re-implemented the same ``os.path.exists`` calls
in a hand-written ``detect()``; and ``PlainTarget`` carried a hand-maintained
list of "manifests belonging to some other target" that had to be edited every
time a target was added or retired.

``BaseTarget.detect`` now consumes ``detection_files``, and plain's stand-off
set is derived from the registry plus a declared extras set for manifests that
belong to no current target. Targets that inspect file CONTENT keep their
overrides -- that is the honest half of the axis, and this file pins it.
"""


import pytest

from rlsbl.targets import TARGETS
from rlsbl.targets.base import BaseTarget
from rlsbl.targets.plain import _EXTRA_FOREIGN_MANIFESTS, _foreign_manifests

# Targets that legitimately keep their own detect(), each with the reason.
# Every other target must inherit the base implementation and let its declared
# detection_files answer. A new entry here needs a real justification: "the
# declared filenames cannot express this rule" or "the override documents a
# deliberate narrowing".
DECLARED_DETECT_OVERRIDES = {
    "pypi": "pyproject.toml without a [project] table is a uv virtual root",
    "dart": "pubspec.yaml WITHOUT a flutter: section",
    "flutter": "pubspec.yaml WITH a flutter: section",
    "maven": "build.gradle that is not an Android application",
    "native-android": "build.gradle that IS an Android application",
    "native-ios": ".pbxproj / Tuist discovery, and Package.swift rejects it",
    "spec": "version.json in the project root OR in a spec/ subdirectory",
    "swift-apple": "never auto-detects; must be declared in config",
    "plain": "VERSION plus the absence of every foreign manifest",
    "pgdesign": (
        "keeps an explicit override whose docstring records that detection "
        "looks only at the directory it is handed and never descends into a "
        "schema/ subdirectory"
    ),
}

# The subset whose override actually changes the answer relative to the base
# implementation. pgdesign's does not -- it is documentation -- so it is
# excluded from the base-equivalence exemption below.
DETECT_EQUIVALENT_TO_BASE = {"pgdesign"}


@pytest.mark.parametrize("name", sorted(TARGETS))
def test_filename_only_targets_inherit_base_detect(name):
    """A target decided by filename alone must not hand-roll detect()."""
    target = TARGETS[name]
    overrides = type(target).detect is not BaseTarget.detect
    if name in DECLARED_DETECT_OVERRIDES:
        assert overrides, (
            f"'{name}' declares a detect() override reason "
            f"({DECLARED_DETECT_OVERRIDES[name]}) but inherits the base "
            f"implementation; the declaration is stale"
        )
    else:
        assert not overrides, (
            f"'{name}' hand-rolls detect(). If it is decided by filename alone, "
            f"declare detection_files and delete the override; if the override "
            f"is justified, add it to DECLARED_DETECT_OVERRIDES with the reason"
        )


@pytest.mark.parametrize(
    "name",
    sorted(
        n
        for n in TARGETS
        if (n not in DECLARED_DETECT_OVERRIDES or n in DETECT_EQUIVALENT_TO_BASE)
        and TARGETS[n].detection_files
    ),
)
def test_base_detect_finds_each_declared_manifest(tmp_path, name):
    """Every declared detection file, on its own, detects the target."""
    target = TARGETS[name]
    for filename in target.detection_files:
        d = tmp_path / f"{name}-{filename.replace('/', '_')}"
        d.mkdir()
        (d / filename).write_text("")
        assert target.detect(str(d)), f"{name} did not detect {filename}"


@pytest.mark.parametrize("name", sorted(TARGETS))
def test_no_target_detects_an_empty_directory(tmp_path, name):
    """An empty directory belongs to nobody -- including plain (no VERSION)."""
    d = tmp_path / name
    d.mkdir()
    assert not TARGETS[name].detect(str(d))


class TestPlainStandOff:
    """Plain's foreign-manifest set is derived, not hand-maintained."""

    def test_derived_set_covers_every_other_target_manifest(self):
        derived = _foreign_manifests()
        for name, target in TARGETS.items():
            if name == "plain":
                continue
            for filename in target.detection_files:
                assert filename in derived, (
                    f"plain would auto-detect over {name}'s {filename}"
                )

    def test_extras_are_only_manifests_no_target_claims(self):
        """The declared extras must not restate something the registry knows."""
        registry_manifests = {
            f
            for name, t in TARGETS.items()
            if name != "plain"
            for f in t.detection_files
        }
        overlap = _EXTRA_FOREIGN_MANIFESTS & registry_manifests
        assert not overlap, (
            f"{sorted(overlap)} is declared as an extra but a registered target "
            f"already declares it; delete the extra"
        )

    def test_plain_detects_a_bare_version_file(self, tmp_path):
        (tmp_path / "VERSION").write_text("1.0.0\n")
        assert TARGETS["plain"].detect(str(tmp_path))

    @pytest.mark.parametrize("manifest", sorted(_foreign_manifests()))
    def test_plain_stands_off_every_foreign_manifest(self, tmp_path, manifest):
        d = tmp_path / manifest.replace("/", "_")
        d.mkdir()
        (d / "VERSION").write_text("1.0.0\n")
        (d / manifest).write_text("")
        assert not TARGETS["plain"].detect(str(d))

    def test_a_new_target_teaches_plain_without_editing_plain(self, monkeypatch):
        """Registering a target extends the stand-off set automatically."""

        class FakeTarget(BaseTarget):
            detection_files = ("Wibble.toml",)

            @property
            def name(self):
                return "wibble"

        import rlsbl.targets as targets_pkg

        _foreign_manifests.cache_clear()
        monkeypatch.setitem(targets_pkg.TARGETS, "wibble", FakeTarget())
        try:
            assert "Wibble.toml" in _foreign_manifests()
        finally:
            _foreign_manifests.cache_clear()
