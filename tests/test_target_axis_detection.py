"""Per-axis conformance: detection.

Targets declared ``detection_files`` and then re-implemented the same
``os.path.exists`` calls in a hand-written ``detect()``, a duplication of the
registry.

``BaseTarget.detect`` now consumes ``detection_files``. Targets that inspect
file CONTENT keep their overrides -- that is the honest half of the axis, and
this file pins it.
"""

import pytest

from rlsbl.targets import TARGETS
from rlsbl.targets.base import BaseTarget

# Targets that legitimately keep their own detect(), each with the reason.
# Every other target must inherit the base implementation and let its declared
# detection_files answer. A new entry here needs a real justification: "the
# declared filenames cannot express this rule" or "the override documents a
# deliberate narrowing".
DECLARED_DETECT_OVERRIDES = {
    "pypi": "pyproject.toml without a [project] table is a uv virtual root",
    "spec": "version.json in the project root OR in a spec/ subdirectory",
}


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
        if n not in DECLARED_DETECT_OVERRIDES and TARGETS[n].detection_files
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
    """An empty directory belongs to nobody."""
    d = tmp_path / name
    d.mkdir()
    assert not TARGETS[name].detect(str(d))
