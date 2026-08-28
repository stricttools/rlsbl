"""Tests for ``old-repo-archived`` and ``go-deprecation-published``.

Both read the committed lineage record and probe the outside world for a
follow-up the recorded surgery still owes:

* an absorb leaves its source repository standing, collecting issues and pull
  requests for code that moved here;
* a go-module-path transition leaves the OLD module path published with no
  sign that it moved.

Both are fail-closed: an unanswered probe is an error, never a pass. Every
probe here is stubbed -- no test contacts GitHub or the module proxy.
"""

import json
from types import SimpleNamespace

import pytest
import strictcli

from rlsbl import app, effects
from rlsbl import registry
from rlsbl.lineage import (
    ConversionEvent,
    IdentityTransitionEvent,
    LineageEndpoint,
    append_events,
    get_lineage_path,
)
from rlsbl.lineage_followup import (
    absorbed_sources,
    evaluate_go_deprecation_published,
    evaluate_old_repo_archived,
    github_slug,
    go_module_transitions,
    probe_repo_archived,
)

from conftest import make_ctx


def _gh(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _absorb(repo="https://github.com/owner/absorbed.git"):
    return ConversionEvent(
        direction="absorb",
        source=LineageEndpoint(repo=repo, project="absorbed"),
        destination=LineageEndpoint(
            repo=".", path="packages/absorbed", project="absorbed",
        ),
        commit="a" * 40,
    )


def _extract(repo="https://github.com/owner/extracted.git"):
    return ConversionEvent(
        direction="extract",
        source=LineageEndpoint(repo=".", path="packages/x", project="x"),
        destination=LineageEndpoint(repo=repo, project="x"),
        commit="b" * 40,
    )


def _transition(old="github.com/owner/old", new="github.com/owner/new"):
    return IdentityTransitionEvent(
        facet="go-module-path", old=old, new=new, effective_version="0.4.0",
    )


def _write_lineage(root, events):
    (root / ".rlsbl").mkdir(parents=True, exist_ok=True)
    path = get_lineage_path(str(root))
    append_events(path, events)
    return path


def _run(name, root):
    ctx = make_ctx(root, config={"publish_mode": "ci"})
    return app._check_defs[name].impl(ctx)


def _text(result):
    return " ".join(p.text for p in result.problems)


# ---------------------------------------------------------------------------
# Reading the record
# ---------------------------------------------------------------------------


class TestReadingTheRecord:
    def test_only_absorbs_contribute_a_source(self):
        sources = absorbed_sources([_absorb(), _extract()])
        assert [slug for slug, _stated in sources] == ["owner/absorbed"]

    def test_an_extract_source_is_this_repository(self):
        assert absorbed_sources([_extract()]) == []

    @pytest.mark.parametrize("repo,expected", [
        ("https://github.com/owner/repo.git", "owner/repo"),
        ("git@github.com:owner/repo.git", "owner/repo"),
        ("https://gitlab.com/owner/repo.git", None),
        ("git@gp:owner/repo.git", None),
        ("../sibling-checkout", None),
        (".", None),
    ])
    def test_github_slug(self, repo, expected):
        assert github_slug(repo) == expected

    def test_only_go_module_path_transitions_are_read(self):
        events = [
            _transition(),
            IdentityTransitionEvent(
                facet="package-name", old="a", new="b", effective_version="1.0.0",
            ),
        ]
        assert go_module_transitions(events) == [
            ("github.com/owner/old", "github.com/owner/new", "0.4.0"),
        ]


# ---------------------------------------------------------------------------
# old-repo-archived
# ---------------------------------------------------------------------------


class TestRepoArchivedProbe:
    def test_true_is_archived(self, monkeypatch):
        monkeypatch.setattr(effects, "gh", lambda *a, **k: _gh(0, "true\n"))
        assert probe_repo_archived("owner/repo")["status"] == "archived"

    def test_false_is_active(self, monkeypatch):
        monkeypatch.setattr(effects, "gh", lambda *a, **k: _gh(0, "false\n"))
        assert probe_repo_archived("owner/repo")["status"] == "active"

    def test_a_404_is_unknown_not_archived(self, monkeypatch):
        """Deleted and private-without-access are indistinguishable here."""
        monkeypatch.setattr(
            effects, "gh", lambda *a, **k: _gh(1, "", "gh: Not Found (HTTP 404)"),
        )
        assert probe_repo_archived("owner/repo")["status"] == "unknown"

    def test_a_recorded_call_is_unknown(self, monkeypatch):
        monkeypatch.setattr(
            effects, "gh",
            lambda *a, **k: strictcli.Unsettled("«stale»", None, "probe", True),
        )
        assert probe_repo_archived("owner/repo")["status"] == "unknown"

    def test_the_argv_is_the_get_pinned_read(self, monkeypatch):
        seen = {}

        def fake(args, **kwargs):
            seen["args"] = list(args)
            return _gh(0, "true")

        monkeypatch.setattr(effects, "gh", fake)
        probe_repo_archived("owner/repo")
        assert seen["args"][:4] == [
            "api", "--method", "GET", "repos/owner/repo",
        ]


class TestOldRepoArchived:
    def test_an_archived_source_passes(self):
        verdict = evaluate_old_repo_archived(
            [_absorb()], probe=lambda slug: {"status": "archived"},
        )
        assert verdict.ok

    def test_an_active_source_names_the_archive_command(self):
        verdict = evaluate_old_repo_archived(
            [_absorb()], probe=lambda slug: {"status": "active"},
        )
        assert not verdict.ok
        assert "gh repo archive owner/absorbed" in verdict.problems[0]

    def test_an_unanswered_probe_is_an_error(self):
        verdict = evaluate_old_repo_archived(
            [_absorb()],
            probe=lambda slug: {"status": "unknown", "message": "no credential"},
        )
        assert not verdict.ok
        assert "no credential" in verdict.problems[0]

    def test_a_record_with_no_absorb_skips(self):
        verdict = evaluate_old_repo_archived([_extract()])
        assert verdict.skip_reason is not None

    def test_a_non_github_source_is_a_note_not_a_probe(self):
        def refuse(slug):
            raise AssertionError("a non-GitHub source must not be probed")

        verdict = evaluate_old_repo_archived(
            [_absorb("../sibling-checkout")], probe=refuse,
        )
        assert verdict.ok
        assert "not a github.com repository" in verdict.notes[0]

    def test_one_repository_is_probed_once(self):
        calls = []

        def probe(slug):
            calls.append(slug)
            return {"status": "archived"}

        evaluate_old_repo_archived([_absorb(), _absorb()], probe=probe)
        assert calls == ["owner/absorbed"]


class TestOldRepoArchivedCheck:
    def test_no_lineage_record_skips(self, tmp_path):
        assert _run("old-repo-archived", tmp_path).status == "skip"

    def test_an_active_source_fails(self, tmp_path, monkeypatch):
        _write_lineage(tmp_path, [_absorb()])
        monkeypatch.setattr(effects, "gh", lambda *a, **k: _gh(0, "false"))
        result = _run("old-repo-archived", tmp_path)
        assert result.status == "fail"
        assert "gh repo archive" in _text(result)

    def test_an_archived_source_passes(self, tmp_path, monkeypatch):
        _write_lineage(tmp_path, [_absorb()])
        monkeypatch.setattr(effects, "gh", lambda *a, **k: _gh(0, "true"))
        assert _run("old-repo-archived", tmp_path).status == "pass"

    def test_a_malformed_record_is_reported_not_raised(self, tmp_path):
        (tmp_path / ".rlsbl").mkdir(parents=True)
        get_lineage_path(str(tmp_path))
        (tmp_path / ".rlsbl" / "lineage.jsonl").write_text("{not json\n")
        result = _run("old-repo-archived", tmp_path)
        assert result.status == "fail"
        assert "could not be read" in _text(result)


# ---------------------------------------------------------------------------
# go-deprecation-published
# ---------------------------------------------------------------------------


class TestGoModDeprecationReading:
    def test_a_notice_above_the_module_directive(self):
        text = (
            "// Deprecated: moved to github.com/owner/new\n"
            "module github.com/owner/old\n\ngo 1.23\n"
        )
        assert registry.go_mod_deprecation(text) == "moved to github.com/owner/new"

    def test_a_notice_on_the_module_line(self):
        text = "module github.com/o/old // Deprecated: gone\n"
        assert registry.go_mod_deprecation(text) == "gone"

    def test_an_unrelated_comment_is_not_a_notice(self):
        text = "// A module.\nmodule github.com/o/old\n"
        assert registry.go_mod_deprecation(text) is None

    def test_a_deprecated_retract_reason_is_not_the_module_notice(self):
        text = (
            "module github.com/o/old\n\ngo 1.23\n\n"
            "// Deprecated: bad release\nretract v0.1.0\n"
        )
        assert registry.go_mod_deprecation(text) is None

    def test_a_blank_line_breaks_the_comment_block(self):
        text = "// Deprecated: stale note\n\nmodule github.com/o/old\n"
        assert registry.go_mod_deprecation(text) is None

    @pytest.mark.parametrize("path,expected", [
        ("github.com/owner/repo", "github.com/owner/repo"),
        ("github.com/Owner/Repo", "github.com/!owner/!repo"),
    ])
    def test_escape_module_path(self, path, expected):
        assert registry.escape_module_path(path) == expected


class TestProxyProbe:
    def test_a_deprecated_module_is_reported(self, monkeypatch):
        monkeypatch.setattr(
            registry, "query_go_version",
            lambda path: {"status": "found", "version": "0.4.0"},
        )
        monkeypatch.setattr(
            registry, "query_go_mod",
            lambda path, version: {
                "status": "found",
                "text": "// Deprecated: moved\nmodule github.com/o/old\n",
            },
        )
        result = registry.query_go_module_deprecation("github.com/o/old")
        assert result == {
            "status": "deprecated", "version": "v0.4.0", "message": "moved",
        }

    def test_an_undeprecated_module_is_reported(self, monkeypatch):
        monkeypatch.setattr(
            registry, "query_go_version",
            lambda path: {"status": "found", "version": "0.4.0"},
        )
        monkeypatch.setattr(
            registry, "query_go_mod",
            lambda path, version: {
                "status": "found", "text": "module github.com/o/old\n",
            },
        )
        result = registry.query_go_module_deprecation("github.com/o/old")
        assert result["status"] == "not_deprecated"

    def test_an_unpublished_module_is_not_found(self, monkeypatch):
        monkeypatch.setattr(
            registry, "query_go_version", lambda path: {"status": "not_found"},
        )
        assert registry.query_go_module_deprecation("x")["status"] == "not_found"

    def test_a_proxy_error_propagates_as_an_error(self, monkeypatch):
        monkeypatch.setattr(
            registry, "query_go_version",
            lambda path: {"status": "error", "message": "HTTP 500"},
        )
        assert registry.query_go_module_deprecation("x")["status"] == "error"


class TestGoDeprecationPublished:
    def test_a_deprecated_old_path_passes(self):
        verdict = evaluate_go_deprecation_published(
            [_transition()],
            probe=lambda old: {
                "status": "deprecated", "version": "v0.3.0", "message": "moved",
            },
        )
        assert verdict.ok

    def test_an_undeprecated_old_path_names_the_steps(self):
        verdict = evaluate_go_deprecation_published(
            [_transition()],
            probe=lambda old: {"status": "not_deprecated", "version": "v0.3.0"},
        )
        assert not verdict.ok
        problem = verdict.problems[0]
        assert "// Deprecated: moved to github.com/owner/new" in problem
        assert "retract" in problem

    def test_a_never_published_old_path_is_a_note(self):
        verdict = evaluate_go_deprecation_published(
            [_transition()], probe=lambda old: {"status": "not_found"},
        )
        assert verdict.ok
        assert "never served" in verdict.notes[0]

    def test_an_unanswered_probe_is_an_error(self):
        verdict = evaluate_go_deprecation_published(
            [_transition()],
            probe=lambda old: {"status": "error", "message": "HTTP 500"},
        )
        assert not verdict.ok
        assert "HTTP 500" in verdict.problems[0]

    def test_a_record_with_no_transition_skips(self):
        verdict = evaluate_go_deprecation_published([_absorb()])
        assert verdict.skip_reason is not None


class TestGoDeprecationPublishedCheck:
    def test_no_lineage_record_skips(self, tmp_path):
        assert _run("go-deprecation-published", tmp_path).status == "skip"

    def test_an_undeprecated_path_fails(self, tmp_path, monkeypatch):
        _write_lineage(tmp_path, [_transition()])
        monkeypatch.setattr(
            registry, "query_go_module_deprecation",
            lambda old: {"status": "not_deprecated", "version": "v0.3.0"},
        )
        result = _run("go-deprecation-published", tmp_path)
        assert result.status == "fail"
        assert "Deprecated" in _text(result)

    def test_a_deprecated_path_passes(self, tmp_path, monkeypatch):
        _write_lineage(tmp_path, [_transition()])
        monkeypatch.setattr(
            registry, "query_go_module_deprecation",
            lambda old: {"status": "deprecated", "version": "v0.3.0",
                         "message": "moved"},
        )
        assert _run("go-deprecation-published", tmp_path).status == "pass"

    def test_the_record_is_read_from_the_project_home(self, tmp_path):
        """The standalone home, `<project>/.rlsbl/lineage.jsonl`."""
        path = _write_lineage(tmp_path, [_transition()])
        assert path == str(tmp_path / ".rlsbl" / "lineage.jsonl")
        with open(path, encoding="utf-8") as f:
            assert json.loads(f.readline())["facet"] == "go-module-path"
