"""`rlsbl monorepo add` stops on a failed scaffold or sync, and `monorepo
remove` refuses a path no member has.

`add` registers the member in workspace.toml and then runs `rlsbl scaffold` in
the member and `rlsbl monorepo sync` at the root, as child processes. Their
failures used to be swallowed: the command exited 0 with the member registered
and committed but never scaffolded, or never in the CI router. A failure now
exits 1 with workspace.toml restored to its bytes before the command, and the
registration is committed only after both children succeed.

Scaffolding cannot move before the registration: `rlsbl scaffold` in a
directory the workspace does not declare writes a standalone project's layout
(its own `.rlsbl/version` and `.rlsbl/bases/`) instead of a member's, so the
registration is written first and restored on failure.
"""

import json
import subprocess

import pytest

import rlsbl
from rlsbl.workspace import load_workspace


def _git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True,
    ).stdout


def _commit_all(root, message):
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", message)


@pytest.fixture
def ws(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "add@example.invalid")
    _git(root, "config", "user.name", "add")
    (root / "README.md").write_text("x\n")
    _commit_all(root, "init")
    monkeypatch.chdir(root)
    assert rlsbl.app.test(["monorepo", "init", "--root-dev-node"]).exit_code == 0
    return root


def _workspace_bytes(root):
    return (root / ".rlsbl-monorepo" / "workspace.toml").read_bytes()


def _member_paths(root):
    return [p["path"] for p in load_workspace(str(root))]


def _npm_member(root, name, engines=True):
    member = root / name
    member.mkdir()
    manifest = {"name": name, "version": "0.1.0"}
    if engines:
        manifest["engines"] = {"node": ">=22"}
    (member / "package.json").write_text(json.dumps(manifest))
    return member


class TestAddStopsOnAFailedScaffold:
    def test_the_member_is_not_registered_and_the_fix_clears_it(self, ws):
        member = _npm_member(ws, "web", engines=False)
        _commit_all(ws, "web")
        before = _workspace_bytes(ws)
        head = _git(ws, "rev-parse", "HEAD")

        result = rlsbl.app.test(["monorepo", "add", "web", "--releasable", "web"])

        assert result.exit_code == 1, result.stdout
        assert "`rlsbl scaffold` in web failed" in result.stderr
        assert "member 'web' is not added" in result.stderr
        assert "re-run this `rlsbl monorepo add`" in result.stderr
        assert _workspace_bytes(ws) == before
        assert "web" not in _member_paths(ws)
        assert _git(ws, "rev-parse", "HEAD") == head

        # The fix: what scaffold refused (no engines.node), then the re-run.
        manifest = json.loads((member / "package.json").read_text())
        manifest["engines"] = {"node": ">=22"}
        (member / "package.json").write_text(json.dumps(manifest))
        _commit_all(ws, "web: declare engines")
        result = rlsbl.app.test(["monorepo", "add", "web", "--releasable", "web"])
        assert result.exit_code == 0, result.stderr
        assert "web" in _member_paths(ws)
        assert _git(ws, "status", "--short", "--untracked-files=all") == ""


_BROKEN_CI = "name: ci\non: [push\njobs: {\n"
_CI = (
    "name: ci\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
    "    steps:\n      - run: echo ok\n"
)


class TestAddStopsOnAFailedSync:
    def _scaffolded_member_with_ci(self, root, name, ci):
        member = _npm_member(root, name)
        (member / ".rlsbl").mkdir()
        (member / ".rlsbl" / "config.json").write_text(json.dumps({
            "targets": ["npm"],
            "publish_mode": "ci",
            "pipelines": {"npm": {"type": "npm", "local": False, "target": "npm"}},
        }))
        (member / ".github" / "workflows").mkdir(parents=True)
        (member / ".github" / "workflows" / "ci.yml").write_text(ci)
        return member

    def test_the_member_is_not_registered_and_the_fix_clears_it(self, ws):
        member = self._scaffolded_member_with_ci(ws, "api", _BROKEN_CI)
        _commit_all(ws, "api")
        before = _workspace_bytes(ws)

        result = rlsbl.app.test(["monorepo", "add", "api", "--releasable", "api"])

        assert result.exit_code == 1, result.stdout
        assert "`rlsbl monorepo sync` failed" in result.stderr
        assert "member 'api' is not added" in result.stderr
        assert _workspace_bytes(ws) == before
        assert "api" not in _member_paths(ws)
        log = _git(ws, "log", "--format=%s")
        assert "monorepo: add api" not in log

        # The fix: a ci.yml the sync can read, then the re-run.
        (member / ".github" / "workflows" / "ci.yml").write_text(_CI)
        _commit_all(ws, "api: fix ci")
        result = rlsbl.app.test(["monorepo", "add", "api", "--releasable", "api"])
        assert result.exit_code == 0, result.stderr
        assert "api" in _member_paths(ws)
        assert "monorepo: add api" in _git(ws, "log", "--format=%s")

    def test_without_auto_commit_the_registration_is_restored_too(self, ws):
        self._scaffolded_member_with_ci(ws, "api", _BROKEN_CI)
        _commit_all(ws, "api")
        before = _workspace_bytes(ws)

        result = rlsbl.app.test(
            ["monorepo", "add", "api", "--releasable", "api", "--no-auto-commit"],
        )

        assert result.exit_code == 1, result.stdout
        assert _workspace_bytes(ws) == before


class TestRemoveOfAnUnknownPath:
    def test_it_is_refused_naming_the_members_and_a_named_path_works(self, ws):
        _npm_member(ws, "web")
        _commit_all(ws, "web")
        assert rlsbl.app.test(
            ["monorepo", "add", "web", "--releasable", "web"],
        ).exit_code == 0
        before = _workspace_bytes(ws)

        result = rlsbl.app.test(["monorepo", "remove", "wbe"])

        assert result.exit_code == 1, result.stdout
        assert "no member at 'wbe'" in result.stderr
        assert "'.' (root)" in result.stderr
        assert "'web' (web)" in result.stderr
        assert _workspace_bytes(ws) == before

        # The fix: one of the paths the refusal names.
        result = rlsbl.app.test(["monorepo", "remove", "web"])
        assert result.exit_code == 0, result.stderr
        assert "web" not in _member_paths(ws)

    def test_a_path_spelled_with_a_trailing_slash_is_not_a_member_path(self, ws):
        _npm_member(ws, "web")
        _commit_all(ws, "web")
        assert rlsbl.app.test(
            ["monorepo", "add", "web", "--releasable", "web"],
        ).exit_code == 0

        result = rlsbl.app.test(["monorepo", "remove", "web/"])

        assert result.exit_code == 1, result.stdout
        assert "'web' (web)" in result.stderr
        assert "web" in _member_paths(ws)
