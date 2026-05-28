"""Targets command that lists all available release targets (npm, PyPI, Go, Cargo, etc.) and shows their auto-detection status."""

import sys

from ..targets import TARGETS, detect_targets


def run_cmd(registry, args, flags, project_root=None):
    """List all available targets with their detection status and version file."""
    if project_root is None:
        project_root = "."
    dir_path = str(project_root)
    detected = detect_targets(dir_path)
    detected_names = {entry.name for entry in detected}

    # Column headers
    headers = ("Target", "Detected", "Version file")

    # Build rows
    rows = []
    for name, target in TARGETS.items():
        is_detected = "yes" if name in detected_names else "no"
        vfile = target.version_file() or "(none)"
        rows.append((name, is_detected, vfile))

    # Calculate column widths
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    # Format and print
    def fmt_row(cells):
        return "   ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells))

    print(fmt_row(headers))
    for row in rows:
        print(fmt_row(row))
