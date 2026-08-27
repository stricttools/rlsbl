"""Render a CI router in a test the way ``rlsbl monorepo sync`` renders one.

``_generate_router`` no longer derives paths filters from the projects handed
to it: a member's filter depends on territories it does not own (its
dependencies', and -- for the root member -- every other member's), so the
derivation needs the whole workspace and lives in
:class:`rlsbl.router_filters.RouterFilters`.  This helper composes the two so a
test that only cares about job inlining does not have to.
"""

import os

from rlsbl.commands.monorepo.sync import _generate_router
from rlsbl.router_filters import RouterFilters


def generate_router(projects, releasables=None, root=None, all_projects=None):
    """Render ci-router.yml content for *projects*.

    *all_projects* is the workspace the filters are derived from; it defaults
    to *projects*, which is what most tests want (every member they declare
    also carries CI).  A test exercising a member with no CI workflow passes
    the full member list explicitly.

    *root* is the workspace root the derivation reads root-level manifests and
    lockfiles from.  It defaults to the current directory, which under the
    test sandbox is the test's own empty temporary directory.
    """
    members = projects if all_projects is None else all_projects
    filters = RouterFilters(root or os.getcwd(), members, releasables)
    return _generate_router(projects, filters)
