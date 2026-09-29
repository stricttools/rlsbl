# Publish gate emits duplicate `case` branches for a multi-member releasable

## Context

The generated `Publish Router` resolves which project's CI checks to verify
from the tag ref, with a shell `case` built one branch per workspace project:

```sh
case "$tag_ref" in
  "pkg-a@v"*)  regex='^(pkg\-a\-ci) / ' ;;
  "pkg-b@v"*)  regex='^(pkg\-b\-ci) / ' ;;
  ...
esac
```

In explicit releasable mode every member of a releasable is tagged with the
**releasable's** prefix, not its own project name. The tag for a releasable
named `foo` with members `a-foo`, `b-foo`, `c-foo` is `foo@vX.Y.Z` for all
three.

## Problem

The generator still emits one branch per *project*, so all three branches get
the same pattern:

```sh
case "$tag_ref" in
  "foo@v"*)  regex='^(a\-foo\-ci) / ' ;;
  "foo@v"*)  regex='^(b\-foo\-ci) / ' ;;   # unreachable
  "foo@v"*)  regex='^(c\-foo\-ci) / ' ;;   # unreachable
  *) echo "::error::... (known prefixes: foo@v, foo@v, foo@v)." ;;
esac
```

`case` takes the first match, so:

1. **The gate verifies one member's CI and calls it a day.** Members 2..N of the
   releasable are never checked before publishing. A red or missing CI job on
   those members does not block their publish job. This is exactly the guarantee
   the gate exists to provide, and it is silently weaker the moment a releasable
   has more than one member.
2. The failure message lists the same prefix N times, which reads as a
   generator bug to anyone debugging a ref mismatch.

Observed on a releasable with three members (pypi + go + npm). The publish run
was green and published all three, so nothing surfaced the hole — it only shows
up by reading the generated YAML.

## Solution

Group by tag prefix instead of by project: one `case` branch per distinct
prefix, whose regex alternates over every member's CI job prefix.

```sh
"foo@v"*)  regex='^(a\-foo\-ci|b\-foo\-ci|c\-foo\-ci) / ' ;;
```

The downstream jq already groups check runs by name and requires each to be
green, so an alternation makes the gate demand all members' jobs — which is
what the releasable "run-everything hook" (every member's paths filter is
anchored on the releasable CHANGELOG.md) already guarantees will exist on a
release commit. The two mechanisms then agree: the router runs every member's
CI, and the gate insists on every member's CI.

Also dedupe the `known prefixes:` list in the error message.

### Alternative considered

Keep per-project branches and let the first one win, documenting that the gate
is per-representative. Rejected: it makes a member's red CI publishable, and
the whole point of the check is that no artifact ships from a commit whose
tests failed. There is no `--skip` for this and there should not be one.

## Affected files

- the publish-router generation (the `case` builder and the `known prefixes`
  message)
- `rlsbl/ci_checks.py` if the gate's jq counterpart makes the same per-project
  assumption (the two are documented as needing to stay in lockstep)
- tests: generate a router for a workspace with a 3-member releasable and assert
  one branch, three alternatives, and no duplicate patterns

## Effort

Small — a grouping in the generator plus a test. Correctness-relevant: today a
multi-member releasable publishes with only one member's CI verified.
