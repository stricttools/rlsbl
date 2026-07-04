# monorepo init resolves to ancestor project instead of CWD

## Problem

Running `rlsbl monorepo init` from a fresh subdirectory beneath an existing rlsbl project resolves to the ancestor project (via `_require_project_root`'s upward walk) and creates the workspace there, not at CWD.

`_require_project_root` walks parent directories looking for `.rlsbl/` or `.rlsbl-monorepo/` markers. When the user creates a new directory under an existing rlsbl-managed project and runs `monorepo init` there, the walker finds the parent project's marker and uses that as the project root. The workspace files then get created in the wrong location.

## Context

The Phase 9 bootstrap fix handled the marker-requirement chicken-and-egg problem (you need markers to run commands, but you need commands to create markers). However, it did not address the ancestry walk issue: `_require_project_root` still walks upward unconditionally, so a new subdirectory beneath an existing project inherits the parent rather than initializing in-place.

## Severity

Low. This only affects greenfield monorepo initialization beneath an existing rlsbl project. Workaround: initialize from a directory that is not a descendant of any rlsbl project, then move it.

## Possible fix

`monorepo init` should check CWD first before walking upward. If CWD has no markers and the user explicitly ran `init`, assume CWD is the intended root. Only walk upward for non-init commands that need an existing project.
