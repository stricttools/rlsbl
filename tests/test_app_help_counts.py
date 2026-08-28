"""The app help's derived counts must match the live registries.

Two families of count appear in rlsbl's help text: how many commands are
registered, and how many release targets exist. Both are derived at
registration time (rlsbl/__init__.py), never hand-maintained. This test
independently recounts each registry and parses the numbers out of the help
sentences, so any regression to hand-written literals -- or a registration
added after the derivation point -- fails here.

The target counts had already drifted before they were derived: the app help
said "17 release targets" while the monorepo group help said "18", and
`dev install` claimed a hand-typed set of targets per install mode.
"""

import re

import pytest

from rlsbl import app
from rlsbl.targets import TARGETS

# The go target reads go.mod and .rlsbl/config.json when handed a real Go
# project, so the support probe must be run against a directory that is not
# one -- the same reason rlsbl/__init__.py probes a non-project path.
NOT_A_PROJECT = "/nonexistent/rlsbl-help-count-probe"


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


def test_monorepo_release_subgroup_help_lists_every_subcommand():
    """Regression: this subgroup's literal said '3 subcommands' by hand.

    It is the one group whose sentence carries a parenthetical per subcommand;
    the count and the names are still the registry's, and the parentheticals
    are checked to be present so the map cannot silently lose one.
    """
    group = app._groups["monorepo"]._groups["release"]
    count, entries = _parse_subcommand_sentence(group.help)
    assert count == len(group.commands)
    assert [e.split(" (")[0] for e in entries] == list(group.commands)
    for entry in entries:
        assert re.fullmatch(r"[a-z-]+ \([^()]+\)", entry), entry


class _StubGroup:
    """The shape `_append_subcommand_sentence` reads off a strictcli group."""

    def __init__(self, commands):
        self.name = "stub"
        self.help = "Stub group."
        self.commands = {name: object() for name in commands}
        self._groups = {}


def test_a_summary_map_that_misses_a_subcommand_is_refused():
    """A subcommand added without a parenthetical raises, never renders bare."""
    from rlsbl import _append_subcommand_sentence

    group = _StubGroup(["run", "init"])
    with pytest.raises(ValueError, match=r"missing \['init'\]"):
        _append_subcommand_sentence(group, "subcommands", summaries={"run": "a"})


def test_a_summary_for_a_removed_subcommand_is_refused():
    from rlsbl import _append_subcommand_sentence

    group = _StubGroup(["run"])
    with pytest.raises(ValueError, match=r"stale \['gone'\]"):
        _append_subcommand_sentence(
            group, "subcommands", summaries={"run": "a", "gone": "b"}
        )


def _dev_install_support(mode):
    return [
        name for name, target in TARGETS.items()
        if target.dev_install_command(NOT_A_PROJECT).get(mode) is not None
    ]


def _dev_uninstall_support():
    names = []
    for name, target in TARGETS.items():
        specs = target.dev_install_command(NOT_A_PROJECT)
        if any(
            (specs.get(mode) or {}).get("uninstall_args_template") is not None
            for mode in ("global", "venv")
        ):
            names.append(name)
    return names


def _parse_count_and_names(help_text, lead):
    m = re.search(lead + r" (\d+) [a-z ]*targets?: ([^.]+)\.", help_text)
    assert m, f"help lacks a {lead!r} target sentence: {help_text!r}"
    return int(m.group(1)), [n.strip() for n in m.group(2).split(",")]


def test_app_help_target_count_matches_the_registry():
    count, names = _parse_count_and_names(app.help, "Covers")
    assert count == len(TARGETS)
    assert names == list(TARGETS)


def test_monorepo_help_target_count_matches_the_registry():
    """Regression: this literal said 18 while the app help said 17."""
    m = re.search(r"Supports all (\d+) release targets", app._groups["monorepo"].help)
    assert m, f"monorepo help lacks the target-count sentence: {app._groups['monorepo'].help!r}"
    assert int(m.group(1)) == len(TARGETS)


def test_dev_install_help_lists_the_targets_each_mode_supports():
    """Regression: the mode target lists were hand-typed beside a '7 targets'."""
    help_text = app._groups["dev"].commands["install"].help

    count, names = _parse_count_and_names(help_text, r"--target global is supported by")
    expected = _dev_install_support("global")
    assert (count, names) == (len(expected), expected)

    count, names = _parse_count_and_names(help_text, r"--target venv is supported by")
    expected = _dev_install_support("venv")
    assert (count, names) == (len(expected), expected)

    count, names = _parse_count_and_names(
        help_text, r"--uninstall reverses a previous install on"
    )
    expected = _dev_uninstall_support()
    assert (count, names) == (len(expected), expected)


def test_no_help_string_carries_a_hand_written_target_count():
    """Every "N ... targets" claim anywhere in the help must be the live count."""
    texts = [app.help]
    for group in app._groups.values():
        texts.append(group.help)
        for command in group.commands.values():
            texts.append(command.help)
    for command in app._commands.values():
        texts.append(command.help)

    for text in texts:
        for m in re.finditer(r"(\d+) release targets", text or ""):
            assert int(m.group(1)) == len(TARGETS), (
                f"help claims {m.group(1)} release targets but "
                f"{len(TARGETS)} are registered: {text!r}"
            )


def _all_groups(groups, prefix=""):
    """Every group and subgroup, as (dotted name, group) pairs."""
    for name, group in groups.items():
        path = f"{prefix}{name}"
        yield path, group
        yield from _all_groups(group._groups, prefix=f"{path}.")


def test_no_group_help_carries_a_hand_written_subcommand_count():
    """Any group help stating a count must state the derived one.

    Subgroups are walked too: `monorepo release` kept a hand-written
    '3 subcommands' precisely because this test only looked one level down.
    """
    for name, group in _all_groups(app._groups):
        for m in re.finditer(r"(\d+) (?:[a-z]+ )?subcommands", group.help):
            assert int(m.group(1)) == len(group.commands), (
                f"{name} group help claims {m.group(1)} subcommands but "
                f"{len(group.commands)} are registered"
            )
