"""Detect native platform file changes in Flutter projects since a given git ref.

Used by the release flow to validate whether an OTA release is safe (Dart-only
changes) or whether a full build release is required (native code changed).
"""

import subprocess

# Directories and file patterns that indicate native platform code changes.
# Changes in these paths require a full build release (not OTA).
NATIVE_PATTERNS = (
    "android/",
    "ios/",
    "macos/",
    "windows/",
    "linux/",
    "web/",
)


def detect_native_changes(project_dir: str, since_ref: str) -> list[str]:
    """Return list of changed native files since a git ref.

    Runs git diff --name-only to find files changed between since_ref and HEAD,
    then filters for files under native platform directories.

    Returns an empty list if no native changes are found (OTA is safe).
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", f"{since_ref}..HEAD", "--", project_dir],
            capture_output=True,
            text=True,
            cwd=project_dir,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []

    if result.returncode != 0:
        return []

    native_files = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        for pattern in NATIVE_PATTERNS:
            if stripped.startswith(pattern) or f"/{pattern}" in stripped:
                native_files.append(stripped)
                break

    return native_files
