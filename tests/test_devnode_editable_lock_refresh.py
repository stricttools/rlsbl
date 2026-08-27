"""A dev_node's editable-sibling uv.lock is refreshed by the bump that stales it.

A monorepo can hold a non-releasable project -- a conformance harness, a
cross-implementation test node -- that installs a releasable sibling through an
EDITABLE uv path source. Its ``uv.lock`` therefore records the sibling's current
version, and such a node commonly ships a meta-test asserting the lock's
recorded version equals the sibling's declared one.

The version bump is the moment that lock goes stale. Left unrefreshed, the
release candidate carries a sibling at the new version and a dev_node lock
pinning the old one: the dev_node's lock-pin test goes red at the CI gate, on a
candidate that is already pushed. The fix-forward for it touches ONLY the
dev_node's path, so on resume every releasable's path-filtered job concludes
skipped -- the wedge this test exists to prevent.

The bump is also the only moment the release can fix it as part of the candidate
commit, so it does: every non-releasable workspace project whose ``uv.lock``
resolves a path source into a directory this release bumps gets ``uv lock`` run
in it, and the refreshed lock joins the version-bump commit.
"""

import json
import os
import shutil
from unittest.mock import patch

from rlsbl.commands.release import phase_a
from rlsbl.workspace import Releasable, save_workspace, write_releasable_version

from conftest import with_root_member


PYPROJECT = """\
[project]
name = "{name}"
version = "{version}"
"""

# The shape uv writes for an editable path dependency on a workspace sibling.
UV_LOCK = """\
version = 1
requires-python = ">=3.11"

[[package]]
name = "conformance"
version = "0.0.0"
source = {{ editable = "." }}

[[package]]
name = "impl"
version = "{pinned}"
source = {{ editable = "{path}" }}
"""


class _State:
    """The slice of ReleaseState build_phase_a_plan reads."""

    current_version = "0.40.0"
    new_version = "0.41.0"
    tag = "alpha@v0.41.0"
    branch = "main"
    quiet = False
    releasable_name = "alpha"
    member_package_paths = ["python"]
    monorepo_project_path = "python"
    monorepo_name = "impl"
    changes_dir = None
    hook_generated = ()
    pre_existing_dirty = None
    flags: dict = {}


def _workspace(root, *, lock_target="../python", pinned="0.40.0",
               dev_node=True):
    """A releasable ``python/`` and a dev_node ``conformance/`` that locks it."""
    (root / "python").mkdir()
    (root / "python" / "pyproject.toml").write_text(
        PYPROJECT.format(name="impl", version="0.40.0")
    )
    (root / "conformance").mkdir()
    (root / "conformance" / "pyproject.toml").write_text(
        PYPROJECT.format(name="conformance", version="0.0.0")
    )
    (root / "conformance" / "uv.lock").write_text(
        UV_LOCK.format(pinned=pinned, path=lock_target)
    )

    conformance = {"path": "conformance", "name": "conformance"}
    releasables = [Releasable(name="alpha")]
    if dev_node:
        conformance["dev_node"] = True
        conformance["dev_only"] = True
        conformance["releasable"] = False
    else:
        # A releasable sibling belongs to a releasable of its own.
        conformance["releasable"] = "beta"
        releasables.append(Releasable(name="beta"))
    save_workspace(
        str(root),
        with_root_member([
            {"path": "python", "name": "impl", "releasable": "alpha"},
            conformance,
        ]),
        releasables=releasables,
    )
    write_releasable_version(str(root), "alpha", "0.40.0")
    rel_dir = root / ".rlsbl-monorepo" / "releasables" / "alpha"
    rel_dir.mkdir(parents=True, exist_ok=True)
    (rel_dir / "config.json").write_text(
        json.dumps({"publish_mode": "ci", "targets": ["pypi"], "pipelines": {}})
        + "\n"
    )
    return root / "python"


def _plan(root):
    project_dir = str(root / "python")
    return phase_a.build_phase_a_plan(phase_a.BuildInputs(
        state=_State(),
        ctx=type("C", (), {"config": {}, "project_root": project_dir})(),
        log=lambda _m: None,
        project_dir=project_dir, git_root=str(root),
        monorepo_root=str(root),
        releasable_cfg_dir=str(root / ".rlsbl-monorepo" / "releasables" / "alpha"),
        rep_is_private=False,
        registry="pypi", primary_path=project_dir,
        target_paths={"pypi": project_dir}, secondary_targets={},
        state_path=str(root / "in-progress.json"),
        completed=set(), pin_sha=None, baseline_dirty=set(),
        commit_msg="alpha@v0.41.0", lock_dir=".rlsbl",
    ))


def _uv_lock_syncs(plan):
    return [
        s.payload for s in plan.steps
        if s.kind == phase_a.SYNC_LOCKFILE and s.payload["lockfile"] == "uv.lock"
    ]


def _with_uv(func, *args, **kwargs):
    """Run *func* with ``uv`` answered as present on PATH."""
    real_which = shutil.which

    def fake_which(name, *a, **kw):
        return "/usr/bin/uv" if name == "uv" else real_which(name, *a, **kw)

    with patch("shutil.which", side_effect=fake_which):
        return func(*args, **kwargs)


class TestDevNodeLockJoinsTheBump:

    def test_uv_lock_runs_in_the_dev_node(self, tmp_path):
        _workspace(tmp_path)
        plan = _with_uv(_plan, tmp_path)

        dirs = [os.path.normpath(p["cwd"]) for p in _uv_lock_syncs(plan)]
        assert os.path.normpath(str(tmp_path / "conformance")) in dirs, (
            f"the dev_node's lock records the bumped sibling editable and must "
            f"be refreshed by the bump; uv lock ran in {dirs}"
        )

    def test_the_refreshed_lock_joins_the_version_bump_commit(self, tmp_path):
        _workspace(tmp_path)
        plan = _with_uv(_plan, tmp_path)

        expected = os.path.normpath(str(tmp_path / "conformance" / "uv.lock"))
        committed = [os.path.normpath(f) for f in plan.files_to_commit]
        assert expected in committed, (
            f"a refreshed lock left out of the commit is the same stale "
            f"candidate; commit set: {committed}"
        )

    def test_only_uv_lock_is_run_in_the_dev_node(self, tmp_path):
        """The dev_node is not a release target: only its uv.lock is owed."""
        _workspace(tmp_path)
        (tmp_path / "conformance" / "package-lock.json").write_text("{}\n")
        plan = _with_uv(_plan, tmp_path)

        conformance = os.path.normpath(str(tmp_path / "conformance"))
        cmds = [
            s.payload["cmd"] for s in plan.steps
            if s.kind == phase_a.SYNC_LOCKFILE
            and os.path.normpath(s.payload["cwd"]) == conformance
        ]
        assert cmds == [["uv", "lock"]], cmds


class TestUnrelatedDevNodesAreLeftAlone:

    def test_a_lock_pointing_elsewhere_is_not_refreshed(self, tmp_path):
        """The path source resolves outside every directory this release bumps."""
        _workspace(tmp_path, lock_target="../vendor/other")
        plan = _with_uv(_plan, tmp_path)

        dirs = [os.path.normpath(p["cwd"]) for p in _uv_lock_syncs(plan)]
        assert os.path.normpath(str(tmp_path / "conformance")) not in dirs

    def test_a_releasable_project_is_not_treated_as_a_dev_node(self, tmp_path):
        """A releasable sibling bumps and locks itself on its own release."""
        _workspace(tmp_path, dev_node=False)
        plan = _with_uv(_plan, tmp_path)

        dirs = [os.path.normpath(p["cwd"]) for p in _uv_lock_syncs(plan)]
        assert os.path.normpath(str(tmp_path / "conformance")) not in dirs

    def test_a_dev_node_without_a_uv_lock_adds_nothing(self, tmp_path):
        _workspace(tmp_path)
        os.remove(tmp_path / "conformance" / "uv.lock")
        plan = _with_uv(_plan, tmp_path)

        dirs = [os.path.normpath(p["cwd"]) for p in _uv_lock_syncs(plan)]
        assert os.path.normpath(str(tmp_path / "conformance")) not in dirs
