"""A workspace's root member keeps its config in its releasable's directory.

A root ``.rlsbl/`` beside ``.rlsbl-monorepo/`` is refused by the
``root-rlsbl-conflict`` check, and ``rlsbl scaffold`` does not scaffold the
workspace root, so the root member's config is
``.rlsbl-monorepo/releasables/<name>/config.json``. Every refusal about that
config names that file, ``monorepo init --root-releasable`` creates it, and each
named fix, once applied, clears the refusal it came from.
"""

import json
import subprocess

import pytest

from conftest import capture_all_checks, workspace_toml

from rlsbl.errors import ConfigError
from rlsbl.workspace import WORKSPACE_DIR, WORKSPACE_FILE, write_releasable_version


RELEASABLE = "app"
ROOT_CONFIG = f".rlsbl-monorepo/releasables/{RELEASABLE}/config.json"


@pytest.fixture
def root_workspace(tmp_path):
    """A workspace whose root member belongs to releasable ``app``, unconfigured."""
    ws_dir = tmp_path / WORKSPACE_DIR
    ws_dir.mkdir()
    (ws_dir / WORKSPACE_FILE).write_text(
        workspace_toml(
            '[[projects]]\npath = "."\nname = "root"\nreleasable = "app"\n',
            releasables=[{"name": RELEASABLE, "tag_format": "v{version}"}],
            root_member="",
        )
    )
    write_releasable_version(str(tmp_path), RELEASABLE, "1.0.0")
    return tmp_path


def _root_config(root):
    from rlsbl.context import create_context

    return create_context(root, workspace_root=root).config


def _apply_named_fix(root, key, value):
    """Write *key* into the file the refusal named, as an operator would."""
    path = root / ROOT_CONFIG
    data = json.loads(path.read_text()) if path.exists() else {}
    data[key] = value
    path.write_text(json.dumps(data))


class TestPublishModeRefusalsNameTheReleasableConfig:

    def test_the_schema_refusal_names_the_releasable_config(self, root_workspace):
        from rlsbl.config import validate_config_schema

        with pytest.raises(ConfigError) as exc:
            validate_config_schema(_root_config(root_workspace), project_dir=str(root_workspace))
        assert ROOT_CONFIG in str(exc.value)
        assert ".rlsbl/config.json" not in str(exc.value)

        _apply_named_fix(root_workspace, "publish_mode", "ci")
        validate_config_schema(_root_config(root_workspace), project_dir=str(root_workspace))

    def test_the_release_integrity_refusal_names_the_file_not_scaffold(self, root_workspace):
        from rlsbl.commands.release.validate import (
            ReleaseValidationError, validate_config_integrity,
        )

        with pytest.raises(ReleaseValidationError) as exc:
            validate_config_integrity(_root_config(root_workspace), project_dir=str(root_workspace))
        assert ROOT_CONFIG in str(exc.value)
        # `rlsbl scaffold` skips the workspace root, so it is no fix here.
        assert "scaffold" not in str(exc.value)

        _apply_named_fix(root_workspace, "publish_mode", "none")
        validate_config_integrity(_root_config(root_workspace), project_dir=str(root_workspace))

    def test_the_member_context_refusal_names_the_releasable_config(self, root_workspace):
        from rlsbl.member_context import resolve_member_context
        from rlsbl.workspace import get_releasable_dir

        rel_dir = get_releasable_dir(str(root_workspace), RELEASABLE)
        with pytest.raises(ConfigError, match=ROOT_CONFIG):
            resolve_member_context(str(root_workspace), releasable_config_dir=rel_dir).publish_mode

        _apply_named_fix(root_workspace, "publish_mode", "ci")
        member = resolve_member_context(str(root_workspace), releasable_config_dir=rel_dir)
        assert member.publish_mode == "ci"

    def test_a_standalone_project_keeps_its_own_config_and_scaffold_fix(self, tmp_path):
        from rlsbl.commands.release.validate import (
            ReleaseValidationError, validate_config_integrity,
        )

        with pytest.raises(ReleaseValidationError) as exc:
            validate_config_integrity({}, project_dir=str(tmp_path))
        assert ".rlsbl/config.json" in str(exc.value)
        assert "rlsbl scaffold" in str(exc.value)


class TestRootRlsblConflictNamesTheDestination:

    def _conflict(self, root, monkeypatch):
        from rlsbl.context import create_context

        monkeypatch.chdir(root)
        ctx = create_context(root, workspace_root=root)
        return capture_all_checks()["root-rlsbl-conflict"](ctx)

    def test_the_refusal_names_the_releasable_config_and_the_fix_clears_it(
        self, root_workspace, monkeypatch,
    ):
        from rlsbl.saferm import saferm_delete

        stray = root_workspace / ".rlsbl"
        stray.mkdir()
        (stray / "config.json").write_text(json.dumps({"publish_mode": "ci"}))

        result = self._conflict(root_workspace, monkeypatch)
        assert result.status == "fail"
        assert ROOT_CONFIG in result.message

        # The named fix: move the keys to the releasable config, delete .rlsbl/.
        _apply_named_fix(root_workspace, "publish_mode", "ci")
        saferm_delete(str(stray), description="test: the named fix", recursive=True)
        assert self._conflict(root_workspace, monkeypatch).status == "pass"
        assert _root_config(root_workspace)["publish_mode"] == "ci"


def _git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, timeout=30, capture_output=True)


def _fresh_repo(root, go_module=False):
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t.local")
    _git(root, "config", "user.name", "T")
    (root / "README.md").write_text("# test\n")
    files = ["README.md"]
    if go_module:
        (root / "go.mod").write_text("module example.com/app\n\ngo 1.22\n")
        (root / "app.go").write_text("package app\n\nfunc A() int { return 1 }\n")
        files += ["go.mod", "app.go"]
    _git(root, "add", *files)
    _git(root, "commit", "-q", "-m", "initial")
    return root


def _init(root, **extra):
    from rlsbl.commands.monorepo.commands import _cmd_init

    flags = {
        "auto-commit": True,
        "root-releasable": RELEASABLE,
        "root-tag-format": "v{version}",
    }
    flags.update(extra)
    _cmd_init(flags, project_root=root)


def _tracked(root):
    out = subprocess.run(
        ["git", "ls-files"], cwd=root, check=True, timeout=30,
        capture_output=True, text=True,
    ).stdout
    return set(out.split())


class TestInitCreatesTheRootConfig:

    def test_init_writes_and_commits_the_releasable_config(self, tmp_path, monkeypatch):
        _fresh_repo(tmp_path)
        monkeypatch.chdir(tmp_path)
        _init(tmp_path, **{"root-publish-mode": "none"})

        assert ROOT_CONFIG in _tracked(tmp_path)
        assert not (tmp_path / ".rlsbl").exists()
        # The file init writes is the file the release reads.
        assert _root_config(tmp_path)["publish_mode"] == "none"

    def test_a_missing_publish_mode_is_refused_before_any_write(
        self, tmp_path, monkeypatch, capsys,
    ):
        _fresh_repo(tmp_path)
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit):
            _init(tmp_path)
        assert "--publish-mode is required" in capsys.readouterr().err
        assert not (tmp_path / WORKSPACE_DIR).exists()

        _init(tmp_path, **{"root-publish-mode": "none"})
        assert _root_config(tmp_path)["publish_mode"] == "none"

    def test_a_publishing_root_must_name_its_gate_regex(self, tmp_path, monkeypatch, capsys):
        _fresh_repo(tmp_path, go_module=True)
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit):
            _init(tmp_path, **{"root-publish-mode": "ci"})
        err = capsys.readouterr().err
        assert "--publish-gate-check-regex is required" in err
        assert not (tmp_path / WORKSPACE_DIR).exists()

        regex = "^(test|lint)( \\(.*\\))?$"
        _init(tmp_path, **{"root-publish-mode": "ci", "root-publish-gate-check-regex": regex})
        config = _root_config(tmp_path)
        assert config["publish_gate_check_regex"] == regex
        assert config["targets"] == ["go"]
        assert config["pipelines"]["go"]["target"] == "go"
        assert config["pipelines"]["go"]["artifact"] == "library"

    def test_a_failed_commit_is_fatal_and_leaves_nothing_behind(
        self, tmp_path, monkeypatch, capsys,
    ):
        from rlsbl.commands.monorepo import commands

        _fresh_repo(tmp_path)
        monkeypatch.chdir(tmp_path)

        def _fail(*_a, **_k):
            raise subprocess.CalledProcessError(1, "safegit", stderr="index.lock exists")

        monkeypatch.setattr(commands, "commit_files", _fail)
        with pytest.raises(SystemExit) as exc:
            _init(tmp_path, **{"root-publish-mode": "none"})
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "index.lock exists" in err and "re-run" in err
        assert not (tmp_path / WORKSPACE_DIR).exists()

        # The re-run the error names succeeds once the commit can.
        monkeypatch.undo()
        monkeypatch.chdir(tmp_path)
        _init(tmp_path, **{"root-publish-mode": "none"})
        assert ROOT_CONFIG in _tracked(tmp_path)
