"""Lint configuration loading that reads per-project rule overrides and severity settings from .rlsbl/lint/ TOML configuration files."""

import os
import tomllib
from dataclasses import dataclass, field

from ..errors import ConfigError


# Per-language default forbidden imports
_DEFAULT_FORBIDDEN = {
    "python": [
        "argparse", "click", "typer",
        "flask", "fastapi", "django",
        "uvicorn", "granian", "starlette",
        "tornado", "bottle",
    ],
    "go": [
        "net/http",
        "github.com/spf13/cobra",
        "github.com/urfave/cli",
    ],
    "npm": [
        "express", "koa", "hono",
        "commander", "yargs",
    ],
}


@dataclass
class LanguageLintConfig:
    forbidden_imports: list[str] = field(default_factory=list)
    allowed_imports: list[str] = field(default_factory=list)
    stdout_enabled: bool = True
    stdout_ignore: list[str] = field(default_factory=list)
    entry_point_enabled: bool = True
    entry_point_ignore: list[str] = field(default_factory=list)
    exclude_patterns: list[str] = field(default_factory=list)


def _require_table(data: dict, key: str, config_path: str) -> dict:
    """Return ``data[key]`` as a TOML table (dict), or the default empty dict.

    An absent section keeps the empty-dict default. A section that is
    *present* but not a table is a hard error -- never silently coerced.
    """
    if key not in data:
        return {}
    value = data[key]
    if not isinstance(value, dict):
        raise ConfigError(
            f"Invalid lint config {config_path}: [{key}] must be a table "
            f"(TOML section), got {value!r} (type {type(value).__name__})."
        )
    return value


def _read_bool(section: dict, full_key: str, config_path: str, default: bool, *, key: str) -> bool:
    """Read ``section[key]`` as a bool. Absent keeps *default*; wrong type errors."""
    if key not in section:
        return default
    value = section[key]
    if not isinstance(value, bool):
        raise ConfigError(
            f"Invalid lint config {config_path}: {full_key} must be a boolean, "
            f"got {value!r} (type {type(value).__name__})."
        )
    return value


def _read_str_list(section: dict, full_key: str, config_path: str, default: list, *, key: str) -> list:
    """Read ``section[key]`` as a list of strings.

    Absent keeps *default*. A present value that is not a list, or a list
    containing a non-string element, is a hard error -- never silently used
    as-is (e.g. a bare string where a list is required).
    """
    if key not in section:
        return default
    value = section[key]
    if not isinstance(value, list):
        raise ConfigError(
            f"Invalid lint config {config_path}: {full_key} must be a list of "
            f"strings, got {value!r} (type {type(value).__name__})."
        )
    for item in value:
        if not isinstance(item, str):
            raise ConfigError(
                f"Invalid lint config {config_path}: {full_key} must contain only "
                f"strings, found {item!r} (type {type(item).__name__})."
            )
    return value


def load_language_config(
    project_path: str,
    language: str,
    releasable_lint_dir: str | None = None,
) -> LanguageLintConfig:
    """Read the lint config for *language*, falling back to defaults if missing.

    Two-level resolution (member wins wholesale, mirroring the config.json
    precedent): the member-level ``.rlsbl/lint/<language>.toml`` is used when it
    exists; otherwise, when the project belongs to a releasable and
    ``releasable_lint_dir`` is supplied, the releasable-level
    ``<releasable>/lint/<language>.toml`` is used. If neither exists, the
    per-language defaults apply.
    """
    member_path = os.path.join(project_path, ".rlsbl", "lint", f"{language}.toml")
    defaults = _DEFAULT_FORBIDDEN.get(language, [])

    config_path = member_path
    if not os.path.isfile(config_path) and releasable_lint_dir:
        rel_path = os.path.join(releasable_lint_dir, f"{language}.toml")
        if os.path.isfile(rel_path):
            config_path = rel_path

    if not os.path.isfile(config_path):
        return LanguageLintConfig(forbidden_imports=list(defaults))

    try:
        with open(config_path, "rb") as f:
            data = tomllib.load(f)
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise ConfigError(
            f"Invalid lint config {config_path}: {exc}"
        ) from exc

    fi_section = _require_table(data, "forbidden-imports", config_path)
    forbidden = _read_str_list(fi_section, "forbidden-imports.modules", config_path, list(defaults), key="modules")
    allowed = _read_str_list(fi_section, "forbidden-imports.allow", config_path, [], key="allow")

    stdout_section = _require_table(data, "stdout", config_path)
    stdout_enabled = _read_bool(stdout_section, "stdout.enabled", config_path, True, key="enabled")
    stdout_ignore = _read_str_list(stdout_section, "stdout.ignore", config_path, [], key="ignore")

    ep_section = _require_table(data, "entry-point", config_path)
    ep_enabled = _read_bool(ep_section, "entry-point.enabled", config_path, True, key="enabled")
    ep_ignore = _read_str_list(ep_section, "entry-point.ignore", config_path, [], key="ignore")

    files_section = _require_table(data, "files", config_path)
    exclude = _read_str_list(files_section, "files.exclude", config_path, [], key="exclude")

    return LanguageLintConfig(
        forbidden_imports=forbidden,
        allowed_imports=allowed,
        stdout_enabled=stdout_enabled,
        stdout_ignore=stdout_ignore,
        entry_point_enabled=ep_enabled,
        entry_point_ignore=ep_ignore,
        exclude_patterns=exclude,
    )


def load_parser_setting(project_path: str) -> str:
    """Read parser type from .rlsbl/lint.toml, defaulting to 'ast'.

    A missing file keeps the documented ``"ast"`` default. Malformed TOML
    or a present-but-invalid ``parser`` value is a hard error
    (:class:`ConfigError`) naming the file and the problem -- never
    silently defaulted.
    """
    lint_toml = os.path.join(project_path, ".rlsbl", "lint.toml")
    if not os.path.isfile(lint_toml):
        return "ast"
    try:
        with open(lint_toml, "rb") as f:
            data = tomllib.load(f)
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise ConfigError(
            f"Invalid lint config {lint_toml}: {exc}"
        ) from exc
    parser = data.get("parser", "ast")
    if parser not in ("ast", "regex"):
        raise ConfigError(
            f"Invalid parser in {lint_toml}: {parser!r}. "
            f"Must be one of ('ast', 'regex')."
        )
    return parser

