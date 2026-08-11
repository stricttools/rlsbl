# Documentation steps skip when the tool is absent, and never run in CI

Two questions here. The first is a defect with an obvious shape. The second is
genuinely open and is posed to whoever picks this up, not answered.

## The defect

`_run_selfdoc_gen` (`rlsbl/commands/release/validate.py:1004`) and
`_run_selfdoc_check` (`:1052`) both begin with `require_tool("selfdoc",
fatal=False)`. When the tool is not installed, each prints a note and returns
`True`.

The release then continues and completes. Its outcome is indistinguishable from
a release where documentation was regenerated and verified — same exit code,
same completion summary, same tag, same publish. Only a line of stdout,
scrolled past hours earlier, records that two pipeline steps did nothing.

This reaches further than it looks, because the same pattern appears at
`rlsbl/pipelines/cloudflare_pages.py:24` and
`rlsbl/commands/release/publish.py:45`, so a publish step can also no-op on a
machine missing a tool.

The shape of the fix follows from rlsbl's own doctrine — a step that cannot run
should be a hard error rather than a non-blocking notice, because a release that
skipped verification is not the same artifact as one that passed it. What needs
deciding is only whether the hard error is unconditional, or whether a project
may declare that it has no documentation step at all, so that "no selfdoc.json"
and "selfdoc.json but no tool" stop being handled by the same branch.

Note that this is *detected* correctly today: the code only reaches these lines
because a `selfdoc.json` was found. So the project has declared it has
documentation, and the tooling to verify that documentation is absent. That is
precisely the condition that should stop a release.

## The open question

**Should documentation checks run in continuous integration, rather than only
locally during a release?**

Today they run in exactly one place: the release pipeline, on whichever machine
runs `rlsbl release run`. There is no CI invocation of these checks anywhere in
the fleet — no scaffold template emits one, and no workflow calls one.

Arguments for adding them to CI:

- The check would run somewhere it cannot be skipped, uninstalled, or run with a
  differently-configured toolchain.
- It would hold for a contributor, or for any future automated build, rather
  than only for the person who happens to run releases.
- Every other quality check in the fleet has a CI presence; documentation is the
  exception.

Arguments against:

- It duplicates a step the release already performs, so a repository would run
  it twice for every release.
- Documentation checks read the working tree and, for some rules, compare
  against stored baselines. Whether those baselines are meaningful on a fresh
  clone needs establishing before this is worth doing.
- Some checks may depend on user-level state that a build machine cannot reach —
  which is a design constraint on any future check that carries per-user data,
  not just an inconvenience.

That last point is the reason this is a real question rather than an obvious
yes. Deciding to run these checks in CI constrains what such a check is allowed
to depend on, permanently. Deciding to keep them local leaves them dependent on
one machine being correctly set up, which the defect above shows is not
currently guaranteed.

No decision has been made. Both halves are open, and the first does not depend
on the second.

## Affected files

- `rlsbl/commands/release/validate.py` — the two documentation steps
- `rlsbl/pipelines/cloudflare_pages.py`, `rlsbl/commands/release/publish.py` —
  the same non-blocking-skip pattern in publish paths
- `rlsbl/utils.py` — `require_tool` and its `fatal` parameter
- CI scaffold templates, if the second question is answered yes

## Effort

Small for the defect. The CI question is a fleet-wide change touching every
scaffolded workflow, and should not be started until the constraint it places on
check design is accepted.
