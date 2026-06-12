"""Native iOS release target supporting version management via MARKETING_VERSION in Xcode project files and Tuist Project.swift."""

import glob
import os
import re
import tempfile

from .base import BaseTarget
from ..errors import VersionError


class NativeIosTarget(BaseTarget):
    """Release target for native iOS apps using Xcode or Tuist."""

    detection_files = ()  # Content-based detection
    capabilities = frozenset({"read_name"})
    ecosystem = "iOS"
    auto_detectable = "yes"

    @property
    def name(self):
        return "native-ios"

    def _find_pbxproj(self, dir_path):
        """Find the first .xcodeproj/project.pbxproj with version keys.

        Returns the path to the pbxproj file, or None.
        """
        xcodeproj_dirs = glob.glob(os.path.join(dir_path, "*.xcodeproj"))
        for xcp in xcodeproj_dirs:
            if not os.path.isdir(xcp):
                continue
            pbxproj = os.path.join(xcp, "project.pbxproj")
            if os.path.isfile(pbxproj):
                with open(pbxproj, "r", encoding="utf-8") as f:
                    content = f.read()
                if "MARKETING_VERSION" in content or "CURRENT_PROJECT_VERSION" in content:
                    return pbxproj
        return None

    def _find_tuist(self, dir_path):
        """Find Project.swift with CFBundleShortVersionString.

        Returns the path to Project.swift, or None.
        """
        project_swift = os.path.join(dir_path, "Project.swift")
        if os.path.isfile(project_swift):
            with open(project_swift, "r", encoding="utf-8") as f:
                content = f.read()
            if "CFBundleShortVersionString" in content:
                return project_swift
        return None

    def detect(self, dir_path):
        """Detect native iOS project. Rejects SPM projects (Package.swift)."""
        if os.path.exists(os.path.join(dir_path, "Package.swift")):
            return False
        if self._find_pbxproj(dir_path) is not None:
            return True
        if self._find_tuist(dir_path) is not None:
            return True
        return False

    def read_version(self, dir_path):
        """Read marketing version from pbxproj or Tuist Project.swift."""
        pbxproj = self._find_pbxproj(dir_path)
        if pbxproj is not None:
            with open(pbxproj, "r", encoding="utf-8") as f:
                content = f.read()
            m = re.search(r"MARKETING_VERSION\s*=\s*([^;]+);", content)
            if m:
                return m.group(1).strip()

        tuist = self._find_tuist(dir_path)
        if tuist is not None:
            with open(tuist, "r", encoding="utf-8") as f:
                content = f.read()
            m = re.search(r'"CFBundleShortVersionString":\s*"([^"]+)"', content)
            if m:
                return m.group(1)

        raise VersionError(f"No iOS version source found in {dir_path}")

    def write_version(self, dir_path, version, ctx):
        """Write version to pbxproj or Tuist, incrementing build number.

        Returns a list of relative file paths that were modified.
        """
        modified = []

        pbxproj = self._find_pbxproj(dir_path)
        if pbxproj is not None:
            with open(pbxproj, "r", encoding="utf-8") as f:
                content = f.read()

            # Replace MARKETING_VERSION
            content = re.sub(
                r"(MARKETING_VERSION\s*=\s*)[^;]+(;)",
                rf"\g<1>{version}\2",
                content,
            )

            # Increment CURRENT_PROJECT_VERSION
            m = re.search(r"CURRENT_PROJECT_VERSION\s*=\s*([^;]+);", content)
            if m:
                raw = m.group(1).strip()
                try:
                    build_num = int(raw) + 1
                except ValueError:
                    build_num = 1
                content = re.sub(
                    r"(CURRENT_PROJECT_VERSION\s*=\s*)[^;]+(;)",
                    rf"\g<1>{build_num}\2",
                    content,
                )

            rel_path = os.path.relpath(pbxproj, dir_path)
            _atomic_write(pbxproj, content)
            modified.append(rel_path)
            return modified

        tuist = self._find_tuist(dir_path)
        if tuist is not None:
            with open(tuist, "r", encoding="utf-8") as f:
                content = f.read()

            # Replace CFBundleShortVersionString
            content = re.sub(
                r'("CFBundleShortVersionString":\s*")[^"]+"',
                rf'\g<1>{version}"',
                content,
            )

            # Increment CFBundleVersion
            m = re.search(r'"CFBundleVersion":\s*"(\d+)"', content)
            if m:
                build_num = int(m.group(1)) + 1
                content = re.sub(
                    r'("CFBundleVersion":\s*")\d+"',
                    rf'\g<1>{build_num}"',
                    content,
                )

            rel_path = os.path.relpath(tuist, dir_path)
            _atomic_write(tuist, content)
            modified.append(rel_path)
            return modified

        raise VersionError(f"No iOS version source found in {dir_path}")

    def version_file(self, dir_path=None):
        # Dynamic: xcodeproj name varies per project.
        return None

    def read_name(self, dir_path, ctx):
        """Return the xcodeproj name without .xcodeproj suffix, or directory basename."""
        xcodeproj_dirs = glob.glob(os.path.join(dir_path, "*.xcodeproj"))
        for xcp in xcodeproj_dirs:
            if os.path.isdir(xcp):
                return os.path.splitext(os.path.basename(xcp))[0]
        return os.path.basename(os.path.abspath(dir_path))

    def template_dir(self):
        return None

    def template_mappings(self, ctx):
        return []


def _atomic_write(path, content):
    """Write content to path atomically via tmpfile + os.replace."""
    dir_name = os.path.dirname(path)
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, path)
    except BaseException:
        # Clean up temp file on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
