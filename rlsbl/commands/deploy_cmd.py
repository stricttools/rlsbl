"""Deploy command that orchestrates deployment to configured remote targets via SSH using the deploy primitives and config layer."""

import sys

from ..config import read_deploy_config
from ..context import ProjectContext
from ..deploy import deploy_target
from ..utils import get_current_branch


def run_cmd(registry, args, flags, *, ctx):
    """Deploy to configured targets.

    Usage:
        rlsbl deploy [name]       Deploy to target (auto-selects if only one)
        rlsbl deploy --dry-run    Show what would be deployed
        rlsbl deploy --force      Override branch restrictions
    """
    # 1. Read deploy config
    targets, errors = read_deploy_config(ctx.project_root)

    if not targets:
        print("Error: No deploy targets configured in .rlsbl/config.json", file=sys.stderr)
        sys.exit(1)

    if errors:
        print("Error: deploy config has validation errors:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        sys.exit(1)

    # 2. Select target
    target_name = args[0] if args else None

    if target_name:
        matched = [t for t in targets if t["name"] == target_name]
        if not matched:
            available = ", ".join(t["name"] for t in targets)
            print(f'Error: unknown deploy target "{target_name}". Available: {available}', file=sys.stderr)
            sys.exit(1)
        target_config = matched[0]
    elif len(targets) == 1:
        target_config = targets[0]
    else:
        available = ", ".join(t["name"] for t in targets)
        print(f"Error: multiple deploy targets configured. Specify one: {available}", file=sys.stderr)
        sys.exit(1)

    # 3. Get current branch
    branch = get_current_branch()

    # 4. Dry run
    if flags.get("dry-run"):
        _print_dry_run(target_config, branch)
        sys.exit(0)

    # 5. Branch restriction (unless --force)
    if not flags.get("force"):
        only_on = target_config["only_on"]
        if branch not in only_on:
            print(
                f'Error: current branch "{branch}" is not in allowed branches {only_on}. '
                "Use --force to override.",
                file=sys.stderr,
            )
            sys.exit(1)

    # 6. Deploy
    result = deploy_target(target_config, branch)

    # 7. Print result
    if result.success:
        print(f"[{result.target_name}] {result.message}")
    else:
        msg = f"[{result.target_name}] Deploy failed: {result.message}"
        if result.rolled_back:
            msg += " (rolled back)"
        print(msg, file=sys.stderr)
        sys.exit(1)


def _print_dry_run(target_config, branch):
    """Print deploy info without executing."""
    name = target_config["name"]
    host = target_config["host"]
    steps = target_config["steps"]
    only_on = target_config["only_on"]
    user = target_config.get("user", "root")
    directory = target_config.get("directory")
    health = target_config.get("health")

    print(f"Deploy target: {name}")
    print(f"  Host:      {user}@{host}")
    print(f"  Branch:    {branch}")
    print(f"  Only on:   {', '.join(only_on)}")
    if directory:
        print(f"  Directory: {directory}")
    print(f"  Steps:")
    for i, step in enumerate(steps, 1):
        print(f"    {i}. {step}")
    if health:
        print(f"  Health:    {health['type']}", end="")
        if health["type"] == "http":
            print(f" ({health.get('url', '')})")
        elif health["type"] == "tcp":
            print(f" (port {health.get('port', '')})")
        elif health["type"] == "script":
            print(f" ({health.get('command', '')})")
        else:
            print()
    print("--- No changes made (dry run) ---")
