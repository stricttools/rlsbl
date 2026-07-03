"""Scope adapter for the strictcli check system, interpreting colon-separated scope tokens to pre-filter checks by project path and workspace context.

Interprets colon-separated scope tokens to pre-filter the check context
before the check function runs.  Registered via ``app.set_scope_adapter()``.

Tokens:
- ``workspace``       -- requires WorkspaceCheckContext
- ``non_dev_only``    -- filter projects to exclude dev-only
- ``non_dev_node``    -- filter projects to exclude dev_node
- ``library``         -- filter projects to only library projects
- ``releasable``      -- filter projects to only releasable projects
- ``push``            -- requires push_stdin is not None
"""

from dataclasses import replace

from strictcli import CheckResult

from ..check_context import WorkspaceCheckContext
from ..workspace import project_is_dev_only, project_is_releasable


def scope_adapter(ctx, scope_string):
    """Interpret *scope_string* and return a filtered context or a CheckResult.

    Each colon-separated token is processed left-to-right.  If a token
    produces a CheckResult (skip/fail), processing stops and that result
    is returned.  Otherwise, the token transforms the context (e.g. by
    filtering ``ctx.projects``).

    Unknown tokens are silently ignored (pass through).
    """
    tokens = scope_string.split(":")

    for token in tokens:
        result = _apply_token(ctx, token)
        if isinstance(result, CheckResult):
            return result
        ctx = result

    return ctx


def _apply_token(ctx, token):
    """Apply a single scope token to *ctx*.

    Returns either a new context (filtered copy) or a CheckResult.
    """
    if token == "workspace":
        if not isinstance(ctx, WorkspaceCheckContext):
            return CheckResult("skip", "not a monorepo workspace")
        return ctx

    if token == "non_dev_only":
        if not isinstance(ctx, WorkspaceCheckContext):
            return ctx
        filtered = [p for p in ctx.projects if not project_is_dev_only(p)]
        return replace(ctx, projects=filtered)

    if token == "non_dev_node":
        if not isinstance(ctx, WorkspaceCheckContext):
            return ctx
        filtered = [p for p in ctx.projects if not p.get("dev_node", False)]
        return replace(ctx, projects=filtered)

    if token == "library":
        if not isinstance(ctx, WorkspaceCheckContext):
            return ctx
        filtered = [p for p in ctx.projects if p.get("library")]
        return replace(ctx, projects=filtered)

    if token == "releasable":
        if not isinstance(ctx, WorkspaceCheckContext):
            return ctx
        filtered = [p for p in ctx.projects if project_is_releasable(p)]
        return replace(ctx, projects=filtered)

    if token == "push":
        if ctx.push_stdin is None:
            return CheckResult("skip", "not in push context")
        return ctx

    # Unknown token: pass through
    return ctx
