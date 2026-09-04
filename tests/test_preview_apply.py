"""The shared observe -> preview -> apply skeleton.

Covers the three pieces the module exports -- the keyed verdict list, the one
renderer, and the entry skeleton with its no-writes line -- plus the fact that
the mirror reconciler's plan output is produced BY that renderer (the one-item
case of the list shape), not by a private printer of its own.
"""

import io
import subprocess

import pytest

from rlsbl import effects
from rlsbl.preview_apply import (
    ObserveWriteError,
    Preview,
    Reconciler,
    VerdictItem,
    git_subcommand,
    no_writes,
    reconcile,
    render_preview,
    single,
)


def _render(preview, *, show_keys):
    out = io.StringIO()
    render_preview(preview, show_keys=show_keys, out=out)
    return out.getvalue()


# ---------------------------------------------------------------------------
# The preview: an ordered list of keyed items
# ---------------------------------------------------------------------------


class TestPreviewShape:
    def test_order_is_preserved_verbatim(self):
        items = [VerdictItem(key=k, state="converged") for k in ("c", "a", "b")]
        assert Preview(items).keys == ("c", "a", "b")

    def test_duplicate_keys_are_refused(self):
        with pytest.raises(ValueError, match="duplicate preview key"):
            Preview([VerdictItem(key="x", state="a"), VerdictItem(key="x", state="b")])

    def test_by_key_and_states(self):
        p = Preview([
            VerdictItem(key="one", state="converged"),
            VerdictItem(key="two", state="behind"),
        ])
        assert p.states == ("converged", "behind")
        assert p.by_key("two").state == "behind"
        assert p.by_key("missing") is None
        assert len(p) == 2

    def test_single_is_the_one_item_case(self):
        item = VerdictItem(key="repo", state="virgin")
        p = single(item)
        assert len(p) == 1 and p.only() is item

    def test_only_refuses_a_multi_item_preview(self):
        p = Preview([VerdictItem(key="a", state="s"), VerdictItem(key="b", state="s")])
        with pytest.raises(ValueError):
            p.only()

    def test_state_label_defaults_to_hyphenated_state(self):
        assert VerdictItem(key="k", state="scaffold_missing").state_label == (
            "scaffold-missing"
        )

    def test_explicit_label_overrides_the_state(self):
        item = VerdictItem(key="k", state="virgin", label="remote-missing-or-empty")
        assert item.state_label == "remote-missing-or-empty"

    def test_data_carries_the_observation_record_untouched(self):
        record = {"split": "abc"}
        assert VerdictItem(key="k", state="s", data=record).data is record


# ---------------------------------------------------------------------------
# The one renderer
# ---------------------------------------------------------------------------


class TestRenderer:
    def test_headline_facts_actions_and_detail_in_order(self):
        item = VerdictItem(
            key="mylib",
            state="behind",
            summary="a new split is available.",
            facts=("old split: aaaaaaaaaaaa", "new split: bbbbbbbbbbbb"),
            actions=("apply would force-push.",),
            detail="free-form\n  block",
        )
        assert _render(single(item), show_keys=False) == (
            "behind: a new split is available.\n"
            "  old split: aaaaaaaaaaaa\n"
            "  new split: bbbbbbbbbbbb\n"
            "  apply would force-push.\n"
            "free-form\n  block\n"
        )

    def test_show_keys_prefixes_the_headline(self):
        item = VerdictItem(key="mylib", state="converged", summary="up to date.")
        assert _render(single(item), show_keys=True) == (
            "mylib: converged: up to date.\n"
        )

    def test_headline_without_summary_is_just_the_label(self):
        assert _render(single(VerdictItem(key="k", state="virgin")), show_keys=False) == (
            "virgin\n"
        )

    def test_every_item_is_rendered_in_list_order(self):
        p = Preview([
            VerdictItem(key="a", state="converged", summary="ok."),
            VerdictItem(key="b", state="behind", summary="stale."),
        ])
        assert _render(p, show_keys=True) == (
            "a: converged: ok.\nb: behind: stale.\n"
        )

    def test_default_stream_is_stdout_at_call_time(self, capsys):
        render_preview(
            single(VerdictItem(key="k", state="converged", summary="ok.")),
            show_keys=False,
        )
        assert capsys.readouterr().out == "converged: ok.\n"


# ---------------------------------------------------------------------------
# The no-writes line
# ---------------------------------------------------------------------------


class TestGitSubcommandScan:
    @pytest.mark.parametrize("argv,expected", [
        (["git", "push", "origin", "main"], "push"),
        (["git", "-C", "/repo", "commit", "-m", "x"], "commit"),
        (["git", "--no-optional-locks", "diff", "--name-only"], "diff"),
        (["git", "-c", "user.name=x", "tag", "v1"], "tag"),
        (["/usr/bin/git", "ls-remote", "origin"], "ls-remote"),
        (["gh", "release", "create"], None),
        ([], None),
        ("git push", None),
    ])
    def test_subcommand_extraction(self, argv, expected):
        assert git_subcommand(argv) == expected


# Every argv below escaped the private denylist the guard used to carry: it
# knew a fixed set of mutating GIT SUBCOMMANDS, so a mutating git verb it had
# never heard of, a mutating git verb spelled as a subcommand's option, and any
# non-git program at all all ran during "observation".  Screening against the
# observe allowlist instead makes the list of things that may run the only
# authority, and these are simply not on it.
DENYLIST_ESCAPES = [
    ["git", "subtree", "push", "--prefix=pkg", "origin", "main"],
    ["git", "clean", "-fdx"],
    ["git", "config", "--global", "user.email", "evil@example.com"],
    ["git", "remote", "add", "origin", "git@example:x/y"],
    ["git", "fetch", "--prune"],
    ["git", "init"],
    ["python", "-c", "open('escaped.txt', 'w').write('written')"],
    ["/bin/sh", "-c", "rm -rf ~"],
    ["npm", "publish"],
    ["gh", "release", "create", "v1.0.0"],
]


class TestNoWrites:
    def test_mutating_git_is_refused(self):
        with pytest.raises(ObserveWriteError, match="git push"):
            with no_writes():
                effects.run(["git", "push", "origin", "main"], capture_output=True)

    @pytest.mark.parametrize("argv", DENYLIST_ESCAPES, ids=" ".join)
    def test_argv_off_the_allowlist_is_refused(self, argv, tmp_path):
        with pytest.raises(ObserveWriteError):
            with no_writes():
                effects.run(argv, capture_output=True, cwd=str(tmp_path))

    def test_gh_rides_the_same_screen(self, tmp_path):
        """``effects.gh`` funnels into ``effects.run``, so it is screened too.

        Not a gh verb classification -- there is none, and the allowlist's
        handful of gh reads is the whole vocabulary an observation has. What
        this pins is the direction: an unlisted gh call is refused, not run.
        """
        with pytest.raises(ObserveWriteError, match="gh release create"):
            with no_writes():
                effects.gh(["release", "create", "v9.9.9"], capture_output=True)

    def test_a_shell_string_is_refused(self, tmp_path):
        with pytest.raises(ObserveWriteError, match="shell command"):
            with no_writes():
                effects.run("touch escaped.txt", shell=True, cwd=str(tmp_path))

    @pytest.mark.parametrize("argv", [
        ["git", "subtree", "split", "--prefix", "pkg"],
        ["git", "clone", "--quiet", "--single-branch", "--branch", "main",
         "/nonexistent-remote.git", "/nonexistent-scratch/mirror"],
        ["git", "ls-remote", "origin"],
        ["git", "merge-base", "--is-ancestor", "a", "b"],
    ], ids=" ".join)
    def test_the_observation_argv_the_mirror_needs_is_allowed(self, argv, tmp_path):
        """These reach the real runner: the guard raises before running."""
        with no_writes():
            effects.run(argv, capture_output=True, text=True, cwd=str(tmp_path))

    def test_rmtree_outside_the_observation_scratch_is_refused(self, tmp_path):
        victim = tmp_path / "not-scratch"
        (victim / "sub").mkdir(parents=True)
        (victim / "sub" / "keep.txt").write_text("precious\n")
        with pytest.raises(ObserveWriteError, match="rmtree"):
            with no_writes():
                effects.rmtree(str(victim), ignore_errors=True)
        assert (victim / "sub" / "keep.txt").exists()

    def test_reading_git_still_runs(self, tmp_path):
        with no_writes():
            r = effects.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                capture_output=True, text=True, cwd=str(tmp_path),
            )
        assert isinstance(r, subprocess.CompletedProcess)

    def test_filesystem_mutation_is_refused(self, tmp_path):
        with pytest.raises(ObserveWriteError, match="effects.write_text"):
            with no_writes():
                effects.write_text(str(tmp_path / "f.txt"), "nope")

    def test_scratch_space_stays_available(self):
        with no_writes():
            d = effects.mkdtemp(prefix="rlsbl-preview-apply-test-")
            effects.rmtree(d, ignore_errors=True)

    def test_observation_scratch_is_real_and_is_cleaned_up(self):
        """The clone an observation runs needs a parent that really exists."""
        import os

        with no_writes():
            d = effects.mkdtemp(prefix="rlsbl-preview-apply-test-")
            assert os.path.isdir(d), (
                "observation's scratch directory must be REAL: an allowlisted "
                "`git clone` aimed at a synthetic path fails on a missing "
                "parent and the preview reports that instead of a verdict"
            )
            effects.rmtree(d, ignore_errors=True)
            assert not os.path.exists(d)

    def test_scratch_tracking_ends_with_the_block(self, tmp_path):
        with no_writes():
            d = effects.mkdtemp(prefix="rlsbl-preview-apply-test-")
        assert not effects.observe_scratch_owns(d)
        effects.rmtree(d, ignore_errors=True)

    def test_everything_is_restored_after_the_block(self, tmp_path):
        before_run = effects.run
        before_write = effects.write_text
        with no_writes():
            pass
        assert effects.run is before_run
        assert effects.write_text is before_write

    def test_everything_is_restored_after_a_raise(self):
        before_run = effects.run
        with pytest.raises(RuntimeError):
            with no_writes():
                raise RuntimeError("boom")
        assert effects.run is before_run


# ---------------------------------------------------------------------------
# The entry skeleton
# ---------------------------------------------------------------------------


def _recorder():
    seen = []
    return seen, lambda item: seen.append(item.key)


class TestReconcile:
    def test_dry_run_renders_and_applies_nothing(self, capsys):
        seen, apply_item = _recorder()
        p = Preview([VerdictItem(key="k", state="behind", summary="stale.")])
        got = reconcile(
            Reconciler(observe=lambda: p, apply_item=apply_item, show_keys=False),
            dry_run=True,
        )
        assert got is p
        assert seen == []
        assert capsys.readouterr().out == "behind: stale.\n"

    def test_apply_visits_every_item_in_order_and_prints_nothing_itself(self, capsys):
        seen, apply_item = _recorder()
        p = Preview([
            VerdictItem(key="first", state="behind"),
            VerdictItem(key="second", state="virgin"),
        ])
        reconcile(
            Reconciler(observe=lambda: p, apply_item=apply_item, show_keys=True),
            dry_run=False,
        )
        assert seen == ["first", "second"]
        assert capsys.readouterr().out == ""

    def test_observation_runs_under_the_no_writes_guard(self):
        def observe():
            effects.run(["git", "commit", "-m", "sneaky"], capture_output=True)
            return Preview([])

        with pytest.raises(ObserveWriteError):
            reconcile(
                Reconciler(observe=observe, apply_item=lambda i: None, show_keys=False),
                dry_run=True,
            )

    def test_apply_is_below_the_line_and_may_write(self, tmp_path):
        target = tmp_path / "written.txt"

        def apply_item(item):
            effects.write_text(str(target), "applied\n")

        reconcile(
            Reconciler(
                observe=lambda: single(VerdictItem(key="k", state="behind")),
                apply_item=apply_item,
                show_keys=False,
            ),
            dry_run=False,
        )
        assert target.read_text() == "applied\n"

    def test_observe_must_return_a_preview(self):
        with pytest.raises(TypeError, match="must return a Preview"):
            reconcile(
                Reconciler(
                    observe=lambda: ["not", "a", "preview"],
                    apply_item=lambda i: None,
                    show_keys=False,
                ),
                dry_run=True,
            )


# ---------------------------------------------------------------------------
# The mirror is the one-item case of the shared shape
# ---------------------------------------------------------------------------


class TestMirrorUsesTheSharedSkeleton:
    """The mirror's plan output must come out of the shared renderer."""

    def test_mirror_has_no_private_plan_printer(self):
        from rlsbl.commands.monorepo import mirror_cmd

        assert not hasattr(mirror_cmd, "print_plan")

    @pytest.mark.parametrize("state,expected", [
        (
            dict(state="converged", split_sha="a" * 40, split_ancestry_sha="a" * 40),
            "converged: mirror is up to date (split aaaaaaaaaaaa, "
            "scaffold layer present).\n",
        ),
        (
            dict(state="behind", split_sha="b" * 40, split_ancestry_sha="a" * 40),
            "behind: a new split is available.\n"
            "  old split: aaaaaaaaaaaa\n"
            "  new split: bbbbbbbbbbbb\n"
            "  apply would force-push the new split (with lease) and "
            "re-scaffold.\n",
        ),
        (
            dict(state="scaffold_missing", split_sha="a" * 40,
                 split_ancestry_sha="a" * 40, behind=False),
            "scaffold-missing: tip is the current bare split commit "
            "aaaaaaaaaaaa with no scaffold layer.\n"
            "  apply would add the scaffold commit and push.\n",
        ),
        (
            dict(state="scaffold_missing", split_sha="b" * 40,
                 split_ancestry_sha="a" * 40, behind=True),
            "scaffold-missing (and behind): tip is a bare split commit "
            "aaaaaaaaaaaa, older than current split bbbbbbbbbbbb.\n"
            "  apply would force-push the new split (with lease) and scaffold.\n",
        ),
        (
            dict(state="virgin", split_sha="a" * 40),
            "remote-missing-or-empty: mirror at git@host:o/r.git is virgin.\n"
            "  apply would push split aaaaaaaaaaaa and scaffold CI.\n",
        ),
    ])
    def test_plan_text_is_unchanged_by_the_migration(self, state, expected):
        """Byte-for-byte the output the mirror printed before the lift."""
        from rlsbl.commands.monorepo.mirror_cmd import MirrorPlan, verdict_item

        item = verdict_item(
            MirrorPlan(**state), "git@host:o/r.git", "packages/mylib", "mylib"
        )
        assert _render(single(item), show_keys=False) == expected

    def test_contract_violation_keeps_its_remediation_block(self):
        from rlsbl.commands.monorepo.mirror_cmd import MirrorPlan, verdict_item

        plan = MirrorPlan(
            state="contract_violated",
            split_sha="a" * 40,
            remote_tip="c" * 40,
            foreign_commits=[("d" * 40, ["src/hand_authored.py"])],
        )
        text = _render(
            single(verdict_item(plan, "git@host:o/r.git", "packages/mylib", "mylib")),
            show_keys=False,
        )
        assert text.startswith(
            "contract-violated: foreign commit(s) detected on the mirror.\n"
        )
        assert "  - commit dddddddddddd touches non-scaffold paths: "\
               "src/hand_authored.py" in text
        assert "packages/mylib" in text

    def test_unknown_state_still_renders_honestly(self):
        from rlsbl.commands.monorepo.mirror_cmd import MirrorPlan, verdict_item

        item = verdict_item(
            MirrorPlan(state="martian", split_sha="a" * 40), "r", "p", "mylib"
        )
        assert _render(single(item), show_keys=False) == "unknown state: martian\n"

    def test_item_key_is_the_project_name_and_data_is_the_plan(self):
        from rlsbl.commands.monorepo.mirror_cmd import MirrorPlan, verdict_item

        plan = MirrorPlan(state="virgin", split_sha="a" * 40)
        item = verdict_item(plan, "r", "p", "mylib")
        assert item.key == "mylib" and item.data is plan
