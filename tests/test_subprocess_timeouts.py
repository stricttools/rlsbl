"""Tests for subprocess timeout handling in testing.py."""

import subprocess
from unittest.mock import patch, MagicMock


from rlsbl.testing import run_project_tests


# ---------------------------------------------------------------------------
# testing.py timeout tests
# ---------------------------------------------------------------------------


class TestPypiTestsTimeout:
    """_run_pypi_tests handles subprocess.TimeoutExpired."""

    def test_pypi_pytest_timeout_returns_false(self, tmp_path):
        """When uv run pytest times out, run_project_tests returns False."""
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "pkg"\nversion = "0.1.0"\n\n'
            '[dependency-groups]\ndev = ["pytest>=8.0"]\n'
        )
        with (
            patch("rlsbl.testing.require_tool") as mock_tool,
            patch("rlsbl.testing.find_uv_workspace_root", return_value=None),
            patch(
                "rlsbl.effects.run",
            ) as mock_run,
        ):
            mock_tool.return_value = "/usr/bin/uv"
            # Standalone: single call (uv run pytest) times out
            mock_run.side_effect = [
                subprocess.TimeoutExpired(
                    cmd=["uv", "run", "pytest"], timeout=120
                ),
            ]

            result = run_project_tests("pypi", project_dir=str(tmp_path))

        assert not result.passed

    def test_pypi_uses_config_timeout(self, tmp_path):
        """run_project_tests passes config-driven timeout to subprocess.run."""
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "pkg"\nversion = "0.1.0"\n\n'
            '[dependency-groups]\ndev = ["pytest>=8.0"]\n'
        )
        mock_result = MagicMock()
        mock_result.returncode = 0

        with (
            patch("rlsbl.testing.require_tool") as mock_tool,
            patch("rlsbl.testing.find_uv_workspace_root", return_value=None),
            patch(
                "rlsbl.effects.run",
                return_value=mock_result,
            ) as mock_run,
        ):
            mock_tool.return_value = "/usr/bin/uv"
            run_project_tests(
                "pypi", project_dir=str(tmp_path),
                config={"check_timeout": 600},
            )

        assert mock_run.call_args.kwargs.get("timeout") == 600


class TestGoTestsTimeout:
    """_run_go_tests handles subprocess.TimeoutExpired."""

    def test_go_timeout_returns_false(self, tmp_path):
        """When go test times out, run_project_tests returns False."""
        with patch(
            "rlsbl.effects.run",
            side_effect=subprocess.TimeoutExpired(
                cmd=["go", "test", "./...", "-race", "-short", "-count=1"],
                timeout=120,
            ),
        ):
            result = run_project_tests("go", project_dir=str(tmp_path))

        assert not result.passed

    def test_go_uses_config_timeout(self, tmp_path):
        """run_project_tests passes config-driven timeout to subprocess.run."""
        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch(
            "rlsbl.effects.run",
            return_value=mock_result,
        ) as mock_run:
            run_project_tests(
                "go", project_dir=str(tmp_path),
                config={"check_timeout": 450},
            )

        assert mock_run.call_args.kwargs.get("timeout") == 450
