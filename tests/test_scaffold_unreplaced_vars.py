"""Tests for the scaffold-unreplaced-vars quality check."""

import os

from conftest import make_ctx

from rlsbl import app


class TestScaffoldUnreplacedVars:
    """Tests for the scaffold-unreplaced-vars check."""

    def test_workflow_with_template_var_fails(self, tmp_project):
        """Workflow file containing {{pypi.minRequiredPython}} is flagged."""
        wf_dir = tmp_project / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "publish.yml").write_text(
            "name: Publish\n"
            "on: push\n"
            "jobs:\n"
            "  build:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            '      - uses: actions/setup-python@v4\n'
            "        with:\n"
            "          python-version: '{{pypi.minRequiredPython}}'\n"
        )
        ctx = make_ctx(tmp_project)
        result = app._check_defs["scaffold-unreplaced-vars"].impl(ctx)
        assert result.status == "fail"
        assert "unreplaced" in result.message
        assert any("{{pypi.minRequiredPython}}" in d for d in (p.text for p in result.problems))

    def test_clean_workflow_passes(self, tmp_project):
        """Workflow file with no rlsbl template variables passes."""
        wf_dir = tmp_project / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "ci.yml").write_text(
            "name: CI\n"
            "on: push\n"
            "jobs:\n"
            "  test:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            '      - uses: actions/checkout@v4\n'
        )
        ctx = make_ctx(tmp_project)
        result = app._check_defs["scaffold-unreplaced-vars"].impl(ctx)
        assert result.status == "pass"

    def test_github_actions_syntax_not_flagged(self, tmp_project):
        """GitHub Actions ${{ github.token }} syntax is not flagged."""
        wf_dir = tmp_project / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "ci.yml").write_text(
            "name: CI\n"
            "on: push\n"
            "env:\n"
            "  TOKEN: ${{ github.token }}\n"
            "  REF: ${{ github.ref }}\n"
            "jobs:\n"
            "  test:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            '      - uses: actions/checkout@v4\n'
        )
        ctx = make_ctx(tmp_project)
        result = app._check_defs["scaffold-unreplaced-vars"].impl(ctx)
        assert result.status == "pass"

    def test_hook_with_template_var_fails(self, tmp_project):
        """Hook script containing {{projectName}} is flagged."""
        hooks_dir = tmp_project / ".rlsbl" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "pre-release.sh").write_text(
            "#!/bin/bash\n"
            "echo 'Releasing {{projectName}}'\n"
        )
        ctx = make_ctx(tmp_project)
        result = app._check_defs["scaffold-unreplaced-vars"].impl(ctx)
        assert result.status == "fail"
        assert any("{{projectName}}" in d for d in (p.text for p in result.problems))

    def test_no_scaffold_files_passes(self, tmp_project):
        """Project with no scaffold files passes."""
        ctx = make_ctx(tmp_project)
        result = app._check_defs["scaffold-unreplaced-vars"].impl(ctx)
        assert result.status == "pass"

    def test_mixed_github_and_rlsbl_syntax(self, tmp_project):
        """File with both ${{ ... }} and {{ ... }} flags only the rlsbl syntax."""
        wf_dir = tmp_project / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "publish.yml").write_text(
            "name: Publish\n"
            "on: push\n"
            "env:\n"
            "  TOKEN: ${{ secrets.NPM_TOKEN }}\n"
            "  VERSION: {{version}}\n"
            "jobs:\n"
            "  publish:\n"
            "    runs-on: ubuntu-latest\n"
        )
        ctx = make_ctx(tmp_project)
        result = app._check_defs["scaffold-unreplaced-vars"].impl(ctx)
        assert result.status == "fail"
        assert any("{{version}}" in d for d in (p.text for p in result.problems))
        # ${{ secrets.NPM_TOKEN }} should NOT appear in errors
        assert not any("secrets" in d for d in (p.text for p in result.problems))

    def test_docker_metadata_action_not_flagged(self, tmp_project):
        """Docker metadata-action type=semver,pattern={{version}} is not flagged."""
        wf_dir = tmp_project / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "publish.yml").write_text(
            "name: Publish\n"
            "on: push\n"
            "jobs:\n"
            "  docker:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - uses: docker/metadata-action@v5\n"
            "        with:\n"
            "          tags: |\n"
            "            type=semver,pattern={{version}}\n"
            "            type=semver,pattern={{major}}.{{minor}}\n"
            "            type=semver,pattern={{major}}\n"
        )
        ctx = make_ctx(tmp_project)
        result = app._check_defs["scaffold-unreplaced-vars"].impl(ctx)
        assert result.status == "pass"

    def test_docker_metadata_mixed_with_real_template_var(self, tmp_project):
        """Docker metadata patterns pass but real template vars on other lines fail."""
        wf_dir = tmp_project / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "publish.yml").write_text(
            "name: Publish\n"
            "on: push\n"
            "jobs:\n"
            "  docker:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - uses: docker/metadata-action@v5\n"
            "        with:\n"
            "          tags: |\n"
            "            type=semver,pattern={{version}}\n"
            "            type=semver,pattern={{major}}.{{minor}}\n"
            "      - uses: actions/setup-python@v4\n"
            "        with:\n"
            "          python-version: '{{pypi.minRequiredPython}}'\n"
        )
        ctx = make_ctx(tmp_project)
        result = app._check_defs["scaffold-unreplaced-vars"].impl(ctx)
        assert result.status == "fail"
        assert any("{{pypi.minRequiredPython}}" in d for d in (p.text for p in result.problems))
        # Docker metadata patterns should not appear in errors
        assert not any("{{version}}" in d for d in (p.text for p in result.problems))
        assert not any("{{major}}" in d for d in (p.text for p in result.problems))
        assert not any("{{minor}}" in d for d in (p.text for p in result.problems))

    def test_goreleaser_with_template_var_fails(self, tmp_project):
        """.goreleaser.yml containing {{goModule}} is flagged."""
        (tmp_project / ".goreleaser.yml").write_text(
            "project_name: {{goModule}}\n"
            "builds:\n"
            "  - main: ./cmd\n"
        )
        ctx = make_ctx(tmp_project)
        result = app._check_defs["scaffold-unreplaced-vars"].impl(ctx)
        assert result.status == "fail"
        assert any("{{goModule}}" in d for d in (p.text for p in result.problems))
