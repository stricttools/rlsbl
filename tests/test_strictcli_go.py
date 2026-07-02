"""Tests for Go strictcli detection and schema dump branching."""

import os
import subprocess
from unittest.mock import MagicMock

import pytest

from rlsbl.strictcli_detect import (
    StrictcliDetectError,
    _go_mod_has_strictcli,
    detect_strictcli,
)
from rlsbl.commands.release.validate import (
    ReleaseValidationError,
    _run_strictcli_schema_dump,
    _schema_dump_command,
)


# -- Go source snippets for test fixtures --

_GO_MOD_WITH_STRICTCLI = """\
module github.com/example/myapp

go 1.21

require github.com/smm-h/strictcli/go v0.9.0
"""

_GO_MOD_WITHOUT_STRICTCLI = """\
module github.com/example/myapp

go 1.21

require github.com/example/otherlib v1.0.0
"""

_GO_MAIN_WITH_STRICTCLI = """\
package main

import (
\t"fmt"
\t"github.com/smm-h/strictcli/go"
)

func main() {
\tfmt.Println("hello")
}
"""

_GO_MAIN_WITHOUT_STRICTCLI = """\
package main

import "fmt"

func main() {
\tfmt.Println("hello")
}
"""

_GO_CMD_WITH_STRICTCLI = """\
package main

import (
\t"github.com/smm-h/strictcli/go"
)

func main() {}
"""

_GO_CMD_WITHOUT_STRICTCLI = """\
package main

import "fmt"

func main() {
\tfmt.Println("other binary")
}
"""


_GO_MOD_IS_STRICTCLI_ITSELF = """\
module github.com/smm-h/strictcli/go

go 1.21

require github.com/example/otherlib v1.0.0
"""

_GO_MOD_STRICTCLI_IN_REQUIRE_BLOCK = """\
module github.com/example/myapp

go 1.21

require (
\tgithub.com/example/otherlib v1.0.0
\tgithub.com/smm-h/strictcli/go v0.9.0
)
"""


def _write_go_mod(path, content=_GO_MOD_WITH_STRICTCLI):
    """Write a go.mod file at the given directory."""
    (path / "go.mod").write_text(content)


class TestGoDetection:
    """Tests for detect_strictcli() with Go projects."""

    def test_root_main_with_strictcli_import(self, tmp_path):
        """Root main.go importing strictcli returns ('.', 'go')."""
        _write_go_mod(tmp_path)
        (tmp_path / "main.go").write_text(_GO_MAIN_WITH_STRICTCLI)
        result = detect_strictcli(str(tmp_path))
        assert result == (".", "go")

    def test_single_cmd_entry(self, tmp_path):
        """Single cmd/myapp/main.go returns ('./cmd/myapp/', 'go')."""
        _write_go_mod(tmp_path)
        cmd_dir = tmp_path / "cmd" / "myapp"
        cmd_dir.mkdir(parents=True)
        (cmd_dir / "main.go").write_text(_GO_CMD_WITH_STRICTCLI)
        result = detect_strictcli(str(tmp_path))
        assert result == ("./cmd/myapp/", "go")

    def test_multi_cmd_finds_strictcli_binary(self, tmp_path):
        """Multi-binary project: finds the cmd that imports strictcli."""
        _write_go_mod(tmp_path)
        # cmd/server does NOT import strictcli
        server_dir = tmp_path / "cmd" / "server"
        server_dir.mkdir(parents=True)
        (server_dir / "main.go").write_text(_GO_CMD_WITHOUT_STRICTCLI)
        # cmd/cli DOES import strictcli
        cli_dir = tmp_path / "cmd" / "cli"
        cli_dir.mkdir(parents=True)
        (cli_dir / "main.go").write_text(_GO_CMD_WITH_STRICTCLI)
        result = detect_strictcli(str(tmp_path))
        assert result == ("./cmd/cli/", "go")

    def test_returns_none_without_strictcli_dep(self, tmp_path):
        """Go project without strictcli in go.mod returns None."""
        _write_go_mod(tmp_path, _GO_MOD_WITHOUT_STRICTCLI)
        (tmp_path / "main.go").write_text(_GO_MAIN_WITH_STRICTCLI)
        result = detect_strictcli(str(tmp_path))
        assert result is None

    def test_root_main_without_direct_strictcli_import(self, tmp_path):
        """A single main package is the entry point even when the entry file
        doesn't import strictcli directly (the import may be indirect via an
        internal package -- go.mod requiring strictcli is the signal)."""
        _write_go_mod(tmp_path)
        (tmp_path / "main.go").write_text(_GO_MAIN_WITHOUT_STRICTCLI)
        result = detect_strictcli(str(tmp_path))
        assert result == (".", "go")

    def test_go_mod_only_no_entry_points_raises(self, tmp_path):
        """go.mod requires strictcli but no main package exists anywhere:
        hard error instead of a silent None (which silently skipped the
        schema dump on every release)."""
        _write_go_mod(tmp_path)
        # No main.go at root, no cmd/ directory at all -- just go.mod
        with pytest.raises(StrictcliDetectError, match="strictcli"):
            detect_strictcli(str(tmp_path))

    def test_cli_go_entry_file_detected(self, tmp_path):
        """cmd/myapp/cli.go (entry file not named main.go) is detected --
        the old main.go glob returned None and skipped the schema dump."""
        _write_go_mod(tmp_path)
        cmd_dir = tmp_path / "cmd" / "myapp"
        cmd_dir.mkdir(parents=True)
        (cmd_dir / "cli.go").write_text(_GO_CMD_WITH_STRICTCLI)
        result = detect_strictcli(str(tmp_path))
        assert result == ("./cmd/myapp/", "go")

    def test_broken_root_layout_picks_strictcli_main(self, tmp_path):
        """Root package main (version.go without func main) plus a real
        strictcli entry under cmd/ resolves to the cmd entry."""
        _write_go_mod(tmp_path)
        (tmp_path / "version.go").write_text("package main\n\nvar Version string\n")
        cmd_dir = tmp_path / "cmd" / "myapp"
        cmd_dir.mkdir(parents=True)
        (cmd_dir / "cli.go").write_text(_GO_CMD_WITH_STRICTCLI)
        result = detect_strictcli(str(tmp_path))
        assert result == ("./cmd/myapp/", "go")

    def test_strictcli_library_itself_returns_none(self, tmp_path):
        """The strictcli Go library's own go.mod declares
        `module github.com/smm-h/strictcli/go` -- the module declaration
        must NOT be mistaken for a require directive. A library layout
        (no main packages) that merely IS strictcli returns None instead
        of raising StrictcliDetectError."""
        _write_go_mod(tmp_path, _GO_MOD_IS_STRICTCLI_ITSELF)
        (tmp_path / "lib.go").write_text("package strictcli\n")
        result = detect_strictcli(str(tmp_path))
        assert result is None

    def test_strictcli_in_require_block_detected(self, tmp_path):
        """strictcli inside a `require ( ... )` block is detected."""
        _write_go_mod(tmp_path, _GO_MOD_STRICTCLI_IN_REQUIRE_BLOCK)
        (tmp_path / "main.go").write_text(_GO_MAIN_WITH_STRICTCLI)
        result = detect_strictcli(str(tmp_path))
        assert result == (".", "go")

    def test_multi_main_none_importing_strictcli_raises(self, tmp_path):
        """Multiple main packages, none with a direct strictcli import:
        ambiguous -- hard error instead of a silent None."""
        _write_go_mod(tmp_path)
        for name in ("server", "worker"):
            cmd_dir = tmp_path / "cmd" / name
            cmd_dir.mkdir(parents=True)
            (cmd_dir / "main.go").write_text(_GO_CMD_WITHOUT_STRICTCLI)
        with pytest.raises(StrictcliDetectError, match="strictcli"):
            detect_strictcli(str(tmp_path))


class TestGoModHasStrictcli:
    """Unit tests for _go_mod_has_strictcli: only require directives count,
    never the module declaration."""

    def test_module_line_only_is_not_a_require(self, tmp_path):
        """go.mod whose module path IS strictcli, requiring something
        else, must not count as requiring strictcli."""
        _write_go_mod(tmp_path, _GO_MOD_IS_STRICTCLI_ITSELF)
        assert _go_mod_has_strictcli(str(tmp_path)) is False

    def test_single_line_require_detected(self, tmp_path):
        _write_go_mod(tmp_path, _GO_MOD_WITH_STRICTCLI)
        assert _go_mod_has_strictcli(str(tmp_path)) is True

    def test_require_block_detected(self, tmp_path):
        _write_go_mod(tmp_path, _GO_MOD_STRICTCLI_IN_REQUIRE_BLOCK)
        assert _go_mod_has_strictcli(str(tmp_path)) is True

    def test_bare_strictcli_module_path_detected(self, tmp_path):
        """The bare module path (no /go sub-path) also matches."""
        _write_go_mod(
            tmp_path,
            "module github.com/example/myapp\n\ngo 1.21\n\n"
            "require github.com/smm-h/strictcli v0.9.0\n",
        )
        assert _go_mod_has_strictcli(str(tmp_path)) is True

    def test_prefix_lookalike_module_not_detected(self, tmp_path):
        """A module whose path merely starts with the strictcli path as a
        string prefix (not a path segment) does not match."""
        _write_go_mod(
            tmp_path,
            "module github.com/example/myapp\n\ngo 1.21\n\n"
            "require github.com/smm-h/strictcli-extras v1.0.0\n",
        )
        assert _go_mod_has_strictcli(str(tmp_path)) is False

    def test_commented_require_not_detected(self, tmp_path):
        _write_go_mod(
            tmp_path,
            "module github.com/example/myapp\n\ngo 1.21\n\n"
            "// require github.com/smm-h/strictcli/go v0.9.0\n",
        )
        assert _go_mod_has_strictcli(str(tmp_path)) is False

    def test_no_go_mod_returns_false(self, tmp_path):
        assert _go_mod_has_strictcli(str(tmp_path)) is False


class TestSchemaDumpBranching:
    """Tests for schema dump command construction and invocation."""

    def test_go_schema_dump_command(self):
        """Go projects use 'go run' for schema dump."""
        cmd = _schema_dump_command("./cmd/myapp/", "go")
        assert cmd == ["go", "run", "./cmd/myapp/", "--dump-schema"]

    def test_python_schema_dump_command(self):
        """Python projects use 'uv run' for schema dump (no regression)."""
        cmd = _schema_dump_command("myapp", "python")
        assert cmd == ["uv", "run", "myapp", "--dump-schema"]

    def test_schema_dump_invokes_go_run(self, tmp_path, monkeypatch):
        """Schema dump calls 'go run' subprocess for Go projects."""
        _write_go_mod(tmp_path)
        (tmp_path / "main.go").write_text(_GO_MAIN_WITH_STRICTCLI)

        captured_cmds = []

        def fake_subprocess_run(cmd, **kwargs):
            captured_cmds.append(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        monkeypatch.setattr(
            "rlsbl.commands.release.validate.detect_strictcli",
            lambda d: (".", "go"),
        )
        monkeypatch.setattr(
            "rlsbl.commands.release.subprocess.run",
            fake_subprocess_run,
        )

        flags = {}
        log = MagicMock()
        _run_strictcli_schema_dump(flags, log, project_dir=str(tmp_path))

        assert len(captured_cmds) == 1
        assert captured_cmds[0] == ["go", "run", ".", "--dump-schema"]

    def test_schema_dump_invokes_uv_run(self, tmp_path, monkeypatch):
        """Schema dump calls 'uv run' subprocess for Python projects (no regression)."""
        captured_cmds = []

        def fake_subprocess_run(cmd, **kwargs):
            captured_cmds.append(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        monkeypatch.setattr(
            "rlsbl.commands.release.validate.detect_strictcli",
            lambda d: ("myapp", "python"),
        )
        monkeypatch.setattr(
            "rlsbl.commands.release.subprocess.run",
            fake_subprocess_run,
        )

        flags = {}
        log = MagicMock()
        _run_strictcli_schema_dump(flags, log, project_dir=str(tmp_path))

        assert len(captured_cmds) == 1
        assert captured_cmds[0] == ["uv", "run", "myapp", "--dump-schema"]

    def test_dry_run_message_shows_go_command(self, tmp_path, monkeypatch):
        """Dry-run log message shows 'go run' for Go projects."""
        monkeypatch.setattr(
            "rlsbl.commands.release.validate.detect_strictcli",
            lambda d: ("./cmd/myapp/", "go"),
        )

        messages = []
        flags = {"dry-run": True}
        _run_strictcli_schema_dump(flags, messages.append, project_dir=str(tmp_path))

        assert len(messages) == 1
        assert "go run ./cmd/myapp/ --dump-schema" in messages[0]
        assert "uv" not in messages[0]

    def test_detection_failure_is_release_validation_error(self, tmp_path, monkeypatch):
        """A project that requires strictcli but whose entry point can't be
        detected must abort release validation -- never silently skip the
        schema dump."""
        monkeypatch.setattr(
            "rlsbl.commands.release.validate.detect_strictcli",
            MagicMock(side_effect=StrictcliDetectError(
                "go.mod requires strictcli but no entry point was found"
            )),
        )
        with pytest.raises(ReleaseValidationError, match="strictcli"):
            _run_strictcli_schema_dump({}, MagicMock(), project_dir=str(tmp_path))

    def test_detection_failure_raises_in_dry_run_too(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "rlsbl.commands.release.validate.detect_strictcli",
            MagicMock(side_effect=StrictcliDetectError(
                "go.mod requires strictcli but no entry point was found"
            )),
        )
        with pytest.raises(ReleaseValidationError, match="strictcli"):
            _run_strictcli_schema_dump(
                {"dry-run": True}, MagicMock(), project_dir=str(tmp_path)
            )
