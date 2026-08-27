"""End-to-end tests for the ``monorepo mirror`` observe-then-converge reconciler.

Every test uses a REAL git monorepo plus a REAL local ``file://``-style bare
remote (a bare repo path). No network, no real hosts. The reconciler is driven
through ``_cmd_mirror`` (the command entry point) and the observation helpers.
"""

import json
import os
import subprocess

import pytest

from rlsbl.commands.monorepo import _cmd_mirror
from rlsbl.commands.monorepo.mirror_cmd import (
    MirrorError,
    classify_remote,
    compute_split_sha,
    observe,
)
from rlsbl.workspace import WORKSPACE_DIR, WORKSPACE_FILE, save_workspace

from conftest import run_git, with_root_member, make_workspace


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _git(repo, *args, check=True):
    """Run git in ``repo``; return stripped stdout."""
    r = subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=check
    )
    return r.stdout.strip()


def _init_bare(path):
    """Create a bare repo (the mirror remote) at ``path`` and return its path str."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "--bare"], cwd=str(path), check=True)
    return str(path)


def _make_monorepo(root, subtree_remote=None, project_path="mylib", name="mylib"):
    """Build a git monorepo with one npm project and a workspace.toml.

    Returns nothing; ``root`` is left as a committed git repo.
    """
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=str(root), check=True)
    subprocess.run(["git", "config", "user.email", "t@t.local"], cwd=str(root), check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(root), check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=str(root), check=True)

    proj_dir = root / project_path
    proj_dir.mkdir(parents=True, exist_ok=True)
    (proj_dir / "package.json").write_text(
        json.dumps({"name": name, "version": "0.1.0"}) + "\n"
    )
    (proj_dir / "index.js").write_text("module.exports = 1;\n")
    # Give the sub-project a minimal .rlsbl/config.json so scaffold has a target.
    sub_rlsbl = proj_dir / ".rlsbl"
    sub_rlsbl.mkdir(exist_ok=True)
    (sub_rlsbl / "config.json").write_text(
        json.dumps({"targets": ["npm"], "publish_mode": "none"}, indent=2) + "\n"
    )

    proj = {"path": project_path, "name": name}
    if subtree_remote:
        proj["subtree_remote"] = subtree_remote
    (root / WORKSPACE_DIR).mkdir(exist_ok=True)
    make_workspace(str(root), [proj])

    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial monorepo")


def _advance_monorepo(root, project_path="mylib"):
    """Add a commit that touches the sub-project so the split advances."""
    (root / project_path / "index.js").write_text("module.exports = 2;\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "advance mylib")


def _remote_main(remote):
    """SHA of refs/heads/main on the bare remote, or '' if absent."""
    r = subprocess.run(
        ["git", "ls-remote", remote, "refs/heads/main"],
        capture_output=True, text=True, check=True,
    )
    out = r.stdout.strip()
    return out.split()[0] if out else ""


@pytest.fixture
def mono(tmp_path):
    """A committed monorepo (no remote configured yet)."""
    root = tmp_path / "mono"
    return root


# ---------------------------------------------------------------------------
# Early-error coverage (preserved from the original suite)
# ---------------------------------------------------------------------------


class TestEarlyErrors:
    def test_no_workspace_exits(self, tmp_path):
        root = tmp_path / "plain"
        root.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=str(root), check=True)
        with pytest.raises(SystemExit) as exc:
            _cmd_mirror({"project": "anything"}, project_root=root)
        assert exc.value.code == 1

    def test_missing_subtree_remote_message(self, mono, capsys):
        _make_monorepo(mono, subtree_remote=None)
        with pytest.raises(SystemExit):
            _cmd_mirror({"project": "mylib"}, project_root=mono)
        assert "no subtree_remote" in capsys.readouterr().err

    def test_unknown_project_message(self, mono, capsys):
        _make_monorepo(mono, subtree_remote=None)
        with pytest.raises(SystemExit):
            _cmd_mirror({"project": "nope"}, project_root=mono)
        err = capsys.readouterr().err
        assert "not found" in err and "mylib" in err

    def test_unreachable_remote_reports_missing(self, mono, tmp_path):
        # A path that is not a git repo -> ls-remote fails, classified missing.
        bogus = str(tmp_path / "does-not-exist")
        _make_monorepo(mono, subtree_remote=bogus)
        plan = observe(bogus, str(mono), "mylib")
        # Not auth, not populated -> virgin (missing).
        assert plan.state == "virgin"


# ---------------------------------------------------------------------------
# Observation-layer unit coverage
# ---------------------------------------------------------------------------


class TestObservationLayer:
    def test_classify_missing_vs_empty_vs_populated(self, tmp_path):
        # missing
        bogus = str(tmp_path / "nope")
        kind, tip, _ = classify_remote(bogus, cwd=str(tmp_path))
        assert kind == "missing"

        # empty bare repo
        empty = _init_bare(tmp_path / "empty.git")
        kind, tip, _ = classify_remote(empty, cwd=str(tmp_path))
        assert kind == "empty"

        # populated
        repo = tmp_path / "src"
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "config", "user.email", "t@t")
        _git(repo, "config", "user.name", "t")
        (repo / "f").write_text("x")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "c")
        pop = _init_bare(tmp_path / "pop.git")
        _git(repo, "push", "-q", pop, "main")
        kind, tip, _ = classify_remote(pop, cwd=str(repo))
        assert kind == "populated"
        assert tip and len(tip) == 40

    def test_split_sha_deterministic(self, mono):
        _make_monorepo(mono, subtree_remote=None)
        a = compute_split_sha(str(mono), "mylib")
        b = compute_split_sha(str(mono), "mylib")
        assert a == b and len(a) == 40


# ---------------------------------------------------------------------------
# Plan mode (dry-run) — zero writes
# ---------------------------------------------------------------------------


class TestPlanMode:
    def test_virgin_plan_writes_nothing(self, mono, tmp_path, capsys):
        remote = _init_bare(tmp_path / "mirror.git")
        _make_monorepo(mono, subtree_remote=remote)
        before = _remote_main(remote)
        _cmd_mirror({"project": "mylib", "dry-run": True}, project_root=mono)
        after = _remote_main(remote)
        out = capsys.readouterr().out
        assert before == "" and after == ""  # nothing pushed
        assert "remote-missing-or-empty" in out or "virgin" in out


# ---------------------------------------------------------------------------
# Converge (apply) end-to-end
# ---------------------------------------------------------------------------


class TestVirginConverge:
    def test_virgin_pushes_split_and_scaffold(self, mono, tmp_path):
        remote = _init_bare(tmp_path / "mirror.git")
        _make_monorepo(mono, subtree_remote=remote)

        _cmd_mirror({"project": "mylib"}, project_root=mono)

        tip = _remote_main(remote)
        assert tip, "mirror main should exist after converge"

        # Tip is a scaffold commit atop the split; observe reports converged.
        plan = observe(remote, str(mono), "mylib")
        assert plan.state == "converged", plan

        # The mirror root contains the project's files (subtree split) plus
        # scaffold artifacts (.github workflows or .rlsbl scaffold state).
        work = tmp_path / "verify"
        _git(tmp_path, "clone", "-q", remote, str(work))
        assert (work / "package.json").exists()
        assert (work / ".rlsbl").is_dir()


class TestConvergedNoOp:
    def test_second_apply_is_clean_noop(self, mono, tmp_path):
        remote = _init_bare(tmp_path / "mirror.git")
        _make_monorepo(mono, subtree_remote=remote)
        _cmd_mirror({"project": "mylib"}, project_root=mono)
        tip1 = _remote_main(remote)

        # Re-running on a converged mirror must not change the tip.
        _cmd_mirror({"project": "mylib"}, project_root=mono)
        tip2 = _remote_main(remote)
        assert tip1 == tip2


class TestBehindConverge:
    def test_monorepo_advances_then_reconciles(self, mono, tmp_path):
        remote = _init_bare(tmp_path / "mirror.git")
        _make_monorepo(mono, subtree_remote=remote)
        _cmd_mirror({"project": "mylib"}, project_root=mono)
        old_tip = _remote_main(remote)

        # Advance the monorepo and reconcile: should be 'behind' then converge.
        _advance_monorepo(mono)
        plan = observe(remote, str(mono), "mylib")
        assert plan.state == "behind", plan

        _cmd_mirror({"project": "mylib"}, project_root=mono)
        new_tip = _remote_main(remote)
        assert new_tip and new_tip != old_tip
        assert observe(remote, str(mono), "mylib").state == "converged"

        # The advanced source must be present in the mirror.
        work = tmp_path / "verify"
        _git(tmp_path, "clone", "-q", remote, str(work))
        assert "module.exports = 2" in (work / "index.js").read_text()


class TestScaffoldMissingHeals:
    """The pre-bug production shape: a bare split tip with no scaffold layer."""

    def _push_bare_split(self, mono, remote):
        split = compute_split_sha(str(mono), "mylib")
        _git(mono, "push", remote, f"{split}:refs/heads/main")
        return split

    def test_bare_split_tip_reports_scaffold_missing(self, mono, tmp_path):
        remote = _init_bare(tmp_path / "mirror.git")
        _make_monorepo(mono, subtree_remote=remote)
        split = self._push_bare_split(mono, remote)
        assert _remote_main(remote) == split

        plan = observe(remote, str(mono), "mylib")
        assert plan.state == "scaffold_missing"
        assert plan.behind is False

    def test_bare_split_tip_heals_to_converged(self, mono, tmp_path):
        remote = _init_bare(tmp_path / "mirror.git")
        _make_monorepo(mono, subtree_remote=remote)
        self._push_bare_split(mono, remote)

        _cmd_mirror({"project": "mylib"}, project_root=mono)
        assert observe(remote, str(mono), "mylib").state == "converged"


class TestContractViolation:
    """A foreign commit touching a source file must be detected and refused."""

    def _mirror_with_foreign_commit(self, mono, remote, tmp_path):
        # Converge first so the mirror has a real scaffold layer.
        _cmd_mirror({"project": "mylib"}, project_root=mono)
        # Clone, author a foreign commit touching a SOURCE file, push it.
        work = tmp_path / "attacker"
        _git(tmp_path, "clone", "-q", remote, str(work))
        _git(work, "config", "user.email", "evil@x")
        _git(work, "config", "user.name", "Evil")
        (work / "index.js").write_text("module.exports = 999; // hand-edited\n")
        _git(work, "add", "-A")
        _git(work, "commit", "-q", "-m", "sneaky manual edit")
        _git(work, "push", "-q", "origin", "main")

    def test_plan_reports_contract_violation(self, mono, tmp_path):
        remote = _init_bare(tmp_path / "mirror.git")
        _make_monorepo(mono, subtree_remote=remote)
        self._mirror_with_foreign_commit(mono, remote, tmp_path)

        plan = observe(remote, str(mono), "mylib")
        assert plan.state == "contract_violated"
        assert plan.foreign_commits
        _, paths = plan.foreign_commits[0]
        assert paths and any("index.js" in p for p in paths)

    def test_apply_refuses_and_touches_nothing(self, mono, tmp_path, capsys):
        remote = _init_bare(tmp_path / "mirror.git")
        _make_monorepo(mono, subtree_remote=remote)
        self._mirror_with_foreign_commit(mono, remote, tmp_path)
        tip_before = _remote_main(remote)

        with pytest.raises(SystemExit) as exc:
            _cmd_mirror({"project": "mylib"}, project_root=mono)
        assert exc.value.code == 1
        assert "contract-violated" in capsys.readouterr().err
        assert _remote_main(remote) == tip_before  # untouched


class TestLineageUndetermined:
    """A walk git cannot finish refuses WITHOUT accusing anybody.

    The hazard the foreign-commit verdict used to swallow: when the mirror's
    whole history is made of commits the monorepo has no objects for -- pruned
    by gc, never fetched, or (as here) genuinely from somewhere else -- every
    ancestry question comes back unanswerable, not "no". The reconciler still
    refuses and still touches nothing, but the remediation points at the
    missing objects instead of telling the operator to stop authoring on a
    mirror they never touched.
    """

    def _mirror_with_unreachable_history(self, remote, tmp_path):
        """Push a history whose objects the monorepo does not have."""
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        _git(elsewhere, "init", "-q", "-b", "main")
        _git(elsewhere, "config", "user.email", "t@t.local")
        _git(elsewhere, "config", "user.name", "Test")
        _git(elsewhere, "config", "commit.gpgsign", "false")
        (elsewhere / "index.js").write_text("module.exports = 'other';\n")
        _git(elsewhere, "add", "-A")
        _git(elsewhere, "commit", "-q", "-m", "unrelated history")
        _git(elsewhere, "push", "-q", remote, "main:refs/heads/main")
        return _git(elsewhere, "rev-parse", "HEAD")

    def test_plan_reports_lineage_undetermined(self, mono, tmp_path):
        remote = _init_bare(tmp_path / "mirror.git")
        _make_monorepo(mono, subtree_remote=remote)
        unreachable = self._mirror_with_unreachable_history(remote, tmp_path)

        plan = observe(remote, str(mono), "mylib")
        assert plan.state == "lineage_undetermined"
        assert plan.undetermined_commits == [unreachable]
        assert plan.foreign_commits == [], (
            "nothing was shown to be foreign, so the field that says so must "
            "stay empty"
        )

    def test_apply_refuses_and_touches_nothing(self, mono, tmp_path, capsys):
        remote = _init_bare(tmp_path / "mirror.git")
        _make_monorepo(mono, subtree_remote=remote)
        self._mirror_with_unreachable_history(remote, tmp_path)
        tip_before = _remote_main(remote)

        with pytest.raises(SystemExit) as exc:
            _cmd_mirror({"project": "mylib"}, project_root=mono)
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "lineage-undetermined" in err
        assert "could not determine" in err
        assert "never fetched" in err or "pruned" in err
        for accusation in (
            "contract-violated",
            "must never be authored on directly",
            "touches non-scaffold paths",
        ):
            assert accusation not in err, (
                "the refusal must not accuse anybody of authoring on the "
                f"mirror: the question was never answered.\n{err}"
            )
        assert _remote_main(remote) == tip_before  # untouched

    def test_plan_mode_renders_the_verdict(self, mono, tmp_path, capsys):
        remote = _init_bare(tmp_path / "mirror.git")
        _make_monorepo(mono, subtree_remote=remote)
        self._mirror_with_unreachable_history(remote, tmp_path)
        tip_before = _remote_main(remote)

        _cmd_mirror({"project": "mylib", "dry-run": True}, project_root=mono)

        out = capsys.readouterr().out
        assert "lineage-undetermined" in out
        assert "Remediation" in out
        assert _remote_main(remote) == tip_before


class TestInterruptedApplyHeals:
    """Simulate a kill between the split push and the scaffold commit."""

    def test_rerun_after_split_push_only(self, mono, tmp_path):
        remote = _init_bare(tmp_path / "mirror.git")
        _make_monorepo(mono, subtree_remote=remote)
        # First full converge.
        _cmd_mirror({"project": "mylib"}, project_root=mono)
        # Advance, then simulate interruption: only the bare split gets pushed.
        _advance_monorepo(mono)
        split = compute_split_sha(str(mono), "mylib")
        old_tip = _remote_main(remote)
        _git(mono, "push", f"--force-with-lease=main:{old_tip}", remote,
             f"{split}:refs/heads/main")
        # Now the tip is a bare split at the NEW split -> scaffold_missing.
        plan = observe(remote, str(mono), "mylib")
        assert plan.state == "scaffold_missing" and plan.behind is False

        # Re-run heals to converged.
        _cmd_mirror({"project": "mylib"}, project_root=mono)
        assert observe(remote, str(mono), "mylib").state == "converged"


class TestScaffoldFailureHardError:
    """A scaffold failure aborts convergence with a hard error (no warn-continue)."""

    def test_broken_config_aborts(self, mono, tmp_path, capsys, monkeypatch):
        remote = _init_bare(tmp_path / "mirror.git")
        _make_monorepo(mono, subtree_remote=remote)

        # Force scaffold to fail by pointing the subprocess at a bogus module
        # invocation via a stubbed _run_scaffold that raises MirrorError, i.e.
        # exercise the hard-error path directly.
        from rlsbl.commands.monorepo import mirror_cmd

        def boom(clone_dir, sub_config_path):
            raise MirrorError("rlsbl scaffold failed in mirror clone (exit 1)")

        monkeypatch.setattr(mirror_cmd, "_run_scaffold", boom)

        with pytest.raises(SystemExit) as exc:
            _cmd_mirror({"project": "mylib"}, project_root=mono)
        assert exc.value.code == 1
        assert "scaffold failed" in capsys.readouterr().err


class TestStrandedScaffoldCommitRegression:
    """Regression against the old lost-scaffold-commit bug.

    The old implementation ran ``rlsbl scaffold`` with auto-commit ON inside
    the clone, then only pushed when the working tree was still dirty. Because
    scaffold had already committed, the tree was clean, so the scaffold commit
    was never pushed -- the mirror tip stayed a BARE split with no scaffold
    layer. The reconciler must instead land a scaffold commit on the remote.
    """

    def test_scaffold_layer_actually_reaches_remote(self, mono, tmp_path):
        remote = _init_bare(tmp_path / "mirror.git")
        _make_monorepo(mono, subtree_remote=remote)

        _cmd_mirror({"project": "mylib"}, project_root=mono)

        tip = _remote_main(remote)
        split = compute_split_sha(str(mono), "mylib")
        # The bug's signature: tip == bare split (scaffold stranded). Assert the
        # tip is NOT the bare split -- a scaffold commit sits on top.
        assert tip != split, "scaffold commit was stranded (old bug)"

        # And that scaffold commit's parent IS the split (exactly one layer).
        work = tmp_path / "verify"
        _git(tmp_path, "clone", "-q", remote, str(work))
        parent = _git(work, "log", "-1", "--format=%P", "main")
        assert parent == split
