# `rlsbl rewrite go-module-path` mishandles symlinked `go.mod` files

## Context

`rlsbl rewrite go-module-path --from-module <old> --to-module <new>` rewrites every `go.mod` reference and Go import site of a module path across a repository, planning first and then applying. Some repositories hold many fixture modules whose `go.mod` is a symbolic link to one shared `go.mod` (strictgo's `internal/commands/testdata/<fixture>/go.mod -> ../go.mod`, 269 of them).

## Problem

Two defects, both hit renaming `github.com/smm-h/strictgo` to `github.com/stricttools/strictgo` in strictgo, and again renaming its strictcli and go-toml-edit requirements:

1. **The apply stops partway.** The plan counts each symlinked `go.mod` as its own file. After the apply rewrites the link's target, the next link to the same target now holds zero occurrences, so the apply refuses with "the preview counted 2 occurrence(s) in <path> but it now has 0: it changed between the preview and the apply", leaving the repository half rewritten. Rerunning the command plans from the new state and finishes, sometimes after several reruns.
2. **Links become regular files.** Where the apply does rewrite a path that is a symbolic link, it replaces the link with a regular file holding the rewritten content (`git status` shows `T`, a type change) instead of rewriting the target and leaving the link. strictgo's 269 links had to be restored by hand.

## Solutions

1. **Resolve links before planning.** Plan over the set of distinct real files (each symlinked `go.mod` resolved to its target, counted once), rewrite each real file once, and never write through or replace a link. Pros: both defects go; the counts match what is on disk. Cons: a link whose target lies outside the repository needs a decision (refuse it, naming the link and its target).
2. **Refuse links.** Refuse a repository holding a symlinked `go.mod`, naming each. Pros: simple. Cons: refuses legitimate layouts, such as fixture trees sharing one module file.

Recommended: 1, with red-green tests for both defects (a repository with two links to one `go.mod`: the apply completes in one run, and both stay links).

## Affected files

`rlsbl rewrite go-module-path`'s planning and apply code, and its tests.

## Effort

Small.
