"""Verify the framework owns --dry-run/--yes/--quiet/--verbose, and rlsbl does not.

The four names are reserved by strictcli: it registers them on every app,
strips them from argv in its pre-scan, and delivers their values on the
Context.  rlsbl declared the first three as app globals until the effects
regime landed; re-declaring any of them is now a registration-time hard error,
and no handler may take them as parameters.
"""

import inspect

from rlsbl import app

# No handler is permitted to carry **_kwargs (VAR_KEYWORD) any longer -- the
# cycle-end state is zero exemptions.
KWARGS_EXEMPT_HANDLERS = set()

RESERVED_FLAG_NAMES = ("dry-run", "yes", "quiet", "verbose")
RESERVED_PARAMS = ("dry_run", "yes", "quiet", "verbose")


def _iter_project_commands():
    """Yield (command_path, Command) for every command rlsbl itself defines."""

    def walk(commands, groups, prefix):
        for name, cmd in commands.items():
            if getattr(cmd.handler, "__module__", None) != "rlsbl":
                continue
            yield (prefix + name, cmd)
        for gname, group in groups.items():
            yield from walk(group.commands, group._groups, prefix + gname + " ")

    yield from walk(app._commands, app._groups, "")


def _iter_project_handlers():
    """Yield (command_path, handler) for every command defined by rlsbl.

    Walks the live registry: top-level ``app._commands`` plus each group's
    ``.commands`` and nested ``._groups`` recursively. Framework-injected
    commands (e.g. the built-in ``check``) live in another module and are
    skipped -- only handlers whose ``__module__`` is ``rlsbl`` are our own.
    """

    def walk(commands, groups, prefix):
        for name, cmd in commands.items():
            handler = cmd.handler
            if getattr(handler, "__module__", None) != "rlsbl":
                continue
            yield (prefix + name, handler)
        for gname, group in groups.items():
            yield from walk(group.commands, group._groups, prefix + gname + " ")

    yield from walk(app._commands, app._groups, "")


def test_app_declares_no_reserved_flag():
    """rlsbl must not re-declare a framework-owned name at app level."""
    flag_names = {f.name for f in app.flags}
    assert flag_names.isdisjoint(RESERVED_FLAG_NAMES)


def test_no_command_declares_a_reserved_flag():
    """Nor at command level: the ban applies at every level."""
    offenders = {}
    for path, cmd in _iter_project_commands():
        clash = sorted({f.name for f in cmd.flags} & set(RESERVED_FLAG_NAMES))
        if clash:
            offenders[path] = clash
    assert not offenders, f"commands declaring reserved flags: {offenders}"


def test_no_handler_takes_a_reserved_parameter():
    """The values arrive on the Context, never as handler kwargs.

    A handler that still named ``dry_run`` would be asking strictcli for a flag
    it strips before parsing -- the parameter could only ever be filled by a
    command flag of the same (banned) name.
    """
    offenders = {}
    for path, handler in _iter_project_handlers():
        names = set(inspect.signature(handler).parameters)
        clash = sorted(names & set(RESERVED_PARAMS))
        if clash:
            offenders[path] = clash
    assert not offenders, f"handlers taking reserved params: {offenders}"


def test_reserved_flags_reach_the_context():
    """The framework really delivers them, and rlsbl handlers really read them."""
    result = app.test(["--dry-run", "monorepo", "remove", "some/path"])
    assert result.exit_code == 1
    assert "does not support --dry-run" in result.stderr


def test_no_handler_swallows_kwargs():
    """No rlsbl command handler may carry a ``**_kwargs`` (VAR_KEYWORD) param.

    A ``**_kwargs`` catch-all disables strictcli's registration-time signature
    validation and silently absorbs flags/args/globals the handler forgot to
    declare. Only the handlers in ``KWARGS_EXEMPT_HANDLERS`` are permitted to
    keep it (their registration blocks are removed in later phases); that set
    must shrink to empty as those phases land.
    """
    offenders = []
    for path, handler in _iter_project_handlers():
        has_var_kw = any(
            p.kind is inspect.Parameter.VAR_KEYWORD
            for p in inspect.signature(handler).parameters.values()
        )
        if has_var_kw and handler.__name__ not in KWARGS_EXEMPT_HANDLERS:
            offenders.append(f"{path} ({handler.__name__})")
    assert not offenders, f"handlers carrying **_kwargs: {sorted(offenders)}"


class TestReservedFlagHoisting:
    """`rlsbl <command> --dry-run` must work, not just `rlsbl --dry-run <command>`.

    strictcli's reserved-flag pre-scan stops at the first non-flag token, so it
    only sees the four framework flags BEFORE the command name -- while every
    documented rlsbl invocation writes them after it. `main()` moves them to
    the front before dispatch; these tests pin that, including its interaction
    with the variadic-argument extraction that also rewrites argv.
    """

    def test_flag_after_the_command_is_hoisted(self):
        from rlsbl import hoist_reserved_flags

        assert hoist_reserved_flags(
            ["rlsbl", "release", "run", "--allow-dirty", "--yes"]
        ) == ["rlsbl", "--yes", "release", "run", "--allow-dirty"]

    def test_negated_forms_hoist_too(self):
        from rlsbl import hoist_reserved_flags

        assert hoist_reserved_flags(["rlsbl", "status", "--no-quiet"]) == [
            "rlsbl", "--no-quiet", "status",
        ]

    def test_flag_already_leading_is_left_alone(self):
        from rlsbl import hoist_reserved_flags

        argv = ["rlsbl", "--dry-run", "status"]
        assert hoist_reserved_flags(argv) == argv

    def test_nothing_after_a_bare_separator_is_touched(self):
        """`rlsbl commit -- --yes` names a file, however badly."""
        from rlsbl import hoist_reserved_flags

        argv = ["rlsbl", "commit", "-m", "x", "--", "--yes", "a.txt"]
        assert hoist_reserved_flags(argv) == argv

    def test_variadic_extraction_still_sees_the_command(self, monkeypatch):
        """Extraction reads argv[1]; hoisting must not run before it.

        `rlsbl commit --dry-run -m x -- file` reached the hoist first once, and
        the file list was then parsed as if `--dry-run` were the command.
        """
        import rlsbl

        monkeypatch.setattr(
            "sys.argv", ["rlsbl", "commit", "--dry-run", "-m", "x", "--", "a.txt"],
        )
        variadic = rlsbl._extract_variadic_args()
        assert variadic == ["a.txt"]
        assert rlsbl.hoist_reserved_flags(__import__("sys").argv) == [
            "rlsbl", "--dry-run", "commit", "-m", "x",
        ]
