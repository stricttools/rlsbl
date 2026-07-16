"""Project configuration loading with layered precedence.

Layers (highest to lowest priority):
1. Per-package config.json
2. Releasable config.json

CLI flags override project-level .rlsbl/config.json which overrides
user-level defaults.
"""

import json
import os
import sys
import tempfile

from .errors import ConfigError


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
    if not flags.get("auto-tag", True):
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


def read_project_config(project_root, releasable_config_dir=None):
    """Read project config with optional releasable-level inheritance.

    When ``releasable_config_dir`` is provided (path to a releasable's
    state directory, e.g. ``.rlsbl-monorepo/releasables/www/``), config
    is loaded with 2-level precedence:

    1. Per-package config.json (highest)
    2. Releasable config.json (lowest)

    When ``releasable_config_dir`` is None, loads only the per-package level.

    Ensures ``coverage_unit`` is always present (defaults to ``"commit"``
    for backward compatibility with configs that predate Phase 8).
    """
    pkg_config = read_json_config(_project_config(project_root))

    if releasable_config_dir is None:
        pkg_config.setdefault("coverage_unit", "commit")
        return pkg_config

    # Load releasable level
    rel_config_path = os.path.join(str(releasable_config_dir), "config.json")
    rel_config = read_json_config(rel_config_path)

    if not rel_config:
        pkg_config.setdefault("coverage_unit", "commit")
        return pkg_config

    # Merge: per-package on top of releasable
    merged = merge_config(rel_config, pkg_config)
    merged.setdefault("coverage_unit", "commit")
    return merged


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

    Returns an empty dict when ``batch_limits`` is absent. A present but
    non-dict ``batch_limits`` is a hard error (:class:`ConfigError`) --
    never silently treated as absent.
    """
    batch_limits = config.get("batch_limits", {})
    if not isinstance(batch_limits, dict):
        raise ConfigError(
            f"Invalid batch_limits in .rlsbl/config.json: {batch_limits!r} "
            f"(type {type(batch_limits).__name__}). "
            f"Must be a JSON object (dict)."
        )
    return batch_limits



# The closed set of valid publish_mode values. "ci" publishes via CI
# pipelines (the old private:false); "none" suppresses publishing (old
# private:true). "local" is reserved for a future direct-publish mode.
PUBLISH_MODES = frozenset({"ci", "none"})


def old_private_key_message():
    """Exact-edit remediation for the removed ``private`` config key.

    The ``private`` key was misleading (it read as GitHub repo visibility but
    meant "suppress publishing"). It is replaced by the ``publish_mode`` enum.
    """
    return (
        'The "private" key in .rlsbl/config.json has been replaced by '
        '"publish_mode". Replace "private": false with "publish_mode": "ci" '
        '(publish via CI pipelines), or replace "private": true with '
        '"publish_mode": "none" (suppress publishing).'
    )


def get_publish_mode(config):
    """Return the ``publish_mode`` enum value (one of :data:`PUBLISH_MODES`).

    Single source of truth for reading the publish mode. Raises
    :class:`ConfigError` when the deprecated ``private`` key is present, when
    ``publish_mode`` is absent (it is required, no default), or when its value
    is not one of the valid modes.
    """
    if "private" in config:
        raise ConfigError(old_private_key_message())
    if "publish_mode" not in config:
        raise ConfigError(
            'missing required "publish_mode" key in .rlsbl/config.json — set '
            '"publish_mode": "ci" to publish via CI, or "publish_mode": "none" '
            'to suppress publishing.'
        )
    mode = config["publish_mode"]
    if mode not in PUBLISH_MODES:
        raise ConfigError(
            f'invalid "publish_mode" value {mode!r} in .rlsbl/config.json — '
            f'must be one of {sorted(PUBLISH_MODES)}.'
        )
    return mode


def suppresses_publish(config):
    """True when the config's publish_mode suppresses publishing (``"none"``).

    Derives the old ``is_private`` boolean from the enum. Raises
    :class:`ConfigError` via :func:`get_publish_mode` when the key is absent or
    invalid (required-read, no silent default).
    """
    return get_publish_mode(config) == "none"


def empty_targets_ban_message(location):
    """Return the standard error message for a banned empty ``targets`` list.

    *location* describes where the empty list was found (e.g. ``"config"`` or a
    config file path). Shared so every call site emits an identical message.
    """
    return (
        f'targets is an empty list in {location}. '
        'Remove the "targets" key entirely, or set "publish_mode": "none" '
        'to suppress publishing.'
    )


def non_list_targets_ban_message(location, value):
    """Return the standard error message for a non-list ``targets`` value.

    A present-but-non-list ``targets`` (string, dict, ...) is a hard error --
    never silently treated as absent.
    """
    return (
        f'targets must be a list in {location}, got {type(value).__name__}. '
        'Provide a list of target names, remove the "targets" key entirely, '
        'or set "publish_mode": "none" to suppress publishing.'
    )


def validate_config_schema(config, *, project_dir=None):
    """Consolidated config schema validation -- single entry point for all
    banned keys and structural invariants.

    Checks:
    1. ``publish_mode`` -- hard error if the deprecated ``private`` key is
       present, if ``publish_mode`` is absent, or if its value is invalid.
    2. ``targets: []`` -- hard error if targets key exists and is an empty
       list.  Use ``publish_mode: "none"`` to suppress publishing instead.
    3. ``release.mode`` -- hard error if the key exists.  PR mode was
       removed; even ``mode = "imperative"`` is dead config.

    Called early in the release flow before any mutations.

    Args:
        config: the project config dict.
        project_dir: unused (kept for call-site compatibility).

    Raises:
        ConfigError on any violation.
    """
    # 1. Require publish_mode (and ban the deprecated private key)
    get_publish_mode(config)

    # 2. Ban targets: []
    targets = config.get("targets")
    if isinstance(targets, list) and len(targets) == 0:
        raise ConfigError(empty_targets_ban_message("config"))

    # 2. Ban release.mode key entirely
    release_section = config.get("release")
    if isinstance(release_section, dict) and "mode" in release_section:
        raise ConfigError(
            'release.mode is no longer supported (PR mode has been removed). '
            'Remove the "release" section from .rlsbl/config.json.'
        )


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

        # npm pipelines must declare provenance explicitly (boolean). npm
        # build-provenance attestations are signed via GitHub Actions OIDC and
        # require a PUBLIC source repository; there is no default because the
        # correct value depends on repo visibility.
        if ptype == "npm":
            if "provenance" not in entry:
                raise ConfigError(
                    f"pipeline '{name}' (type npm) is missing required key "
                    "'provenance'. Set it based on repository visibility: "
                    '"provenance": true for a PUBLIC repo (npm records a signed '
                    "build-provenance attestation via GitHub OIDC), or "
                    '"provenance": false for a PRIVATE repo (provenance is '
                    "impossible without a public source repo and the publish "
                    "would fail)."
                )
            if not isinstance(entry["provenance"], bool):
                raise ConfigError(
                    f"pipeline '{name}' (type npm).provenance must be a boolean, "
                    f"got {type(entry['provenance']).__name__}. Use true for a "
                    "public repository or false for a private one."
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


def validate_test_config(config):
    """Validate the optional ``test`` section of a project config.

    The ``test`` section maps a release target name to a block of per-target
    test options::

        {"test": {"pypi": {"markers": "not integration"}}}

    Absent section or absent target key means "run everything" (today's
    behavior). Everything must be declared -- unknown targets and unknown
    inner keys are hard errors (no silent tolerance of typos like ``marker``).

    Only ``pypi.markers`` is recognized today; the shape is built so future
    per-target options (go tags, npm script selection) slot in without
    reshaping.

    Raises ``ConfigError`` if:
    - ``test`` is present but not a dict
    - a target key is not a recognized test target
    - a target block is not a dict
    - an inner key is not a recognized option for that target
    - ``pypi.markers`` is present but not a string, or is an empty string
    """
    test_section = config.get("test")
    if test_section is None:
        return

    if not isinstance(test_section, dict):
        raise ConfigError(
            f"test must be a dict, got {type(test_section).__name__}"
        )

    known_targets = {"pypi"}
    for target_name, block in test_section.items():
        if target_name not in known_targets:
            raise ConfigError(
                f"test.'{target_name}' is not a recognized test target. "
                f"Valid targets: {', '.join(sorted(known_targets))}"
            )
        if not isinstance(block, dict):
            raise ConfigError(
                f"test.'{target_name}' must be a dict, got {type(block).__name__}"
            )
        if target_name == "pypi":
            _validate_pypi_test_block(block)


def _validate_pypi_test_block(block):
    """Validate the ``test.pypi`` options block. See ``validate_test_config``."""
    known_keys = {"markers"}
    for key in block:
        if key not in known_keys:
            raise ConfigError(
                f"test.pypi.'{key}' is not a recognized option. "
                f"Valid options: {', '.join(sorted(known_keys))}"
            )

    if "markers" in block:
        markers = block["markers"]
        if not isinstance(markers, str):
            raise ConfigError(
                f"test.pypi.markers must be a string, got {type(markers).__name__}"
            )
        if markers == "":
            raise ConfigError(
                'test.pypi.markers is an empty string. Provide a pytest marker '
                'expression (e.g. "not integration") or omit the key entirely.'
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
    # Absent batch_limits: nothing to clean. But a present-but-non-dict value
    # is invalid config -- hard error rather than silently no-op, mirroring
    # get_changelog_validation_config's boundary (this function re-reads the
    # config from disk independently, so it cannot assume prior validation).
    if "batch_limits" not in config:
        return 0
    batch_limits = config["batch_limits"]
    if not isinstance(batch_limits, dict):
        raise ConfigError(
            f"Invalid batch_limits in {config_path}: {batch_limits!r} "
            f"(type {type(batch_limits).__name__}). "
            f"Must be a JSON object (dict)."
        )
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
    parent = os.path.dirname(config_path)
    os.makedirs(parent, exist_ok=True)
    existing = read_json_config(config_path)
    existing[key] = value
    # Atomic write: serialize into a temp file in the same dir, then replace.
    # A failure mid-write leaves the original config.json untouched, and the
    # temp file is cleaned up so no residue is left behind.
    fd, tmp_path = tempfile.mkstemp(dir=parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2)
            f.write("\n")
        os.replace(tmp_path, config_path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
    return existing
