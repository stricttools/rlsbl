"""Per-axis conformance: the yank dispatch.

``rlsbl/commands/yank.py`` used to branch on the target NAME (npm / go / pypi)
and fall through to a stderr line for anything else. The dispatch is now
``ReleaseTarget.yank``. Both sides are pinned here:

- a supported target behaves exactly as it did before the migration, and
- an unsupported target answers UNSUPPORTED naming itself, which the command
  renders as the same named skip line it printed before.
"""

from unittest.mock import patch

import pytest

from rlsbl.targets import TARGETS
from rlsbl.targets.outcomes import YankStatus


# --------------------------------------------------------------------------
# Axis: yank dispatch
# --------------------------------------------------------------------------


class TestYankAxis:
    """`ReleaseTarget.yank` replaces the npm/go/pypi name chain in yank.py."""

    def test_unsupported_target_reports_unsupported_naming_itself(self):
        """A target with no registry-removal action says so, naming itself."""
        target = TARGETS["plain"]
        outcome = target.yank(".", "1.0.0", "v1.0.0", reason=None, dry_run=True)
        assert outcome.status is YankStatus.UNSUPPORTED
        assert "plain" in outcome.message

    @pytest.mark.parametrize("name", sorted(TARGETS))
    def test_every_target_answers_yank(self, name):
        """No target may fall through: yank() always returns a YankOutcome."""
        target = TARGETS[name]
        outcome = target.yank(".", "1.0.0", "v1.0.0", reason=None, dry_run=True)
        assert isinstance(outcome.status, YankStatus)
        assert outcome.message

    def test_npm_dry_run_describes_the_deprecate_call(self, tmp_path):
        """npm still previews the exact `npm deprecate` invocation."""
        (tmp_path / "package.json").write_text('{"name": "my-pkg", "version": "1.0.0"}')
        outcome = TARGETS["npm"].yank(
            str(tmp_path), "1.0.0", "v1.0.0", reason="bad build", dry_run=True,
        )
        assert outcome.status is YankStatus.DONE
        assert "npm deprecate my-pkg@1.0.0" in outcome.message
        assert "bad build" in outcome.message

    def test_npm_without_a_name_is_incomplete_not_silent(self, tmp_path):
        outcome = TARGETS["npm"].yank(
            str(tmp_path), "1.0.0", "v1.0.0", reason=None, dry_run=True,
        )
        assert outcome.status is YankStatus.INCOMPLETE
        assert "package name" in outcome.message

    def test_go_adds_a_retract_directive(self, tmp_path):
        """go still writes the retract directive, and is idempotent about it."""
        (tmp_path / "go.mod").write_text("module example.com/foo\n\ngo 1.22\n")
        outcome = TARGETS["go"].yank(
            str(tmp_path), "1.0.0", "v1.0.0", reason=None, dry_run=False,
        )
        assert outcome.status is YankStatus.DONE
        assert "retract v1.0.0" in (tmp_path / "go.mod").read_text()

        again = TARGETS["go"].yank(
            str(tmp_path), "1.0.0", "v1.0.0", reason=None, dry_run=False,
        )
        assert again.status is YankStatus.DONE
        assert "already present" in again.message
        assert (tmp_path / "go.mod").read_text().count("retract v1.0.0") == 1

    def test_go_dry_run_writes_nothing(self, tmp_path):
        (tmp_path / "go.mod").write_text("module example.com/foo\n\ngo 1.22\n")
        outcome = TARGETS["go"].yank(
            str(tmp_path), "1.0.0", "v1.0.0", reason=None, dry_run=True,
        )
        assert outcome.status is YankStatus.DONE
        assert "retract" not in (tmp_path / "go.mod").read_text()

    def test_pypi_dry_run_waits_for_nothing(self, tmp_path, capsys):
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "my-pkg"\nversion = "1.0.0"\n'
        )
        outcome = TARGETS["pypi"].yank(
            str(tmp_path), "1.0.0", "v1.0.0", reason=None, dry_run=True,
        )
        assert outcome.status is YankStatus.DONE
        printed = capsys.readouterr().out
        assert "PyPI does not have a yank API" in printed
        assert "https://pypi.org/project/my-pkg/1.0.0/" in printed

    def test_command_prints_the_named_skip_for_an_unsupported_target(self, capsys):
        """The command surfaces the skip on stderr naming the target."""
        from rlsbl.commands.yank import _yank_target

        outcome = _yank_target(TARGETS["plain"], ".", "1.0.0", "v1.0.0", None, True)
        assert outcome.status is YankStatus.UNSUPPORTED
        err = capsys.readouterr().err
        assert "plain: no yank implementation (skipping)" in err


