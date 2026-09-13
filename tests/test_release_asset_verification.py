"""A binary pipeline's release must carry the binaries it claims to ship.

The registry probe answers "is this version being served"; for a Go module
that is true the moment the tag exists, whether or not goreleaser produced a
single archive. So a publish workflow that died after the tag, the GitHub
Release and the registries were already written ended as a *green* release
whose Release page carried zero assets -- and every launcher shim installing
that version had nothing to download.

These are the red fixtures: the GitHub Release is missing archives, or
checksums.txt, or cannot be read at all, and the run must exit nonzero naming
what is absent and how to re-dispatch the publish.
"""

from __future__ import annotations

import json
import types

import pytest

from rlsbl.commands.release import execute
from rlsbl.commands.release.execute import (
    ReleaseAssetProbeError,
    _binary_artifact_targets,
    _expected_release_assets,
    _missing_release_assets,
    _probe_release_assets,
    _verify_publication,
    _verify_publication_members,
)
from rlsbl.resolved_target import ResolvedTarget
from rlsbl.targets import TargetEntry

from test_publication_verification import (  # noqa: E402
    PUBLISHED,
    _FakeRegistry,
    _collapse_delays,
    _register,
)


# The platform matrix rlsbl's own goreleaser template declares.
PLATFORMS = [
    ("darwin", "amd64"), ("darwin", "arm64"),
    ("linux", "amd64"), ("linux", "arm64"),
    ("windows", "amd64"), ("windows", "arm64"),
]

GORELEASER_YML = """\
version: 2

builds:
  - main: ./cmd/alpha
    goos:
      - linux
      - darwin
      - windows
    goarch:
      - amd64
      - arm64

archives:
  - format: tar.gz
    name_template: "{{ .ProjectName }}_{{ .Version }}_{{ .Os }}_{{ .Arch }}"
    format_overrides:
      - goos: windows
        format: zip

checksum:
  name_template: checksums.txt
"""


def _assets(version="1.2.3", *, skip=(), checksums=True):
    """The asset names a healthy goreleaser run uploads."""
    names = []
    for goos, goarch in PLATFORMS:
        if (goos, goarch) in skip:
            continue
        ext = "zip" if goos == "windows" else "tar.gz"
        names.append(f"alpha_{version}_{goos}_{goarch}.{ext}")
    if checksums:
        names.append("checksums.txt")
    return names


def _target(path, *, artifact_kind="binary", name="go", publish_mode="ci"):
    return ResolvedTarget(
        target=TargetEntry(name=name, path=str(path)),
        path=str(path),
        pipeline=None,
        publish_mode=publish_mode,
        artifact_kind=artifact_kind,
        primary=True,
    )


def _ctx():
    return types.SimpleNamespace(config={})


def _gh_returning(*payloads):
    """A fake ``run_gh`` serving one asset list per call, last one repeating."""
    calls = []
    remaining = list(payloads)

    def fake_gh(args, config=None, **kwargs):
        calls.append(list(args))
        names = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        if isinstance(names, Exception):
            raise names
        return json.dumps({"assets": [{"name": n} for n in names]})

    fake_gh.calls = calls
    return fake_gh


def _no_asset_delays(monkeypatch, attempts=1):
    monkeypatch.setattr(
        execute, "_RELEASE_ASSET_PROBE_DELAYS", tuple([0] * attempts),
    )


@pytest.fixture
def go_project(tmp_path):
    (tmp_path / ".goreleaser.yml").write_text(GORELEASER_YML)
    return tmp_path


# ---------------------------------------------------------------------------
# Which targets are asked at all
# ---------------------------------------------------------------------------


class TestBinaryArtifactTargets:
    def test_a_binary_pipeline_is_selected(self, tmp_path):
        assert _binary_artifact_targets([_target(tmp_path)])

    def test_a_library_pipeline_is_not(self, tmp_path):
        assert not _binary_artifact_targets(
            [_target(tmp_path, artifact_kind="library")]
        )

    def test_an_unpublished_target_is_not(self, tmp_path):
        assert not _binary_artifact_targets(
            [_target(tmp_path, publish_mode="none")]
        )

    def test_nothing_binary_asks_gh_nothing(self, tmp_path, monkeypatch):
        _no_asset_delays(monkeypatch)
        fake = _gh_returning([])
        monkeypatch.setattr("rlsbl.commands.release.run_gh", fake)
        missing, expectation = _probe_release_assets(
            [_target(tmp_path, artifact_kind="library")],
            "v1.2.3", _ctx(), log=lambda _line: None,
        )
        assert (missing, expectation) == ([], None)
        assert fake.calls == []


# ---------------------------------------------------------------------------
# What is expected of the Release
# ---------------------------------------------------------------------------


class TestExpectedAssets:
    def test_the_declared_matrix_becomes_the_expectation(self, go_project):
        platforms, expectation = _expected_release_assets(str(go_project))
        assert platforms == PLATFORMS
        assert "linux/arm64" in expectation
        assert "checksums.txt" in expectation

    def test_no_config_falls_back_to_the_floor(self, tmp_path):
        platforms, expectation = _expected_release_assets(str(tmp_path))
        assert platforms == []
        assert "no readable goreleaser config" in expectation

    def test_a_custom_archive_name_template_drops_to_the_floor(self, tmp_path):
        (tmp_path / ".goreleaser.yml").write_text(
            "builds:\n  - goos: [linux]\n    goarch: [amd64]\n"
            "archives:\n  - name_template: \"{{ .ProjectName }}-{{ .Version }}\"\n"
        )
        platforms, expectation = _expected_release_assets(str(tmp_path))
        assert platforms == []
        assert "custom archive name template" in expectation

    def test_an_ignored_combination_is_not_expected(self, tmp_path):
        (tmp_path / ".goreleaser.yml").write_text(
            "builds:\n  - goos: [linux, windows]\n    goarch: [amd64, arm64]\n"
            "    ignore:\n      - goos: windows\n        goarch: arm64\n"
        )
        platforms, _ = _expected_release_assets(str(tmp_path))
        assert ("windows", "arm64") not in platforms
        assert ("windows", "amd64") in platforms

    def test_a_build_declaring_no_matrix_expects_nothing_specific(self, tmp_path):
        """goreleaser's own defaults would be invented, not declared."""
        (tmp_path / ".goreleaser.yml").write_text("builds:\n  - main: ./cmd/x\n")
        platforms, expectation = _expected_release_assets(str(tmp_path))
        assert platforms == []
        assert "no declared build matrix" in expectation


# ---------------------------------------------------------------------------
# What the Release is missing
# ---------------------------------------------------------------------------


class TestMissingAssets:
    def test_a_complete_release_is_missing_nothing(self):
        assert _missing_release_assets(_assets(), PLATFORMS) == []

    def test_an_empty_release_names_the_platforms_and_checksums(self):
        missing = _missing_release_assets([], PLATFORMS)
        assert "checksums.txt" in missing
        for goos, goarch in PLATFORMS:
            assert f"an archive for {goos}/{goarch}" in missing

    def test_one_absent_platform_is_named_alone(self):
        missing = _missing_release_assets(
            _assets(skip=[("darwin", "arm64")]), PLATFORMS,
        )
        assert missing == ["an archive for darwin/arm64"]

    def test_absent_checksums_alone_is_a_miss(self):
        missing = _missing_release_assets(_assets(checksums=False), PLATFORMS)
        assert missing == ["checksums.txt"]

    def test_the_floor_wants_an_archive_at_all(self):
        assert _missing_release_assets(["README.md"], []) == [
            "at least one binary archive (.tar.gz/.tgz/.zip)",
            "checksums.txt",
        ]

    def test_the_floor_is_satisfied_by_one_archive(self):
        assert _missing_release_assets(
            ["alpha_1.2.3_linux_amd64.tar.gz", "checksums.txt"], [],
        ) == []


# ---------------------------------------------------------------------------
# The probe, against a fake gh
# ---------------------------------------------------------------------------


class TestProbeReleaseAssets:
    def test_a_complete_release_passes(self, go_project, monkeypatch):
        _no_asset_delays(monkeypatch)
        monkeypatch.setattr(
            "rlsbl.commands.release.run_gh", _gh_returning(_assets()),
        )
        missing, expectation = _probe_release_assets(
            [_target(go_project)], "v1.2.3", _ctx(), log=lambda _line: None,
        )
        assert missing == []
        assert "linux/amd64" in expectation

    def test_an_empty_release_is_reported(self, go_project, monkeypatch):
        _no_asset_delays(monkeypatch)
        monkeypatch.setattr(
            "rlsbl.commands.release.run_gh", _gh_returning([]),
        )
        missing, _ = _probe_release_assets(
            [_target(go_project)], "v1.2.3", _ctx(), log=lambda _line: None,
        )
        assert "checksums.txt" in missing
        assert any("linux/amd64" in m for m in missing)

    def test_a_slow_publish_workflow_is_waited_out(self, go_project, monkeypatch):
        """The publish workflow starts when the Release is created, so the
        first look can legitimately find nothing."""
        _no_asset_delays(monkeypatch, attempts=3)
        fake = _gh_returning([], [], _assets())
        monkeypatch.setattr("rlsbl.commands.release.run_gh", fake)
        missing, _ = _probe_release_assets(
            [_target(go_project)], "v1.2.3", _ctx(), log=lambda _line: None,
        )
        assert missing == []
        assert len(fake.calls) == 3

    def test_the_probe_asks_for_this_tag(self, go_project, monkeypatch):
        _no_asset_delays(monkeypatch)
        fake = _gh_returning(_assets())
        monkeypatch.setattr("rlsbl.commands.release.run_gh", fake)
        _probe_release_assets(
            [_target(go_project)], "alpha@v1.2.3", _ctx(), log=lambda _line: None,
        )
        assert fake.calls == [
            ["release", "view", "alpha@v1.2.3", "--json", "assets"]
        ]

    def test_an_unreadable_asset_list_raises(self, go_project, monkeypatch):
        _no_asset_delays(monkeypatch)
        monkeypatch.setattr(
            "rlsbl.commands.release.run_gh",
            _gh_returning(RuntimeError("gh: HTTP 401")),
        )
        with pytest.raises(ReleaseAssetProbeError, match="401"):
            _probe_release_assets(
                [_target(go_project)], "v1.2.3", _ctx(), log=lambda _line: None,
            )

    def test_unparseable_json_raises(self, go_project, monkeypatch):
        _no_asset_delays(monkeypatch)
        monkeypatch.setattr(
            "rlsbl.commands.release.run_gh",
            lambda args, config=None, **kw: "not json",
        )
        with pytest.raises(ReleaseAssetProbeError, match="unparseable"):
            _probe_release_assets(
                [_target(go_project)], "v1.2.3", _ctx(), log=lambda _line: None,
            )


# ---------------------------------------------------------------------------
# The wired-in verdicts
# ---------------------------------------------------------------------------


class TestVerifyPublicationChecksAssets:
    def _registry(self, monkeypatch):
        return _register(monkeypatch, _FakeRegistry("go", [PUBLISHED]))

    def test_a_served_registry_with_an_empty_release_is_red(
            self, go_project, monkeypatch, capsys):
        _collapse_delays(monkeypatch)
        _no_asset_delays(monkeypatch)
        self._registry(monkeypatch)
        monkeypatch.setattr(
            "rlsbl.commands.release.run_gh", _gh_returning([]),
        )
        with pytest.raises(SystemExit) as exit_info:
            _verify_publication(
                [_target(go_project)], "1.2.3", "v1.2.3", _ctx(),
                log=lambda _line: None,
            )
        assert exit_info.value.code == 1
        err = capsys.readouterr().err
        assert "does not carry the binaries" in err
        assert "checksums.txt" in err
        assert "rlsbl release retry" in err

    def test_a_complete_release_passes(self, go_project, monkeypatch):
        _collapse_delays(monkeypatch)
        _no_asset_delays(monkeypatch)
        self._registry(monkeypatch)
        monkeypatch.setattr(
            "rlsbl.commands.release.run_gh", _gh_returning(_assets()),
        )
        _verify_publication(
            [_target(go_project)], "1.2.3", "v1.2.3", _ctx(),
            log=lambda _line: None,
        )

    def test_an_unreadable_asset_list_is_red(
            self, go_project, monkeypatch, capsys):
        _collapse_delays(monkeypatch)
        _no_asset_delays(monkeypatch)
        self._registry(monkeypatch)
        monkeypatch.setattr(
            "rlsbl.commands.release.run_gh",
            _gh_returning(RuntimeError("gh: HTTP 401")),
        )
        with pytest.raises(SystemExit):
            _verify_publication(
                [_target(go_project)], "1.2.3", "v1.2.3", _ctx(),
                log=lambda _line: None,
            )
        err = capsys.readouterr().err
        assert "could not be read" in err
        assert "gh auth status" in err

    def test_a_library_release_never_asks_gh(self, tmp_path, monkeypatch):
        _collapse_delays(monkeypatch)
        _no_asset_delays(monkeypatch)
        self._registry(monkeypatch)
        fake = _gh_returning([])
        monkeypatch.setattr("rlsbl.commands.release.run_gh", fake)
        _verify_publication(
            [_target(tmp_path, artifact_kind="library")], "1.2.3", "v1.2.3",
            _ctx(), log=lambda _line: None,
        )
        assert fake.calls == []


class TestVerifyPublicationMembersChecksAssets:
    def test_one_member_with_an_empty_release_reds_the_batch(
            self, tmp_path, monkeypatch, capsys):
        _collapse_delays(monkeypatch)
        _no_asset_delays(monkeypatch)
        _register(monkeypatch, _FakeRegistry("go", [PUBLISHED]))
        good = tmp_path / "good"
        bad = tmp_path / "bad"
        for d in (good, bad):
            d.mkdir()
            (d / ".goreleaser.yml").write_text(GORELEASER_YML)

        def fake_gh(args, config=None, **kwargs):
            tag = args[2]
            names = _assets() if tag == "good@v1.2.3" else []
            return json.dumps({"assets": [{"name": n} for n in names]})

        monkeypatch.setattr("rlsbl.commands.release.run_gh", fake_gh)
        specs = [
            ("good", [_target(good)], "1.2.3", "good@v1.2.3", _ctx()),
            ("bad", [_target(bad)], "1.2.3", "bad@v1.2.3", _ctx()),
        ]
        with pytest.raises(SystemExit) as exit_info:
            _verify_publication_members(specs, log=lambda _line: None)
        assert exit_info.value.code == 1
        err = capsys.readouterr().err
        assert "bad@v1.2.3" in err
        assert "good@v1.2.3" not in err
        assert "rlsbl release retry" in err

    def test_a_complete_batch_passes(self, tmp_path, monkeypatch):
        _collapse_delays(monkeypatch)
        _no_asset_delays(monkeypatch)
        _register(monkeypatch, _FakeRegistry("go", [PUBLISHED]))
        (tmp_path / ".goreleaser.yml").write_text(GORELEASER_YML)
        monkeypatch.setattr(
            "rlsbl.commands.release.run_gh", _gh_returning(_assets()),
        )
        _verify_publication_members(
            [("alpha", [_target(tmp_path)], "1.2.3", "alpha@v1.2.3", _ctx())],
            log=lambda _line: None,
        )
