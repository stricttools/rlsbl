"""Lint configuration loading that reads per-project rule overrides and severity settings from .rlsbl/lint/ TOML configuration files."""

import os
import tomllib
from dataclasses import dataclass, field


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


def load_language_config(project_path: str, language: str) -> LanguageLintConfig:
    """Read .rlsbl/lint/<language>.toml, falling back to defaults if missing."""
    config_path = os.path.join(project_path, ".rlsbl", "lint", f"{language}.toml")
    defaults = _DEFAULT_FORBIDDEN.get(language, [])

    if not os.path.isfile(config_path):
        return LanguageLintConfig(forbidden_imports=list(defaults))

    try:
        with open(config_path, "rb") as f:
            data = tomllib.load(f)
    except Exception:
        return LanguageLintConfig(forbidden_imports=list(defaults))

    fi_section = data.get("forbidden-imports", {})
    forbidden = fi_section.get("modules", list(defaults))
    allowed = fi_section.get("allow", [])

    stdout_section = data.get("stdout", {})
    stdout_enabled = stdout_section.get("enabled", True)
    stdout_ignore = stdout_section.get("ignore", [])

    ep_section = data.get("entry-point", {})
    ep_enabled = ep_section.get("enabled", True)
    ep_ignore = ep_section.get("ignore", [])

    files_section = data.get("files", {})
    exclude = files_section.get("exclude", [])

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
    """Read parser type from .rlsbl/lint.toml, defaulting to 'ast'."""
    lint_toml = os.path.join(project_path, ".rlsbl", "lint.toml")
    if not os.path.isfile(lint_toml):
        return "ast"
    try:
        with open(lint_toml, "rb") as f:
            data = tomllib.load(f)
        parser = data.get("parser", "ast")
        if parser not in ("ast", "regex"):
            return "ast"
        return parser
    except Exception:
        return "ast"

