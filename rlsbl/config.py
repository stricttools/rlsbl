"""Project configuration loading with layered precedence: CLI flags override project-level .rlsbl/config.json which overrides user-level defaults."""

import json
import os
import sys


def load_env_file(path):
    """Load KEY=VALUE pairs from a file into os.environ.

    Supports ~ expansion. Ignores comments (#) and blank lines.
    Strips surrounding quotes from values.
    """
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        print(f"Warning: env file not found: {path}", file=sys.stderr)
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            # Strip surrounding quotes
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            os.environ[key] = value


def _project_config():
    """Resolve project config path at call time (respects cwd changes)."""
    return os.path.join(".rlsbl", "config.json")

USER_CONFIG = os.path.expanduser("~/.rlsbl/config.json")


def read_json_config(path):
    """Safely read a JSON file, returning {} on missing or malformed."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def should_tag(flags):
    """Returns True if tagging is enabled, checking flag > project > user > default."""
    # CLI flag takes highest precedence
    if flags.get("no-tag"):
        return False

    # Project-level config
    project = read_json_config(_project_config())
    if "tag" in project:
        return bool(project["tag"])

    # User-level config
    user = read_json_config(USER_CONFIG)
    if "tag" in user:
        return bool(user["tag"])

    # Default: tagging enabled
    return True


def read_project_config():
    """Read .rlsbl/config.json, return dict or empty dict if missing/malformed."""
    return read_json_config(_project_config())


def read_deploy_config():
    """Read and validate deploy targets from project config. Returns (targets, errors)."""
    from rlsbl.deploy import validate_deploy_config

    config = read_project_config()
    targets = config.get("deploy", [])
    if not targets:
        return [], []
    errors = validate_deploy_config(targets)
    return targets, errors


def get_publish_config(target_name):
    """Read per-target publish config from .rlsbl/config.json.

    Returns a dict like {"local": True, "token_var": "PYPI_TOKEN"}.
    Returns empty dict if no config exists for this target.
    """
    config = read_project_config()
    publish = config.get("publish", {})
    if not isinstance(publish, dict):
        return {}
    target_config = publish.get(target_name, {})
    return target_config if isinstance(target_config, dict) else {}


def validate_publish_config(config, target_name):
    """Validate per-target publish config, especially the ``assets`` schema.

    Raises ``ValueError`` if:
    - ``assets`` is ``true`` but ``max_asset_size_mb`` is missing.
    - ``max_asset_size_mb`` is present but not a positive integer.
    """
    publish = config.get("publish", {})
    if not isinstance(publish, dict):
        return
    target_cfg = publish.get(target_name, {})
    if not isinstance(target_cfg, dict):
        return

    assets_enabled = target_cfg.get("assets", False)
    max_size = target_cfg.get("max_asset_size_mb")

    if max_size is not None:
        if not isinstance(max_size, int) or max_size <= 0:
            raise ValueError(
                f"publish.{target_name}.max_asset_size_mb must be a positive integer, "
                f"got {max_size!r}"
            )

    if assets_enabled and max_size is None:
        raise ValueError(
            f"publish.{target_name}.assets is true but max_asset_size_mb is not set. "
            f"Add publish.{target_name}.max_asset_size_mb (positive integer, in MB)."
        )


def get_changelog_validation_config():
    """Read changelog validation config from .rlsbl/config.json.

    Returns the batch_limits section as a dict like
    {"max_commits_per_entry": 5, "max_entries_per_commit": 2,
     "exclusions": [{"reason": "...", "commits": [...],
                     "entries": [{"version": "...", "line": N}]}]}.

    Each exclusion object has a required "reason" string for audit
    purposes; "commits" and "entries" are optional lists silencing the
    corresponding batch_size_commits and batch_size_entries violations.

    Returns an empty dict if no config or malformed.
    """
    config = read_project_config()
    batch_limits = config.get("batch_limits", {})
    if not isinstance(batch_limits, dict):
        return {}
    return batch_limits



def write_project_config(key, value):
    """Write or update a key in .rlsbl/config.json (creates dir if needed)."""
    os.makedirs(os.path.dirname(_project_config()), exist_ok=True)
    existing = read_json_config(_project_config())
    existing[key] = value
    with open(_project_config(), "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2)
        f.write("\n")
