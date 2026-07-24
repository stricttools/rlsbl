"""Tests for the git-filter-repo commit-map bridge.

git-filter-repo writes ``.git/filter-repo/commit-map`` with two format quirks
that rlsbl's remap machinery must handle:

(a) A header row of the literal tokens ``old new`` -- must be skipped, not
    ingested as a junk ``{"old": "new"}`` mapping.
(b) Pruned commits map to the all-zeros null SHA -- must be dropped, never
    used to rewrite a real hash to nothing (the actual corruption vector).

The fixtures use a REAL commit-map generated with git-filter-repo when the
tool is on PATH, and fall back to a byte-faithful synthetic file otherwise.
"""

import os
import shutil
import subprocess

import pytest

from rlsbl.changelog.files import (
    NULL_SHA,
    load_filter_repo_commit_map,
    remap_jsonl_hashes,
)
from rlsbl.changelog.schema import ChangelogEntry, parse_jsonl, serialize_entry
from rlsbl.commands.changelog_cmd import _parse_sha_map_lines


def _git(args, cwd):
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )


def _build_real_commit_map(repo_dir: str) -> str:
    """Create a repo, prune one commit with git-filter-repo, return the
    path to the generated commit-map. Pruning the commit that solely adds
    ``b.txt`` makes it empty after ``--invert-paths``, so filter-repo maps it
    to the null SHA -- exercising the real corruption vector."""
    _git(["init", "-q"], repo_dir)
    _git(["config", "user.email", "t@t.t"], repo_dir)
    _git(["config", "user.name", "t"], repo_dir)
    for name, content in (("a.txt", "one"), ("b.txt", "two"), ("c.txt", "three")):
        with open(os.path.join(repo_dir, name), "w", encoding="utf-8") as f:
            f.write(content + "\n")
        _git(["add", name], repo_dir)
        _git(["commit", "-qm", name], repo_dir)
    subprocess.run(
        ["git", "filter-repo", "--path", "b.txt", "--invert-paths", "--force"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
    )
    return os.path.join(repo_dir, ".git", "filter-repo", "commit-map")


def _synth_commit_map(path: str) -> str:
    """Write a byte-faithful synthetic commit-map matching git-filter-repo's
    documented format: a padded ``old new`` header, a changed-hash row, an
    unchanged row, and a pruned (null-target) row."""
    old_changed = "a" * 40
    new_changed = "b" * 40
    unchanged = "c" * 40
    pruned = "d" * 40
    header = "old" + " " * 38 + "new"  # split() -> ["old", "new"]
    lines = [
        header,
        f"{old_changed} {new_changed}",
        f"{unchanged} {unchanged}",
        f"{pruned} {NULL_SHA}",
    ]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


@pytest.fixture
def commit_map(tmp_path):
    """Return ``(path, rows)`` for a commit-map. ``rows`` is an independent
    oracle mapping every data row's old SHA to its new SHA (header excluded,
    null targets included), parsed without the code-under-test."""
    if shutil.which("git-filter-repo"):
        repo = tmp_path / "repo"
        repo.mkdir()
        cm = _build_real_commit_map(str(repo))
    else:
        cm = _synth_commit_map(str(tmp_path / "commit-map"))

    rows = {}
    with open(cm, encoding="utf-8") as f:
        for line in f:
            parts = line.split()
            if len(parts) < 2:
                continue
            if parts[0] == "old" and parts[1] == "new":
                continue
            rows[parts[0]] = parts[1]
    return cm, rows


def _pruned_old_shas(rows):
    return sorted(o for o, n in rows.items() if n == NULL_SHA)


def _surviving_map(rows):
    return {o: n for o, n in rows.items() if n != NULL_SHA}


# ---------------------------------------------------------------------------
# Defect (a): the literal "old new" header row
# ---------------------------------------------------------------------------


def test_parse_sha_map_lines_skips_filter_repo_header(commit_map):
    """The header row must not become a junk ``{"old": "new"}`` mapping."""
    cm, _rows = commit_map
    with open(cm, encoding="utf-8") as f:
        parsed = _parse_sha_map_lines(f.readlines())
    assert "old" not in parsed


def test_loader_skips_filter_repo_header(commit_map):
    cm, _rows = commit_map
    sha_map, _pruned = load_filter_repo_commit_map(cm)
    assert "old" not in sha_map


# ---------------------------------------------------------------------------
# Defect (b): pruned commits map to the null SHA
# ---------------------------------------------------------------------------


def test_parse_sha_map_lines_drops_null_targets(commit_map, capsys):
    """Null-target rows are dropped with a warning; no real hash may map to
    the null SHA."""
    cm, rows = commit_map
    pruned = _pruned_old_shas(rows)
    assert pruned, "fixture must contain at least one pruned commit"

    with open(cm, encoding="utf-8") as f:
        parsed = _parse_sha_map_lines(f.readlines())

    for old in pruned:
        assert old not in parsed
    assert NULL_SHA not in parsed.values()
    assert "null SHA" in capsys.readouterr().err


def test_null_target_does_not_corrupt_jsonl(commit_map, tmp_path):
    """The real corruption vector: a JSONL entry referencing a pruned commit
    must NOT be rewritten to the null SHA when a raw commit-map is fed to the
    remap pipeline via _parse_sha_map_lines."""
    cm, rows = commit_map
    pruned = _pruned_old_shas(rows)[0]

    changes = tmp_path / "changes"
    changes.mkdir()
    entry = ChangelogEntry(commits=[pruned], user_facing=False)
    (changes / "unreleased.jsonl").write_text(
        serialize_entry(entry) + "\n", encoding="utf-8"
    )

    with open(cm, encoding="utf-8") as f:
        sha_map = _parse_sha_map_lines(f.readlines())
    remap_jsonl_hashes(str(changes), sha_map)

    result = parse_jsonl(str(changes / "unreleased.jsonl"))
    assert result[0].commits == [pruned]
    assert NULL_SHA not in result[0].commits


def test_loader_drops_null_and_reports_pruned(commit_map):
    cm, rows = commit_map
    sha_map, pruned = load_filter_repo_commit_map(cm)

    assert NULL_SHA not in sha_map.values()
    assert sorted(pruned) == _pruned_old_shas(rows)
    assert sha_map == _surviving_map(rows)


# ---------------------------------------------------------------------------
# Happy path: abbreviated hashes in JSONL resolve against full-SHA map keys
# ---------------------------------------------------------------------------


def test_remap_abbreviated_hashes_against_full_sha_map(commit_map, tmp_path):
    cm, _rows = commit_map
    sha_map, _pruned = load_filter_repo_commit_map(cm)

    # Pick a surviving row whose target differs from its source, so the remap
    # actually changes the hash.
    old, new = next((o, n) for o, n in sha_map.items() if o != n)

    changes = tmp_path / "changes"
    changes.mkdir()
    entry = ChangelogEntry(commits=[old[:8]], user_facing=False)
    (changes / "unreleased.jsonl").write_text(
        serialize_entry(entry) + "\n", encoding="utf-8"
    )

    remap_jsonl_hashes(str(changes), sha_map)

    result = parse_jsonl(str(changes / "unreleased.jsonl"))
    assert result[0].commits == [new]
