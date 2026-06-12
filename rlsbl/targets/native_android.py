"""Native Android release target that reads and bumps versionName and auto-increments versionCode in build.gradle for Android app releases."""

import os
import re

from .base import BaseTarget
from ..errors import VersionError


class NativeAndroidTarget(BaseTarget):
    """Release target for native Android apps (build.gradle with com.android.application)."""

    # Content-based detection — no manifest conflicts with maven's detection_files.
    detection_files = ()
    capabilities = frozenset({"read_name"})
    ecosystem = "Android"
    auto_detectable = "yes"

    @property
    def name(self):
        return "native-android"

    def _gradle_file(self, dir_path):
        """Return the absolute path to the gradle build file (kts preferred)."""
        kts = os.path.join(dir_path, "build.gradle.kts")
        if os.path.exists(kts):
            return kts
        return os.path.join(dir_path, "build.gradle")

    def _is_android_app(self, content):
        """Check if gradle content declares the com.android.application plugin."""
        return "com.android.application" in content

    def detect(self, dir_path):
        """Detect if dir has a build.gradle(.kts) with the Android application plugin.

        Returns False for com.android.library projects (those stay with maven).
        """
        gradle_path = self._gradle_file(dir_path)
        if not os.path.exists(gradle_path):
            return False
        with open(gradle_path, "r", encoding="utf-8") as f:
            content = f.read()
        return self._is_android_app(content)

    def read_version(self, dir_path):
        """Read versionName from build.gradle."""
        gradle_path = self._gradle_file(dir_path)
        with open(gradle_path, "r", encoding="utf-8") as f:
            content = f.read()
        m = re.search(r'versionName\s+"([^"]+)"', content)
        if not m:
            raise VersionError(f"No versionName found in {gradle_path}")
        return m.group(1)

    def write_version(self, dir_path, version, ctx):
        """Write versionName and increment versionCode in build.gradle.

        Returns a list of relative file paths that were modified.
        """
        gradle_path = self._gradle_file(dir_path)
        rel_path = os.path.relpath(gradle_path, dir_path)

        with open(gradle_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Update versionName
        content = re.sub(
            r'(versionName\s+")[^"]+"',
            rf'\g<1>{version}"',
            content,
            count=1,
        )

        # Increment versionCode
        code_match = re.search(r"versionCode\s+(\d+)", content)
        if code_match:
            new_code = int(code_match.group(1)) + 1
            content = re.sub(
                r"(versionCode\s+)\d+",
                rf"\g<1>{new_code}",
                content,
                count=1,
            )

        # Atomic write
        tmp_path = gradle_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, gradle_path)
        return [rel_path]

    def version_file(self, dir_path=None):
        """Return the gradle build filename.

        When dir_path is provided, checks which variant exists on disk.
        """
        if dir_path is not None:
            kts = os.path.join(dir_path, "build.gradle.kts")
            if os.path.exists(kts):
                return "build.gradle.kts"
        return "build.gradle"

    def read_name(self, dir_path, ctx):
        """Read applicationId from build.gradle, falling back to directory name."""
        gradle_path = self._gradle_file(dir_path)
        if not os.path.exists(gradle_path):
            return os.path.basename(os.path.abspath(dir_path))
        with open(gradle_path, "r", encoding="utf-8") as f:
            content = f.read()
        m = re.search(r'applicationId\s*[=]?\s*"([^"]+)"', content)
        if m:
            return m.group(1)
        return os.path.basename(os.path.abspath(dir_path))

    def template_dir(self):
        return None

    def template_mappings(self, ctx):
        return []
