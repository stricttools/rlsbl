# Scaffold customization contract is undocumented (and easy to invert); publish gate silently drops same-named check runs

Two independent findings from a consumer repo's CI rework. Both are
about silent failure shapes in scaffold-managed release safety.

## 1. The scaffold customization contract: customizations live in the GENERATED file; the base must stay pristine — and nothing says so

Mechanics (from `rlsbl/commands/init_cmd.py`'s three-way merge): when
the working file equals the stored base, the plan is `write theirs` —
the new template replaces the file outright; and after every apply the
stored base is replaced with pristine template output. Consequence: a
customization copied INTO `.rlsbl/bases/` makes ours == base, so the
next `rlsbl scaffold` against an updated template DESTROYS the
customization. A customization survives re-scaffolds only when it lives
in the generated file while the base stays template output (then
`ours != base` and the three-way merge preserves the hunk).

This is a reasonable contract, but it is stated nowhere, and the
intuitive mental model ("the base records what I want preserved") is
exactly backwards — a consumer repo's maintainers planned a CI change on
the inverted model and would have had their matrix, a workflow rename,
and a goreleaser edit all silently reverted at the next scaffold if the
implementation had not read the merge code first.

At least: a "customizing scaffold-managed files" paragraph in the
scaffold docs stating the contract and the failure mode. Better: `rlsbl
scaffold` (or `rlsbl check`) detects a base that differs from any known
template version — the signature of a hand-edited base — and errors,
making the wrong model impossible rather than documented.

## 2. The publish gate's `group_by(.name) | last` silently discards same-named check runs across workflows

The scaffolded `publish.yml` gate collects check runs and reduces with
`group_by(.name) | map(sort_by(.started_at,.id) | last)`. Check-run
names come from JOB names. If two push-triggered workflows both have a
job named `test` (or `build`) — which happens the moment a maintainer
hand-adds a second CI workflow next to the scaffolded one, and was the
live state of a consumer repo — the gate keeps only the newest run of
that name and SILENTLY ignores the other workflow's verdict. A failing
suite in the older-started workflow cannot block publish.

The consumer repo fixed it locally by consolidating to one workflow and
renaming the other's jobs, but the hazard is structural in the template:
the gate treats name collisions as retries (which is right WITHIN one
workflow re-run) and cannot tell them apart from distinct jobs.

Options to consider: (a) the gate errors when a name group contains runs
from more than one workflow id (distinguishable via the check run's
workflow/app linkage) instead of reducing; (b) key the group by
(workflow, name) and require every group green; (c) at minimum a loud
comment in the template plus a `rlsbl check` that flags duplicate job
names across push-triggered workflows. (a)/(b) are the structural fixes;
(c) is the cheap tripwire.

## Effort

Item 1 doc-only is trivial; the base-drift check is small. Item 2 (a) or
(b) is a contained jq/logic change in one template plus scaffold
re-rollout; (c) is small.
