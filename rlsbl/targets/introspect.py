"""Target introspection -- generates raw table data for all release targets showing ecosystem, detection files, capabilities, and tag formats."""

from . import TARGETS

CAPABILITY_COLUMNS = ("publish", "build_assets", "read_name", "read_metadata", "ci_templates")

HEADERS = [
    "Name", "Ecosystem", "Detection files", "Version file", "Auto-detectable",
    "Tag format", "Monorepo tag format", "publish", "build_assets", "read_name",
    "read_metadata", "ci_templates", "dev_install",
]


def _format_dev_install(target) -> str:
    """Format the dev_install_command output into a compact string."""
    if "dev_install" not in target.capabilities:
        return ""
    result = target.dev_install_command(".")
    global_spec = result.get("global")
    venv_spec = result.get("venv")

    parts = []
    for mode, spec in [("global", global_spec), ("venv", venv_spec)]:
        if spec is not None:
            tool = spec["tool"]
            args = " ".join(spec["args"])
            parts.append(f"{mode}: {tool} {args}")

    return ", ".join(parts)


def generate_target_table_data() -> tuple[list[str], list[list[str]]]:
    """Generate raw data for a markdown table of all release targets.

    Returns ``(headers, rows)`` where *headers* is a 13-element list and
    each row is a 13-element list of strings, sorted alphabetically by
    target name.
    """
    rows: list[list[str]] = []

    for target_name, target in sorted(TARGETS.items()):
        # Detection files
        if target_name == "plain":
            detection = "VERSION (conditional)"
        elif target.detection_files:
            detection = ", ".join(target.detection_files)
        else:
            detection = "---"

        # Version file
        vf = target.version_file()
        version_file = vf if vf is not None else "---"

        # Tag format
        tf = target.tag_format("{version}")
        tag_format = tf if tf is not None else "---"

        # Monorepo tag format
        monorepo_tf = target.monorepo_tag_format("{name}", "{version}", "{path}")

        # Capability columns
        cap_cells = []
        for cap in CAPABILITY_COLUMNS:
            cap_cells.append("✓" if cap in target.capabilities else "")

        # Dev install
        dev_install = _format_dev_install(target)

        row = [
            target_name,
            target.ecosystem,
            detection,
            version_file,
            target.auto_detectable,
            tag_format,
            monorepo_tf,
            *cap_cells,
            dev_install,
        ]
        rows.append(row)

    return HEADERS, rows
