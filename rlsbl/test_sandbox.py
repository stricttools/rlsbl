"""The ``test_sandbox`` config family: sandboxed test-runner distribution.

rlsbl is the *distributor* of the testisolation floor's outer layer. The floor
itself (the pytest plugin, the Go env-hygiene module) lives in the testisolation
repo; what ships from here is the bubblewrap runner script that the floor's
bare-run refusal points at, plus the adoption check that keeps an adopted repo
honest.

A project opts in by switching the ``rlsbl:test-sandbox`` option on
(``rlsbl options set rlsbl:test-sandbox --current on --ideal on --reason ...``)
and declaring a ``test_sandbox`` section in ``.rlsbl/config.json``::

    "test_sandbox": {
      "runner_path": "scripts/test.sh",
      "command": "uv sync --offline && uv run --offline pytest",
      "default_args": "-q -n auto",
      "caches": ["uv", "go", "python_user_base"],
      "prewarm": ["scripts/test-prewarm.sh"],
      "extra_env": {"LEGACY_SANDBOX_VAR": "1"},
      "ci_workflows": [".github/workflows/ci.yml"],
      "carry_ignored": [".rlsbl-test-tools"]
    }

``rlsbl scaffold`` then renders ``templates/shared/test-sandbox.sh.tpl`` to
``runner_path`` (executable), and the ``testisolation-floor`` check enforces that
the runner exists and that every declared CI workflow actually invokes it.

Design notes:

* **The option is the choice.** The section is required while
  ``rlsbl:test-sandbox`` is on and refused while it is off, and no key has a
  runtime fallback: the section is either absent (unadopted -- the check skips
  visibly) or present and complete (the check enforces).
* **``caches`` is a closed enum.** Only ecosystems the template genuinely
  implements are accepted; an unimplemented name is a hard error, never a
  silently ignored bind.
* **``prewarm`` runs OUTSIDE the sandbox**, from the project root, with network
  access, before the sandbox is entered. It exists so a repo can warm a
  toolchain cache that the offline in-sandbox build then hits. Each entry is a
  shell command line; a non-zero exit aborts the run.
* **The working copy is git's view of the tree**: tracked files, untracked
  files that are not ignored, and ``.git``. Gitignored bulk (experiment
  scratch, build output) stays out, since it can outgrow the temp filesystem.
  A gitignored path the suite does read -- typically what a ``prewarm``
  command stages -- is declared in ``carry_ignored`` and copied when present.
"""

from __future__ import annotations

import os
import re
import tomllib

from .errors import ConfigError

CONFIG_KEY = "test_sandbox"

#: The environment variable the runner exports; the testisolation floor reads it
#: to lift its bare-run refusal. Not configurable -- the floor and the runner
#: must agree, and a repo that needs a second (legacy) name declares it under
#: ``extra_env``.
SANDBOX_ENV_VAR = "TESTISOLATION_SANDBOX"

#: Toolchain caches the runner template knows how to bind. Closed on purpose:
#: an ecosystem is listed here only once the template implements its binds and
#: its offline environment.
CACHE_NAMES = ("uv", "go", "python_user_base")

REQUIRED_KEYS = ("runner_path", "command")
OPTIONAL_KEYS = (
    "default_args",
    "caches",
    "prewarm",
    "extra_env",
    "ci_workflows",
    "carry_ignored",
)
ALLOWED_KEYS = REQUIRED_KEYS + OPTIONAL_KEYS

TEMPLATE_NAME = "test-sandbox.sh.tpl"

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ENV_VALUE_RE = re.compile(r"^[A-Za-z0-9_.:/=+-]+$")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _require_relative(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{CONFIG_KEY}.{label} must be a non-empty string")
    if os.path.isabs(value) or value.startswith("~"):
        raise ConfigError(
            f"{CONFIG_KEY}.{label} must be a path relative to the project "
            f"root, got {value!r}"
        )
    parts = value.replace("\\", "/").split("/")
    if ".." in parts:
        raise ConfigError(
            f"{CONFIG_KEY}.{label} must stay inside the project root "
            f"(no '..' segments), got {value!r}"
        )


def _reject_single_quote(value, label):
    """Reject values the runner template embeds in a single-quoted shell literal.

    ``command`` and ``default_args`` are rendered as ``'<value>'`` in the
    generated script; a single quote inside would break out of the literal.
    Escaping it silently would be worse than refusing it -- the config would no
    longer read like the shell it becomes.
    """
    if "'" in value:
        raise ConfigError(
            f"{CONFIG_KEY}.{label} must not contain a single quote: the runner "
            "embeds it in a single-quoted shell literal. Use double quotes, or "
            "move the command into a script and reference that script."
        )


def _require_str_list(value, label):
    if not isinstance(value, list):
        raise ConfigError(
            f"{CONFIG_KEY}.{label} must be a list of strings, "
            f"got {type(value).__name__}"
        )
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(
                f"{CONFIG_KEY}.{label} entries must be non-empty strings"
            )


def validate_test_sandbox_config(config):
    """Validate the ``test_sandbox`` section of a project config.

    Absent section is valid (the project has not adopted the sandbox runner).
    Present-but-malformed is a hard error: unknown keys, missing required keys,
    absolute or escaping paths, and unknown cache names all raise.

    Raises:
        ConfigError on any violation.
    """
    section = (config or {}).get(CONFIG_KEY)
    if section is None:
        return

    if not isinstance(section, dict):
        raise ConfigError(
            f"{CONFIG_KEY} must be a map of settings, "
            f"got {type(section).__name__}"
        )

    unknown = sorted(set(section) - set(ALLOWED_KEYS))
    if unknown:
        raise ConfigError(
            f"{CONFIG_KEY} has unknown key(s): {', '.join(unknown)}. "
            f"Valid keys: {', '.join(ALLOWED_KEYS)}."
        )

    missing = [k for k in REQUIRED_KEYS if k not in section]
    if missing:
        raise ConfigError(
            f"{CONFIG_KEY} is missing required key(s): {', '.join(missing)}. "
            f"Every {CONFIG_KEY} section must declare {', '.join(REQUIRED_KEYS)}."
        )

    _require_relative(section["runner_path"], "runner_path")

    command = section["command"]
    if not isinstance(command, str) or not command.strip():
        raise ConfigError(
            f"{CONFIG_KEY}.command must be a non-empty string -- the command "
            "run inside the sandbox (e.g. "
            '"uv sync --offline && uv run --offline pytest").'
        )
    _reject_single_quote(command, "command")

    if "default_args" in section:
        if not isinstance(section["default_args"], str):
            raise ConfigError(f"{CONFIG_KEY}.default_args must be a string")
        _reject_single_quote(section["default_args"], "default_args")

    if "caches" in section:
        _require_str_list(section["caches"], "caches")
        unknown_caches = [c for c in section["caches"] if c not in CACHE_NAMES]
        if unknown_caches:
            raise ConfigError(
                f"{CONFIG_KEY}.caches accepts only caches the runner template "
                f"implements. Unknown: {', '.join(sorted(set(unknown_caches)))}. "
                f"Valid names: {', '.join(CACHE_NAMES)}."
            )

    if "prewarm" in section:
        _require_str_list(section["prewarm"], "prewarm")

    if "ci_workflows" in section:
        _require_str_list(section["ci_workflows"], "ci_workflows")
        for path in section["ci_workflows"]:
            _require_relative(path, "ci_workflows entry")

    if "carry_ignored" in section:
        _require_str_list(section["carry_ignored"], "carry_ignored")
        for path in section["carry_ignored"]:
            _require_relative(path, "carry_ignored entry")
            _reject_single_quote(path, "carry_ignored entry")

    if "extra_env" in section:
        extra = section["extra_env"]
        if not isinstance(extra, dict):
            raise ConfigError(
                f"{CONFIG_KEY}.extra_env must be a map of environment-variable "
                f"name to value, got {type(extra).__name__}"
            )
        for name, value in extra.items():
            if not _ENV_NAME_RE.match(str(name)):
                raise ConfigError(
                    f"{CONFIG_KEY}.extra_env key {name!r} is not a valid "
                    "environment variable name"
                )
            if not isinstance(value, str) or not value.strip():
                raise ConfigError(
                    f"{CONFIG_KEY}.extra_env['{name}'] must be a non-empty string"
                )
            if not _ENV_VALUE_RE.match(value):
                raise ConfigError(
                    f"{CONFIG_KEY}.extra_env['{name}'] value {value!r} contains "
                    "characters the runner cannot pass through unquoted. "
                    "Allowed: letters, digits, and _.:/=+-"
                )
            if name == SANDBOX_ENV_VAR:
                raise ConfigError(
                    f"{CONFIG_KEY}.extra_env must not redeclare "
                    f"{SANDBOX_ENV_VAR}: the runner always exports it (that is "
                    "the variable the testisolation floor reads)."
                )


# ---------------------------------------------------------------------------
# Scaffold wiring
# ---------------------------------------------------------------------------


def get_section(config, project_dir):
    """Return the validated ``test_sandbox`` section while the
    ``rlsbl:test-sandbox`` option is on for *project_dir*, or None while it is
    off.

    Raises :class:`ConfigError` when the two disagree: a section present
    while the option is off (nothing reads it), or absent while it is on.
    """
    from .options import setting_problem

    problem = setting_problem("test-sandbox", config, project_dir)
    if problem is not None:
        raise ConfigError(problem)
    validate_test_sandbox_config(config)
    return (config or {}).get(CONFIG_KEY)


def runner_mapping(config, project_dir):
    """Return the scaffold mapping that emits the runner, or None.

    The mapping carries ``executable: True`` so ``apply_plans`` chmods the
    rendered script 0755 -- a runner that is not executable is not a runner.
    """
    section = get_section(config, project_dir)
    if section is None:
        return None
    return {
        "template": TEMPLATE_NAME,
        "target": section["runner_path"],
        "executable": True,
    }


def _root_relative(runner_path):
    """Return the path from the runner's directory back to the project root."""
    parent = os.path.dirname(runner_path.replace("\\", "/"))
    if not parent or parent == ".":
        return "."
    return "/".join([".."] * len([p for p in parent.split("/") if p]))


def template_vars(config, project_dir):
    """Return the ``sandbox*`` template variables for the runner template.

    Returns an empty dict when the project has not adopted the family, so
    callers can unconditionally merge the result into their vars dict.
    """
    section = get_section(config, project_dir)
    if section is None:
        return {}

    caches = list(section.get("caches") or [])
    prewarm = list(section.get("prewarm") or [])
    extra_env = dict(section.get("extra_env") or {})

    extra_env_lines = "\n".join(
        f"  --setenv {name} {value}" for name, value in sorted(extra_env.items())
    )
    prewarm_lines = "\n".join(prewarm)
    carry_words = " ".join(
        f"'{path}'" for path in section.get("carry_ignored") or []
    )

    return {
        "sandboxRunnerPath": section["runner_path"],
        "sandboxRootRelative": _root_relative(section["runner_path"]),
        "sandboxCommand": section["command"],
        "sandboxDefaultArgs": section.get("default_args", ""),
        "sandboxCaches": " ".join(caches),
        "sandboxPrewarm": prewarm_lines,
        "sandboxExtraEnv": extra_env_lines,
        "sandboxCarryIgnored": carry_words,
        # The dev-overlay block is uv-specific (it excludes packages from
        # `uv sync` and installs them editable), so it is rendered only for
        # repos that declared the uv cache. Everyone else gets the runner
        # unchanged, byte for byte.
        "sandboxUvOverlays": "1" if "uv" in caches else "",
    }


# ---------------------------------------------------------------------------
# Adoption detection + floor verdict (consumed by the testisolation-floor check)
# ---------------------------------------------------------------------------

PLUGIN_DIST_NAME = "testisolation"
_PYTEST_INI_SECTION = ("tool", "pytest", "ini_options")


def _read_pyproject(project_root):
    path = os.path.join(str(project_root), "pyproject.toml")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None


def _requirement_names(entries):
    """Yield the bare distribution name of each PEP 508 requirement string."""
    for entry in entries or []:
        if not isinstance(entry, str):
            continue
        name = re.split(r"[<>=!~\[;\s]", entry.strip(), maxsplit=1)[0]
        if name:
            yield name.lower().replace("_", "-")


def plugin_declared(project_root):
    """True when the testisolation pytest plugin is a declared dependency.

    Looks at runtime dependencies, PEP 735 dependency groups, and optional
    dependency extras -- wherever a repo happens to put its test tooling.
    """
    data = _read_pyproject(project_root)
    if data is None:
        return False
    buckets = []
    project = data.get("project") or {}
    buckets.append(project.get("dependencies"))
    for extra in (project.get("optional-dependencies") or {}).values():
        buckets.append(extra)
    for group in (data.get("dependency-groups") or {}).values():
        buckets.append(group)
    for bucket in buckets:
        if PLUGIN_DIST_NAME in _requirement_names(bucket):
            return True
    return False


def _pytest_ini_options(project_root):
    data = _read_pyproject(project_root)
    if data is None:
        return {}
    node = data
    for key in _PYTEST_INI_SECTION:
        node = node.get(key) if isinstance(node, dict) else None
        if node is None:
            return {}
    return node if isinstance(node, dict) else {}


def sandbox_required_declared(project_root):
    """True when the suite declares ``testisolation_sandbox_required`` truthy.

    A repo that says its full suite must go through a sandbox runner, but has
    no runner distributed to it, is broken -- that is the state this reports.
    """
    value = _pytest_ini_options(project_root).get("testisolation_sandbox_required")
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


class FloorVerdict:
    """Result of evaluating the testisolation floor for one project."""

    def __init__(self, *, adopted, skip_reason=None, problems=None, notes=None):
        self.adopted = adopted
        self.skip_reason = skip_reason
        self.problems = list(problems or [])
        self.notes = list(notes or [])

    @property
    def ok(self):
        return not self.problems


def evaluate_floor(config, project_root):
    """Evaluate the testisolation floor's adoption state for a project.

    Returns a :class:`FloorVerdict`. Unadopted repos (``rlsbl:test-sandbox``
    off and no testisolation plugin dependency) come back with
    ``adopted=False`` and a skip reason. Adopted repos come back with the
    concrete broken states, if any.
    """
    from .options import OFF, option_value

    root = str(project_root)
    sandbox = option_value("test-sandbox", root)
    has_plugin = plugin_declared(root)

    if sandbox.value == OFF and not has_plugin:
        return FloorVerdict(
            adopted=False,
            skip_reason=(
                f"testisolation floor not adopted (rlsbl:test-sandbox is off "
                f"({sandbox.source}), no testisolation dependency)"
            ),
        )
    try:
        section = get_section(config, root)
    except ConfigError as e:
        return FloorVerdict(adopted=True, problems=[str(e)])

    problems = []
    notes = []

    if section is None:
        # Plugin adopted without a runner. Only broken when the suite itself
        # declares that it requires one.
        if sandbox_required_declared(root):
            from .options import member_scope, set_command

            problems.append(
                "pyproject.toml sets testisolation_sandbox_required = true, but "
                f"rlsbl:test-sandbox is off ({sandbox.source}), so no sandbox "
                "runner is distributed to this repo. Switch it on ("
                + set_command(
                    "test-sandbox", "on", "on",
                    "<why this project runs its suite in the sandbox>",
                    scope=member_scope(root),
                )
                + f"), declare the '{CONFIG_KEY}' section in "
                ".rlsbl/config.json, and run `rlsbl scaffold`."
            )
        else:
            notes.append(
                "testisolation plugin adopted; no sandbox runner declared "
                "(testisolation_sandbox_required is false)"
            )
        return FloorVerdict(adopted=True, problems=problems, notes=notes)

    runner_path = section["runner_path"]
    runner_abs = os.path.join(root, runner_path)
    if not os.path.isfile(runner_abs):
        problems.append(
            f"'{CONFIG_KEY}' declares runner_path '{runner_path}', but that "
            "file does not exist. Run `rlsbl scaffold` to emit the runner."
        )
    elif not os.access(runner_abs, os.X_OK):
        problems.append(
            f"the sandbox runner '{runner_path}' is not executable. Run "
            "`rlsbl scaffold` (or chmod +x it)."
        )
    else:
        notes.append(f"runner {runner_path} present")

    for workflow in section.get("ci_workflows") or []:
        wf_abs = os.path.join(root, workflow)
        if not os.path.isfile(wf_abs):
            problems.append(
                f"'{CONFIG_KEY}.ci_workflows' names '{workflow}', which does "
                "not exist."
            )
            continue
        try:
            with open(wf_abs, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError as e:  # pragma: no cover - unreadable workflow
            problems.append(f"could not read '{workflow}': {e}")
            continue
        if runner_path not in content:
            problems.append(
                f"CI workflow '{workflow}' does not invoke the sandbox runner "
                f"'{runner_path}'. The repo declared that this workflow runs "
                "the suite through the sandbox; it does not."
            )
        else:
            notes.append(f"{workflow} invokes {runner_path}")

    return FloorVerdict(adopted=True, problems=problems, notes=notes)
