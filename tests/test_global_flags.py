"""The framework owns --dry-run/--approve-consequential/--quiet/--verbose.

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

RESERVED_FLAG_NAMES = ("dry-run", "approve-consequential", "quiet", "verbose", "yes")
RESERVED_PARAMS = ("dry_run", "approve_consequential", "quiet", "verbose", "yes")


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


class TestReservedFlagsAfterTheCommand:
    """`rlsbl <command> --dry-run` must work, not just `rlsbl --dry-run <command>`.

    Every documented rlsbl invocation writes the framework-owned flags AFTER the
    command name (`rlsbl release run --no-allow-dirty --watch
    --approve-consequential`). strictcli recognizes them anywhere in argv, so this works with no argv
    rewriting on rlsbl's side; `main()` carried a hoisting shim until then.
    These tests pin the property, not the removed shim.
    """

    def test_flag_after_the_command_reaches_the_framework(self):
        result = app.test(["monorepo", "remove", "some/path", "--dry-run"])
        assert result.exit_code == 1
        assert "does not support --dry-run" in result.stderr

    def test_flag_after_a_nested_subcommand_reaches_the_framework(self):
        result = app.test(["release", "init", "--dry-run"])
        assert result.exit_code == 1
        assert "does not support --dry-run" in result.stderr

    def test_approve_consequential_after_the_command_is_not_unknown(self):
        """The exact shape the RLSBL protocol prescribes."""
        result = app.test(["status", "--approve-consequential"])
        assert "unknown flag" not in result.stderr

    def test_yes_is_gone_and_stays_gone(self):
        """`yes` is a banned flag name; nothing in rlsbl may resurrect it."""
        result = app.test(["status", "--yes"])
        assert "unknown flag" in result.stderr

    def test_variadic_extraction_leaves_a_post_command_flag_in_place(
        self, monkeypatch
    ):
        """Extraction reads argv[1] and rebuilds argv around it.

        It must not consume or reorder the reserved flag: strictcli picks it up
        from the command region on its own.
        """
        import rlsbl

        monkeypatch.setattr(
            "sys.argv", ["rlsbl", "commit", "--dry-run", "-m", "x", "--", "a.txt"],
        )
        variadic = rlsbl._extract_variadic_args()
        assert variadic == ["a.txt"]
        assert __import__("sys").argv == [
            "rlsbl", "commit", "--dry-run", "-m", "x",
        ]

    def test_a_file_named_like_a_reserved_flag_stays_a_file(self, monkeypatch):
        """`rlsbl commit -- --approve-consequential` names a file, however badly."""
        import rlsbl

        monkeypatch.setattr(
            "sys.argv",
            ["rlsbl", "commit", "-m", "x", "--", "--approve-consequential", "a.txt"],
        )
        variadic = rlsbl._extract_variadic_args()
        assert variadic == ["--approve-consequential", "a.txt"]
        assert __import__("sys").argv == ["rlsbl", "commit", "-m", "x"]
