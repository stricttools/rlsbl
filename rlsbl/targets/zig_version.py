"""Version management helpers for Zig projects, reading from a VERSION file and synchronizing the .version field in build.zig.zon on writes."""

import os
import re
import sys
from .. import effects

VERSION_FILE = "VERSION"
ZON_FILE = "build.zig.zon"

_ZON_VERSION_RE = re.compile(r'(\.version\s*=\s*)"([^"]+)"')


def read_zon_version(zon_path):
    """Extract the .version value from a build.zig.zon file.

    Returns the version string, or None if the field is not found.
    """
    if not os.path.exists(zon_path):
        return None
    with open(zon_path, "r", encoding="utf-8") as f:
        content = f.read()
    match = _ZON_VERSION_RE.search(content)
    return match.group(2) if match else None


def read_zig_version(dir_path):
    """Read the Zig project version.

    Tries the VERSION file first. Falls back to extracting .version from
    build.zig.zon. Raises FileNotFoundError if neither source is available.
    """
    version_path = os.path.join(dir_path, VERSION_FILE)
    if os.path.exists(version_path):
        with open(version_path, "r", encoding="utf-8") as f:
            return f.read().strip()

    zon_path = os.path.join(dir_path, ZON_FILE)
    version = read_zon_version(zon_path)
    if version is not None:
        return version

    raise FileNotFoundError(
        f"No {VERSION_FILE} or {ZON_FILE} with .version field found. "
        "Run 'rlsbl scaffold' first."
    )


def write_zig_version(dir_path, version):
    """Write the version to VERSION (atomic) and sync build.zig.zon if present.

    Returns a list of relative file paths that were modified.
    """
    # Write VERSION atomically
    version_path = os.path.join(dir_path, VERSION_FILE)
    effects.atomic_write_text(version_path, version + "\n")

    modified = [VERSION_FILE]

    # Sync build.zig.zon if it exists
    zon_path = os.path.join(dir_path, ZON_FILE)
    if not os.path.exists(zon_path):
        return modified

    with open(zon_path, "r", encoding="utf-8") as f:
        content = f.read()

    new_content, count = _ZON_VERSION_RE.subn(
        rf'\g<1>"{version}"', content, count=1
    )
    if count == 0:
        print(
            "Warning: could not sync version to build.zig.zon",
            file=sys.stderr,
        )
        return modified

    effects.atomic_write_text(zon_path, new_content)
    modified.append(ZON_FILE)

    return modified
