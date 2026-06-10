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


def _project_config(project_root):
    """Resolve project config path at call time.

    Returns an absolute path based on project_root.
    """
    return os.path.join(str(project_root), ".rlsbl", "config.json")

USER_CONFIG = os.path.expanduser("~/.rlsbl/config.json")


def read_json_config(path):
    """Safely read a JSON file, returning {} on missing."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        raise ValueError(f"Malformed JSON in {path}: {e}") from e


def should_tag(flags, config):
    """Returns True if tagging is enabled, checking flag > project > user > default.

    ``config`` is the project config dict (already loaded).
    User-level config is still read from disk.
    """
    # CLI flag takes highest precedence
    if flags.get("no-tag"):
        return False

    # Project-level config
    if "tag" in config:
        return bool(config["tag"])

    # User-level config
    user = read_json_config(USER_CONFIG)
    if "tag" in user:
        return bool(user["tag"])

    # Default: tagging enabled
    return True


def read_project_config(project_root):
    """Read .rlsbl/config.json, return dict or empty dict if missing/malformed."""
    return read_json_config(_project_config(project_root))


def read_deploy_config(config):
    """Read and validate deploy targets from project config dict. Returns (targets, errors)."""
    from rlsbl.deploy import validate_deploy_config

    targets = config.get("deploy", [])
    if not targets:
        return [], []
    errors = validate_deploy_config(targets)
    return targets, errors


def get_changelog_validation_config(config):
    """Read changelog validation config from a project config dict.

    Returns the batch_limits section as a dict like
    {"max_commits_per_entry": 5, "max_entries_per_commit": 2,
     "exclusions": [{"reason": "...", "commits": [...],
                     "entries": [{"version": "...", "line": N}]}]}.

    Each exclusion object has a required "reason" string for audit
    purposes; "commits" and "entries" are optional lists silencing the
    corresponding batch_size_commits and batch_size_entries violations.

    Returns an empty dict if no config or malformed.
    """
    batch_limits = config.get("batch_limits", {})
    if not isinstance(batch_limits, dict):
        return {}
    return batch_limits



def validate_pipelines_config(config):
    """Validate the ``pipelines`` section of a project config.

    Raises ``ValueError`` if:
    - ``pipelines`` is present but not a dict
    - An entry is not a dict
    - An entry is missing ``type`` (str) or ``local`` (bool)
    - ``type`` is not a registered pipeline type
    - ``assets`` is true but ``max_asset_size_mb`` is missing or not a positive int
    - ``custom_assets`` is present but ``max_asset_size_mb`` is missing or not a positive int
    - ``custom_assets`` entries are malformed (missing ``name`` or ``build``)
    """
    from rlsbl.pipelines import PIPELINE_TYPES

    pipelines = config.get("pipelines")
    if pipelines is None:
        return

    if not isinstance(pipelines, dict):
        raise ValueError(
            f"pipelines must be a dict, got {type(pipelines).__name__}"
        )

    for name, entry in pipelines.items():
        if not isinstance(entry, dict):
            raise ValueError(
                f"pipeline '{name}' must be a dict, got {type(entry).__name__}"
            )

        # type is required and must be a registered pipeline type
        if "type" not in entry:
            raise ValueError(f"pipeline '{name}' is missing required key 'type'")
        ptype = entry["type"]
        if not isinstance(ptype, str):
            raise ValueError(
                f"pipeline '{name}'.type must be a string, got {type(ptype).__name__}"
            )
        if ptype not in PIPELINE_TYPES:
            raise ValueError(
                f"pipeline '{name}'.type '{ptype}' is not a registered pipeline type. "
                f"Valid types: {', '.join(sorted(PIPELINE_TYPES.keys())) or '(none registered)'}"
            )

        # local is required and must be a bool
        if "local" not in entry:
            raise ValueError(f"pipeline '{name}' is missing required key 'local'")
        if not isinstance(entry["local"], bool):
            raise ValueError(
                f"pipeline '{name}'.local must be a boolean, got {type(entry['local']).__name__}"
            )

        # assets validation
        if entry.get("assets"):
            max_size = entry.get("max_asset_size_mb")
            if max_size is None:
                raise ValueError(
                    f"pipeline '{name}' has assets=true but max_asset_size_mb is not set"
                )
            if not isinstance(max_size, int) or max_size <= 0:
                raise ValueError(
                    f"pipeline '{name}'.max_asset_size_mb must be a positive integer, "
                    f"got {max_size!r}"
                )

        # custom_assets validation
        custom_assets = entry.get("custom_assets")
        if custom_assets is not None:
            if not isinstance(custom_assets, list):
                raise ValueError(
                    f"pipeline '{name}'.custom_assets must be a list, "
                    f"got {type(custom_assets).__name__}"
                )
            # custom_assets requires max_asset_size_mb
            max_size = entry.get("max_asset_size_mb")
            if max_size is None:
                raise ValueError(
                    f"pipeline '{name}' has custom_assets but max_asset_size_mb is not set"
                )
            if not isinstance(max_size, int) or max_size <= 0:
                raise ValueError(
                    f"pipeline '{name}'.max_asset_size_mb must be a positive integer, "
                    f"got {max_size!r}"
                )
            for i, asset in enumerate(custom_assets):
                if not isinstance(asset, dict):
                    raise ValueError(
                        f"pipeline '{name}'.custom_assets[{i}] must be a dict, "
                        f"got {type(asset).__name__}"
                    )
                if "name" not in asset or not isinstance(asset["name"], str):
                    raise ValueError(
                        f"pipeline '{name}'.custom_assets[{i}] is missing required string key 'name'"
                    )
                if "build" not in asset or not isinstance(asset["build"], str):
                    raise ValueError(
                        f"pipeline '{name}'.custom_assets[{i}] is missing required string key 'build'"
                    )


def clean_stale_exclusions(config_path):
    """Remove batch_limits exclusions that reference version="unreleased".

    After release finalization renames unreleased.jsonl to X.Y.Z.jsonl,
    exclusions with version="unreleased" become dead references. This
    function cleans them up.

    Returns the number of exclusions removed. Returns 0 and does not
    write to disk if nothing changed.
    """
    config = read_json_config(config_path)
    batch_limits = config.get("batch_limits")
    if not isinstance(batch_limits, dict):
        return 0
    exclusions = batch_limits.get("exclusions")
    if not isinstance(exclusions, list) or not exclusions:
        return 0

    def _has_unreleased_entry(exclusion):
        entries = exclusion.get("entries", [])
        return any(
            isinstance(e, dict) and e.get("version") == "unreleased"
            for e in entries
        )

    cleaned = [ex for ex in exclusions if not _has_unreleased_entry(ex)]
    removed = len(exclusions) - len(cleaned)
    if removed == 0:
        return 0

    batch_limits["exclusions"] = cleaned
    # Atomic write: tmp file then replace
    tmp_path = config_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp_path, config_path)
    return removed


def write_project_config(key, value, project_root):
    """Write or update a key in .rlsbl/config.json (creates dir if needed).

    Returns the updated config dict after writing to disk.
    """
    config_path = _project_config(project_root)
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    existing = read_json_config(config_path)
    existing[key] = value
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2)
        f.write("\n")
    return existing
