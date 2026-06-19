"""Monorepo workspace management commands: init, add, remove, list, sync, status, outdated, release-order, check-names, graph, impact, and batch release."""

from .commands import (
    _cmd_init,
    _cmd_add,
    _cmd_remove,
    _cmd_list,
    _cmd_status,
    _cmd_outdated,
    _cmd_release_order,
    _cmd_check_names,
    _evaluate_constraint,
    _parse_version_tuple,
)

from .batch_release import _cmd_batch_release
from .batch_release_init import _cmd_batch_release_init

from .graph import _cmd_graph

from .impact import _cmd_impact

from .mirror_cmd import _cmd_mirror

from .snapshot_cmd import _cmd_snapshot

from .extract import (
    require_filter_repo,
    cmd_extract,
    cmd_absorb,
    cmd_extract_releasable,
    ExtractError,
    validate_extract_preconditions,
    validate_absorb_preconditions,
)

from .sync import (
    _cmd_sync,
    _build_project_template_vars,
    parse_ci_workflow,
    emit_ci_workflow,
    _rewrite_trigger,
    _inject_working_directory,
    _rewrite_version_file_inputs,
    _inject_packages_dir,
    _generate_router,
    _get_monorepo_tag_prefix,
    scaffold_releasable_dirs,
)

__all__ = [
    # commands
    "_cmd_init",
    "_cmd_add",
    "_cmd_remove",
    "_cmd_list",
    "_cmd_status",
    "_cmd_outdated",
    "_cmd_release_order",
    "_cmd_check_names",
    "_cmd_batch_release",
    "_cmd_batch_release_init",
    "_cmd_graph",
    "_cmd_impact",
    "_cmd_mirror",
    "_cmd_snapshot",
    # extract/absorb
    "require_filter_repo",
    "cmd_extract",
    "cmd_absorb",
    "cmd_extract_releasable",
    "ExtractError",
    "validate_extract_preconditions",
    "validate_absorb_preconditions",
    "_evaluate_constraint",
    "_parse_version_tuple",
    # sync
    "_cmd_sync",
    "_build_project_template_vars",
    "parse_ci_workflow",
    "emit_ci_workflow",
    "_rewrite_trigger",
    "_inject_working_directory",
    "_rewrite_version_file_inputs",
    "_inject_packages_dir",
    "_generate_router",
    "_get_monorepo_tag_prefix",
    "scaffold_releasable_dirs",
]
