"""The ``rlsbl rewrite`` command group: working-tree rewrites, previewed first.

Each command in this group changes source or manifest files in the CURRENT
working tree -- nothing is pushed, tagged, published or sent anywhere. They
share one contract, and it is the reason they live together:

* **Observe, then preview, then apply.**  The plan is built by reading the
  tree, under :func:`rlsbl.preview_apply.no_writes`, and ``--dry-run`` renders
  that plan instead of performing it.  The plan is per FILE (or per dependency
  entry), and every item carries its **occurrence count**.
* **The counts are the contract.**  An apply re-derives each item's count from
  disk immediately before writing and refuses when it disagrees with the count
  the preview reported.  A tree that moved between preview and apply is a hard
  abort with nothing further written -- never a silent partial sweep against
  content nobody previewed.

Both commands are classified ``mutating`` but NOT ``consequential``: the writes
are local, fully previewable, and revertible with git.

This package deliberately re-exports nothing.  Each command's handler in
``rlsbl/__init__.py`` imports its own submodule, so the group's modules stay
independent of one another and a test patches a dispatch target at the module
that defines it.
"""
