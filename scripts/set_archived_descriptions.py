#!/usr/bin/env python3
"""Removed. This script no longer does anything: it always exits 1 with instructions.

Authoring reviewed descriptions into archived release files is now
``rlsbl release backfill --overrides <path>``. The overrides file is TOML
rather than JSON, one ``[versions."X.Y.Z"]`` table per version carrying a
``description`` and an optional ``context`` -- so the reviewed text sits in the
same document format as the archives it is written into, and ``context``
(which this script could never set) rides along with it.

The command applies the overrides BEFORE any derivation, so a version whose
description an operator has reviewed never gets a reconstructed one, and a
version the file names that the repository does not have is a hard error rather
than a silent no-op.

The file stays as this stub so a saved command line or an older document names
something that says where the work went, rather than failing with "no such
file".
"""

import sys

MESSAGE = """\
error: scripts/set_archived_descriptions.py was removed.

Authoring descriptions into archived release files is now part of the backfill:

    rlsbl release backfill --overrides descriptions.toml --dry-run
    rlsbl release backfill --overrides descriptions.toml --approve-consequential

The file is TOML, not JSON:

    [versions."0.1.0"]
    description = "What this release was."
    context = \"\"\"
    Optional, multiline, why.
    \"\"\"
"""


def main(argv=None):
    print(MESSAGE, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
