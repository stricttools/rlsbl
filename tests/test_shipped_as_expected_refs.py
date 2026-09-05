"""``shipped_as`` is a recorded alias, and the current spelling is what a version expects.

A renamed releasable's past versions were tagged under the OLD spelling, and
their archives say so in ``shipped_as``. Two facts follow, and ``expected_refs``
-- the single authority for the refs one version owns -- is where both are
derived:

* the old spelling is a RECORDED ALIAS of that version: an explained ref, not a
  tag nothing accounts for;
* the version's expected PRIMARY ref is the CURRENT scheme's spelling, so
  ``rlsbl release reconcile`` mints it at the archive's release commit through
  the materialize path it already had, while the old tag stands where it is.

The two alias sources -- a ``boundary-alias`` event and an archive's
``shipped_as`` -- may agree, and one of them may be the only one present. When
both cover a version and DISAGREE on the spelling, neither outranks the other
and the disagreement is a hard error naming both.
"""

import pytest

from rlsbl.commands.release_reconcile import (
    Explanations,
    Observation,
    STATE_ALREADY_CORRECT,
    STATE_MATERIALIZE,
    apply_item,
    build_preview,
)
from rlsbl.release_file import write_archived_release_file
from rlsbl.targets.base import BaseTarget
from rlsbl.targets.refs import ExpectedRefsError, ref_context
from rlsbl.transition_record import (
    BoundaryAlias,
    BoundaryAliasEvent,
    append_events,
    get_transition_record_path,
)

SHA = "2" * 40
OTHER = "3" * 40


def _releasable(tmp_path, name="core"):
    rel_dir = tmp_path / ".rlsbl-monorepo" / "releasables" / name
    (rel_dir / "releases").mkdir(parents=True)
    return rel_dir


def _archive(rel_dir, version, *, shipped_as=None, sha=SHA):
    write_archived_release_file(
        str(rel_dir / "releases"), version, bump="minor", include=["plain"],
        description=f"Version {version}.",
        candidate_sha=sha, tree_hashes={".": "f" * 40},
        shipped_as=shipped_as,
    )


def _ctx(tmp_path, rel_dir, name="core"):
    return ref_context(
        repo_root=str(tmp_path),
        primary_tag_format="{name}@v{version}", releasable_name=name,
        releasable_config_dir=str(rel_dir),
    )


def _record_alias(rel_dir, tmp_path, alias_tag, aliased_tag, commit=SHA):
    append_events(
        get_transition_record_path(str(tmp_path), releasable_dir=str(rel_dir)),
        [BoundaryAliasEvent(aliases=[BoundaryAlias(
            alias_tag=alias_tag, aliased_tag=aliased_tag, commit=commit,
        )])],
    )


# ---------------------------------------------------------------------------
# The derivation
# ---------------------------------------------------------------------------


class TestShippedAsIsARecordedAlias:

    def test_the_old_spelling_joins_the_versions_ref_set(self, tmp_path):
        rel_dir = _releasable(tmp_path)
        _archive(rel_dir, "1.0.0", shipped_as="widget@v1.0.0")

        refs = BaseTarget().expected_refs("1.0.0", _ctx(tmp_path, rel_dir))
        assert refs.primary == "core@v1.0.0", (
            "the expected primary ref is the CURRENT scheme's spelling"
        )
        assert refs.aliases == ("widget@v1.0.0",)
        assert refs.tags == ("core@v1.0.0", "widget@v1.0.0")

    def test_a_version_with_no_shipped_as_is_unchanged(self, tmp_path):
        """The regression pin: nothing is invented for an ordinary archive."""
        rel_dir = _releasable(tmp_path)
        _archive(rel_dir, "1.0.0")

        refs = BaseTarget().expected_refs("1.0.0", _ctx(tmp_path, rel_dir))
        assert refs.aliases == ()
        assert refs.tags == ("core@v1.0.0",)

    def test_another_versions_shipped_as_is_not_this_versions(self, tmp_path):
        rel_dir = _releasable(tmp_path)
        _archive(rel_dir, "1.0.0", shipped_as="widget@v1.0.0")
        _archive(rel_dir, "2.0.0")

        refs = BaseTarget().expected_refs("2.0.0", _ctx(tmp_path, rel_dir))
        assert refs.aliases == ()

    def test_a_standalone_repository_reads_its_own_archives(self, tmp_path):
        releases = tmp_path / ".rlsbl" / "releases"
        write_archived_release_file(
            str(releases), "1.0.0", bump="minor", include=["plain"],
            description="d", candidate_sha=SHA, tree_hashes={".": "f" * 40},
            shipped_as="widget@v1.0.0",
        )
        refs = BaseTarget().expected_refs(
            "1.0.0", ref_context(repo_root=str(tmp_path)),
        )
        assert refs.primary == "v1.0.0"
        assert refs.aliases == ("widget@v1.0.0",)


class TestTheTwoAliasSourcesTogether:

    def test_agreement_yields_one_alias(self, tmp_path):
        rel_dir = _releasable(tmp_path)
        _archive(rel_dir, "1.0.0", shipped_as="widget@v1.0.0")
        _record_alias(rel_dir, tmp_path, "core@v1.0.0", "widget@v1.0.0")

        refs = BaseTarget().expected_refs("1.0.0", _ctx(tmp_path, rel_dir))
        assert set(refs.aliases) == {"core@v1.0.0", "widget@v1.0.0"}
        assert refs.tags == ("core@v1.0.0", "widget@v1.0.0")

    def test_disagreement_is_a_hard_error_naming_both_sources(self, tmp_path):
        rel_dir = _releasable(tmp_path)
        _archive(rel_dir, "1.0.0", shipped_as="gizmo@v1.0.0")
        _record_alias(rel_dir, tmp_path, "core@v1.0.0", "widget@v1.0.0")

        with pytest.raises(ExpectedRefsError) as exc:
            BaseTarget().expected_refs("1.0.0", _ctx(tmp_path, rel_dir))
        message = str(exc.value)
        # Both spellings.
        assert "gizmo@v1.0.0" in message
        assert "widget@v1.0.0" in message
        # Both sources, by path.
        assert "transitions.jsonl" in message
        assert "v1.0.0.toml" in message

    def test_an_event_for_another_version_does_not_disagree(self, tmp_path):
        """The rule compares sources that COVER THE SAME version."""
        rel_dir = _releasable(tmp_path)
        _archive(rel_dir, "2.0.0", shipped_as="gizmo@v2.0.0")
        _record_alias(rel_dir, tmp_path, "core@v1.0.0", "widget@v1.0.0")

        refs = BaseTarget().expected_refs("2.0.0", _ctx(tmp_path, rel_dir))
        assert refs.aliases == ("gizmo@v2.0.0",)


# ---------------------------------------------------------------------------
# What reconcile does with it
# ---------------------------------------------------------------------------


def _refs_map(*tags, sha=SHA):
    refs = {}
    for tag in tags:
        refs[f"refs/tags/{tag}"] = sha
        refs[f"refs/tags/{tag}^{{}}"] = sha
    return refs


class TestReconcileMintsTheCurrentSpelling:
    """The renamed-releasable fixture: old tags standing, current ones absent."""

    def _preview(self, tmp_path, rel_dir, *, remote, local):
        return build_preview(
            observation=Observation(
                remote_refs=remote, local_refs=local,
                releases=frozenset(), releases_known=False,
            ),
            explanations=Explanations(),
            target=BaseTarget(),
            ref_ctx=_ctx(tmp_path, rel_dir),
            releases_dir=str(rel_dir / "releases"),
        )

    def test_the_current_spelling_is_minted_at_the_release_commit(self, tmp_path):
        rel_dir = _releasable(tmp_path)
        _archive(rel_dir, "1.0.0", shipped_as="widget@v1.0.0")
        preview = self._preview(
            tmp_path, rel_dir,
            remote=_refs_map("widget@v1.0.0"), local=_refs_map("widget@v1.0.0"),
        )
        item = preview.by_key("refs/tags/core@v1.0.0")
        assert item.state == STATE_MATERIALIZE
        assert item.data.target == SHA, "minted at the archive's release commit"
        assert item.data.create_local_tag is True
        assert item.data.observed == ""

    def test_the_old_tag_stands_explained(self, tmp_path):
        rel_dir = _releasable(tmp_path)
        _archive(rel_dir, "1.0.0", shipped_as="widget@v1.0.0")
        preview = self._preview(
            tmp_path, rel_dir,
            remote=_refs_map("widget@v1.0.0"), local=_refs_map("widget@v1.0.0"),
        )
        item = preview.by_key("refs/tags/widget@v1.0.0")
        assert item is not None, (
            "the old spelling belongs to the version, so the reconcile owes a "
            "verdict on it instead of passing over it"
        )
        assert item.state == STATE_ALREADY_CORRECT
        assert "version 1.0.0" in " ".join(item.facts)
        assert item.data is None, "nothing is done to a tag that already stands"

    def test_an_old_tag_origin_lacks_is_pushed_rather_than_ignored(self, tmp_path):
        rel_dir = _releasable(tmp_path)
        _archive(rel_dir, "1.0.0", shipped_as="widget@v1.0.0")
        preview = self._preview(
            tmp_path, rel_dir, remote={}, local=_refs_map("widget@v1.0.0"),
        )
        item = preview.by_key("refs/tags/widget@v1.0.0")
        assert item.state == STATE_MATERIALIZE
        assert item.data.target == SHA

    def test_the_minted_write_is_exactly_a_tag_and_a_push(self, tmp_path):
        """No new write machinery: create the tag at the release commit, push it."""
        rel_dir = _releasable(tmp_path)
        _archive(rel_dir, "1.0.0", shipped_as="widget@v1.0.0")
        preview = self._preview(
            tmp_path, rel_dir,
            remote=_refs_map("widget@v1.0.0"), local=_refs_map("widget@v1.0.0"),
        )
        item = preview.by_key("refs/tags/core@v1.0.0")

        calls = []

        def git(binary, args, **kwargs):
            calls.append((binary, list(args)))
            return ""

        class Ctx:
            config = {}

        apply_item(
            item, ctx=Ctx(), releases_dir=str(rel_dir / "releases"),
            changelog_path=str(tmp_path / "CHANGELOG.md"),
            push_timeout=30, git=git, log=lambda *_: None,
        )
        assert calls[0] == ("git", ["tag", "-f", "core@v1.0.0", SHA])
        pushes = [c for c in calls if "push" in c[1]]
        assert len(pushes) == 1
        push = pushes[0][1]
        assert "refs/tags/core@v1.0.0" in " ".join(push)
        assert not any("widget@v1.0.0" in token for token in push), (
            "the mint writes the current spelling only; the old tag is not "
            "moved, deleted or re-pushed by it"
        )
