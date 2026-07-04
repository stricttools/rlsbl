Title: Clarify yank vs prerelease vs deprecation semantics

`rlsbl release yank` currently does three things that are conceptually distinct:

1. **Yank** -- in PyPI/npm terms, this means removing a published version from the registry so `pip install` / `npm install` can no longer resolve it. rlsbl does NOT do this.

2. **Mark as pre-release** -- GitHub's `prerelease: true` flag on a Release. Signals the version is not production-ready. Semantically different from yanking a previously-stable release.

3. **Deprecation** -- adding a notice to the release notes saying "don't use this." Informational, no mechanical effect.

`rlsbl release yank` conflates all three: it sets `prerelease: true` AND adds a deprecation notice. But these are different operations with different meanings:

- A release that was never published (e.g., CI failed) should be deletable or marked, not "yanked"
- A release that WAS published but has a critical bug should be yanked from registries (PyPI/npm) AND flagged on GitHub
- A pre-release (alpha/beta/rc) is not a yank -- it's an intentional pre-release channel

Consider splitting into distinct operations:
- `rlsbl release yank <version>` -- registry-level removal (PyPI yank, npm unpublish)
- `rlsbl release deprecate <version> --reason "..."` -- soft flag on GitHub Release (notice + prerelease flag)
- `rlsbl release delete <version>` -- remove GitHub Release entirely (for releases that were never published)
- Pre-release status should come from the release flow itself (`preid`), not from yanking

Triggered by: selfdoc v0.25.0 was tagged and a GitHub Release was created, but CI failed so it was never published to PyPI. The only applicable action is "delete" or "deprecate with a note," but `rlsbl release yank` is the only tool available and it semantically overpromises.
