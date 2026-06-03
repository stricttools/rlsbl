"""Tests for the license-file project check."""

import os

from conftest import make_ctx

from rlsbl.checks import register_checks
from rlsbl import app


class TestLicenseFileCheck:
    """Tests for the license-file check."""

    def test_missing_license_fails(self, tmp_project):
        """Missing LICENSE file returns fail."""
        ctx = make_ctx(tmp_project)
        result = app._check_defs["license-file"].impl(ctx)
        assert result.status == "fail"
        assert "not found" in result.message

    def test_empty_license_fails(self, tmp_project):
        """Empty LICENSE file returns fail."""
        (tmp_project / "LICENSE").write_text("")
        ctx = make_ctx(tmp_project)
        result = app._check_defs["license-file"].impl(ctx)
        assert result.status == "fail"
        assert "empty" in result.message

    def test_license_with_template_vars_fails(self, tmp_project):
        """LICENSE containing {{author}} returns fail."""
        (tmp_project / "LICENSE").write_text(
            "MIT License\n\nCopyright (c) 2026 {{author}}\n"
        )
        ctx = make_ctx(tmp_project)
        result = app._check_defs["license-file"].impl(ctx)
        assert result.status == "fail"
        assert "{{author}}" in result.message
        assert "unreplaced" in result.message

    def test_license_with_dotted_template_var_fails(self, tmp_project):
        """LICENSE containing {{project.name}} returns fail."""
        (tmp_project / "LICENSE").write_text(
            "MIT License\n\nCopyright (c) 2026 {{project.name}}\n"
        )
        ctx = make_ctx(tmp_project)
        result = app._check_defs["license-file"].impl(ctx)
        assert result.status == "fail"
        assert "{{project.name}}" in result.message

    def test_valid_license_passes(self, tmp_project):
        """A normal LICENSE file returns pass."""
        (tmp_project / "LICENSE").write_text(
            "MIT License\n\nCopyright (c) 2026 Test Author\n\n"
            "Permission is hereby granted, free of charge...\n"
        )
        ctx = make_ctx(tmp_project)
        result = app._check_defs["license-file"].impl(ctx)
        assert result.status == "pass"
        assert "valid" in result.message
