"""docs/checks.md must describe exactly the checks that exist.

The per-tag counts and per-tag tables in the docs were hand-maintained and
drifted: the changelog tag claimed 9 checks with 11 registered, workspace 15
with 17, quality 9 with 10, and five checks had no row at all. Deriving them
from ``rlsbl/data/checks.toml`` here means the next added check fails this
test instead of quietly rotting the reference page.

A check is "documented" when its name appears in a table row of the section
for one of its tags (or, for untagged checks, the untagged section). Checks
carrying several tags need one row plus the prose cross-reference the page
already uses ("`test-suite` (see prepush checks) is also tagged `quality`").

The checks strictcli registers into the same registry are in none of those
sections and in no count on the page, because they are in no rlsbl file the
page derives from -- they had no row at all, while the page said it covered
every check. They are documented in their own section, bound here to the live
registry rather than to ``checks.toml``.
"""

import re
import tomllib
from collections import Counter
from pathlib import Path


DOCS = Path(__file__).resolve().parents[1] / "docs" / "checks.md"
CHECKS_TOML = Path(__file__).resolve().parents[1] / "rlsbl" / "data" / "checks.toml"

# Tag -> the "## ..." heading in docs/checks.md that documents it.
TAG_SECTIONS = {
    "project": "Project checks",
    "release": "Release checks",
    "changelog": "Changelog checks",
    "workspace": "Workspace checks",
    "quality": "Quality checks",
    "prepush": "Prepush checks",
}


def _checks():
    with open(CHECKS_TOML, "rb") as f:
        return tomllib.load(f)["checks"]


def _docs_text():
    return DOCS.read_text(encoding="utf-8")


def _tag_counts():
    counts = Counter()
    for meta in _checks().values():
        for tag in meta["tags"]:
            counts[tag] += 1
    return counts


def _section(heading):
    """The body of a '## <heading>' section, up to the next '## '."""
    text = _docs_text()
    start = text.index(f"\n## {heading}\n")
    rest = text[start + 1:]
    end = rest.find("\n## ", 1)
    return rest if end == -1 else rest[:end]


def _row_names(section_body):
    """Check names in the leading backtick cell of each table row."""
    return {
        m.group(1)
        for m in re.finditer(r"^\| `([a-z][a-z0-9-]*)` \|", section_body, re.M)
    }


def test_tag_table_counts_match_checks_toml():
    body = _section("Tags")
    counts = _tag_counts()
    documented = {
        m.group(1): int(m.group(2))
        for m in re.finditer(r"^\| `([a-z-]+)` \| [^|]+ \| (\d+) \|", body, re.M)
    }
    assert documented, "the Tags table has no rows"
    assert set(documented) == set(TAG_SECTIONS), (
        f"the Tags table documents {sorted(documented)}, expected "
        f"{sorted(TAG_SECTIONS)}"
    )
    for tag, claimed in documented.items():
        assert claimed == counts[tag], (
            f"docs/checks.md claims {claimed} `{tag}` checks; "
            f"checks.toml registers {counts[tag]}"
        )


def test_every_check_has_a_documented_row():
    checks = _checks()
    documented = set()
    for tag, heading in TAG_SECTIONS.items():
        documented |= _row_names(_section(heading))
    documented |= _row_names(_section("Untagged checks"))

    missing = sorted(name for name in checks if name not in documented)
    assert not missing, (
        f"checks with no row in docs/checks.md: {', '.join(missing)}"
    )


def test_no_documented_row_names_a_nonexistent_check():
    checks = _checks()
    documented = set()
    for heading in list(TAG_SECTIONS.values()) + ["Untagged checks"]:
        documented |= _row_names(_section(heading))
    stale = sorted(name for name in documented if name not in checks)
    assert not stale, (
        f"docs/checks.md documents checks that do not exist: {', '.join(stale)}"
    )


def test_untagged_section_count_matches():
    untagged = [n for n, m in _checks().items() if not m["tags"]]
    body = _section("Untagged checks")
    m = re.search(r"These (\d+) checks have no tag assignment", body)
    assert m, "the untagged section lost its count sentence"
    assert int(m.group(1)) == len(untagged)
    assert _row_names(body) == set(untagged)


def test_internal_tag_counts_match():
    """`preflight` and `preflight-changelog` counts stated in prose."""
    counts = _tag_counts()
    body = _section("Tags")
    for tag in ("preflight", "preflight-changelog"):
        m = re.search(rf"`{tag}` \((\d+) checks:", body)
        assert m, f"the `{tag}` count sentence is missing from the Tags section"
        assert int(m.group(1)) == counts[tag], (
            f"docs/checks.md claims {m.group(1)} `{tag}` checks; "
            f"checks.toml registers {counts[tag]}"
        )


def test_no_undocumented_tag_exists():
    """A new tag must be documented, not silently absent from the page."""
    internal = {"preflight", "preflight-changelog", "maven"}
    live = {tag for meta in _checks().values() for tag in meta["tags"]}
    assert live == set(TAG_SECTIONS) | internal, (
        f"checks.toml tags {sorted(live)} do not match the tags documented in "
        f"docs/checks.md ({sorted(set(TAG_SECTIONS) | internal)})"
    )
    body = _section("Tags")
    intro = _docs_text().split("\n## Running checks")[0]
    assert f"{len(TAG_SECTIONS)} primary tags" in intro
    assert "Three further tags" in intro and len(internal) == 3
    assert body


def _framework_checks():
    """The checks strictcli registers into rlsbl's registry, from the registry.

    Materializing the providers is what puts them there; before that call the
    registry holds exactly ``checks.toml``.
    """
    import rlsbl

    rlsbl.app._materialize_check_providers()
    return {
        name: rlsbl.app._check_defs[name]
        for name in rlsbl.app._check_defs
        if name not in _checks()
    }


def _framework_rows():
    """``{name: (severity, tags)}`` from the framework-checks table."""
    body = _section("Framework checks")
    rows = {}
    for m in re.finditer(
        r"^\| `([a-z][a-z0-9-]*)` \| (error|warn) \| ([^|]+) \|", body, re.M,
    ):
        tags = {t.strip(" `") for t in m.group(3).split(",")}
        rows[m.group(1)] = (m.group(2), tags)
    return rows


def test_every_framework_check_has_a_row():
    live = _framework_checks()
    assert live, "no provider-registered checks found -- the binding is vacuous"
    documented = _framework_rows()
    assert set(documented) == set(live), (
        f"docs/checks.md documents framework checks {sorted(documented)}; "
        f"the registry carries {sorted(live)}"
    )


def test_framework_rows_state_the_real_severity_and_tags():
    live = _framework_checks()
    for name, (severity, tags) in _framework_rows().items():
        assert severity == live[name].severity, (
            f"docs/checks.md gives `{name}` severity {severity}; "
            f"it is registered {live[name].severity}"
        )
        assert tags == set(live[name].tags), (
            f"docs/checks.md gives `{name}` tags {sorted(tags)}; "
            f"it is registered with {sorted(live[name].tags)}"
        )


def test_a_framework_check_is_in_no_tag_section():
    """They belong to their own section, not to the checks.toml-derived ones.

    The tag sections are counted from ``checks.toml``, so a framework check
    listed in one of them would make that section disagree with its own count.
    """
    live = set(_framework_checks())
    for heading in list(TAG_SECTIONS.values()) + ["Untagged checks"]:
        assert not (_row_names(_section(heading)) & live), heading
