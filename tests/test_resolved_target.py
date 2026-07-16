"""Tests for rlsbl.resolved_target: the ResolvedTarget dataclass and the
pipeline/target resolver.

The resolver takes a member's detected targets, its loaded pipelines (each
carrying an explicit ``target`` link from Phase 6.1), and the effective
``publish_mode`` string, and produces:

- ``resolved_targets``: one :class:`ResolvedTarget` per (target, linked
  pipeline) pair. A pipeline-less target yields a single ResolvedTarget with
  ``pipeline=None``. A target served by N pipelines yields N ResolvedTargets
  (the release flow publishes per pipeline).
- ``deploy_pipelines``: the target-less publishers (``target: null``). These
  are NOT targets, so they are surfaced separately -- never faked into a
  ResolvedTarget.

These are covered both as pure functions (disk-free) and end-to-end through
MemberContext against small on-disk fixtures mirroring real fleet shapes.
"""

import json

import pytest

from conftest import run_git
from rlsbl.errors import ConfigError
from rlsbl.member_context import resolve_member_context
from rlsbl.pipelines import load_pipelines
from rlsbl.pipelines.base import BasePipeline
from rlsbl.resolved_target import (
    ResolvedTarget,
    partition_pipelines,
    resolve_targets,
)
from rlsbl.targets import TargetEntry


def _pipe(name, target, *, pipeline_type="go", local=True, artifact=None):
    """Build a pipeline instance mirroring load_pipelines' output.

    load_pipelines sets ``instance.target`` from the entry's ``target`` link;
    we replicate that so pure-function tests match production wiring.
    """
    config = {"type": pipeline_type, "local": local, "target": target}
    if artifact is not None:
        config["artifact"] = artifact
    p = BasePipeline(name=name, pipeline_type=pipeline_type, local=local, config=config)
    p.target = target
    return p


# ---------------------------------------------------------------------------
# ResolvedTarget dataclass
# ---------------------------------------------------------------------------


class TestResolvedTargetDataclass:
    def test_fields_and_name_property(self):
        te = TargetEntry(name="go", path="/proj")
        p = _pipe("gopub", "go")
        rt = ResolvedTarget(
            target=te, path="/proj", pipeline=p,
            publish_mode="ci", artifact_kind="binary",
        )
        assert rt.target is te
        assert rt.path == "/proj"
        assert rt.pipeline is p
        assert rt.publish_mode == "ci"
        assert rt.artifact_kind == "binary"
        assert rt.name == "go"

    def test_pipeline_less_target_carries_none(self):
        te = TargetEntry(name="spec", path="/proj")
        rt = ResolvedTarget(
            target=te, path="/proj", pipeline=None,
            publish_mode="none", artifact_kind=None,
        )
        assert rt.pipeline is None
        assert rt.artifact_kind is None
        assert rt.name == "spec"


# ---------------------------------------------------------------------------
# partition_pipelines
# ---------------------------------------------------------------------------


class TestPartitionPipelines:
    def test_splits_linked_from_deploys(self):
        pipelines = {
            "gopub": _pipe("gopub", "go"),
            "docs": _pipe("docs", None, pipeline_type="cloudflare-pages"),
        }
        by_target, deploys = partition_pipelines(pipelines)
        assert set(by_target.keys()) == {"go"}
        assert [p.name for p in by_target["go"]] == ["gopub"]
        assert [p.name for p in deploys] == ["docs"]

    def test_multiple_pipelines_one_target(self):
        pipelines = {
            "pub1": _pipe("pub1", "npm", pipeline_type="npm"),
            "pub2": _pipe("pub2", "npm", pipeline_type="npm"),
        }
        by_target, deploys = partition_pipelines(pipelines)
        assert [p.name for p in by_target["npm"]] == ["pub1", "pub2"]
        assert deploys == []

    def test_empty(self):
        by_target, deploys = partition_pipelines({})
        assert by_target == {}
        assert deploys == []


# ---------------------------------------------------------------------------
# resolve_targets (pure)
# ---------------------------------------------------------------------------


class TestResolveTargets:
    def test_string_target_with_linked_pipeline(self):
        targets = [TargetEntry(name="go", path="/p")]
        pipelines = {"gopub": _pipe("gopub", "go", artifact="binary")}
        result = resolve_targets(targets, pipelines, "ci")
        assert len(result) == 1
        rt = result[0]
        assert rt.name == "go"
        assert rt.path == "/p"
        assert rt.pipeline.name == "gopub"
        assert rt.publish_mode == "ci"
        assert rt.artifact_kind == "binary"

    def test_dict_path_target(self):
        # dict+path target: TargetEntry already carries the resolved subdir path
        targets = [TargetEntry(name="npm", path="/p/packages/app")]
        pipelines = {"npmpub": _pipe("npmpub", "npm", pipeline_type="npm")}
        result = resolve_targets(targets, pipelines, "ci")
        assert len(result) == 1
        assert result[0].path == "/p/packages/app"
        assert result[0].pipeline.name == "npmpub"

    def test_pipeline_less_target_yields_none_pipeline(self):
        targets = [TargetEntry(name="spec", path="/p")]
        result = resolve_targets(targets, {}, "none")
        assert len(result) == 1
        assert result[0].pipeline is None
        assert result[0].artifact_kind is None
        assert result[0].publish_mode == "none"

    def test_target_less_pipeline_is_not_a_resolved_target(self):
        # A deploy pipeline (target: null) must never appear as a ResolvedTarget.
        targets = [TargetEntry(name="go", path="/p")]
        pipelines = {
            "gopub": _pipe("gopub", "go"),
            "docs": _pipe("docs", None, pipeline_type="cloudflare-pages"),
        }
        result = resolve_targets(targets, pipelines, "ci")
        assert [rt.name for rt in result] == ["go"]
        assert all(rt.pipeline is None or rt.pipeline.name != "docs" for rt in result)

    def test_multi_pipeline_target_yields_one_per_pair(self):
        targets = [TargetEntry(name="npm", path="/p")]
        pipelines = {
            "public": _pipe("public", "npm", pipeline_type="npm"),
            "mirror": _pipe("mirror", "npm", pipeline_type="npm"),
        }
        result = resolve_targets(targets, pipelines, "ci")
        assert len(result) == 2
        assert {rt.pipeline.name for rt in result} == {"public", "mirror"}
        # both pairs point at the same target object
        assert all(rt.name == "npm" for rt in result)

    def test_name_differs_from_type_link(self):
        # pipeline name "docs-site", type cloudflare-pages, links target "assets"
        targets = [TargetEntry(name="assets", path="/p/assets")]
        pipelines = {
            "docs-site": _pipe("docs-site", "assets", pipeline_type="cloudflare-pages"),
        }
        result = resolve_targets(targets, pipelines, "ci")
        assert len(result) == 1
        assert result[0].name == "assets"
        assert result[0].pipeline.name == "docs-site"
        assert result[0].pipeline.pipeline_type == "cloudflare-pages"

    def test_publish_mode_carried_verbatim(self):
        targets = [TargetEntry(name="go", path="/p")]
        result = resolve_targets(targets, {}, "none")
        assert result[0].publish_mode == "none"

    def test_artifact_passthrough_none_when_absent(self):
        targets = [TargetEntry(name="go", path="/p")]
        pipelines = {"gopub": _pipe("gopub", "go")}  # no artifact key
        result = resolve_targets(targets, pipelines, "ci")
        assert result[0].artifact_kind is None

    def test_dangling_ref_hard_errors(self):
        # pipeline links a target that isn't in the targets list -> hard error
        targets = [TargetEntry(name="go", path="/p")]
        pipelines = {"bad": _pipe("bad", "nonexistent")}
        with pytest.raises(ConfigError, match="nonexistent"):
            resolve_targets(targets, pipelines, "ci")


# ---------------------------------------------------------------------------
# MemberContext integration (on-disk fixtures mirroring fleet shapes)
# ---------------------------------------------------------------------------


def _make_standalone(tmp_path, config, files):
    """Create a standalone project dir with .rlsbl/config.json and files.

    ``files`` maps relative path -> content; parent dirs are created.
    """
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".rlsbl").mkdir()
    (proj / ".rlsbl" / "config.json").write_text(json.dumps(config))
    for rel, content in files.items():
        fp = proj / rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
    run_git(proj, "init", "-q", "-b", "main")
    run_git(proj, "config", "user.email", "t@t.local")
    run_git(proj, "config", "user.name", "T")
    return proj


class TestMemberContextResolvedTargets:
    def test_www_like_assets_and_docs_deploy(self, tmp_path):
        """WWW-shaped: a 'plain' assets target plus a target-less docs deploy.

        The docs deploy (target: null) must appear only in deploy_pipelines,
        never as a ResolvedTarget. The assets target has no linked pipeline,
        so it resolves with pipeline=None.
        """
        config = {
            "publish_mode": "ci",
            "targets": ["plain"],
            "pipelines": {
                "docs-deploy": {
                    "type": "cloudflare-pages", "local": True, "target": None,
                },
            },
        }
        proj = _make_standalone(tmp_path, config, {"README.md": "x\n"})
        ctx = resolve_member_context(str(proj))

        assert [rt.name for rt in ctx.resolved_targets] == ["plain"]
        assert ctx.resolved_targets[0].pipeline is None
        assert ctx.resolved_targets[0].publish_mode == "ci"
        assert [p.name for p in ctx.deploy_pipelines] == ["docs-deploy"]

    def test_npm_subdir_linked_pipeline(self, tmp_path):
        """wakethemup-shaped: an npm target in a subdir with a linked pipeline."""
        config = {
            "publish_mode": "ci",
            "targets": [{"name": "npm", "path": "packages/app"}],
            "pipelines": {
                "npm-publish": {
                    "type": "npm", "local": False, "provenance": True,
                    "target": "npm",
                },
            },
        }
        proj = _make_standalone(
            tmp_path, config,
            {"packages/app/package.json": '{"name":"app","version":"1.0.0"}\n'},
        )
        ctx = resolve_member_context(str(proj))

        assert len(ctx.resolved_targets) == 1
        rt = ctx.resolved_targets[0]
        assert rt.name == "npm"
        assert rt.path == str(proj / "packages" / "app")
        assert rt.pipeline.name == "npm-publish"
        assert ctx.deploy_pipelines == []

    def test_resolved_targets_is_cached(self, tmp_path):
        config = {
            "publish_mode": "ci",
            "targets": ["plain"],
        }
        proj = _make_standalone(tmp_path, config, {"README.md": "x\n"})
        ctx = resolve_member_context(str(proj))
        assert ctx.resolved_targets is ctx.resolved_targets
