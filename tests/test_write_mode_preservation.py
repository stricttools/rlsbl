"""Rewriting a file must not narrow its permission bits.

Every one of these writers used to pass ``file_mode=0o600`` -- the mode the
older hand-rolled ``mkstemp`` write happened to leave -- so an ordinary 0o644
file (a committed schema dump, a project config, a selfdoc manifest, an Xcode
project file, a releasable's version file) silently became owner-only the first
time rlsbl touched it. A rewrite changes what a file SAYS, never what it is.

The release-state file is the deliberate exception and is pinned here too: it
is transient, gitignored, tool-owned bookkeeping that rlsbl alone creates,
rewrites and deletes, so its bits are stated outright rather than inherited
from whatever umask happened to create it.
"""

import json
import os
import types

import pytest


def _mode(path):
    return os.stat(path).st_mode & 0o777


def _umask_default():
    current = os.umask(0)
    os.umask(current)
    return 0o666 & ~current


class TestSchemaDumpPatch:
    """`.strictcli/schema.json` is committed and read by every consumer."""

    def _schema(self, tmp_path, mode=0o644):
        d = tmp_path / ".strictcli"
        d.mkdir()
        path = d / "schema.json"
        path.write_text('{\n  "version": "0.1.0",\n  "name": "x"\n}\n')
        os.chmod(path, mode)
        return path

    def test_patching_the_version_keeps_the_files_mode(self, tmp_path):
        from rlsbl.commands.release.validate import _patch_schema_version

        path = self._schema(tmp_path)
        _patch_schema_version(str(tmp_path), "0.2.0")

        assert _mode(str(path)) == 0o644
        assert '"version": "0.2.0"' in path.read_text()

    def test_a_locked_schema_stays_locked(self, tmp_path):
        from rlsbl.commands.release.validate import _patch_schema_version

        path = self._schema(tmp_path, mode=0o444)
        _patch_schema_version(str(tmp_path), "0.2.0")

        assert _mode(str(path)) == 0o444


class TestProjectConfigWrite:
    """`.rlsbl/config.json` is committed and edited by hand."""

    def test_setting_a_key_keeps_the_files_mode(self, tmp_path):
        from rlsbl.config import write_project_config

        d = tmp_path / ".rlsbl"
        d.mkdir()
        path = d / "config.json"
        path.write_text(json.dumps({"publish_mode": "ci"}) + "\n")
        os.chmod(path, 0o644)

        write_project_config("changelog_format", "grouped", str(tmp_path))

        assert _mode(str(path)) == 0o644
        assert json.loads(path.read_text())["changelog_format"] == "grouped"

    def test_creating_the_config_uses_the_umask_default(self, tmp_path):
        from rlsbl.config import write_project_config

        write_project_config("publish_mode", "ci", str(tmp_path))
        path = tmp_path / ".rlsbl" / "config.json"

        assert _mode(str(path)) == _umask_default()


class TestReleasableVersionWrite:
    """A releasable's `version` file is committed and read by humans."""

    def test_bumping_keeps_the_files_mode(self, tmp_path):
        from rlsbl.workspace import get_releasable_version_path, write_releasable_version

        path = get_releasable_version_path(str(tmp_path), "alpha")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("0.1.0\n")
        os.chmod(path, 0o644)

        write_releasable_version(str(tmp_path), "alpha", "0.2.0")

        assert _mode(path) == 0o644
        assert open(path, encoding="utf-8").read() == "0.2.0\n"

    def test_creating_it_uses_the_umask_default(self, tmp_path):
        from rlsbl.workspace import get_releasable_version_path, write_releasable_version

        write_releasable_version(str(tmp_path), "alpha", "0.1.0")

        assert _mode(get_releasable_version_path(str(tmp_path), "alpha")) == _umask_default()


class TestNativeIosWrite:
    """The iOS target rewrites a consumer's own project files."""

    def test_rewriting_a_project_file_keeps_its_mode(self, tmp_path):
        from rlsbl.targets.native_ios import _atomic_write

        path = tmp_path / "project.pbxproj"
        path.write_text("MARKETING_VERSION = 0.1.0;\n")
        os.chmod(path, 0o644)

        _atomic_write(str(path), "MARKETING_VERSION = 0.2.0;\n")

        assert _mode(str(path)) == 0o644
        assert path.read_text() == "MARKETING_VERSION = 0.2.0;\n"


class TestSelfdocBump:
    """`selfdoc.json` is committed; the bump only ever REWRITES it.

    ``_bump_selfdoc_version_content`` returns None when the file is absent, so
    the executor step below never creates one -- it is a pure rewriter.
    """

    def test_bumping_the_version_keeps_the_files_mode(self, tmp_path):
        from rlsbl.commands.release.execute import _bump_selfdoc_version_content
        from rlsbl.commands.release.phase_a import _Executor

        path = tmp_path / "selfdoc.json"
        path.write_text(json.dumps({"version": "0.1.0"}, indent=2) + "\n")
        os.chmod(path, 0o644)

        content = _bump_selfdoc_version_content(str(tmp_path), "0.2.0")
        assert content is not None

        logged = []
        stub = types.SimpleNamespace(_log=logged.append)
        step = types.SimpleNamespace(
            payload={"path": str(path), "content": content},
        )
        _Executor._do_bump_selfdoc(stub, step)

        assert _mode(str(path)) == 0o644
        assert json.loads(path.read_text())["version"] == "0.2.0"


class TestReleaseStateIsDeliberately0600:
    """The one file whose mode is pinned rather than inherited.

    ``.rlsbl/releases/in-progress.json`` is gitignored, transient, and written
    by exactly two call sites (the state saver and the candidate-SHA recorder).
    Pinning both at 0o600 keeps the file's bits identical whichever step wrote
    last, instead of depending on the umask of whoever started the release.
    """

    def test_saving_the_state_pins_0600(self, tmp_path):
        from rlsbl.commands.release.release_state import save_release_state

        path = str(tmp_path / "releases" / "in-progress.json")
        save_release_state(path, {"version": "0.2.0"})

        assert _mode(path) == 0o600

    def test_re_saving_keeps_0600(self, tmp_path):
        from rlsbl.commands.release.release_state import save_release_state

        path = str(tmp_path / "in-progress.json")
        save_release_state(path, {"version": "0.2.0"})
        os.chmod(path, 0o644)
        save_release_state(path, {"version": "0.2.0", "step": "commit"})

        assert _mode(path) == 0o600


@pytest.mark.parametrize("module_path", [
    "rlsbl/commands/release/validate.py",
    "rlsbl/config.py",
    "rlsbl/workspace.py",
    "rlsbl/targets/native_ios.py",
])
def test_no_rewriter_module_pins_0600(module_path):
    """The family stays closed: only the release-state writers pin 0o600."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, module_path), encoding="utf-8") as f:
        assert "file_mode=0o600" not in f.read()
