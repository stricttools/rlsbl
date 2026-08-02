"""A fresh `rlsbl scaffold` must produce output that passes rlsbl's own checks.

Two bootstrap failures, both observed on a virgin Go repo:

1. The config scaffold auto-creates carried a ``pipelines`` entry with no
   ``target`` link, so ``rlsbl check --name config-schema`` immediately failed
   on the tool's own output.
2. Because the link was missing, publish-template resolution could not match
   the pipeline to the target and fell back to the target-name default --
   handing a Go *library* repo the binary/goreleaser publish workflow. The
   second scaffold pass then reported "unchanged", so the documented
   scaffold-review-scaffold flow never healed it.

Both are the same root cause; both are pinned here because they fail in
different places (config validation vs. rendered workflow).
"""

import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from rlsbl.commands.init_cmd import run_cmd
from rlsbl.config import (
    read_project_config,
    validate_config_schema,
    validate_pipeline_target_links,
    validate_pipelines_config,
    validate_test_config,
)
from rlsbl.context import ProjectContext


def _write_go_library(root: Path):
    (root / "go.mod").write_text("module github.com/acme/golib\n\ngo 1.23\n")
    (root / "golib.go").write_text(
        "package golib\n\nfunc Hello() string { return \"hi\" }\n"
    )
    (root / "VERSION").write_text("0.1.0\n")


def _write_go_binary(root: Path):
    (root / "go.mod").write_text("module github.com/acme/gotool\n\ngo 1.23\n")
    (root / "main.go").write_text("package main\n\nfunc main() {}\n")
    (root / "VERSION").write_text("0.1.0\n")


def _scaffold_pass(root: Path):
    """One `rlsbl scaffold` pass with a freshly-read context, like the CLI."""
    ctx = ProjectContext(
        project_root=Path("."), workspace_root=None,
        config=read_project_config("."),
    )
    with patch("sys.stdout", new_callable=StringIO):
        run_cmd("go", [], {"no-tag": True}, ctx=ctx)


def _config(root: Path):
    return json.loads((root / ".rlsbl" / "config.json").read_text())


def _publish(root: Path):
    return (root / ".github" / "workflows" / "publish.yml").read_text()


class TestGeneratedConfigPassesItsOwnSchema:
    """Scaffold must never emit a config its own checks reject."""

    def test_pipeline_entry_declares_its_target_link(self, mock_git_repo):
        root = mock_git_repo
        _write_go_library(root)

        _scaffold_pass(root)

        pipelines = _config(root)["pipelines"]
        assert "target" in pipelines["go"], (
            "scaffold-generated pipeline has no 'target' link"
        )
        assert pipelines["go"]["target"] == "go"

    def test_generated_config_validates(self, mock_git_repo):
        root = mock_git_repo
        _write_go_library(root)

        _scaffold_pass(root)

        # The four validators `rlsbl check --name config-schema` runs.
        config = _config(root)
        validate_config_schema(config, project_dir=str(root))
        validate_pipelines_config(config, project_root=str(root))
        validate_pipeline_target_links(config)
        validate_test_config(config)

    def test_target_link_resolves_to_a_configured_target(self, mock_git_repo):
        root = mock_git_repo
        _write_go_library(root)

        _scaffold_pass(root)

        config = _config(root)
        names = {t["name"] if isinstance(t, dict) else t
                 for t in config["targets"]}
        assert config["pipelines"]["go"]["target"] in names


class TestArtifactKindDrivesTheFirstPass:
    """A library repo must never be handed the goreleaser publish workflow."""

    def test_library_gets_the_library_template_on_the_first_pass(
            self, mock_git_repo):
        root = mock_git_repo
        _write_go_library(root)

        _scaffold_pass(root)

        assert _config(root)["pipelines"]["go"]["artifact"] == "library"
        publish = _publish(root)
        assert "goreleaser" not in publish, (
            "library repo got the binary/goreleaser publish workflow"
        )

    def test_library_stays_library_through_the_documented_flow(
            self, mock_git_repo):
        """scaffold -> review config -> scaffold again (the documented flow)."""
        root = mock_git_repo
        _write_go_library(root)

        _scaffold_pass(root)
        _scaffold_pass(root)

        assert "goreleaser" not in _publish(root)

    def test_binary_still_gets_the_goreleaser_template(self, mock_git_repo):
        root = mock_git_repo
        _write_go_binary(root)

        _scaffold_pass(root)

        assert _config(root)["pipelines"]["go"]["artifact"] == "binary"
        assert "goreleaser" in _publish(root)


class TestNextStepsMatchTheArtifactKind:
    """The printed next steps must not describe a build that never happens."""

    def _steps(self, root):
        from rlsbl.commands.init_cmd import _next_steps_for
        return _next_steps_for("go", _config(root))

    def test_library_next_steps_do_not_mention_goreleaser(self, mock_git_repo):
        root = mock_git_repo
        _write_go_library(root)

        _scaffold_pass(root)

        steps = self._steps(root)
        assert not any("oReleaser" in s for s in steps), steps
        assert any("module proxy" in s for s in steps), steps

    def test_binary_next_steps_still_mention_goreleaser(self, mock_git_repo):
        root = mock_git_repo
        _write_go_binary(root)

        _scaffold_pass(root)

        assert any("oReleaser" in s for s in self._steps(root))

    def test_unknown_registry_has_no_steps(self):
        from rlsbl.commands.init_cmd import _next_steps_for
        assert _next_steps_for("zig", {}) is None

    def test_missing_pipelines_falls_back_to_the_generic_text(self):
        from rlsbl.commands.init_cmd import NEXT_STEPS, _next_steps_for
        assert _next_steps_for("go", {}) == list(NEXT_STEPS["go"])
