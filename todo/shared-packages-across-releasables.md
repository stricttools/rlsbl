Title: Support shared packages across releasables

A package in a monorepo can only belong to one releasable (or none). If two releasables both need the same internal package, the only options are:

1. Put the shared package in one releasable and make the other depend on it (couples the releasables)
2. Duplicate the package (terrible)
3. Make the shared package its own releasable (creates a third PyPI package for internal glue)

None of these are good. The natural model is: a package belongs to one releasable for versioning/changelog purposes, but can be bundled into multiple releasables' published artifacts. Or: shared packages exist outside any releasable but get force-included into whichever wheels need them.

Use case: selfdoc monorepo where `core/`, `html/`, `markdown/` are shared between the `selfdoc` and `selfblog` releasables. Currently impossible without making one depend on the other or creating a third releasable.
