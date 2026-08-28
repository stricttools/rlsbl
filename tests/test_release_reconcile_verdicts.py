"""One fixture per verdict class, and the tripwire that overrules all of them.

``rlsbl release reconcile`` classifies every subject -- one git ref, one
version's GitHub Release -- into exactly five classes. These tests drive the
verdict engine directly with a constructed observation and a constructed set of
explanations, so each class is pinned by itself rather than inferred from an
end-to-end run that happens to exercise it.

The classes:

* ``materialize`` -- the ledger records it; origin does not have it.
* ``already-correct`` -- both sides agree.
* ``re-point-with-lease`` -- origin holds a different commit and a source
  explains the difference.
* ``refuse-foreign`` -- origin holds something no source explains. The
  publication tripwire: one of these aborts the whole reconcile.
* ``refuse-identity-mismatch`` -- the target's materialization policy refuses.
"""

import json

import pytest

from githarness import commit_file, git, init_repo

from rlsbl.commands.release_reconcile import (
    Explanations,
    Observation,
    STATE_ALREADY_CORRECT,
    STATE_MATERIALIZE,
    STATE_REFUSE_FOREIGN,
    STATE_REFUSE_IDENTITY,
    STATE_RE_POINT,
    apply_item,
    build_preview,
    collect_explanations,
    refusals,
    tripwire_error,
)
from rlsbl.lineage import (
    IdentityTransitionEvent,
    append_event,
    get_lineage_path,
)
from rlsbl.release_file import write_archived_release_file
from rlsbl.targets import TARGETS
from rlsbl.targets.base import BaseTarget
from rlsbl.targets.refs import ref_context

OLD = "1" * 40
NEW = "2" * 40
UNRELATED = "3" * 40


@pytest.fixture
def ledger(tmp_path):
    """A releases directory holding one anchored version, 1.0.0 at NEW."""
    releases = tmp_path / ".rlsbl" / "releases"
    write_archived_release_file(
        str(releases), "1.0.0", bump="minor", include=["plain"],
        description="The first release.",
        candidate_sha=NEW, tree_hashes={".": "f" * 40},
    )
    return releases


def _refs(**pairs):
    """Ref map with the peeled entries filled in, as ls-remote reports them."""
    refs = {}
    for name, sha in pairs.items():
        refname = f"refs/tags/{name.replace('_', '.')}"
        refs[refname] = sha
        refs[f"{refname}^{{}}"] = sha
    return refs


def _preview(tmp_path, ledger, *, remote, local, releases=("v1.0.0",),
             releases_known=True, explanations=None, target=None):
    return build_preview(
        observation=Observation(
            remote_refs=remote, local_refs=local,
            releases=frozenset(releases), releases_known=releases_known,
        ),
        explanations=explanations or Explanations(),
        target=target or BaseTarget(),
        ref_ctx=ref_context(repo_root=str(tmp_path)),
        releases_dir=str(ledger),
    )


class TestTheFiveClasses:

    def test_already_correct(self, tmp_path, ledger):
        preview = _preview(
            tmp_path, ledger,
            remote=_refs(v1_0_0=NEW), local=_refs(v1_0_0=NEW),
        )
        assert preview.by_key("refs/tags/v1.0.0").state == STATE_ALREADY_CORRECT
        assert preview.by_key("release:v1.0.0").state == STATE_ALREADY_CORRECT

    def test_materialize_a_ref_origin_is_missing(self, tmp_path, ledger):
        preview = _preview(
            tmp_path, ledger, remote={}, local=_refs(v1_0_0=NEW),
        )
        item = preview.by_key("refs/tags/v1.0.0")
        assert item.state == STATE_MATERIALIZE
        assert item.data.target == NEW
        assert item.data.observed == "", (
            "an absent ref has no remote value, so the lease is the empty "
            "expectation"
        )

    def test_materialize_a_release_that_does_not_exist(self, tmp_path, ledger):
        preview = _preview(
            tmp_path, ledger,
            remote=_refs(v1_0_0=NEW), local=_refs(v1_0_0=NEW), releases=(),
        )
        item = preview.by_key("release:v1.0.0")
        assert item.state == STATE_MATERIALIZE
        assert item.data.kind == "release"
        assert item.data.target == NEW

    def test_re_point_with_lease_when_a_source_explains_it(self, tmp_path, ledger):
        preview = _preview(
            tmp_path, ledger,
            remote=_refs(v1_0_0=OLD), local=_refs(v1_0_0=NEW),
            explanations=Explanations(
                commit_map={OLD: NEW}, origins={OLD: "a recorded rewrite"},
            ),
        )
        item = preview.by_key("refs/tags/v1.0.0")
        assert item.state == STATE_RE_POINT
        assert item.data.observed == OLD, "the lease is the observed remote value"
        assert item.data.target == NEW

    def test_refuse_foreign_when_nothing_explains_it(self, tmp_path, ledger):
        preview = _preview(
            tmp_path, ledger,
            remote=_refs(v1_0_0=UNRELATED), local=_refs(v1_0_0=NEW),
        )
        item = preview.by_key("refs/tags/v1.0.0")
        assert item.state == STATE_REFUSE_FOREIGN
        assert item.data is None, "a refusal carries no action"

    def test_refuse_identity_mismatch(self, tmp_path, ledger):
        """A Go tag IS the published artifact, so it may not be recreated
        under an identity the version was never published under."""
        append_event(get_lineage_path(str(tmp_path)), IdentityTransitionEvent(
            facet="go-module-path",
            old="example.com/old", new="example.com/new",
            effective_version="2.0.0",
        ))
        preview = build_preview(
            observation=Observation(
                remote_refs={}, local_refs=_refs(v1_0_0=NEW),
                releases=frozenset(), releases_known=False,
            ),
            explanations=collect_explanations(
                [str(ledger)],
                ref_context(repo_root=str(tmp_path)).lineage_paths,
            ),
            target=TARGETS["go"],
            ref_ctx=ref_context(repo_root=str(tmp_path)),
            releases_dir=str(ledger),
        )
        item = preview.by_key("refs/tags/v1.0.0")
        assert item.state == STATE_REFUSE_IDENTITY
        assert "example.com/old" in " ".join(item.facts)

    def test_the_same_transition_leaves_a_later_version_alone(self, tmp_path):
        """Only versions published under the OLD identity are refused."""
        releases = tmp_path / ".rlsbl" / "releases"
        write_archived_release_file(
            str(releases), "3.0.0", bump="major", include=["go"],
            description="After the move.",
            candidate_sha=NEW, tree_hashes={".": "f" * 40},
        )
        append_event(get_lineage_path(str(tmp_path)), IdentityTransitionEvent(
            facet="go-module-path",
            old="example.com/old", new="example.com/new",
            effective_version="2.0.0",
        ))
        preview = build_preview(
            observation=Observation(
                remote_refs={}, local_refs=_refs(v3_0_0=NEW),
                releases=frozenset(), releases_known=False,
            ),
            explanations=collect_explanations(
                [str(releases)],
                ref_context(repo_root=str(tmp_path)).lineage_paths,
            ),
            target=TARGETS["go"],
            ref_ctx=ref_context(repo_root=str(tmp_path)),
            releases_dir=str(releases),
        )
        assert preview.by_key("refs/tags/v3.0.0").state == STATE_MATERIALIZE

    def test_a_non_go_target_materializes_across_a_transition(self, tmp_path, ledger):
        """The refusal is a per-target fact, not a universal rule."""
        append_event(get_lineage_path(str(tmp_path)), IdentityTransitionEvent(
            facet="package-name", old="old", new="new",
            effective_version="2.0.0",
        ))
        preview = build_preview(
            observation=Observation(
                remote_refs={}, local_refs=_refs(v1_0_0=NEW),
                releases=frozenset(), releases_known=False,
            ),
            explanations=collect_explanations(
                [str(ledger)],
                ref_context(repo_root=str(tmp_path)).lineage_paths,
            ),
            target=TARGETS["npm"],
            ref_ctx=ref_context(repo_root=str(tmp_path)),
            releases_dir=str(ledger),
        )
        assert preview.by_key("refs/tags/v1.0.0").state == STATE_MATERIALIZE


class TestTheTripwire:

    def test_one_refusal_is_reported_over_every_repairable_subject(
        self, tmp_path,
    ):
        releases = tmp_path / ".rlsbl" / "releases"
        for version, sha in (("1.0.0", NEW), ("1.1.0", OLD)):
            write_archived_release_file(
                str(releases), version, bump="minor", include=["plain"],
                description="d", candidate_sha=sha, tree_hashes={".": "f" * 40},
            )
        preview = build_preview(
            observation=Observation(
                remote_refs={
                    **_refs(v1_0_0=UNRELATED),
                    **_refs(v1_1_0=OLD),
                },
                local_refs={**_refs(v1_0_0=NEW), **_refs(v1_1_0=OLD)},
                releases=frozenset({"v1.0.0", "v1.1.0"}), releases_known=True,
            ),
            explanations=Explanations(),
            target=BaseTarget(),
            ref_ctx=ref_context(repo_root=str(tmp_path)),
            releases_dir=str(releases),
        )
        blocked = refusals(preview)
        assert [i.key for i in blocked] == ["refs/tags/v1.0.0"]
        message = tripwire_error(preview)
        assert "refs/tags/v1.0.0" in message
        assert "NOTHING has been changed" in message

    def test_a_local_ref_disagreeing_with_the_ledger_is_refused(
        self, tmp_path, ledger,
    ):
        """The ledger is the authority for where a released ref belongs."""
        preview = _preview(
            tmp_path, ledger,
            remote=_refs(v1_0_0=OLD), local=_refs(v1_0_0=UNRELATED),
        )
        item = preview.by_key("refs/tags/v1.0.0")
        assert item.state == STATE_REFUSE_FOREIGN
        assert "ledger" in item.summary


class TestTheReleaseHalf:

    def test_an_unread_listing_judges_no_releases_at_all(self, tmp_path, ledger):
        """An unanswered gh listing is never read as 'no Releases exist'."""
        preview = _preview(
            tmp_path, ledger, remote=_refs(v1_0_0=NEW), local=_refs(v1_0_0=NEW),
            releases=(), releases_known=False,
        )
        assert preview.by_key("release:v1.0.0") is None

    def test_a_materialized_release_carries_the_marker_from_the_ledger(
        self, tmp_path, ledger, monkeypatch,
    ):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "CHANGELOG.md").write_text(
            "# Changelog\n\n## 1.0.0\n\n### Features\n\n- **Ship it.** Yes.\n"
        )
        preview = _preview(
            tmp_path, ledger, remote=_refs(v1_0_0=NEW), local=_refs(v1_0_0=NEW),
            releases=(),
        )
        item = preview.by_key("release:v1.0.0")

        seen = {}

        def gh(args, **kwargs):
            if args[:2] == ["release", "create"]:
                seen["args"] = list(args)
                path = args[args.index("--notes-file") + 1]
                seen["body"] = open(path, encoding="utf-8").read()
            return ""

        class Ctx:
            config = {}

        apply_item(
            item, ctx=Ctx(), releases_dir=str(ledger),
            changelog_path=str(tmp_path / "CHANGELOG.md"),
            push_timeout=30, gh=gh, log=lambda *_: None,
        )
        assert f"<!-- rlsbl-ci-sha: {NEW} -->" in seen["body"], (
            "a Release the reconcile creates must carry the ledger anchor's "
            "marker, or the publish workflow has nothing to judge"
        )
        assert "Ship it" in seen["body"]
        assert "--prerelease" not in seen["args"]

    def test_a_prerelease_version_is_marked_as_one(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        releases = tmp_path / ".rlsbl" / "releases"
        write_archived_release_file(
            str(releases), "1.0.0-rc.1", bump="prerelease", include=["plain"],
            description="d", candidate_sha=NEW, tree_hashes={".": "f" * 40},
        )
        preview = build_preview(
            observation=Observation(
                remote_refs=_refs(**{"v1_0_0-rc.1": NEW}),
                local_refs=_refs(**{"v1_0_0-rc.1": NEW}),
                releases=frozenset(), releases_known=True,
            ),
            explanations=Explanations(),
            target=BaseTarget(),
            ref_ctx=ref_context(repo_root=str(tmp_path)),
            releases_dir=str(releases),
        )
        item = preview.by_key("release:v1.0.0-rc.1")
        assert item.state == STATE_MATERIALIZE

        seen = {}

        def gh(args, **kwargs):
            if args[:2] == ["release", "create"]:
                seen["args"] = list(args)
            return ""

        class Ctx:
            config = {}

        apply_item(
            item, ctx=Ctx(), releases_dir=str(releases), changelog_path=None,
            push_timeout=30, gh=gh, log=lambda *_: None,
        )
        assert "--prerelease" in seen["args"]


class TestTheReleaseListing:
    """The listing is a bounded question, so its bound must be visible."""

    class _Ctx:
        config = {}

    def _list(self, out):
        from rlsbl.commands.release_reconcile import list_releases

        return list_releases(
            ctx=self._Ctx(), gh=lambda *a, **k: out,
            gh_installed=lambda: True, gh_auth=lambda: True,
        )

    def test_a_listing_that_hits_the_cap_is_a_hard_error(self):
        """`gh release list` takes only a --limit, so a full answer and a
        truncated one look identical. A repository with more Releases than the
        cap must never have the missing ones read as absent and proposed for
        creation."""
        from rlsbl.commands.release_reconcile import (
            _RELEASE_LIST_LIMIT,
            ReconcileError,
        )

        out = "\n".join(f"v0.0.{i}" for i in range(_RELEASE_LIST_LIMIT))
        with pytest.raises(ReconcileError) as exc:
            self._list(out)
        assert str(_RELEASE_LIST_LIMIT) in str(exc.value)

    def test_a_listing_under_the_cap_is_answered(self):
        tags, known = self._list("v1.0.0\nv1.1.0\n")
        assert known is True
        assert tags == frozenset({"v1.0.0", "v1.1.0"})


class TestTheFreshCloneCase:
    """The journal lives under .git; the scrub archives are committed."""

    def test_a_committed_scrub_archive_explains_a_moved_ref(self, tmp_path):
        scrubs = tmp_path / ".rlsbl" / "scrubs"
        scrubs.mkdir(parents=True)
        (scrubs / "scrub-abcdef123456.json").write_text(json.dumps({
            "schema_version": 1, "mode": "match", "reason": "remove a token",
            "old_head": OLD, "new_head": NEW,
            "rewrites": {OLD: NEW}, "tags": [], "completed_steps": [],
        }))
        releases = tmp_path / ".rlsbl" / "releases"
        write_archived_release_file(
            str(releases), "1.0.0", bump="minor", include=["plain"],
            description="d", candidate_sha=NEW, tree_hashes={".": "f" * 40},
        )

        explanations = collect_explanations([str(releases)], ())
        assert explanations.commit_map == {OLD: NEW}
        assert "committed scrub archives" in explanations.sources_present[0]

        preview = build_preview(
            observation=Observation(
                remote_refs=_refs(v1_0_0=OLD), local_refs=_refs(v1_0_0=NEW),
                releases=frozenset(), releases_known=False,
            ),
            explanations=explanations,
            target=BaseTarget(),
            ref_ctx=ref_context(repo_root=str(tmp_path)),
            releases_dir=str(releases),
        )
        item = preview.by_key("refs/tags/v1.0.0")
        assert item.state == STATE_RE_POINT, (
            "a clone that never saw safegit's journal must still be able to "
            "explain a scrub it carries the archive for"
        )
        assert "scrub archive" in " ".join(item.facts)

    def test_a_lineage_anchor_remap_explains_a_moved_ref(self, tmp_path):
        from rlsbl.lineage import AnchorMapping, AnchorRemapEvent

        releases = tmp_path / ".rlsbl" / "releases"
        write_archived_release_file(
            str(releases), "1.0.0", bump="minor", include=["plain"],
            description="d", candidate_sha=NEW, tree_hashes={".": "f" * 40},
        )
        lineage = get_lineage_path(str(tmp_path))
        append_event(lineage, AnchorRemapEvent(
            rewrite="scrub-1", mappings=[AnchorMapping(old_sha=OLD, new_sha=NEW)],
        ))

        explanations = collect_explanations([str(releases)], (lineage,))
        preview = build_preview(
            observation=Observation(
                remote_refs=_refs(v1_0_0=OLD), local_refs=_refs(v1_0_0=NEW),
                releases=frozenset(), releases_known=False,
            ),
            explanations=explanations, target=BaseTarget(),
            ref_ctx=ref_context(repo_root=str(tmp_path)),
            releases_dir=str(releases),
        )
        assert preview.by_key("refs/tags/v1.0.0").state == STATE_RE_POINT

    def test_successive_rewrites_chain(self, tmp_path):
        """A commit rewritten twice is still explained."""
        releases = tmp_path / ".rlsbl" / "releases"
        write_archived_release_file(
            str(releases), "1.0.0", bump="minor", include=["plain"],
            description="d", candidate_sha=NEW, tree_hashes={".": "f" * 40},
        )
        middle = "9" * 40
        explanations = Explanations(
            commit_map={OLD: middle, middle: NEW},
            origins={OLD: "first scrub", middle: "second scrub"},
        )
        preview = build_preview(
            observation=Observation(
                remote_refs=_refs(v1_0_0=OLD), local_refs=_refs(v1_0_0=NEW),
                releases=frozenset(), releases_known=False,
            ),
            explanations=explanations, target=BaseTarget(),
            ref_ctx=ref_context(repo_root=str(tmp_path)),
            releases_dir=str(releases),
        )
        item = preview.by_key("refs/tags/v1.0.0")
        assert item.state == STATE_RE_POINT
        assert "first scrub" in " ".join(item.facts)
        assert "second scrub" in " ".join(item.facts)

    def test_an_unreadable_scrub_archive_is_a_hard_error(self, tmp_path):
        from rlsbl.commands.release_reconcile import ReconcileError

        scrubs = tmp_path / ".rlsbl" / "scrubs"
        scrubs.mkdir(parents=True)
        (scrubs / "scrub-broken.json").write_text("{not json")
        releases = tmp_path / ".rlsbl" / "releases"
        releases.mkdir(parents=True)
        with pytest.raises(ReconcileError) as exc:
            collect_explanations([str(releases)], ())
        assert "scrub-broken.json" in str(exc.value)


class TestTheExpectedRefSet:
    """Companions and recorded aliases are part of a version's ref set."""

    def test_a_recorded_alias_is_judged_too(self, tmp_path):
        from rlsbl.lineage import BoundaryAlias, BoundaryAliasEvent

        releases = tmp_path / ".rlsbl" / "releases"
        write_archived_release_file(
            str(releases), "1.0.0", bump="minor", include=["plain"],
            description="d", candidate_sha=NEW, tree_hashes={".": "f" * 40},
        )
        append_event(get_lineage_path(str(tmp_path)), BoundaryAliasEvent(
            aliases=[BoundaryAlias(
                alias_tag="lib@v1.0.0", aliased_tag="v1.0.0", commit=NEW,
            )],
        ))
        preview = build_preview(
            observation=Observation(
                remote_refs=_refs(v1_0_0=NEW), local_refs=_refs(v1_0_0=NEW),
                releases=frozenset(), releases_known=False,
            ),
            explanations=Explanations(),
            target=BaseTarget(),
            ref_ctx=ref_context(repo_root=str(tmp_path)),
            releases_dir=str(releases),
        )
        alias = preview.by_key("refs/tags/lib@v1.0.0")
        assert alias is not None, (
            "expected_refs is the single authority for a version's ref set, "
            "and a recorded alias belongs to it"
        )
        assert alias.state == STATE_MATERIALIZE, (
            "the alias addresses a released version, so it is created at that "
            "version's ledger anchor like any other ref it owns"
        )
        assert alias.data.target == NEW
        assert alias.data.create_local_tag is True


class TestThePlanFile:
    """The plan is the preview's artifact and the apply step's only input."""

    def _plan_preview(self, tmp_path, ledger):
        return _preview(
            tmp_path, ledger,
            remote=_refs(v1_0_0=OLD), local=_refs(v1_0_0=NEW),
            releases=(),
            explanations=Explanations(
                commit_map={OLD: NEW}, origins={OLD: "a recorded rewrite"},
            ),
        )

    def test_it_round_trips_through_the_strictspec_validator(
        self, tmp_path, ledger,
    ):
        from rlsbl.commands.release_reconcile import read_plan, render_plan

        preview = self._plan_preview(tmp_path, ledger)
        text = render_plan(preview, "deadbeef", generated_by="0.0.0")
        path = tmp_path / "reconcile-plan.toml"
        path.write_text(text)

        plan = read_plan(str(path))
        assert plan.world_digest == "deadbeef"
        by_key = {i.key: i for i in plan.items}
        assert by_key["refs/tags/v1.0.0"].state == "re-point-with-lease"
        assert by_key["refs/tags/v1.0.0"].observed == OLD
        assert by_key["refs/tags/v1.0.0"].target == NEW
        assert by_key["release:v1.0.0"].kind == "release"

    def test_a_plan_missing_the_version_gate_is_refused(self, tmp_path):
        from rlsbl.commands.release_reconcile import ReconcileError, read_plan

        path = tmp_path / "reconcile-plan.toml"
        path.write_text(
            'generated_at = "2026-01-01T00:00:00+01:00"\n'
            'generated_by = "0.0.0"\nworld_digest = "x"\nitems = []\n'
        )
        with pytest.raises(ReconcileError) as exc:
            read_plan(str(path))
        assert "not a valid plan document" in str(exc.value)

    def test_a_plan_with_an_unknown_verdict_is_refused(self, tmp_path):
        from rlsbl.commands.release_reconcile import ReconcileError, read_plan

        path = tmp_path / "reconcile-plan.toml"
        path.write_text(
            "format_version = 1\n"
            'generated_at = "2026-01-01T00:00:00+01:00"\n'
            'generated_by = "0.0.0"\nworld_digest = "x"\n'
            '[[items]]\nkey = "refs/tags/v1.0.0"\nkind = "ref"\n'
            'state = "just-do-it"\n'
        )
        with pytest.raises(ReconcileError):
            read_plan(str(path))

    def test_an_absent_plan_names_the_command_that_writes_one(self, tmp_path):
        from rlsbl.commands.release_reconcile import ReconcileError, read_plan

        with pytest.raises(ReconcileError) as exc:
            read_plan(str(tmp_path / "nope.toml"))
        assert "--plan" in str(exc.value)

    def test_the_digest_changes_when_the_world_does(self, tmp_path):
        a = Observation(remote_refs=_refs(v1_0_0=NEW), local_refs={},
                        releases=frozenset({"v1.0.0"}), releases_known=True)
        b = Observation(remote_refs=_refs(v1_0_0=OLD), local_refs={},
                        releases=frozenset({"v1.0.0"}), releases_known=True)
        c = Observation(remote_refs=_refs(v1_0_0=NEW), local_refs={},
                        releases=frozenset(), releases_known=True)
        assert a.digest != b.digest
        assert a.digest != c.digest, (
            "the Release listing is part of the world the plan was judged "
            "against, so a Release appearing or vanishing invalidates it"
        )
        assert a.digest == Observation(
            remote_refs=_refs(v1_0_0=NEW), local_refs={"anything": "else"},
            releases=frozenset({"v1.0.0"}), releases_known=True,
        ).digest, (
            "the digest covers what the REMOTE holds; the local side is this "
            "checkout's own business and the apply re-derives it"
        )
