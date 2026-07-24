"""Verify that --dry-run, --yes, and --quiet are registered as global App flags."""

import inspect

from rlsbl import app

# Handlers that still carry **_kwargs (VAR_KEYWORD). Phases 3-5 remove these
# registration blocks entirely; this list must shrink to empty by the end of
# that work. Identified by handler function __name__.
KWARGS_EXEMPT_HANDLERS = {
    "cmd_mono_absorb",
    "cmd_mono_extract",
    "cmd_mono_extract_releasable",
}

GLOBAL_FLAG_PARAMS = ("dry_run", "yes", "quiet")


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


def test_app_has_three_global_flags():
    flag_names = {f.name for f in app.flags}
    assert "dry-run" in flag_names
    assert "yes" in flag_names
    assert "quiet" in flag_names


def test_global_flag_types():
    by_name = {f.name: f for f in app.flags}
    assert by_name["dry-run"].type is bool
    assert by_name["yes"].type is bool
    assert by_name["quiet"].type is bool


def test_yes_flag_has_short_alias():
    by_name = {f.name: f for f in app.flags}
    assert by_name["yes"].short == "y"


def test_all_handlers_declare_global_flag_params():
    """Every rlsbl command handler must explicitly name each global-flag param.

    A handler that omits ``dry_run``/``yes``/``quiet`` only works today because
    of a trailing ``**_kwargs`` swallowing the global's value -- and swallowing
    is exactly the bug (a command ignoring ``--dry-run`` and acting for real
    during a preview). We assert the parameters are named explicitly.
    """
    offenders = {}
    for path, handler in _iter_project_handlers():
        names = set(inspect.signature(handler).parameters)
        missing = [p for p in GLOBAL_FLAG_PARAMS if p not in names]
        if missing:
            offenders[path] = missing
    assert not offenders, f"handlers missing global-flag params: {offenders}"


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
