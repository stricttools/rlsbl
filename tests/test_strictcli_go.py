"""Tests for Go strictcli detection and schema dump branching."""

import os
import subprocess
from unittest.mock import MagicMock

import pytest

from rlsbl.strictcli_detect import detect_strictcli
from rlsbl.commands.release.validate import (
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

    def test_root_main_without_strictcli_import(self, tmp_path):
        """Root main.go that does NOT import strictcli returns None."""
        _write_go_mod(tmp_path)
        (tmp_path / "main.go").write_text(_GO_MAIN_WITHOUT_STRICTCLI)
        result = detect_strictcli(str(tmp_path))
        assert result is None

    def test_go_mod_only_no_entry_points(self, tmp_path):
        """go.mod with strictcli dep but no main.go anywhere returns None."""
        _write_go_mod(tmp_path)
        # No main.go at root, no cmd/ directory at all -- just go.mod
        result = detect_strictcli(str(tmp_path))
        assert result is None


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
