"""Standalone/implicit-mode publish loop routes each pipeline to its target path.

A multi-target project whose targets live in distinct subdirectories must have
each pipeline publish from ITS OWN linked target's path -- not the primary
target's path. Targetless (deploy) pipelines fall back to the primary/root
path.

Covers _publish_standalone_pipelines (extracted from _execute_release).
"""

import json
from types import SimpleNamespace

import pytest

from rlsbl.commands.release.execute import _publish_standalone_pipelines


class _FakePipeline:
    """Minimal pipeline double recording (path, version) publish calls."""

    def __init__(self, name, target, fail=False):
        self.name = name
        self.target = target
        self.fail = fail
        self.publish_calls = []

    def publish(self, path, version, ctx=None):
        self.publish_calls.append((path, version))
        if self.fail:
            raise RuntimeError(f"{self.name} boom")


def _patch_pipelines(monkeypatch, pipelines):
    # The helper resolves load_pipelines through the release package __init__
    # so mock.patch/setattr on that name is honoured at call time.
    monkeypatch.setattr(
        "rlsbl.commands.release.load_pipelines",
        lambda config: pipelines,
    )


def test_each_pipeline_publishes_from_its_own_target_path(tmp_path, monkeypatch):
    pypi = _FakePipeline("pypi", "pypi")
    npm = _FakePipeline("npm", "npm")
    pipelines = {"pypi": pypi, "npm": npm}
    _patch_pipelines(monkeypatch, pipelines)

    target_paths = {"pypi": "/repo/py", "npm": "/repo/js"}
    state_path = str(tmp_path / "state.json")

    _publish_standalone_pipelines(
        SimpleNamespace(config={}), target_paths, "/repo", "1.2.3",
        state_path, lambda _m: None,
    )

    # Each pipeline published from its own linked target's subdirectory,
    # NOT the shared primary path "/repo".
    assert pypi.publish_calls == [("/repo/py", "1.2.3")]
    assert npm.publish_calls == [("/repo/js", "1.2.3")]


def test_targetless_pipeline_uses_primary_path(tmp_path, monkeypatch):
    pypi = _FakePipeline("pypi", "pypi")
    deploy = _FakePipeline("deploy", None)  # deploy pipeline, no target link
    _patch_pipelines(monkeypatch, {"pypi": pypi, "deploy": deploy})

    target_paths = {"pypi": "/repo/py"}
    state_path = str(tmp_path / "state.json")

    _publish_standalone_pipelines(
        SimpleNamespace(config={}), target_paths, "/repo/root", "2.0.0",
        state_path, lambda _m: None,
    )

    assert pypi.publish_calls == [("/repo/py", "2.0.0")]
    # No target_paths entry for a None target -> primary/root path.
    assert deploy.publish_calls == [("/repo/root", "2.0.0")]


def test_target_without_path_entry_falls_back_to_primary(tmp_path, monkeypatch):
    # A pipeline whose target has no target_paths entry (e.g. a root target)
    # publishes from the primary path.
    go = _FakePipeline("go", "go")
    _patch_pipelines(monkeypatch, {"go": go})

    state_path = str(tmp_path / "state.json")
    _publish_standalone_pipelines(
        SimpleNamespace(config={}), {}, "/repo", "3.1.0",
        state_path, lambda _m: None,
    )
    assert go.publish_calls == [("/repo", "3.1.0")]


def test_published_targets_recorded_and_resumed(tmp_path, monkeypatch):
    pypi = _FakePipeline("pypi", "pypi")
    npm = _FakePipeline("npm", "npm")
    _patch_pipelines(monkeypatch, {"pypi": pypi, "npm": npm})

    state_path = str(tmp_path / "state.json")
    # Pre-seed state: pypi already published.
    with open(state_path, "w") as f:
        json.dump({"published_targets": ["pypi"]}, f)

    _publish_standalone_pipelines(
        SimpleNamespace(config={}), {"pypi": "/repo/py", "npm": "/repo/js"},
        "/repo", "1.0.0", state_path, lambda _m: None,
    )

    # pypi is skipped (already published); npm publishes from its own path.
    assert pypi.publish_calls == []
    assert npm.publish_calls == [("/repo/js", "1.0.0")]

    with open(state_path) as f:
        final = json.load(f)
    assert set(final["published_targets"]) == {"pypi", "npm"}


def test_failure_preserves_partial_state_and_raises(tmp_path, monkeypatch):
    from rlsbl.errors import PostReleaseError

    ok = _FakePipeline("pypi", "pypi")
    bad = _FakePipeline("npm", "npm", fail=True)
    _patch_pipelines(monkeypatch, {"pypi": ok, "npm": bad})

    state_path = str(tmp_path / "state.json")
    with pytest.raises(PostReleaseError):
        _publish_standalone_pipelines(
            SimpleNamespace(config={}),
            {"pypi": "/repo/py", "npm": "/repo/js"},
            "/repo", "1.0.0", state_path, lambda _m: None,
        )

    # The successful pipeline's progress is persisted for resume.
    with open(state_path) as f:
        state = json.load(f)
    assert state["published_targets"] == ["pypi"]
