"""Hook helpers: content hashing, template hash lookup, hook emptiness check, hook runner."""

import hashlib
import os

from .validate import HookError

# Lazily computed on first access via _get_pre_release_template_hashes().
_PRE_RELEASE_TEMPLATE_HASHES = None


def _compute_content_hash(content):
    """SHA-256 of content with trailing whitespace stripped."""
    return hashlib.sha256(content.rstrip().encode("utf-8")).hexdigest()


def _get_pre_release_template_hashes():
    """Return a frozenset of content hashes for known scaffold template versions of pre-release.sh.

    Currently there is only one version (the template has never changed),
    but using a set follows the same pattern as hook_hashes.py, making it
    easy to add historical versions later.
    """
    global _PRE_RELEASE_TEMPLATE_HASHES
    if _PRE_RELEASE_TEMPLATE_HASHES is not None:
        return _PRE_RELEASE_TEMPLATE_HASHES

    template_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "templates", "shared", "hooks", "pre-release.sh.tpl",
    )
    with open(template_path, "r", encoding="utf-8") as f:
        template_content = f.read()

    # V1: original scaffold template (before the comment was updated to
    # describe the override behavior).
    _V1 = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "# Project-specific pre-release checks.\n"
        "# Built-in checks (tests, lint) run automatically before this hook.\n"
        "# Add custom validation here, e.g.:\n"
        "#   - Check for uncommitted documentation\n"
        "#   - Verify external service connectivity\n"
        "#   - Run integration tests not covered by the test suite\n"
    )

    _PRE_RELEASE_TEMPLATE_HASHES = frozenset({
        _compute_content_hash(template_content),
        _compute_content_hash(_V1),
    })
    return _PRE_RELEASE_TEMPLATE_HASHES


def _is_hook_effectively_empty(hook_path):
    """Check if a pre-release hook file is effectively empty (matches scaffold template).

    Returns True (hook is boilerplate / not customized) when:
    - The hook file does not exist
    - The hook file's content hash matches a known scaffold template version

    Returns False (hook has been customized) when:
    - The hook exists and its content does not match any known template version
    """
    if not os.path.exists(hook_path):
        return True

    with open(hook_path, "r", encoding="utf-8") as f:
        hook_content = f.read()

    hook_hash = _compute_content_hash(hook_content)
    return hook_hash in _get_pre_release_template_hashes()


def run_release_hook(hook_name, hook_path, project_dir, env, timeout):
    """Run a release hook script (pre-checks or pre-release).

    hook_name: human-readable name for error messages (e.g. "pre-checks").
    hook_path: absolute path to the shell script.
    project_dir: working directory for the hook.
    env: environment dict to pass to the subprocess.
    timeout: seconds before the hook is killed.

    Raises HookError on non-zero exit or timeout.
    """
    # Late-bind subprocess through the package namespace so tests can patch
    # rlsbl.commands.release.subprocess and the mock is visible here.
    from . import subprocess as _subprocess

    if not os.path.exists(hook_path):
        return

    hook_path = os.path.abspath(hook_path)
    try:
        _subprocess.run(
            ["bash", hook_path], env=env, check=True,
            timeout=timeout, cwd=project_dir,
        )
    except _subprocess.CalledProcessError as e:
        raise HookError(
            f"{hook_name} hook exited with code {e.returncode}."
        ) from e
    except _subprocess.TimeoutExpired as e:
        raise HookError(
            f"{hook_name} hook timed out after {timeout}s."
        ) from e
