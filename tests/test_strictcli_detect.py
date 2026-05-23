"""Tests for strictcli project detection."""

from rlsbl.strictcli_detect import detect_strictcli


class TestDetectStrictcli:
    """Tests for detect_strictcli()."""

    def test_detects_strictcli_and_returns_entry_point(self, tmp_path):
        """When pyproject.toml has strictcli in deps and a script, returns the entry point."""
        (tmp_path / "pyproject.toml").write_text(
            '[project]\n'
            'name = "myapp"\n'
            'version = "1.0.0"\n'
            'dependencies = ["strictcli", "tomlkit"]\n'
            '\n'
            '[project.scripts]\n'
            'myapp = "myapp:main"\n'
        )
        result = detect_strictcli(str(tmp_path))
        assert result == ("myapp", "python")

    def test_returns_none_when_no_strictcli(self, tmp_path):
        """When strictcli is not in dependencies, returns None."""
        (tmp_path / "pyproject.toml").write_text(
            '[project]\n'
            'name = "myapp"\n'
            'version = "1.0.0"\n'
            'dependencies = ["tomlkit", "click"]\n'
            '\n'
            '[project.scripts]\n'
            'myapp = "myapp:main"\n'
        )
        result = detect_strictcli(str(tmp_path))
        assert result is None

    def test_returns_none_when_no_pyproject(self, tmp_path):
        """When no pyproject.toml exists, returns None."""
        result = detect_strictcli(str(tmp_path))
        assert result is None

    def test_extracts_first_entry_point(self, tmp_path):
        """When multiple scripts are defined, returns the first one."""
        (tmp_path / "pyproject.toml").write_text(
            '[project]\n'
            'name = "myapp"\n'
            'version = "1.0.0"\n'
            'dependencies = ["strictcli"]\n'
            '\n'
            '[project.scripts]\n'
            'myapp = "myapp:main"\n'
            'myapp-helper = "myapp:helper"\n'
        )
        result = detect_strictcli(str(tmp_path))
        assert result == ("myapp", "python")

    def test_returns_none_when_no_scripts(self, tmp_path):
        """When strictcli is a dep but no scripts section exists, returns None."""
        (tmp_path / "pyproject.toml").write_text(
            '[project]\n'
            'name = "mylib"\n'
            'version = "1.0.0"\n'
            'dependencies = ["strictcli"]\n'
        )
        result = detect_strictcli(str(tmp_path))
        assert result is None

    def test_detects_strictcli_with_version_constraint(self, tmp_path):
        """When strictcli has a version constraint (e.g. 'strictcli>=1.0'), still detects it."""
        (tmp_path / "pyproject.toml").write_text(
            '[project]\n'
            'name = "myapp"\n'
            'version = "1.0.0"\n'
            'dependencies = ["strictcli>=1.0"]\n'
            '\n'
            '[project.scripts]\n'
            'myapp = "myapp:main"\n'
        )
        result = detect_strictcli(str(tmp_path))
        assert result == ("myapp", "python")

    def test_returns_none_when_no_project_section(self, tmp_path):
        """When pyproject.toml has no [project] section, returns None."""
        (tmp_path / "pyproject.toml").write_text(
            '[build-system]\n'
            'requires = ["hatchling"]\n'
        )
        result = detect_strictcli(str(tmp_path))
        assert result is None
