# Record a Go module path move within the same repository

## Context

When a GitHub repository is transferred to another owner (for example from a
personal account to an organization), its Go module path changes from
`github.com/<old-owner>/<name>` to `github.com/<new-owner>/<name>`, while the
repository, its history, and its tags stay the same. GitHub redirects the old
address to the new one.

rlsbl handles this case badly in two ways.

1. **Nothing records the move.** `rlsbl rewrite go-module-path` rewrites
   every `go.mod` and import site but writes no identity-transition event.
   The only command that records a `go-module-path` identity transition is
   `rlsbl rewrite project-name`, which is meant for renames and refuses
   inside a workspace. So a module moved to its new owner leaves no record
   of the old path in `.rlsbl/transitions.jsonl`.
2. **Recording it would make a release check fail forever.** The
   `go-deprecation-published` check, evaluated by
   `evaluate_go_deprecation_published` in `rlsbl/transition_record_followup.py`,
   requires that the old module path publishes a `// Deprecated:` notice, and
   its message tells the operator to release that notice "in the OLD
   repository". After a same-repository transfer there is no old repository:
   both paths are served from one repository and one set of tags. Verified
   against selfdoc, whose module moved to `github.com/stricttools/selfdoc`:
   - the Go module proxy's `@latest` for `github.com/smm-h/selfdoc` returns
     the newest release made under the new path;
   - `go get github.com/smm-h/selfdoc@latest` fails with
     `module declares its path as: github.com/stricttools/selfdoc but was
     required as: github.com/smm-h/selfdoc`, which already names the new
     path;
   - a release declaring the old path is refused by rlsbl's own
     `go-module-identity` check (`rlsbl/go_identity.py`), because the module
     line must match the repository's remote;
   - even if such a release existed, Go reads a module's deprecation from its
     latest version's `go.mod`, so the next new-path release would hide it.

## What to build

- `rlsbl rewrite go-module-path` records one `go-module-path`
  identity-transition event from the old path to the new one, effective from
  the next release, committed separately from the rewrite so a crash between
  them is completed by re-running, the same way `rewrite project-name`
  records its events.
- The event distinguishes a move within the same repository (the old path's
  repository address redirects to this repository) from a move to a
  different repository. How it is established must read inputs the
  repository owns plus a declared network probe, never a guess.
- `go-deprecation-published` passes for a same-repository move without
  demanding a deprecation release, and states why in its result. For a move
  to a different repository it keeps its current requirement.
- The closing message of `rewrite go-module-path` names the remaining steps
  for a same-repository move, which do not include a deprecation release.

## Constraints

- Red-green tests for each change, including one that performs every
  remaining step the closing message prints.
- No escape hatch: the distinction between same-repository and
  different-repository moves is determined, never declared by a flag that
  disables the check.

## Effort

Small to medium: one new event write in the rewrite command, one branch in
the follow-up check, and tests.
