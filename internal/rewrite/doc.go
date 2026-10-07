// Package rewrite holds the working-tree rewrites of the `rlsbl rewrite`
// group: renaming a Go module path across a repository, renaming a
// standalone project's published identity, and turning path- and
// workspace-sourced Python dependencies into registry floors.
//
// Every rewrite shares one contract, through internal/previewapply:
//
//   - observe, then preview, then apply. The plan is built by reading the
//     tree under the observer, which cannot write, and --dry-run renders it
//     instead of performing it. The plan is per file (or per dependency),
//     and every item carries its occurrence count;
//   - the counts are the contract. An apply derives each item's count from
//     disk again immediately before writing and refuses when it differs
//     from the count the preview reported, so content nobody previewed is
//     never swept;
//   - an aborted apply names what it already wrote: there is no rollback,
//     the working tree is git's to restore.
//
// The sweeps are local and revertible with git; renaming a project's
// identity is consequential, because it records pending identities in the
// lifecycle-and-license record, and declaring that a published identity
// changes is a human's call.
package rewrite
