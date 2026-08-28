"""Tests for the ``go-module-identity`` check.

The failure class: a repository is renamed, changes owner, or is absorbed into
a monorepo under a subdirectory, and its ``go.mod`` keeps declaring the old
module path. A Go module path IS the URL the toolchain fetches from, so the
published module stops being fetchable. ``rlsbl rewrite go-module-path`` is the
remedy, and every finding here names it.
"""

import json

import pytest

from rlsbl import app
from rlsbl.go_identity import (
    evaluate_go_module_identity,
    expected_module_path,
    parse_remote,
    strip_major_suffix,
)

from conftest import make_ctx
from githarness import git, init_repo


# ---------------------------------------------------------------------------
# Remote parsing: two spellings, one identity
# ---------------------------------------------------------------------------


class TestParseRemote:
    @pytest.mark.parametrize("url", [
        "git@github.com:owner/repo.git",
        "git@github.com:owner/repo",
        "https://github.com/owner/repo.git",
        "https://github.com/owner/repo",
        "ssh://git@github.com/owner/repo.git",
        "ssh://git@github.com:22/owner/repo.git",
    ])
    def test_every_spelling_yields_the_same_identity(self, url):
        identity = parse_remote(url)
        assert (identity.host, identity.path) == ("github.com", "owner/repo")
        assert identity.host_is_a_domain

    def test_an_ssh_alias_host_is_not_a_domain(self):
        identity = parse_remote("git@gp:owner/repo.git")
        assert identity.path == "owner/repo"
        assert not identity.host_is_a_domain

    def test_a_nested_group_path_is_kept_whole(self):
        identity = parse_remote("https://gitlab.com/group/sub/repo.git")
        assert (identity.host, identity.path) == ("gitlab.com", "group/sub/repo")

    @pytest.mark.parametrize("url", ["", None, "not a url"])
    def test_unparseable_remotes_yield_none(self, url):
        assert parse_remote(url) is None


class TestExpectedPath:
    def test_the_root_module_is_the_repo_itself(self):
        identity = parse_remote("git@github.com:owner/repo.git")
        assert expected_module_path(identity, ".") == (
            "github.com/owner/repo", "owner/repo",
        )

    def test_a_subdirectory_is_appended(self):
        identity = parse_remote("git@github.com:owner/repo.git")
        full, tail = expected_module_path(identity, "services/api")
        assert full == "github.com/owner/repo/services/api"
        assert tail == "owner/repo/services/api"


@pytest.mark.parametrize("path,expected", [
    ("github.com/o/r/v2", "github.com/o/r"),
    ("github.com/o/r/v12", "github.com/o/r"),
    ("github.com/o/r", "github.com/o/r"),
    ("github.com/o/v1beta", "github.com/o/v1beta"),
])
def test_strip_major_suffix(path, expected):
    assert strip_major_suffix(path) == expected


# ---------------------------------------------------------------------------
# The evaluation
# ---------------------------------------------------------------------------


def _go_mod(directory, module):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "go.mod").write_text(f"module {module}\n\ngo 1.23\n")
    return directory


class TestEvaluation:
    def test_a_matching_module_passes(self, tmp_path):
        _go_mod(tmp_path, "github.com/owner/repo")
        verdict = evaluate_go_module_identity(
            tmp_path, [str(tmp_path)], "git@github.com:owner/repo.git",
        )
        assert verdict.ok

    def test_a_moved_module_is_an_error_naming_the_rewrite(self, tmp_path):
        """The class `rlsbl rewrite go-module-path` exists to fix."""
        _go_mod(tmp_path, "github.com/olduser/oldname")
        verdict = evaluate_go_module_identity(
            tmp_path, [str(tmp_path)], "git@github.com:owner/repo.git",
        )
        assert not verdict.ok
        problem = verdict.problems[0]
        assert "github.com/olduser/oldname" in problem
        assert "github.com/owner/repo" in problem
        assert (
            "rlsbl rewrite go-module-path --from-module "
            "github.com/olduser/oldname --to-module github.com/owner/repo"
        ) in problem

    def test_a_member_subdirectory_is_part_of_the_expected_path(self, tmp_path):
        member = _go_mod(tmp_path / "services" / "api", "github.com/owner/repo")
        verdict = evaluate_go_module_identity(
            tmp_path, [str(member)], "https://github.com/owner/repo.git",
        )
        assert not verdict.ok
        assert "github.com/owner/repo/services/api" in verdict.problems[0]

    def test_a_matching_member_subdirectory_passes(self, tmp_path):
        member = _go_mod(
            tmp_path / "services" / "api", "github.com/owner/repo/services/api",
        )
        verdict = evaluate_go_module_identity(
            tmp_path, [str(member)], "https://github.com/owner/repo.git",
        )
        assert verdict.ok

    def test_a_major_version_suffix_is_not_a_mismatch(self, tmp_path):
        _go_mod(tmp_path, "github.com/owner/repo/v3")
        verdict = evaluate_go_module_identity(
            tmp_path, [str(tmp_path)], "git@github.com:owner/repo.git",
        )
        assert verdict.ok

    def test_the_major_version_subdirectory_layout_passes(self, tmp_path):
        """Go's own layout for a v2+ module: a ``v2/`` directory in the repo.

        The subdirectory is part of the module path AND the major suffix at the
        same time, so ``github.com/owner/repo/v2`` is exactly right there --
        expecting ``.../v2/v2`` and printing that as the rewrite target was the
        check misreading a standard layout.
        """
        member = _go_mod(tmp_path / "v2", "github.com/owner/repo/v2")
        verdict = evaluate_go_module_identity(
            tmp_path, [str(member)], "https://github.com/owner/repo.git",
        )
        assert verdict.ok, verdict.problems

    def test_a_moved_module_in_a_major_subdirectory_still_errors(self, tmp_path):
        member = _go_mod(tmp_path / "v2", "github.com/olduser/oldname/v2")
        verdict = evaluate_go_module_identity(
            tmp_path, [str(member)], "https://github.com/owner/repo.git",
        )
        assert not verdict.ok
        assert "--to-module github.com/owner/repo/v2" in verdict.problems[0]
        assert "/v2/v2" not in verdict.problems[0]

    def test_a_major_suffix_is_preserved_in_the_remedy(self, tmp_path):
        _go_mod(tmp_path, "github.com/olduser/oldname/v2")
        verdict = evaluate_go_module_identity(
            tmp_path, [str(tmp_path)], "git@github.com:owner/repo.git",
        )
        assert "--to-module github.com/owner/repo/v2" in verdict.problems[0]

    def test_no_origin_remote_skips_instead_of_guessing(self, tmp_path):
        _go_mod(tmp_path, "github.com/anything/at-all")
        verdict = evaluate_go_module_identity(tmp_path, [str(tmp_path)], None)
        assert verdict.skip_reason is not None
        assert "origin" in verdict.skip_reason

    def test_an_unparseable_remote_skips(self, tmp_path):
        _go_mod(tmp_path, "github.com/owner/repo")
        verdict = evaluate_go_module_identity(
            tmp_path, [str(tmp_path)], "not a url",
        )
        assert verdict.skip_reason is not None

    def test_a_go_mod_without_a_module_line_errors(self, tmp_path):
        (tmp_path / "go.mod").write_text("go 1.23\n")
        verdict = evaluate_go_module_identity(
            tmp_path, [str(tmp_path)], "git@github.com:owner/repo.git",
        )
        assert not verdict.ok
        assert "no module path" in verdict.problems[0]

    def test_several_modules_are_all_reported(self, tmp_path):
        a = _go_mod(tmp_path / "a", "github.com/owner/wrong-a")
        b = _go_mod(tmp_path / "b", "github.com/owner/wrong-b")
        verdict = evaluate_go_module_identity(
            tmp_path, [str(a), str(b)], "git@github.com:owner/repo.git",
        )
        assert len(verdict.problems) == 2


class TestSshAliasRemotes:
    """An alias names no host, so the host segment is not policed."""

    def test_the_tail_is_still_compared(self, tmp_path):
        _go_mod(tmp_path, "github.com/olduser/oldname")
        verdict = evaluate_go_module_identity(
            tmp_path, [str(tmp_path)], "git@gp:owner/repo.git",
        )
        assert not verdict.ok
        assert "--to-module github.com/owner/repo" in verdict.problems[0]

    def test_any_host_is_accepted_when_the_tail_matches(self, tmp_path):
        _go_mod(tmp_path, "example.invalid/owner/repo")
        verdict = evaluate_go_module_identity(
            tmp_path, [str(tmp_path)], "git@gp:owner/repo.git",
        )
        assert verdict.ok

    def test_the_pass_message_says_the_host_was_not_verified(self, tmp_path):
        _go_mod(tmp_path, "github.com/owner/repo")
        verdict = evaluate_go_module_identity(
            tmp_path, [str(tmp_path)], "git@gp:owner/repo.git",
        )
        assert verdict.ok
        assert "NOT verified" in verdict.notes[0]


# ---------------------------------------------------------------------------
# The registered check, against a real repository
# ---------------------------------------------------------------------------


def _go_project(root, module, *, remote="git@github.com:owner/repo.git"):
    init_repo(root)
    (root / ".rlsbl").mkdir(parents=True, exist_ok=True)
    (root / ".rlsbl" / "config.json").write_text(
        json.dumps({"publish_mode": "ci", "targets": ["go"]})
    )
    (root / "go.mod").write_text(f"module {module}\n\ngo 1.23\n")
    (root / "VERSION").write_text("0.1.0\n")
    if remote is not None:
        git(root, "remote", "add", "origin", remote)
    return root


def _run(root):
    ctx = make_ctx(root)
    return app._check_defs["go-module-identity"].impl(ctx)


class TestRegisteredCheck:
    def test_a_matching_repository_passes(self, tmp_path):
        root = _go_project(tmp_path / "repo", "github.com/owner/repo")
        result = _run(root)
        assert result.status == "pass"

    def test_a_moved_repository_fails(self, tmp_path):
        root = _go_project(tmp_path / "repo", "github.com/owner/previous-name")
        result = _run(root)
        assert result.status == "fail"
        assert "rlsbl rewrite go-module-path" in " ".join(
            p.text for p in result.problems
        )

    def test_a_repository_without_origin_skips(self, tmp_path):
        root = _go_project(tmp_path / "repo", "github.com/owner/repo", remote=None)
        result = _run(root)
        assert result.status == "skip"

    def test_a_project_with_no_go_target_skips(self, tmp_path):
        root = tmp_path / "repo"
        root.mkdir()
        (root / ".rlsbl").mkdir()
        (root / ".rlsbl" / "config.json").write_text(
            json.dumps({"publish_mode": "ci", "targets": ["pypi"]})
        )
        (root / "pyproject.toml").write_text(
            '[project]\nname = "p"\nversion = "0.1.0"\n'
        )
        result = _run(root)
        assert result.status == "skip"
