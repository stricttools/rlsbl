"""A required field that is present but EMPTY is missing, not settled.

Two fleet workspaces carried archives written from the scaffolded release file
without ever being filled in: recorded (``candidate_sha`` and ``tree_hashes``
both present) yet carrying ``bump = ""`` and ``description = ""``. The strict
reader refuses them, so ``unpublished-refs`` calls their ref sets underivable
-- and the backfill did NOT repair them, because its completion pass asked only
whether a key was PRESENT.

Empty means nothing, here as everywhere else in the fleet: an empty required
string is a field that was never answered, so the completion pass answers it --
the description through the recovery chain, the bump through version
arithmetic, each naming its source -- and replaces the empty value rather than
leaving it standing beside a note about itself.
"""

import io
import os
import tomllib

import pytest

from githarness import commit_file, git, init_repo

from rlsbl import release_backfill as backfill


VERSION = "0.1.0"

# An archive that a release recorded and nobody ever filled in: the fate is
# settled, the gate is stamped, and the two fields an operator was supposed to
# state are empty strings.
EMPTY_FIELDS_ARCHIVE = """\
# strictspec document version gate (do not remove)
format_version = 1
# Version bump type: patch, minor, major, infra, or prerelease
bump = ""
# Short description of this release (required)
description = ""
context = ""
include = ["pypi"]
exclude = []
candidate_sha = "{sha}"

[tree_hashes]
"." = "{tree}"
"""


def _read(path):
    with open(path, "rb") as f:
        return tomllib.load(f)


def _run(repo, *, dry_run=False):
    out = io.StringIO()
    code = backfill.run(
        str(repo), dry_run=dry_run, use_gh=False, gh=None,
        auto_commit=False, out=out, overrides=None,
    )
    return code, out.getvalue()


@pytest.fixture
def repo(tmp_path):
    """A released 0.1.0 whose archive carries empty ``bump`` and ``description``."""
    repo = tmp_path / "proj"
    repo.mkdir()
    init_repo(repo)
    (repo / ".rlsbl" / "releases").mkdir(parents=True)
    (repo / ".rlsbl" / "changes").mkdir(parents=True)
    commit_file(repo, "README.md", "hello\n", "initial")
    commit_file(repo, "feature.py", "x = 1\n", "add the widget everyone wanted")
    git(repo, "tag", f"v{VERSION}")

    changes = repo / ".rlsbl" / "changes"
    jsonl = changes / f"{VERSION}.jsonl"
    jsonl.write_text(
        '{"format_version":1,"commits":["%s"],"user_facing":false}\n'
        % git(repo, "rev-parse", "HEAD"),
        encoding="utf-8",
    )
    os.chmod(jsonl, 0o444)

    path = repo / ".rlsbl" / "releases" / f"v{VERSION}.toml"
    path.write_text(
        EMPTY_FIELDS_ARCHIVE.format(
            sha=git(repo, "rev-parse", "HEAD"),
            tree=git(repo, "rev-parse", "HEAD^{tree}"),
        ),
        encoding="utf-8",
    )
    os.chmod(path, 0o444)
    return repo


class TestThePassSeesTheEmptyFields:
    def test_the_dry_run_plans_the_completion(self, repo):
        code, output = _run(repo, dry_run=True)

        assert code == 0, output
        assert "0 archive(s) to write" not in output, (
            "an archive with two unanswered required fields is not settled; "
            f"the plan was:\n{output}"
        )
        assert "write bump" in output, output
        assert "write description" in output, output

    def test_the_dry_run_writes_nothing(self, repo):
        _run(repo, dry_run=True)
        data = _read(repo / ".rlsbl" / "releases" / f"v{VERSION}.toml")
        assert data["bump"] == ""
        assert data["description"] == ""


class TestTheApplyReplacesThem:
    def test_the_bump_comes_from_version_arithmetic(self, repo):
        code, output = _run(repo)

        assert code == 0, output
        data = _read(repo / ".rlsbl" / "releases" / f"v{VERSION}.toml")
        # 0.1.0 against no predecessor is measured from 0.0.0: a minor.
        assert data["bump"] == "minor"

    def test_the_description_comes_from_the_recovery_chain(self, repo):
        _run(repo)
        data = _read(repo / ".rlsbl" / "releases" / f"v{VERSION}.toml")
        assert data["description"].strip(), "the empty description was left standing"
        assert "widget" in data["description"], data["description"]

    def test_each_completed_field_names_its_source(self, repo):
        _run(repo)
        text = (repo / ".rlsbl" / "releases" / f"v{VERSION}.toml").read_text(
            encoding="utf-8",
        )
        assert text.count("release backfill") >= 2, (
            "each reconstructed field states where it came from; the file "
            f"was:\n{text}"
        )
        assert "bump reconstructed" in text, text
        assert "description reconstructed" in text, text

    def test_the_recorded_fate_is_left_alone(self, repo):
        before = _read(repo / ".rlsbl" / "releases" / f"v{VERSION}.toml")
        _run(repo)
        after = _read(repo / ".rlsbl" / "releases" / f"v{VERSION}.toml")
        assert after["candidate_sha"] == before["candidate_sha"]
        assert after["tree_hashes"] == before["tree_hashes"]
        assert after["format_version"] == 1

    def test_the_archive_is_relocked(self, repo):
        _run(repo)
        path = repo / ".rlsbl" / "releases" / f"v{VERSION}.toml"
        assert not os.stat(path).st_mode & 0o222

    def test_a_second_run_settles(self, repo):
        _run(repo)
        code, output = _run(repo)
        assert code == 0, output
        assert "0 archive(s) to write" in output, output
        assert "write bump" not in output, output


class TestOnlyStringsCount:
    """An empty LIST is a legitimate answer; an empty string is not one.

    ``exclude = []`` says "nothing excluded", which is a real statement about
    the release. Treating it as unanswered would re-plan it on every run and
    the pass would never settle.
    """

    def test_an_empty_exclude_list_is_not_replanned(self, repo):
        _run(repo)
        data = _read(repo / ".rlsbl" / "releases" / f"v{VERSION}.toml")
        assert data["exclude"] == []
        _code, output = _run(repo)
        assert "write exclude" not in output, output
