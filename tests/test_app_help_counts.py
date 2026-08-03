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


def _parse_subcommand_sentence(help_text):
    m = re.search(r"Provides (\d+) [a-z ]*subcommands: ([^.]+)\.", help_text)
    assert m, f"group help lacks the subcommand sentence: {help_text!r}"
    return int(m.group(1)), [n.strip() for n in m.group(2).split(",")]


def test_release_group_help_lists_every_subcommand():
    """Regression: the literal said '9 subcommands' and omitted `reconcile`."""
    group = app._groups["release"]
    count, names = _parse_subcommand_sentence(group.help)
    assert count == len(group.commands)
    assert names == list(group.commands)
    assert "reconcile" in names


def test_monorepo_group_help_lists_every_subcommand_and_subgroup():
    """Regression: the literal said '16 monorepo subcommands' with 18 live."""
    group = app._groups["monorepo"]
    count, names = _parse_subcommand_sentence(group.help)
    assert count == len(group.commands)
    assert names == list(group.commands)

    subs = re.search(r"Plus (\d+) subgroups?: ([^.]+)\.", group.help)
    assert subs, f"monorepo help lacks the subgroup sentence: {group.help!r}"
    assert int(subs.group(1)) == len(group._groups)
    assert [n.strip() for n in subs.group(2).split(",")] == list(group._groups)


def test_no_group_help_carries_a_hand_written_subcommand_count():
    """Any group help stating a count must state the derived one."""
    for name, group in app._groups.items():
        for m in re.finditer(r"(\d+) (?:[a-z]+ )?subcommands", group.help):
            assert int(m.group(1)) == len(group.commands), (
                f"{name} group help claims {m.group(1)} subcommands but "
                f"{len(group.commands)} are registered"
            )
