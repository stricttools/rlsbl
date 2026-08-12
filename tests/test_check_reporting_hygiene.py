"""Reporter text hygiene: no rlsbl check may hand the reporter empty text.

strictcli's reporter rejects an empty problem text, an empty outcome message
and an empty skip reason with a ``ValueError``, and that exception propagates
out of the entire check run -- every other check's results are lost and nothing
names the check that produced it.  Two rlsbl-side sources of empty text are
covered here: blank lines inside a tool's own output (see also
``test_external_checks.TestBlankLineSeparatedOutput``), and an exception raised
with no message.
"""

import pytest

from strictcli import ErrorReporter

from rlsbl.checks._common import exception_text, reportable_lines, summary_line
from rlsbl.errors import ConfigError


class TestReportableLines:
    def test_drops_blank_and_whitespace_only_lines(self):
        text = "first\n\nsecond\n   \n\t\nthird\n"
        assert reportable_lines(text) == ["first", "second", "third"]

    def test_limit_counts_real_lines_not_blanks(self):
        text = "a\n\nb\n\nc\n\nd\n"
        assert reportable_lines(text, limit=2) == ["a", "b"]

    def test_empty_and_none_yield_nothing(self):
        assert reportable_lines("") == []
        assert reportable_lines(None) == []
        assert reportable_lines("\n \n\n") == []

    def test_trailing_whitespace_is_stripped_indentation_is_kept(self):
        assert reportable_lines("  indented   \n") == ["  indented"]

    def test_every_returned_line_is_reporter_safe(self):
        reporter = ErrorReporter()
        for line in reportable_lines("x\n\n  \ny\n"):
            reporter.error(line)
        outcome = reporter.found("2 findings")
        assert [p.text for p in outcome.problems] == ["x", "y"]


class TestSummaryLine:
    def test_returns_first_non_blank_line(self):
        assert summary_line("\n\n  real finding\nsecond\n") == "real finding"

    def test_truncates_to_limit(self):
        assert summary_line("x" * 300) == "x" * 200
        assert summary_line("x" * 300, limit=10) == "x" * 10

    def test_blank_input_yields_the_fallback_never_empty(self):
        assert summary_line("") == "(no message)"
        assert summary_line("\n \n") == "(no message)"
        assert summary_line(None, fallback="passed") == "passed"


class TestExceptionText:
    def test_keeps_a_real_message(self):
        assert exception_text(ConfigError("bad key")) == "bad key"

    def test_message_less_exception_falls_back_to_the_class_name(self):
        text = exception_text(ConfigError())
        assert text
        assert "ConfigError" in text

    def test_whitespace_only_message_falls_back(self):
        text = exception_text(ConfigError("   \n  "))
        assert text.strip()
        assert "ConfigError" in text

    def test_explicit_fallback_is_used(self):
        assert exception_text(ConfigError(""), fallback="config unreadable") == (
            "config unreadable"
        )

    def test_result_is_always_reporter_safe(self):
        reporter = ErrorReporter()
        reporter.error(exception_text(ConfigError()))
        outcome = reporter.found("one problem")
        assert [p.text for p in outcome.problems] == ["ConfigError (no message)"]


class TestMessagelessExceptionInChecks:
    """A check hitting a message-less exception fails with attribution."""

    def test_strictspec_gate_reports_instead_of_raising(self, tmp_path, monkeypatch):
        from rlsbl import strictspec_gate as gate_mod
        from rlsbl.checks import strictspec_gate as check_mod

        def boom(config, root):
            raise ConfigError()

        monkeypatch.setattr(gate_mod, "evaluate_certificate_gate", boom)

        captured = {}

        class MockApp:
            def error_check(self, name):
                def decorator(fn):
                    captured[name] = fn
                    return fn
                return decorator

        check_mod.register_strictspec_gate_checks(MockApp())
        fn = captured["strictspec-certificate-gate"]

        class FakeCtx:
            project_root = tmp_path
            config = {"strictspec_gate": {"certificate": "cert.json"}}

        result = fn(FakeCtx(), ErrorReporter())
        assert result.status == "fail"
        assert result.message.strip()
        assert all(p.text.strip() for p in result.problems)

    def test_config_schema_reports_instead_of_raising(self, tmp_path, monkeypatch):
        from rlsbl import config as config_mod
        from tests.conftest import capture_all_checks

        def boom(config, project_dir=None):
            raise ConfigError()

        monkeypatch.setattr(config_mod, "validate_config_schema", boom)

        checks = capture_all_checks()
        fn = checks["config-schema"]

        class FakeCtx:
            project_root = tmp_path
            config = {"publish_mode": "ci"}

        result = fn(FakeCtx())
        assert result.status == "fail"
        assert result.message.strip()
        assert all(p.text.strip() for p in result.problems)
        assert any("ConfigError" in p.text for p in result.problems)


@pytest.mark.parametrize("text", ["", "   ", "\n\n"])
def test_reporter_still_rejects_empty_text(text):
    """The framework contract these helpers exist to satisfy."""
    reporter = ErrorReporter()
    with pytest.raises(ValueError, match="non-empty string"):
        reporter.error(text)
