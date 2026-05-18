"""Regression test for release.py in-memory changelog preview.

Bug (fixed): the in-memory preview call at release.py:399 used to call
generate_changelog() without version_override. The returned content then
carried "## Unreleased" as the section heading, so
extract_changelog_entry_from_text(content, new_version) returned None
because it searches for "## X.Y.Z". Later code that wrote
changelog_entry to a GitHub Release notes file crashed with
"write() argument must be str, not None".

The fix passes version_override=new_version to that call so the
preview heading matches what extract_changelog_entry_from_text looks for.
"""

import json

from rlsbl.changelog.generate import generate_changelog
from rlsbl.utils import extract_changelog_entry_from_text


def _jsonl_line(**kwargs) -> str:
    return json.dumps(kwargs, separators=(",", ":"))


def _setup_project(tmp_path, unreleased_lines):
    changes = tmp_path / ".rlsbl" / "changes"
    changes.mkdir(parents=True)
    (changes / "unreleased.jsonl").write_text("\n".join(unreleased_lines) + "\n")
    return tmp_path


def test_in_memory_preview_extraction_returns_entry(tmp_path, monkeypatch):
    """Regression: release.py line 399 must pass version_override so entry extraction works.

    Without version_override, generate_changelog emits "## Unreleased" as the
    section heading and extract_changelog_entry_from_text(content, "1.2.3")
    returns None, which previously crashed the release flow.
    """
    monkeypatch.chdir(tmp_path)
    _setup_project(
        tmp_path,
        unreleased_lines=[
            _jsonl_line(
                commits=["a1b2c3d"],
                user_facing=True,
                description="**A new feature.** Does the thing.",
                type="feature",
            ),
        ],
    )

    new_version = "1.2.3"

    # Mirror release.py:399 — the exact call site that used to be buggy.
    content = generate_changelog(
        str(tmp_path), write_to_disk=False, version_override=new_version
    )

    # Mirror release.py:403 — extract the entry for new_version from the preview.
    entry = extract_changelog_entry_from_text(content, new_version)

    assert entry is not None, (
        "Entry extraction returned None — generate_changelog must be called with "
        "version_override so the heading matches '## X.Y.Z' (regression of v0.34.0 bug)."
    )
    assert "A new feature" in entry
    assert "Does the thing" in entry


def test_in_memory_preview_without_version_override_misses_entry(tmp_path, monkeypatch):
    """Negative control: without version_override, the heading is 'Unreleased'
    and extraction by version number returns None. This locks in the contract
    that release.py relies on — if either generate_changelog or
    extract_changelog_entry_from_text changes behaviour, this test breaks
    and forces a re-review of the release flow's coupling.
    """
    monkeypatch.chdir(tmp_path)
    _setup_project(
        tmp_path,
        unreleased_lines=[
            _jsonl_line(
                commits=["a1b2c3d"],
                user_facing=True,
                description="Something",
                type="feature",
            ),
        ],
    )

    content = generate_changelog(str(tmp_path), write_to_disk=False)

    assert "## Unreleased" in content
    assert extract_changelog_entry_from_text(content, "1.2.3") is None
