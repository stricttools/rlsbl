"""Tests for strictcli schema auto-dump during release."""

import inspect
import json
import subprocess
import sys
from unittest.mock import patch, MagicMock

import pytest

from rlsbl.commands.release import _run_cmd_inner, _run_strictcli_schema_dump
from rlsbl.commands.release.validate import ReleaseValidationError
from rlsbl.release_file import ReleaseConfig


# A schema document in strictcli's own canonical v2 encoding, which is the only
# shape this file's subject ever meets: two-space indent, one member per line,
# `": "` between key and value, empty containers inline, one trailing newline.
# The version patch is textual and pinned to that shape on purpose -- a
# decode/re-encode round trip silently rewrites the whole document (see
# tests/test_schema_version_patch.py).
_CANONICAL_SCHEMA = """\
{
  "schema_version": 2,
  "version": "1.0.0",
  "commands": []
}
"""

_CANONICAL_SCHEMA_WITHOUT_VERSION = """\
{
  "schema_version": 2,
  "commands": []
}
"""


def _rc(bump="patch", include=None, exclude=None):
    """Shorthand for creating a ReleaseConfig with sensible defaults."""
    return ReleaseConfig(
        bump=bump,
        include=include or ["pypi"],
        exclude=exclude or [],
    )


# The two programs a release can meet, reproduced from real strictcli apps in
# Go, Python, and TypeScript (the responses are identical across the three):
#
# * CURRENT strictcli prints the help document on stdout for `help --json`,
#   sends the `--json` envelope to stderr, and refuses `--dump-schema` naming
#   `help --json`.
# * OLDER strictcli (Python 0.43.0, Go v0.36.0, TypeScript 0.42.0 and every
#   release before them) has no `help` command: `help --json` exits 1 with
#   `error: unknown command 'help'` on stderr and an envelope on stdout, while
#   `--dump-schema` writes .strictcli/schema.json itself.
#
# Each stub acts on the argv rlsbl really built, so a release that still asked
# for `--dump-schema`, or fell back to it, is caught by what the stub does.
_STUB = """\
import json, os, sys

KIND = {kind!r}
DOCUMENT = {document!r}
args = sys.argv[1:]
envelope = json.dumps({{"interface_version": 3, "app": "myapp"}})
if KIND == "current":
    if args == ["help", "--json"]:
        sys.stdout.write(DOCUMENT)
        sys.stderr.write(envelope + "\\n")
        sys.exit(0)
    if "--dump-schema" in args:
        sys.stderr.write("error: --dump-schema is not supported; the app's "
                         "help document is printed by 'myapp help --json'\\n"
                         "try 'myapp --help'\\n")
        sys.exit(1)
else:
    if "--dump-schema" in args:
        os.makedirs(".strictcli", exist_ok=True)
        with open(os.path.join(".strictcli", "schema.json"), "w") as f:
            f.write(DOCUMENT)
        sys.exit(0)
    if args and args[0] == "help":
        sys.stdout.write(envelope + "\\n")
        sys.stderr.write("error: unknown command 'help'\\ntry 'myapp --help'\\n")
        sys.exit(1)
sys.stderr.write("stub: unexpected argv %r\\n" % (args,))
sys.exit(3)
"""

# How many leading argv members launch the entry point, per language: the
# stub replaces exactly those, and every argument rlsbl adds after them reaches
# the stub unchanged.
_LAUNCHER_LEN = {"python": 3, "go": 3, "typescript": 2}


def _python_project(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "myapp"\nversion = "1.0.0"\n'
        'dependencies = ["strictcli"]\n'
        '\n[project.scripts]\nmyapp = "myapp:main"\n'
    )


def _install_stub(tmp_path, monkeypatch, kind, document=_CANONICAL_SCHEMA):
    """Make the release's schema command run a stub strictcli program.

    Returns the argv lists the release asked for, in order.
    """
    from rlsbl.commands.release import validate

    stub = tmp_path / f"stub_{kind}.py"
    stub.write_text(_STUB.format(kind=kind, document=document))
    real = validate._schema_dump_command
    asked = []

    def fake(entry_point, lang):
        cmd = real(entry_point, lang)
        asked.append(cmd)
        return [sys.executable, str(stub)] + cmd[_LAUNCHER_LEN[lang]:]

    monkeypatch.setattr(validate, "_schema_dump_command", fake)
    return asked


class TestStrictcliSchemaDumpFunction:
    """Tests for the _run_strictcli_schema_dump helper function."""

    def test_skipped_when_not_strictcli_project(self, tmp_path, capsys):
        """When the project does not use strictcli, nothing happens."""
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "myapp"\nversion = "1.0.0"\n'
            'dependencies = ["click"]\n'
        )
        messages = []
        _run_strictcli_schema_dump(
            {}, lambda msg: messages.append(msg),
            project_dir=str(tmp_path),
        )
        assert not messages
        captured = capsys.readouterr()
        assert captured.err == ""

    def test_dry_run_records_the_dump_instead_of_describing_it(self, tmp_path):
        """A preview routes the dump through ``effects.run``, which records it."""
        _python_project(tmp_path)
        fake_effects = MagicMock()
        fake_effects.unsettled.return_value = True
        with patch("rlsbl.commands.release.effects", fake_effects):
            _run_strictcli_schema_dump(
                {"dry-run": True}, lambda msg: None,
                project_dir=str(tmp_path),
            )
        assert fake_effects.run.call_args[0][0] == [
            "uv", "run", "myapp", "help", "--json",
        ]
        assert not (tmp_path / ".strictmetadata").exists()

    def test_dry_run_silent_when_not_strictcli(self, tmp_path):
        """In dry-run mode with no strictcli, nothing runs and nothing prints."""
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "myapp"\nversion = "1.0.0"\n'
            'dependencies = ["click"]\n'
        )
        messages = []
        fake_effects = MagicMock()
        with patch("rlsbl.commands.release.effects", fake_effects):
            _run_strictcli_schema_dump(
                {"dry-run": True}, lambda msg: messages.append(msg),
                project_dir=str(tmp_path),
            )
        assert not messages
        assert not fake_effects.run.called

    def test_help_document_on_stdout_becomes_the_schema_file(
        self, tmp_path, monkeypatch,
    ):
        """The release asks for `help --json` and commits its stdout verbatim."""
        _python_project(tmp_path)
        asked = _install_stub(tmp_path, monkeypatch, "current")

        _run_strictcli_schema_dump({}, lambda m: None, project_dir=str(tmp_path))

        assert asked == [["uv", "run", "myapp", "help", "--json"]]
        schema = tmp_path / ".strictmetadata" / ".cli-schema" / "schema.json"
        assert schema.read_text(encoding="utf-8") == _CANONICAL_SCHEMA

    def test_version_stamped_into_the_written_document(self, tmp_path, monkeypatch):
        """With a release version, only the top-level version line changes."""
        _python_project(tmp_path)
        _install_stub(tmp_path, monkeypatch, "current")

        _run_strictcli_schema_dump(
            {}, lambda m: None, project_dir=str(tmp_path), version="2.0.0",
        )

        written = (tmp_path / ".strictmetadata" / ".cli-schema" / "schema.json").read_text(encoding="utf-8")
        assert written == _CANONICAL_SCHEMA.replace('"1.0.0"', '"2.0.0"')

    def test_an_existing_schema_file_is_replaced_and_keeps_its_mode(
        self, tmp_path, monkeypatch,
    ):
        _python_project(tmp_path)
        _install_stub(tmp_path, monkeypatch, "current")
        schema = tmp_path / ".strictmetadata" / ".cli-schema" / "schema.json"
        schema.parent.mkdir(parents=True)
        schema.write_text('{\n  "stale": true\n}\n')
        schema.chmod(0o644)

        _run_strictcli_schema_dump({}, lambda m: None, project_dir=str(tmp_path))

        assert schema.read_text(encoding="utf-8") == _CANONICAL_SCHEMA
        assert schema.stat().st_mode & 0o777 == 0o644

    def test_older_strictcli_is_refused_naming_the_upgrade(
        self, tmp_path, monkeypatch,
    ):
        """A program on a strictcli without `help --json` stops the release.

        There is no fallback to `--dump-schema`: the stub would happily write
        a schema for it, and the file's absence proves rlsbl never asked.
        """
        _python_project(tmp_path)
        asked = _install_stub(tmp_path, monkeypatch, "older")

        with pytest.raises(ReleaseValidationError) as exc:
            _run_strictcli_schema_dump(
                {}, lambda m: None, project_dir=str(tmp_path), version="2.0.0",
            )

        message = str(exc.value)
        assert "predates `help --json`" in message
        assert "0.43.0" in message
        assert "upgrade" in message
        assert asked == [["uv", "run", "myapp", "help", "--json"]]
        assert not (tmp_path / ".strictmetadata").exists()

    @pytest.mark.parametrize("lang,entry,version_named", [
        ("go", ".", "v0.36.0"),
        ("typescript", "cli.js", "0.42.0"),
    ])
    def test_older_strictcli_refusal_names_each_language(
        self, tmp_path, monkeypatch, lang, entry, version_named,
    ):
        monkeypatch.setattr(
            "rlsbl.commands.release.validate.detect_strictcli",
            lambda d: (entry, lang),
        )
        _install_stub(tmp_path, monkeypatch, "older")

        with pytest.raises(ReleaseValidationError) as exc:
            _run_strictcli_schema_dump({}, lambda m: None, project_dir=str(tmp_path))
        assert version_named in str(exc.value)
        assert "predates `help --json`" in str(exc.value)

    def test_the_upgrade_the_refusal_names_lets_the_release_through(
        self, tmp_path, monkeypatch,
    ):
        """Applying the fix: the same project on current strictcli succeeds."""
        _python_project(tmp_path)
        _install_stub(tmp_path, monkeypatch, "older")
        with pytest.raises(ReleaseValidationError, match="upgrade"):
            _run_strictcli_schema_dump({}, lambda m: None, project_dir=str(tmp_path))

        monkeypatch.undo()
        _install_stub(tmp_path, monkeypatch, "current")
        _run_strictcli_schema_dump(
            {}, lambda m: None, project_dir=str(tmp_path), version="2.0.0",
        )
        data = json.loads(
            (tmp_path / ".strictmetadata" / ".cli-schema" / "schema.json").read_text(encoding="utf-8")
        )
        assert data["version"] == "2.0.0"

    def test_other_failures_report_the_programs_stderr(self, tmp_path, monkeypatch):
        """A failure that is not the missing `help` command is not misread as one."""
        _python_project(tmp_path)
        monkeypatch.setattr(
            "rlsbl.commands.release.validate._schema_dump_command",
            lambda e, lang: [sys.executable, "-c",
                             "import sys; sys.stderr.write('boom: no module x\\n');"
                             " sys.exit(2)"],
        )
        with pytest.raises(ReleaseValidationError) as exc:
            _run_strictcli_schema_dump({}, lambda m: None, project_dir=str(tmp_path))
        message = str(exc.value)
        assert "exited 2" in message
        assert "boom: no module x" in message
        assert "predates" not in message

    def test_stdout_that_is_not_a_help_document_is_refused(
        self, tmp_path, monkeypatch,
    ):
        """Exit 0 with something other than a help document writes nothing."""
        _python_project(tmp_path)
        monkeypatch.setattr(
            "rlsbl.commands.release.validate._schema_dump_command",
            lambda e, lang: [sys.executable, "-c", "print('Usage: myapp')"],
        )
        with pytest.raises(ReleaseValidationError, match="not a strictcli help document"):
            _run_strictcli_schema_dump({}, lambda m: None, project_dir=str(tmp_path))
        assert not (tmp_path / ".strictmetadata").exists()

    def test_timeout_raises_error(self, tmp_path):
        """When the dump command times out, ReleaseValidationError is raised."""
        _python_project(tmp_path)
        with patch("rlsbl.commands.release.effects") as mock_sp:
            mock_sp.run.side_effect = subprocess.TimeoutExpired("uv", 30)
            mock_sp.TimeoutExpired = subprocess.TimeoutExpired

            with pytest.raises(ReleaseValidationError, match="timed out"):
                _run_strictcli_schema_dump(
                    {}, lambda msg: None, project_dir=str(tmp_path),
                )

    def test_version_patch_errors_on_missing_version_key(self, tmp_path, monkeypatch):
        """A help document with no version key cannot carry the release version."""
        _python_project(tmp_path)
        _install_stub(
            tmp_path, monkeypatch, "current",
            document=_CANONICAL_SCHEMA_WITHOUT_VERSION,
        )
        with pytest.raises(ReleaseValidationError, match="no top-level 'version' key"):
            _run_strictcli_schema_dump(
                {}, lambda msg: None, project_dir=str(tmp_path), version="2.0.0",
            )
        assert not (tmp_path / ".strictmetadata").exists()


class TestStrictcliSchemaOrdering:
    """Tests that the schema dump runs in the correct position in the release pipeline."""

    def test_schema_dump_after_pre_checks_hook(self):
        """Schema dump must run after the pre-checks hook."""
        source = inspect.getsource(_run_cmd_inner)
        pre_checks_pos = source.index("pre_checks_script")
        schema_pos = source.index("_run_strictcli_schema_dump(")

        assert pre_checks_pos < schema_pos, (
            "pre-checks hook must appear before _run_strictcli_schema_dump"
        )

    def test_schema_dump_before_selfdoc_check(self):
        """Schema dump must run before the selfdoc check."""
        source = inspect.getsource(_run_cmd_inner)
        schema_pos = source.index("_run_strictcli_schema_dump(")
        selfdoc_pos = source.index("_run_selfdoc_check(")

        assert schema_pos < selfdoc_pos, (
            "_run_strictcli_schema_dump must appear before _run_selfdoc_check"
        )

    def test_schema_dump_before_preflight(self):
        """Schema dump must run before test/lint preflight checks."""
        source = inspect.getsource(_run_cmd_inner)
        schema_pos = source.index("_run_strictcli_schema_dump(")
        # Find the test/lint preflight (tag_expr=hook_selection("pre-release"), the preflight tag), not the
        # changelog preflight (tag_expr="preflight-changelog")
        preflight_pos = source.index('tag_expr=hook_selection("pre-release")')

        assert schema_pos < preflight_pos, (
            "_run_strictcli_schema_dump must appear before test/lint preflight"
        )
