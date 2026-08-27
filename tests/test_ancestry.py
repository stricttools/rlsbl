"""The one ancestry implementation, and how every caller maps its outcomes.

``git merge-base --is-ancestor`` has three answers, not two: yes (exit 0), no
(exit 1) and "I cannot tell" (exit 128 -- a missing object, a truncated
history, a broken object store).  :func:`rlsbl.git_util.ancestry` returns all
three, and each caller declares what INDETERMINABLE means for it:

============================================  ==============================
caller                                        INDETERMINABLE maps to
============================================  ==============================
mirror reconciler (split-lineage tripwire)    not lineage -> refuse, no writes
changelog validation cache                    cache miss -> recompute
release ``require_recorded_candidate``        hard error, release refuses
``release resume`` HEAD-descends check        hard error, exit 1
============================================  ==============================

Every one of those is fail-closed except the validation cache, whose safe
direction is to do the work again.
"""

import json
import subprocess

import pytest

from rlsbl import git_util
from rlsbl.git_util import Ancestry, ancestry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(repo, *args, check=True):
    r = subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=check
    )
    return r.stdout.strip()


def _repo_with_two_commits(root):
    """A git repo with two linear commits; returns (first_sha, second_sha)."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t.local")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "a.txt").write_text("one\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "one")
    first = _git(root, "rev-parse", "HEAD")
    (root / "a.txt").write_text("two\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "two")
    second = _git(root, "rev-parse", "HEAD")
    return first, second


class _FakeRun:
    """Stand-in for ``effects.run`` that returns a fixed exit code."""

    def __init__(self, returncode):
        self.returncode = returncode
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(list(argv), self.returncode, "", "")


def _stub_ancestry(monkeypatch, module, verdict):
    """Point *module*'s ``ancestry`` name at a stub returning *verdict*."""
    monkeypatch.setattr(module, "ancestry", lambda *a, **k: verdict)


# ---------------------------------------------------------------------------
# The function itself
# ---------------------------------------------------------------------------


class TestAncestryFunction:
    def test_true_for_real_ancestor(self, tmp_path):
        first, second = _repo_with_two_commits(tmp_path / "repo")
        assert ancestry(first, second, cwd=str(tmp_path / "repo")) is Ancestry.TRUE

    def test_true_for_self(self, tmp_path):
        first, _ = _repo_with_two_commits(tmp_path / "repo")
        assert ancestry(first, first, cwd=str(tmp_path / "repo")) is Ancestry.TRUE

    def test_false_for_descendant_asked_backwards(self, tmp_path):
        first, second = _repo_with_two_commits(tmp_path / "repo")
        assert ancestry(second, first, cwd=str(tmp_path / "repo")) is Ancestry.FALSE

    def test_unknown_object_is_indeterminable_not_false(self, tmp_path):
        """The distinction this module exists for: git exits 128, not 1."""
        _, second = _repo_with_two_commits(tmp_path / "repo")
        missing = "0" * 40
        assert (
            ancestry(missing, second, cwd=str(tmp_path / "repo"))
            is Ancestry.INDETERMINABLE
        )

    def test_exit_code_128_is_indeterminable(self, monkeypatch):
        monkeypatch.setattr(git_util.effects, "run", _FakeRun(128))
        assert ancestry("a" * 40, "b" * 40) is Ancestry.INDETERMINABLE

    def test_exit_code_1_is_false(self, monkeypatch):
        monkeypatch.setattr(git_util.effects, "run", _FakeRun(1))
        assert ancestry("a" * 40, "b" * 40) is Ancestry.FALSE

    def test_timeout_is_indeterminable(self, monkeypatch):
        def boom(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, 10)

        monkeypatch.setattr(git_util.effects, "run", boom)
        assert ancestry("a" * 40, "b" * 40) is Ancestry.INDETERMINABLE

    def test_missing_git_binary_is_indeterminable(self, monkeypatch):
        def boom(argv, **kwargs):
            raise FileNotFoundError("git")

        monkeypatch.setattr(git_util.effects, "run", boom)
        assert ancestry("a" * 40, "b" * 40) is Ancestry.INDETERMINABLE

    def test_argv_is_the_documented_git_call(self, monkeypatch):
        fake = _FakeRun(0)
        monkeypatch.setattr(git_util.effects, "run", fake)
        ancestry("aaa", "bbb", cwd="/somewhere")
        assert fake.calls == [["git", "merge-base", "--is-ancestor", "aaa", "bbb"]]


# ---------------------------------------------------------------------------
# Caller 1 -- the mirror reconciler's split-lineage tripwire
# ---------------------------------------------------------------------------


class TestMirrorTripwireMapping:
    """TRUE -> lineage boundary; FALSE and INDETERMINABLE -> refuse."""

    def _mirror(self, tmp_path):
        """A monorepo whose mirror tip is a bare split commit."""
        from rlsbl.workspace import WORKSPACE_DIR, save_workspace

        root = tmp_path / "mono"
        root.mkdir()
        _git(root, "init", "-q", "-b", "main")
        _git(root, "config", "user.email", "t@t.local")
        _git(root, "config", "user.name", "Test")
        _git(root, "config", "commit.gpgsign", "false")
        proj = root / "mylib"
        (proj / ".rlsbl").mkdir(parents=True)
        (proj / "package.json").write_text(
            json.dumps({"name": "mylib", "version": "0.1.0"}) + "\n"
        )
        (proj / "index.js").write_text("module.exports = 1;\n")
        (proj / ".rlsbl" / "config.json").write_text(
            json.dumps({"targets": ["npm"], "publish_mode": "none"}) + "\n"
        )
        remote = tmp_path / "mirror.git"
        remote.mkdir()
        subprocess.run(["git", "init", "-q", "--bare"], cwd=str(remote), check=True)
        (root / WORKSPACE_DIR).mkdir(exist_ok=True)
        save_workspace(
            str(root),
            [{"path": "mylib", "name": "mylib", "subtree_remote": str(remote)}],
        )
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "initial monorepo")

        from rlsbl.commands.monorepo.mirror_cmd import compute_split_sha

        split = compute_split_sha(str(root), "mylib")
        _git(root, "push", "-q", str(remote), f"{split}:refs/heads/main")
        return root, str(remote), split

    def test_true_finds_the_lineage_boundary(self, tmp_path):
        from rlsbl.commands.monorepo.mirror_cmd import observe

        root, remote, _ = self._mirror(tmp_path)
        plan = observe(remote, str(root), "mylib")
        assert plan.state == "scaffold_missing"

    def test_false_refuses_as_contract_violated(self, tmp_path, monkeypatch):
        from rlsbl.commands.monorepo import mirror_cmd

        root, remote, _ = self._mirror(tmp_path)
        _stub_ancestry(monkeypatch, mirror_cmd, Ancestry.FALSE)
        plan = mirror_cmd.observe(remote, str(root), "mylib")
        assert plan.state == "contract_violated"

    def test_indeterminable_refuses_too_and_writes_nothing(
        self, tmp_path, monkeypatch, capsys
    ):
        """Fail-closed: an unanswerable lineage question refuses the push."""
        from rlsbl.commands.monorepo import mirror_cmd

        root, remote, _ = self._mirror(tmp_path)
        tip_before = _git(root, "ls-remote", remote, "refs/heads/main").split()[0]

        _stub_ancestry(monkeypatch, mirror_cmd, Ancestry.INDETERMINABLE)
        plan = mirror_cmd.observe(remote, str(root), "mylib")
        assert plan.state == "contract_violated"

        with pytest.raises(SystemExit) as exc:
            mirror_cmd._cmd_mirror({"project": "mylib"}, project_root=root)
        assert exc.value.code == 1
        assert "contract-violated" in capsys.readouterr().err
        tip_after = _git(root, "ls-remote", remote, "refs/heads/main").split()[0]
        assert tip_after == tip_before


# ---------------------------------------------------------------------------
# Caller 2 -- the changelog validation cache (the one non-fail-closed mapping)
# ---------------------------------------------------------------------------


class TestValidationCacheMapping:
    """TRUE -> cache hit; FALSE and INDETERMINABLE -> recompute (miss)."""

    def _cache(self, tmp_path, monkeypatch, cached_sha, head_sha):
        from rlsbl.changelog import validate

        changes = tmp_path / "changes"
        changes.mkdir()
        (changes / ".validated").write_text(cached_sha + "\n")
        monkeypatch.setattr(validate, "_git_head", lambda: head_sha)
        return validate, str(changes)

    def test_true_is_a_cache_hit(self, tmp_path, monkeypatch):
        validate, changes = self._cache(tmp_path, monkeypatch, "a" * 40, "b" * 40)
        _stub_ancestry(monkeypatch, validate, Ancestry.TRUE)
        assert validate._is_cache_valid(changes) is True

    def test_false_is_a_cache_miss(self, tmp_path, monkeypatch):
        validate, changes = self._cache(tmp_path, monkeypatch, "a" * 40, "b" * 40)
        _stub_ancestry(monkeypatch, validate, Ancestry.FALSE)
        assert validate._is_cache_valid(changes) is False

    def test_indeterminable_is_a_cache_miss_not_an_error(self, tmp_path, monkeypatch):
        """The safe branch here is recomputing, so INDETERMINABLE just misses."""
        validate, changes = self._cache(tmp_path, monkeypatch, "a" * 40, "b" * 40)
        _stub_ancestry(monkeypatch, validate, Ancestry.INDETERMINABLE)
        assert validate._is_cache_valid(changes) is False  # no exception raised


# ---------------------------------------------------------------------------
# Caller 3 -- the release's recorded CI-verified candidate
# ---------------------------------------------------------------------------


class TestRecordedCandidateMapping:
    """TRUE -> the sealed SHA; FALSE and INDETERMINABLE -> hard error."""

    def _state(self, tmp_path, sha):
        state = tmp_path / "in-progress.json"
        state.write_text(json.dumps({"candidate_sha": sha}))
        return str(state)

    def test_true_returns_the_candidate(self, tmp_path):
        from rlsbl.commands.release.execute import require_recorded_candidate

        repo = tmp_path / "repo"
        first, second = _repo_with_two_commits(repo)
        got = require_recorded_candidate(
            self._state(tmp_path, first), cwd=str(repo), version="1.2.3"
        )
        assert got == first

    def test_false_is_a_hard_error(self, tmp_path):
        from rlsbl.commands.release.execute import (
            UnverifiedCandidateError,
            require_recorded_candidate,
        )

        repo = tmp_path / "repo"
        first, second = _repo_with_two_commits(repo)
        # Reset the branch to the first commit: the recorded candidate is now
        # a commit the branch does not contain.
        _git(repo, "reset", "-q", "--hard", first)
        with pytest.raises(UnverifiedCandidateError):
            require_recorded_candidate(
                self._state(tmp_path, second), cwd=str(repo), version="1.2.3"
            )

    def test_indeterminable_is_a_hard_error(self, tmp_path, monkeypatch):
        from rlsbl.commands.release import execute

        repo = tmp_path / "repo"
        first, _ = _repo_with_two_commits(repo)
        _stub_ancestry(monkeypatch, execute, Ancestry.INDETERMINABLE)
        with pytest.raises(execute.UnverifiedCandidateError):
            execute.require_recorded_candidate(
                self._state(tmp_path, first), cwd=str(repo), version="1.2.3"
            )


# ---------------------------------------------------------------------------
# Caller 4 -- `release resume`'s HEAD-descends-from-pre-release check
# ---------------------------------------------------------------------------


class TestResumeDescendsMapping:
    """TRUE -> resume proceeds; FALSE and INDETERMINABLE -> exit 1."""

    def test_true_proceeds(self, monkeypatch):
        import rlsbl

        monkeypatch.setattr(rlsbl.git_util, "ancestry", lambda *a, **k: Ancestry.TRUE)
        rlsbl._abort_unless_head_descends("a" * 40)  # returns, does not exit

    def test_false_exits_one(self, monkeypatch, capsys):
        import rlsbl

        monkeypatch.setattr(rlsbl.git_util, "ancestry", lambda *a, **k: Ancestry.FALSE)
        with pytest.raises(SystemExit) as exc:
            rlsbl._abort_unless_head_descends("a" * 40)
        assert exc.value.code == 1
        assert "not a descendant" in capsys.readouterr().err

    def test_indeterminable_exits_one(self, monkeypatch, capsys):
        import rlsbl

        monkeypatch.setattr(
            rlsbl.git_util, "ancestry", lambda *a, **k: Ancestry.INDETERMINABLE
        )
        with pytest.raises(SystemExit) as exc:
            rlsbl._abort_unless_head_descends("a" * 40)
        assert exc.value.code == 1
        assert "not a descendant" in capsys.readouterr().err
