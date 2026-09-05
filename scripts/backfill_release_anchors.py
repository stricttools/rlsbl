#!/usr/bin/env python3
"""Removed. This script no longer does anything: it always exits 1 with instructions.

The release-archive backfill is a first-class command now, ``rlsbl release
backfill``, and its engine is :mod:`rlsbl.release_backfill`. The command does
everything this script did and more -- it completes every required field an
archive lacks rather than only stamping the strictspec gate, recovers a
description from the commit subjects in a version's tag range when the Release
notes and CHANGELOG.md have none, adopts a version tag no store records, reads
an archive's ``shipped_as`` so a renamed project's historical tags are
accounted for, takes an operator's reviewed descriptions through
``--overrides``, and refuses to write anything at all while a tag in the
namespace is unexplained.

The file stays as this stub so a saved command line, a fleet runbook or an
older document names something that says where the work went, rather than
failing with "no such file".
"""

import sys

MESSAGE = """\
error: scripts/backfill_release_anchors.py was removed.

The release-archive backfill is a command now. From the repository you want to
backfill:

    rlsbl release backfill --dry-run
    rlsbl release backfill --approve-consequential

Read the preview first: the command lists every tag it cannot account for and
refuses the apply while one remains. `--overrides <path>` takes a TOML file of
reviewed descriptions (one [versions."X.Y.Z"] table per version), which
replaces scripts/set_archived_descriptions.py.
"""


def main(argv=None):
    print(MESSAGE, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
