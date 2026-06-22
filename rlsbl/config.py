"""Project configuration loading with layered precedence.

Layers (highest to lowest priority):
1. Per-package publish.json
2. Per-package config.json
3. Releasable publish.json
4. Releasable config.json

CLI flags override project-level .rlsbl/config.json which overrides
user-level defaults.

Publishing fields (targets, private, pipelines, push_timeout, tag) can live in
either ``.rlsbl/config.json`` (legacy) or ``.rlsbl/publish.json`` (preferred).
Having publishing fields in *both* files is a hard error -- no silent fallback.
"""

import json
import os
import sys

from .errors import ConfigError

# Fields that belong in publish.json (publishing-related configuration).
PUBLISH_FIELDS = frozenset({"targets", "private", "pipelines", "push_timeout", "tag"})


def merge_config(base, overlay):
    """Merge two config dicts with shallow-replace, deep-merge for nested dicts.

    Top-level keys in ``overlay`` replace those in ``base``, except when
    both values are dicts -- in that case the nested dict is merged
    recursively (overlay nested keys merge into base nested keys).

    Keys present in ``base`` but absent in ``overlay`` are preserved.

    Returns a new dict; neither input is mutated.
    """
    merged = dict(base)
    for key, value in overlay.items():
        if (
            key in merged
            and isinstance(merged[key], dict)
            and isinstance(value, dict)
        ):
            # Deep merge for nested dicts
            merged[key] = merge_config(merged[key], value)
        else:
            merged[key] = value
    return merged


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
        raise ConfigError(f"Malformed JSON in {path}: {e}") from e


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


def _publish_config_path(project_root):
    """Resolve publish config path at call time."""
    return os.path.join(str(project_root), ".rlsbl", "publish.json")


def read_publish_config(project_root):
    """Read ``.rlsbl/publish.json``, returning dict or empty dict if missing."""
    return read_json_config(_publish_config_path(project_root))


def write_publish_config(project_root, config):
    """Atomically write ``config`` dict to ``.rlsbl/publish.json``."""
    path = _publish_config_path(project_root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp_path, path)


def _load_level_config(config_path, publish_path, label):
    """Load config.json + publish.json for a single level (package or releasable).

    Applies per-level conflict detection: if both config.json and publish.json
    exist at the same level and config.json has PUBLISH_FIELDS, that is a hard
    error.

    Returns the merged dict for this level (non-publishing from config.json +
    publishing from whichever source).
    """
    config_json = read_json_config(config_path)
    publish_json_exists = os.path.isfile(publish_path)
    publish_json = read_json_config(publish_path) if publish_json_exists else {}

    if not publish_json_exists:
        return config_json

    # Conflict check is per-level only
    config_publish_keys = PUBLISH_FIELDS & set(config_json.keys())
    if config_publish_keys and publish_json_exists:
        raise ConfigError(
            f"Publishing fields found in {label}/config.json while "
            f"{label}/publish.json exists: "
            f"{', '.join(sorted(config_publish_keys))}. "
            f"Use migrate_publish_config() to move publishing fields to publish.json, "
            f"or remove them from config.json."
        )

    merged = {k: v for k, v in config_json.items() if k not in PUBLISH_FIELDS}
    merged.update(publish_json)
    return merged


def read_project_config(project_root, releasable_config_dir=None):
    """Read project config with optional releasable-level inheritance.

    When ``releasable_config_dir`` is provided (path to a releasable's
    state directory, e.g. ``.rlsbl-monorepo/releasables/www/``), config
    is loaded with 4-level precedence:

    1. Per-package publish.json (highest)
    2. Per-package config.json
    3. Releasable publish.json
    4. Releasable config.json (lowest)

    Conflict detection (PUBLISH_FIELDS in config.json while publish.json
    exists) is enforced per-level only: a releasable-level publish.json
    does NOT conflict with a per-package config.json that has publishing
    fields.

    When ``releasable_config_dir`` is None, behaves as before: loads only
    the per-package level.
    """
    # Load per-package level
    pkg_config = _load_level_config(
        _project_config(project_root),
        _publish_config_path(project_root),
        ".rlsbl",
    )

    if releasable_config_dir is None:
        return pkg_config

    # Load releasable level
    rel_config_path = os.path.join(str(releasable_config_dir), "config.json")
    rel_publish_path = os.path.join(str(releasable_config_dir), "publish.json")
    rel_config = _load_level_config(rel_config_path, rel_publish_path, "releasable")

    if not rel_config:
        return pkg_config

    # Merge: per-package on top of releasable
    return merge_config(rel_config, pkg_config)


def migrate_publish_config(project_root):
    """Extract PUBLISH_FIELDS from ``.rlsbl/config.json`` into ``.rlsbl/publish.json``.

    Reads config.json, moves any PUBLISH_FIELDS into a new publish.json, and
    rewrites config.json without those fields. Both writes are atomic (write to
    tmp, then rename).

    Returns a tuple of (extracted_fields, remaining_fields) dicts.
    Raises ConfigError if publish.json already exists and has content.
    """
    config_path = _project_config(project_root)
    publish_path = _publish_config_path(project_root)

    config_json = read_json_config(config_path)
    existing_publish = read_json_config(publish_path)

    if existing_publish:
        raise ConfigError(
            f".rlsbl/publish.json already exists and has content. "
            f"Migration would overwrite it. Remove or merge manually."
        )

    # Split config.json into publishing and non-publishing fields
    extracted = {k: v for k, v in config_json.items() if k in PUBLISH_FIELDS}
    remaining = {k: v for k, v in config_json.items() if k not in PUBLISH_FIELDS}

    if not extracted:
        return extracted, remaining

    # Write publish.json atomically
    os.makedirs(os.path.dirname(publish_path), exist_ok=True)
    tmp_publish = publish_path + ".tmp"
    with open(tmp_publish, "w", encoding="utf-8") as f:
        json.dump(extracted, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp_publish, publish_path)

    # Rewrite config.json without publishing fields atomically
    tmp_config = config_path + ".tmp"
    with open(tmp_config, "w", encoding="utf-8") as f:
        json.dump(remaining, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp_config, config_path)

    return extracted, remaining


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

    Raises ``ConfigError`` if:
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
        raise ConfigError(
            f"pipelines must be a dict, got {type(pipelines).__name__}"
        )

    for name, entry in pipelines.items():
        if not isinstance(entry, dict):
            raise ConfigError(
                f"pipeline '{name}' must be a dict, got {type(entry).__name__}"
            )

        # type is required and must be a registered pipeline type
        if "type" not in entry:
            raise ConfigError(f"pipeline '{name}' is missing required key 'type'")
        ptype = entry["type"]
        if not isinstance(ptype, str):
            raise ConfigError(
                f"pipeline '{name}'.type must be a string, got {type(ptype).__name__}"
            )
        if ptype not in PIPELINE_TYPES:
            raise ConfigError(
                f"pipeline '{name}'.type '{ptype}' is not a registered pipeline type. "
                f"Valid types: {', '.join(sorted(PIPELINE_TYPES.keys())) or '(none registered)'}"
            )

        # local is required and must be a bool
        if "local" not in entry:
            raise ConfigError(f"pipeline '{name}' is missing required key 'local'")
        if not isinstance(entry["local"], bool):
            raise ConfigError(
                f"pipeline '{name}'.local must be a boolean, got {type(entry['local']).__name__}"
            )

        # assets validation
        if entry.get("assets"):
            max_size = entry.get("max_asset_size_mb")
            if max_size is None:
                raise ConfigError(
                    f"pipeline '{name}' has assets=true but max_asset_size_mb is not set"
                )
            if not isinstance(max_size, int) or max_size <= 0:
                raise ConfigError(
                    f"pipeline '{name}'.max_asset_size_mb must be a positive integer, "
                    f"got {max_size!r}"
                )

        # custom_assets validation
        custom_assets = entry.get("custom_assets")
        if custom_assets is not None:
            if not isinstance(custom_assets, list):
                raise ConfigError(
                    f"pipeline '{name}'.custom_assets must be a list, "
                    f"got {type(custom_assets).__name__}"
                )
            # custom_assets requires max_asset_size_mb
            max_size = entry.get("max_asset_size_mb")
            if max_size is None:
                raise ConfigError(
                    f"pipeline '{name}' has custom_assets but max_asset_size_mb is not set"
                )
            if not isinstance(max_size, int) or max_size <= 0:
                raise ConfigError(
                    f"pipeline '{name}'.max_asset_size_mb must be a positive integer, "
                    f"got {max_size!r}"
                )
            for i, asset in enumerate(custom_assets):
                if not isinstance(asset, dict):
                    raise ConfigError(
                        f"pipeline '{name}'.custom_assets[{i}] must be a dict, "
                        f"got {type(asset).__name__}"
                    )
                if "name" not in asset or not isinstance(asset["name"], str):
                    raise ConfigError(
                        f"pipeline '{name}'.custom_assets[{i}] is missing required string key 'name'"
                    )
                if "build" not in asset or not isinstance(asset["build"], str):
                    raise ConfigError(
                        f"pipeline '{name}'.custom_assets[{i}] is missing required string key 'build'"
                    )


def _read_unreleased_commits(config_path):
    """Read commit hashes from unreleased.jsonl adjacent to config_path.

    Returns a set of commit hash strings found in the "commits" arrays
    of all entries in unreleased.jsonl.  Returns an empty set if the
    file does not exist or is empty.
    """
    rlsbl_dir = os.path.dirname(config_path)
    unreleased_path = os.path.join(rlsbl_dir, "changes", "unreleased.jsonl")
    if not os.path.isfile(unreleased_path):
        return set()
    commits = set()
    with open(unreleased_path, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            for c in obj.get("commits", []):
                if isinstance(c, str):
                    commits.add(c)
    return commits


def clean_stale_exclusions(config_path):
    """Remove stale batch_limits exclusions after release finalization.

    Two kinds of exclusions become stale:

    1. Entry-level (have ``"entries"`` with ``version="unreleased"``):
       after finalization renames unreleased.jsonl to X.Y.Z.jsonl, these
       are dead references.

    2. Commit-level (have ``"commits"`` but no ``"entries"``): stale when
       ALL referenced commits are no longer in unreleased.jsonl (they
       were moved to a versioned file during finalization).

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

    unreleased_commits = _read_unreleased_commits(config_path)

    def _is_stale(exclusion):
        if not isinstance(exclusion, dict):
            return False
        # Entry-level: stale if any entry references version="unreleased"
        entries = exclusion.get("entries", [])
        if entries and any(
            isinstance(e, dict) and e.get("version") == "unreleased"
            for e in entries
        ):
            return True
        # Commit-level: stale if ALL commits are absent from unreleased.jsonl
        commits = exclusion.get("commits", [])
        if commits and all(
            c not in unreleased_commits
            for c in commits
            if isinstance(c, str)
        ):
            return True
        return False

    cleaned = [ex for ex in exclusions if not _is_stale(ex)]
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


def update_last_build_release(project_dir, version):
    """Store last_build_release version in .rlsbl/config.json for OTA validation."""
    config_path = os.path.join(project_dir, ".rlsbl", "config.json")
    try:
        config = read_json_config(config_path)
    except Exception as e:
        raise RuntimeError(
            f"{config_path} is corrupted or unreadable — fix it before releasing: {e}"
        ) from e
    config["last_build_release"] = version
    tmp_path = config_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
        f.write("\n")
    os.replace(tmp_path, config_path)


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
