"""Tests for lint config filtering in shared_template_mappings."""

from pathlib import Path

from rlsbl.context import ProjectContext
from rlsbl.targets.base import BaseTarget


def _ctx(targets=None):
    """Create a ProjectContext with the given targets config."""
    config = {}
    if targets is not None:
        config["targets"] = targets
    return ProjectContext(project_root=Path("."), workspace_root=None, config=config)


def _lint_targets(mappings):
    """Extract just the lint config target paths from a mappings list."""
    return {m["target"] for m in mappings if m["target"].startswith(".rlsbl/lint/")}


class TestLintConfigFiltering:
    """Unit tests for lint config filtering based on declared targets."""

    def test_pypi_only_gets_python_lint(self):
        ctx = _ctx(targets=["pypi"])
        mappings = BaseTarget().shared_template_mappings(ctx)
        assert _lint_targets(mappings) == {".rlsbl/lint/python.toml"}

    def test_npm_pypi_gets_both(self):
        ctx = _ctx(targets=["npm", "pypi"])
        mappings = BaseTarget().shared_template_mappings(ctx)
        assert _lint_targets(mappings) == {
            ".rlsbl/lint/python.toml",
            ".rlsbl/lint/npm.toml",
        }

    def test_go_gets_go_lint(self):
        ctx = _ctx(targets=["go"])
        mappings = BaseTarget().shared_template_mappings(ctx)
        assert _lint_targets(mappings) == {".rlsbl/lint/go.toml"}

    def test_no_targets_gets_all(self):
        ctx = _ctx()  # no targets key
        mappings = BaseTarget().shared_template_mappings(ctx)
        assert _lint_targets(mappings) == {
            ".rlsbl/lint/python.toml",
            ".rlsbl/lint/npm.toml",
            ".rlsbl/lint/go.toml",
        }

    def test_non_lint_targets_get_nothing(self):
        ctx = _ctx(targets=["cargo", "docker"])
        mappings = BaseTarget().shared_template_mappings(ctx)
        assert _lint_targets(mappings) == set()

    def test_all_three_targets(self):
        ctx = _ctx(targets=["pypi", "npm", "go"])
        mappings = BaseTarget().shared_template_mappings(ctx)
        assert _lint_targets(mappings) == {
            ".rlsbl/lint/python.toml",
            ".rlsbl/lint/npm.toml",
            ".rlsbl/lint/go.toml",
        }

    def test_dict_target_entries(self):
        ctx = _ctx(targets=[{"name": "pypi", "path": "lib"}])
        mappings = BaseTarget().shared_template_mappings(ctx)
        assert _lint_targets(mappings) == {".rlsbl/lint/python.toml"}

    def test_mixed_string_and_dict_targets(self):
        ctx = _ctx(targets=["go", {"name": "npm"}])
        mappings = BaseTarget().shared_template_mappings(ctx)
        assert _lint_targets(mappings) == {
            ".rlsbl/lint/go.toml",
            ".rlsbl/lint/npm.toml",
        }

    def test_empty_targets_list_gets_all(self):
        ctx = _ctx(targets=[])
        mappings = BaseTarget().shared_template_mappings(ctx)
        assert _lint_targets(mappings) == {
            ".rlsbl/lint/python.toml",
            ".rlsbl/lint/npm.toml",
            ".rlsbl/lint/go.toml",
        }

    def test_none_ctx_gets_all(self):
        mappings = BaseTarget().shared_template_mappings(None)
        assert _lint_targets(mappings) == {
            ".rlsbl/lint/python.toml",
            ".rlsbl/lint/npm.toml",
            ".rlsbl/lint/go.toml",
        }

    def test_non_lint_mappings_always_present(self):
        """Non-lint shared mappings are present regardless of targets."""
        ctx = _ctx(targets=["cargo"])
        mappings = BaseTarget().shared_template_mappings(ctx)
        targets = {m["target"] for m in mappings}
        assert "CHANGELOG.md" in targets
        assert ".gitignore" in targets
        assert ".rlsbl/hooks/pre-checks.sh" in targets
        assert ".rlsbl/hooks/pre-release.sh" in targets
        assert ".rlsbl/hooks/post-release.sh" in targets
        assert ".rlsbl/changes/unreleased.jsonl" in targets
