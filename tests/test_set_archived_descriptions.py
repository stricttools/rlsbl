"""Tests for the set_archived_descriptions script.

The script rewrites one field of a read-only archived release file. Two things
can go wrong quietly and both are covered here: writing through a 0444 file
without restoring its permissions, and INSERTING a key into a document whose
last element is a table -- where a naive append lands inside `[tree_hashes]`
and silently turns the description into a tree-hash entry.
"""

import importlib.util
import io
import json
import os
import stat
import sys
import tomllib
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "set_archived_descriptions.py"
_spec = importlib.util.spec_from_file_location("set_archived_descriptions", _SCRIPT)
setdesc = importlib.util.module_from_spec(_spec)
sys.modules["set_archived_descriptions"] = setdesc
_spec.loader.exec_module(setdesc)


ANCHORED_WITH_DESCRIPTION = """\
# strictspec document version gate (do not remove)
format_version = 1
bump = "minor"
description = "The old description."
include = ["pypi"]
exclude = []
candidate_sha = "1111111111111111111111111111111111111111"

[tree_hashes]
"." = "2222222222222222222222222222222222222222"
"""

ANCHORED_WITHOUT_DESCRIPTION = """\
# strictspec document version gate (do not remove)
format_version = 1
bump = "minor"
include = ["pypi"]
exclude = []
candidate_sha = "1111111111111111111111111111111111111111"

[tree_hashes]
"." = "2222222222222222222222222222222222222222"
"""


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "proj"
    (root / ".rlsbl" / "releases").mkdir(parents=True)
    return root


def write_archive(repo, version, content):
    path = repo / ".rlsbl" / "releases" / f"v{version}.toml"
    path.write_text(content, encoding="utf-8")
    os.chmod(path, 0o444)
    return path


def read_toml(path):
    with open(path, "rb") as f:
        return tomllib.load(f)


def run(repo, mapping, *, dry_run=False):
    argv = ["--repo", str(repo), "--stdin", "--no-commit"]
    if dry_run:
        argv.append("--dry-run")
    stdin = sys.stdin
    sys.stdin = io.StringIO(json.dumps(mapping))
    try:
        return setdesc.main(argv)
    finally:
        sys.stdin = stdin


def test_rewrites_an_existing_description(repo):
    path = write_archive(repo, "0.1.0", ANCHORED_WITH_DESCRIPTION)
    run(repo, {"0.1.0": "A new description."})
    assert read_toml(path)["description"] == "A new description."


def test_inserts_a_missing_description_outside_the_trailing_table(repo):
    """The inserted key must be a document field, not a tree_hashes entry."""
    path = write_archive(repo, "0.1.0", ANCHORED_WITHOUT_DESCRIPTION)

    run(repo, {"0.1.0": "An authored description."})

    data = read_toml(path)
    assert data["description"] == "An authored description."
    assert data["tree_hashes"] == {
        ".": "2222222222222222222222222222222222222222"
    }
    assert data["candidate_sha"] == "1111111111111111111111111111111111111111"
    assert data["format_version"] == 1


def test_the_result_is_a_valid_release_document(repo):
    from rlsbl.release_file import read_release_file

    path = write_archive(repo, "0.1.0", ANCHORED_WITHOUT_DESCRIPTION)
    run(repo, {"0.1.0": "An authored description."})
    cfg = read_release_file(str(path))
    assert cfg.description == "An authored description."
    assert cfg.tree_hashes == {".": "2222222222222222222222222222222222222222"}


def test_archive_stays_locked(repo):
    path = write_archive(repo, "0.1.0", ANCHORED_WITH_DESCRIPTION)
    run(repo, {"0.1.0": "A new description."})
    mode = os.stat(path).st_mode
    assert not (mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def test_dry_run_writes_nothing(repo, capsys):
    path = write_archive(repo, "0.1.0", ANCHORED_WITH_DESCRIPTION)
    before = path.read_bytes()
    run(repo, {"0.1.0": "A new description."}, dry_run=True)
    assert path.read_bytes() == before
    assert "--dry-run: nothing written." in capsys.readouterr().out


def test_a_second_run_reports_nothing_to_do(repo, capsys):
    write_archive(repo, "0.1.0", ANCHORED_WITH_DESCRIPTION)
    run(repo, {"0.1.0": "A new description."})
    capsys.readouterr()
    run(repo, {"0.1.0": "A new description."})
    out = capsys.readouterr().out
    assert "0 description(s) to rewrite, 1 already current." in out
    assert "Nothing to do." in out


def test_a_version_with_no_archive_is_a_hard_error(repo, capsys):
    write_archive(repo, "0.1.0", ANCHORED_WITH_DESCRIPTION)
    with pytest.raises(SystemExit) as exc:
        run(repo, {"0.2.0": "Nowhere to put this."})
    assert "no archive for version(s): 0.2.0" in str(exc.value)


def test_an_empty_description_is_refused(repo):
    write_archive(repo, "0.1.0", ANCHORED_WITH_DESCRIPTION)
    with pytest.raises(SystemExit) as exc:
        run(repo, {"0.1.0": "   "})
    assert "non-empty description" in str(exc.value)
