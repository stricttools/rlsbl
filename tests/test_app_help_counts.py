"""The app help's command-count sentence must match the live registry.

The counts are derived at registration time (rlsbl/__init__.py), never
hand-maintained. This test independently recounts the registry and parses
the numbers out of the help sentence, so any regression to hand-written
literals -- or a registration added after the derivation point -- fails here.
"""

import re

from rlsbl import app


def _leaf_count(groups):
    """Recursively count leaf commands across nested group registries."""
    return sum(len(g.commands) + _leaf_count(g._groups) for g in groups.values())


def test_help_command_counts_match_registry():
    m = re.search(
        r"Ships (\d+) commands organized into (\d+) top-level commands and "
        r"(\d+) command groups \(([^)]*)\)",
        app.help,
    )
    assert m, f"app help lacks the command-count sentence: {app.help!r}"

    total = len(app._commands) + _leaf_count(app._groups)
    assert int(m.group(1)) == total
    assert int(m.group(2)) == len(app._commands)
    assert int(m.group(3)) == len(app._groups)
    assert m.group(4) == ", ".join(app._groups)
