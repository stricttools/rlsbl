"""`rlsbl monorepo mirror --dry-run`, driven through the real CLI.

Every other mirror dry-run test calls ``_cmd_mirror({"dry-run": True})``
directly.  That bypasses the strictcli dispatch, so no effects handle is ever
minted and the preview machinery is not in the loop -- which is why the suite
never saw the defect this module pins: observation runs ``git subtree split``
and a scratch ``git clone``, and neither was on the observe allowlist, so under
a real preview both were RECORDED instead of run.  Reading ``.returncode`` off
the recorded carrier raised strictcli's truncation error and
``rlsbl monorepo mirror --dry-run`` died at step 1 with "branched on unsettled
value" instead of rendering a plan.

The second half of the pin is the would-do log: ``effects.mkdtemp`` and
``effects.rmtree`` used to RECORD under preview, so a plan that touches nothing
still advertised a ``mkdir``/``remove`` of a scratch directory that no apply
would ever create.  Observation's scratch lifecycle is real now, and the log
carries only genuinely planned mutations.
"""

import json
import subprocess

import pytest

import rlsbl
from rlsbl.workspace import WORKSPACE_DIR, save_workspace


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True,
    ).stdout.strip()


def _init_bare(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "--bare"], cwd=str(path), check=True)
    return str(path)


def _make_monorepo(root, subtree_remote, project_path="mylib", name="mylib"):
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t.local")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "commit.gpgsign", "false")

    proj_dir = root / project_path
    (proj_dir / ".rlsbl").mkdir(parents=True, exist_ok=True)
    (proj_dir / "package.json").write_text(
        json.dumps({"name": name, "version": "0.1.0"}) + "\n"
    )
    (proj_dir / "index.js").write_text("module.exports = 1;\n")
    (proj_dir / ".rlsbl" / "config.json").write_text(
        json.dumps({"targets": ["npm"], "publish_mode": "none"}, indent=2) + "\n"
    )
    (root / WORKSPACE_DIR).mkdir(exist_ok=True)
    save_workspace(
        str(root),
        [{"path": project_path, "name": name, "subtree_remote": subtree_remote}],
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial monorepo")
    return root


def _remote_main(remote):
    out = subprocess.run(
        ["git", "ls-remote", remote, "refs/heads/main"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return out.split()[0] if out else ""


def _run_dry(monkeypatch, root):
    monkeypatch.chdir(root)
    return rlsbl.app.test(["--dry-run", "monorepo", "mirror", "mylib"])


class TestMirrorDryRunThroughTheCli:
    def test_virgin_remote_renders_a_plan(self, tmp_path, monkeypatch):
        """The preview must reach a verdict, not truncate on an observe."""
        remote = _init_bare(tmp_path / "mirror.git")
        root = _make_monorepo(tmp_path / "mono", remote)

        result = _run_dry(monkeypatch, root)

        assert result.exit_code == 0, (
            "the preview failed before it could report anything; "
            f"stderr was:\n{result.stderr}"
        )
        assert "remote-missing-or-empty" in result.stdout, result.stdout
        assert "apply would push split" in result.stdout, result.stdout
        assert _remote_main(remote) == ""

    def test_populated_remote_renders_a_plan(self, tmp_path, monkeypatch):
        """The scratch clone path: observation clones the mirror to inspect it."""
        from rlsbl.commands.monorepo.mirror_cmd import compute_split_sha

        remote = _init_bare(tmp_path / "mirror.git")
        root = _make_monorepo(tmp_path / "mono", remote)
        split = compute_split_sha(str(root), "mylib")
        _git(root, "push", "-q", remote, f"{split}:refs/heads/main")

        result = _run_dry(monkeypatch, root)

        assert result.exit_code == 0, result.stderr
        assert "scaffold-missing" in result.stdout, result.stdout
        assert _remote_main(remote) == split

    def test_would_do_log_carries_no_synthetic_scratch_lines(
        self, tmp_path, monkeypatch
    ):
        """A plan that touches nothing must not advertise scratch mutations.

        Observation's temp directory is created and removed for real inside the
        preview, so it is neither a mutation the apply would make nor a line in
        the log.
        """
        from rlsbl.commands.monorepo.mirror_cmd import compute_split_sha

        remote = _init_bare(tmp_path / "mirror.git")
        root = _make_monorepo(tmp_path / "mono", remote)
        split = compute_split_sha(str(root), "mylib")
        _git(root, "push", "-q", remote, f"{split}:refs/heads/main")

        result = _run_dry(monkeypatch, root)

        assert result.exit_code == 0, result.stderr
        combined = result.stdout + result.stderr
        assert "rlsbl-mirror-observe" not in combined, (
            "the would-do log names the observation's scratch directory, which "
            f"no apply would ever create:\n{combined}"
        )
