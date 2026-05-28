"""Record-gif command that captures a terminal demo GIF using vhs, suitable for embedding in README files and documentation pages."""

import os
import subprocess
import sys
import tempfile

from ..targets import TARGETS, detect_targets
from ..utils import require_tool


def _parse_int_flag(flags, name, default):
    """Parse an integer flag value, exiting with a clear error on invalid input."""
    raw = flags.get(name, default)
    try:
        return int(raw)
    except (ValueError, TypeError):
        print(f"Invalid value for --{name}: expected integer, got '{raw}'", file=sys.stderr)
        sys.exit(1)


def _get_bin_command(project_root=None):
    """Auto-detect the project's binary command name via registry template vars."""
    dir_path = str(project_root) if project_root is not None else "."
    target_entries = detect_targets(dir_path)
    if not target_entries:
        return None
    # Use the first detected target
    first_name, first_path = target_entries[0]
    registry_module = TARGETS.get(first_name)
    if not registry_module:
        return None
    try:
        tvars = registry_module.template_vars(first_path)
        return tvars.get("binCommand") or None
    except Exception:
        return None


def run_cmd(registry, args, flags, project_root=None):
    """Record a demo GIF of '<binCommand> --help' using vhs.

    Requires vhs (https://github.com/charmbracelet/vhs) to be installed.
    Output is saved to assets/demo.gif.
    """
    if project_root is None:
        project_root = "."
    root_str = str(project_root)

    if not require_tool("vhs", fatal=False):
        print("Error: vhs is required.", file=sys.stderr)
        print("Install: go install github.com/charmbracelet/vhs@latest", file=sys.stderr)
        sys.exit(1)

    bin_command = _get_bin_command(project_root)
    if not bin_command:
        print("Error: could not detect project binary command.", file=sys.stderr)
        print("Ensure package.json, pyproject.toml, or go.mod exists with a CLI entry point.", file=sys.stderr)
        sys.exit(1)

    # Parse configurable VHS parameters from flags
    width = _parse_int_flag(flags, "width", 1200)
    height = _parse_int_flag(flags, "height", 600)
    font_size = _parse_int_flag(flags, "font-size", 24)
    duration = _parse_int_flag(flags, "duration", 10)

    assets_dir = os.path.join(root_str, "assets")
    os.makedirs(assets_dir, exist_ok=True)

    # Create a temporary VHS tape file in the project directory
    tape_content = (
        'Set FontFamily "monospace"\n'
        f"Set FontSize {font_size}\n"
        f"Set Width {width}\n"
        f"Set Height {height}\n"
        "Set TypingSpeed 50ms\n"
        f'Type "{bin_command} --help"\n'
        "Enter\n"
        f"Sleep {duration}s\n"
    )

    tape_fd, tape_path = tempfile.mkstemp(suffix=".tape", dir=root_str)
    try:
        with os.fdopen(tape_fd, "w") as f:
            f.write(tape_content)

        output_path = os.path.join(assets_dir, "demo.gif")
        print("Recording demo...")

        subprocess.run(
            ["vhs", tape_path, "-o", output_path],
            check=True, timeout=120,
        )

        print(f"Done. GIF saved to {output_path}")
    except subprocess.CalledProcessError:
        print("Error: vhs recording failed.", file=sys.stderr)
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print("Error: vhs recording timed out after 120s.", file=sys.stderr)
        sys.exit(1)
    finally:
        # Clean up the temp tape file
        try:
            os.unlink(tape_path)
        except OSError:
            pass
