"""Pipeline introspection -- generates raw table data for all registered pipeline types showing auth method, env vars, and ecosystem."""

from . import PIPELINE_TYPES
from .base import TokenPipeline, CredentialPipeline

HEADERS = ["Type", "Auth method", "Required env vars", "Ecosystem"]

# Ecosystem descriptions for each pipeline type
_ECOSYSTEMS: dict[str, str] = {
    "npm": "npm registry",
    "pypi": "Python Package Index",
    "go": "Go module proxy",
    "cargo": "crates.io",
    "deno": "JSR (Deno)",
    "hex": "hex.pm (Elixir)",
    "maven": "Maven Central / Gradle",
    "maven-central": "Maven Central (Central Portal)",
    "docker": "Container registry",
    "cloudflare-pages": "Cloudflare Pages",
}


def _auth_method(cls: type) -> str:
    """Determine the auth method from the pipeline class hierarchy."""
    if issubclass(cls, TokenPipeline):
        return "token"
    if issubclass(cls, CredentialPipeline):
        return "credential"
    return "none"


def _default_env_vars(cls: type) -> str:
    """Extract default env var names from the pipeline class."""
    if issubclass(cls, TokenPipeline):
        var = getattr(cls, "_default_token_var", "")
        return var if var else ""
    if issubclass(cls, CredentialPipeline):
        uvar = getattr(cls, "_default_username_var", "")
        pvar = getattr(cls, "_default_password_var", "")
        parts = [v for v in (uvar, pvar) if v]
        return ", ".join(parts) if parts else ""
    return ""


def generate_pipeline_table_data() -> tuple[list[str], list[list[str]]]:
    """Generate raw data for a markdown table of all registered pipeline types.

    Returns ``(headers, rows)`` where *headers* is a 4-element list and
    each row is a 4-element list of strings, sorted alphabetically by
    pipeline type name.
    """
    rows: list[list[str]] = []

    for type_name, cls in sorted(PIPELINE_TYPES.items()):
        row = [
            type_name,
            _auth_method(cls),
            _default_env_vars(cls),
            _ECOSYSTEMS.get(type_name, type_name),
        ]
        rows.append(row)

    return list(HEADERS), rows
