"""Target introspection -- generates raw table data for all release targets showing ecosystem, detection files, supported operations, and tag formats."""

from . import TARGETS

# Support columns and the property each one asks. The table used to read a
# hand-declared ``capabilities`` frozenset, which had drifted from the code:
# several targets implemented read_metadata without declaring it, and the
# swift-apple row's dev-install cell was blank for the opposite reason.
SUPPORT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("read_name", "supports_read_name"),
    ("read_metadata", "supports_read_metadata"),
    ("ci_templates", "provides_ci_templates"),
)

HEADERS = [
    "Name", "Ecosystem", "Detection files", "Version file", "Auto-detectable",
    "Tag format", "Monorepo tag format",
    *(column for column, _prop in SUPPORT_COLUMNS),
    "dev_install",
]

# Targets whose detection is decided by file content rather than by a
# filename, with the phrasing the table shows for them.
_DETECTION_OVERRIDES = {
    "plain": "VERSION (conditional)",
    "flutter": "pubspec.yaml (flutter)",
}


def _format_dev_install(target) -> str:
    """Format the dev_install_command output into a compact string."""
    result = target.dev_install_command(".")
    parts = []
    for mode in ("global", "venv"):
        spec = result.get(mode)
        if spec is not None:
            tool = spec["tool"]
            args = " ".join(spec["args"])
            parts.append(f"{mode}: {tool} {args}")

    return ", ".join(parts)


def generate_target_table_data() -> tuple[list[str], list[list[str]]]:
    """Generate raw data for a markdown table of all release targets.

    Returns ``(headers, rows)`` where each row has one cell per header, sorted
    alphabetically by target name.
    """
    rows: list[list[str]] = []

    for target_name, target in sorted(TARGETS.items()):
        # Detection files
        detection = _DETECTION_OVERRIDES.get(target_name)
        if detection is None:
            detection = (
                ", ".join(target.detection_files) if target.detection_files else "---"
            )

        # Version file
        vf = target.version_file()
        version_file = vf if vf is not None else "---"

        # Tag format
        tf = target.tag_format("{version}")
        tag_format = tf if tf is not None else "---"

        # Monorepo tag format
        monorepo_tf = target.monorepo_tag_format("{name}", "{version}", "{path}")

        # Support columns, each derived from the target rather than declared
        support_cells = [
            "✓" if getattr(target, prop) else "" for _column, prop in SUPPORT_COLUMNS
        ]

        row = [
            target_name,
            target.ecosystem,
            detection,
            version_file,
            target.auto_detectable,
            tag_format,
            monorepo_tf,
            *support_cells,
            _format_dev_install(target),
        ]
        rows.append(row)

    return HEADERS, rows
