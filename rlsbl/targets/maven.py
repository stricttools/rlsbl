"""Maven and Gradle release target supporting version management across pom.xml, build.gradle, build.gradle.kts, gradle.properties, and gradle/libs.versions.toml files."""

import json
import os
import re
import subprocess
import xml.etree.ElementTree as ET

import tomlkit

from .base import BaseTarget
from ..errors import VersionError
from ..utils import run


class MavenTarget(BaseTarget):
    """Release target for Maven/Gradle (Java/Kotlin) projects."""

    detection_files = ("build.gradle.kts", "build.gradle", "pom.xml")
    capabilities = frozenset({"read_name", "read_metadata", "ci_templates"})
    ecosystem = "Java / Maven"

    @property
    def name(self):
        return "maven"

    def _read_project_name(self, dir_path):
        """Extract project name from pom.xml, build.gradle.kts, or build.gradle.

        Returns the name string if found, or None.
        """
        # Try pom.xml for groupId:artifactId
        pom_path = os.path.join(dir_path, "pom.xml")
        if os.path.exists(pom_path):
            ns = {"m": "http://maven.apache.org/POM/4.0.0"}
            try:
                tree = ET.parse(pom_path)
                root = tree.getroot()
                group = root.find("m:groupId", ns)
                if group is None:
                    group = root.find("groupId")
                artifact = root.find("m:artifactId", ns)
                if artifact is None:
                    artifact = root.find("artifactId")
                parts = []
                if group is not None and group.text:
                    parts.append(group.text.strip())
                if artifact is not None and artifact.text:
                    parts.append(artifact.text.strip())
                if parts:
                    return ":".join(parts)
            except ET.ParseError:
                pass

        # Try build.gradle.kts for group
        kts = os.path.join(dir_path, "build.gradle.kts")
        if os.path.exists(kts):
            with open(kts, "r", encoding="utf-8") as f:
                content = f.read()
            group_m = re.search(r'group\s*=\s*"([^"]+)"', content)
            if group_m:
                return group_m.group(1)

        # Try build.gradle for group
        groovy = os.path.join(dir_path, "build.gradle")
        if os.path.exists(groovy):
            with open(groovy, "r", encoding="utf-8") as f:
                content = f.read()
            group_m = re.search(r"""group\s*=?\s*['"]([^'"]+)['"]""", content)
            if group_m:
                return group_m.group(1)

        return None

    def read_name(self, dir_path, ctx):
        """Read the project name (groupId:artifactId or group) from build files."""
        return self._read_project_name(dir_path)

    def read_metadata(self, dir_path):
        """Read metadata from pom.xml, build.gradle.kts, or build.gradle.

        Extracts available fields:
        - POM: groupId, artifactId, version, name, description, url, licenses
        - Gradle: group, version, description

        Returns a dict with string keys and values. Only fields that are
        present in the build file are included.
        """
        # Try pom.xml first (richest metadata)
        pom_path = os.path.join(dir_path, "pom.xml")
        if os.path.isfile(pom_path):
            result = self._read_pom_metadata(pom_path)
            if result:
                return result

        # Try build.gradle.kts
        kts_path = os.path.join(dir_path, "build.gradle.kts")
        if os.path.isfile(kts_path):
            return self._read_gradle_metadata(kts_path, kts=True)

        # Try build.gradle (Groovy)
        groovy_path = os.path.join(dir_path, "build.gradle")
        if os.path.isfile(groovy_path):
            return self._read_gradle_metadata(groovy_path, kts=False)

        return {}

    def _read_pom_metadata(self, pom_path):
        """Extract metadata from a pom.xml file."""
        ns = {"m": "http://maven.apache.org/POM/4.0.0"}
        try:
            tree = ET.parse(pom_path)
        except ET.ParseError:
            return {}
        root = tree.getroot()

        result = {}

        def _find_text(tag):
            elem = root.find(f"m:{tag}", ns)
            if elem is None:
                elem = root.find(tag)
            if elem is not None and elem.text:
                return elem.text.strip()
            return None

        group_id = _find_text("groupId")
        if group_id:
            result["groupId"] = group_id

        artifact_id = _find_text("artifactId")
        if artifact_id:
            result["artifactId"] = artifact_id

        version = _find_text("version")
        if version:
            result["version"] = version

        name = _find_text("name")
        if name:
            result["name"] = name

        description = _find_text("description")
        if description:
            result["description"] = description

        url = _find_text("url")
        if url:
            result["url"] = url

        # Extract licenses: look for <licenses><license><name>...</name></license></licenses>
        licenses_elem = root.find("m:licenses", ns)
        if licenses_elem is None:
            licenses_elem = root.find("licenses")
        if licenses_elem is not None:
            license_names = []
            for lic in licenses_elem:
                # Handle both namespaced and non-namespaced
                lic_name = lic.find("m:name", ns)
                if lic_name is None:
                    lic_name = lic.find("name")
                if lic_name is not None and lic_name.text:
                    license_names.append(lic_name.text.strip())
            if license_names:
                result["license"] = ", ".join(license_names)

        return result

    @staticmethod
    def _read_gradle_metadata(gradle_path, *, kts):
        """Extract metadata from a build.gradle or build.gradle.kts file."""
        try:
            with open(gradle_path, "r", encoding="utf-8") as f:
                content = f.read()
        except (OSError, UnicodeDecodeError):
            return {}

        result = {}

        if kts:
            # Kotlin DSL: group = "com.example"
            group_m = re.search(r'group\s*=\s*"([^"]+)"', content)
            if group_m:
                result["group"] = group_m.group(1)

            version_m = re.search(r'version\s*=\s*"([^"]+)"', content)
            if version_m:
                result["version"] = version_m.group(1)

            desc_m = re.search(r'description\s*=\s*"([^"]+)"', content)
            if desc_m:
                result["description"] = desc_m.group(1)
        else:
            # Groovy DSL: group = 'com.example' or group 'com.example'
            group_m = re.search(r"""group\s*=?\s*['"]([^'"]+)['"]""", content)
            if group_m:
                result["group"] = group_m.group(1)

            version_m = re.search(r"""version\s*=?\s*['"]([^'"]+)['"]""", content)
            if version_m:
                result["version"] = version_m.group(1)

            desc_m = re.search(r"""description\s*=?\s*['"]([^'"]+)['"]""", content)
            if desc_m:
                result["description"] = desc_m.group(1)

        return result

    def _is_android_app(self, dir_path):
        """Check if a gradle file declares the Android application plugin."""
        for name in ("build.gradle.kts", "build.gradle"):
            path = os.path.join(dir_path, name)
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                if "com.android.application" in content:
                    return True
        return False

    def detect(self, dir_path):
        """Detect if dir has build.gradle.kts, build.gradle, or pom.xml.

        Returns False for Android application projects (those use native-android).
        """
        has_gradle = (
            os.path.exists(os.path.join(dir_path, "build.gradle.kts"))
            or os.path.exists(os.path.join(dir_path, "build.gradle"))
        )
        if has_gradle and self._is_android_app(dir_path):
            return False
        return (
            has_gradle
            or os.path.exists(os.path.join(dir_path, "pom.xml"))
        )

    @staticmethod
    def _load_version_catalog_key(dir_path):
        """Load the version_catalog_key from .rlsbl/config.json.

        Returns the key string or raises VersionError if not configured.
        """
        config_path = os.path.join(dir_path, ".rlsbl", "config.json")
        if not os.path.exists(config_path):
            raise VersionError(
                "Gradle version catalog detected but no .rlsbl/config.json found. "
                'Set "version_catalog_key" in config to specify which [versions] '
                "entry holds the project version."
            )
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        key = config.get("version_catalog_key")
        if not key:
            raise VersionError(
                "Gradle version catalog detected but version_catalog_key is not set "
                "in .rlsbl/config.json. Add a version_catalog_key field specifying "
                "which [versions] entry holds the project version "
                '(e.g., "version_catalog_key": "app-version").'
            )
        return key

    def _find_version_file(self, dir_path):
        """Return (filepath, format) tuple for the version source."""
        # Priority 0: Gradle version catalog
        catalog_path = os.path.join(dir_path, "gradle", "libs.versions.toml")
        if os.path.exists(catalog_path):
            return catalog_path, "version_catalog"

        # Priority 1: gradle.properties
        gp = os.path.join(dir_path, "gradle.properties")
        if os.path.exists(gp):
            with open(gp, "r", encoding="utf-8") as f:
                content = f.read()
            if re.search(r"^(?:VERSION_NAME|version)\s*=", content, re.MULTILINE):
                return gp, "gradle_properties"

        # Priority 2: build.gradle.kts
        kts = os.path.join(dir_path, "build.gradle.kts")
        if os.path.exists(kts):
            return kts, "gradle_kts"

        # Priority 3: build.gradle
        groovy = os.path.join(dir_path, "build.gradle")
        if os.path.exists(groovy):
            return groovy, "gradle_groovy"

        # Priority 4: pom.xml
        pom = os.path.join(dir_path, "pom.xml")
        if os.path.exists(pom):
            return pom, "pom"

        return None, None

    def _read_version_catalog(self, dir_path, catalog_path):
        """Read version from a Gradle version catalog (libs.versions.toml).

        Requires version_catalog_key in .rlsbl/config.json to specify which
        [versions] entry holds the project version.
        """
        key = self._load_version_catalog_key(dir_path)

        with open(catalog_path, "r", encoding="utf-8") as f:
            doc = tomlkit.load(f)

        versions = doc.get("versions")
        if versions is None:
            raise VersionError(
                f"No [versions] section found in {catalog_path}"
            )

        if key not in versions:
            raise VersionError(
                f"Key {key!r} not found in [versions] section of {catalog_path}"
            )

        value = versions[key]
        # Rich version declarations are TOML tables/inline-tables, not strings
        if isinstance(value, dict):
            raise VersionError(
                f"Rich version declaration for {key!r} in {catalog_path}. "
                "rlsbl cannot write complex version objects "
                "(e.g., {{strictly = ...}}, {{require = ...}}). "
                "Use a plain string version instead."
            )

        return str(value)

    def _write_version_catalog(self, dir_path, catalog_path, version):
        """Write version to a Gradle version catalog (libs.versions.toml).

        Returns the relative path of the modified file.
        """
        key = self._load_version_catalog_key(dir_path)

        with open(catalog_path, "r", encoding="utf-8") as f:
            doc = tomlkit.load(f)

        versions = doc.get("versions")
        if versions is None:
            raise VersionError(
                f"No [versions] section found in {catalog_path}"
            )

        if key not in versions:
            raise VersionError(
                f"Key {key!r} not found in [versions] section of {catalog_path}"
            )

        current = versions[key]
        if isinstance(current, dict):
            raise VersionError(
                f"Rich version declaration for {key!r} in {catalog_path}. "
                "rlsbl cannot write complex version objects "
                "(e.g., {{strictly = ...}}, {{require = ...}}). "
                "Use a plain string version instead."
            )

        versions[key] = version

        # Atomic write
        tmp_path = catalog_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            tomlkit.dump(doc, f)
        os.replace(tmp_path, catalog_path)

        return os.path.relpath(catalog_path, dir_path)

    def read_version(self, dir_path):
        """Read version from the detected version source."""
        filepath, fmt = self._find_version_file(dir_path)
        if filepath is None:
            raise VersionError(f"No version source found in {dir_path}")

        if fmt == "version_catalog":
            return self._read_version_catalog(dir_path, filepath)

        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()

        if fmt == "gradle_properties":
            # Match VERSION_NAME=X.Y.Z or version=X.Y.Z
            m = re.search(
                r"^(?:VERSION_NAME|version)\s*=\s*(.+)$", content, re.MULTILINE
            )
            if m:
                return m.group(1).strip()
            raise VersionError(f"No version key found in {filepath}")

        elif fmt == "gradle_kts":
            # version = "X.Y.Z"
            m = re.search(r'version\s*=\s*"([^"]+)"', content)
            if m:
                return m.group(1)
            raise VersionError(f"No version found in {filepath}")

        elif fmt == "gradle_groovy":
            # version = 'X.Y.Z' or version "X.Y.Z" or version = "X.Y.Z"
            m = re.search(r"""version\s*=?\s*['"]([^'"]+)['"]""", content)
            if m:
                return m.group(1)
            raise VersionError(f"No version found in {filepath}")

        elif fmt == "pom":
            ns = {"m": "http://maven.apache.org/POM/4.0.0"}
            tree = ET.parse(filepath)
            root = tree.getroot()
            # Try with namespace first
            version_elem = root.find("m:version", ns)
            if version_elem is None:
                # Try without namespace
                version_elem = root.find("version")
            if version_elem is not None and version_elem.text:
                return version_elem.text.strip()
            raise VersionError(f"No <version> found in {filepath}")

        raise VersionError(f"Unknown format: {fmt}")

    def write_version(self, dir_path, version, ctx):
        """Write version to the same file it was read from.

        Returns a list of relative file paths (relative to dir_path) that
        were modified.
        """
        filepath, fmt = self._find_version_file(dir_path)
        if filepath is None:
            raise VersionError(f"No version source found in {dir_path}")

        if fmt == "version_catalog":
            rel = self._write_version_catalog(dir_path, filepath, version)
            return [rel]

        rel_path = os.path.relpath(filepath, dir_path)

        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()

        if fmt == "gradle_properties":
            # Replace VERSION_NAME=... or version=...
            new_content = re.sub(
                r"^((?:VERSION_NAME|version)\s*=\s*)(.+)$",
                lambda m: m.group(1) + version,
                content,
                count=1,
                flags=re.MULTILINE,
            )

        elif fmt == "gradle_kts":
            # Replace version = "X.Y.Z"
            new_content = re.sub(
                r'(version\s*=\s*)"[^"]+"',
                lambda m: m.group(1) + f'"{version}"',
                content,
                count=1,
            )

        elif fmt == "gradle_groovy":
            # Replace version = 'X.Y.Z' or version "X.Y.Z" or version = "X.Y.Z"
            new_content = re.sub(
                r"""(version\s*=?\s*)(['"])[^'"]+\2""",
                lambda m: m.group(1) + m.group(2) + version + m.group(2),
                content,
                count=1,
            )

        elif fmt == "pom":
            ns = {"m": "http://maven.apache.org/POM/4.0.0"}
            tree = ET.parse(filepath)
            root = tree.getroot()
            version_elem = root.find("m:version", ns)
            if version_elem is None:
                version_elem = root.find("version")
            if version_elem is not None:
                version_elem.text = version
            # Write back, preserving XML declaration if present
            tmp_path = filepath + ".tmp"
            tree.write(tmp_path, xml_declaration=True, encoding="unicode")
            # Ensure trailing newline
            with open(tmp_path, "r", encoding="utf-8") as f:
                xml_content = f.read()
            if not xml_content.endswith("\n"):
                with open(tmp_path, "a", encoding="utf-8") as f:
                    f.write("\n")
            os.replace(tmp_path, filepath)
            return [rel_path]

        else:
            raise VersionError(f"Unknown format: {fmt}")

        # Atomic write for non-XML formats
        tmp_path = filepath + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(new_content)
        os.replace(tmp_path, filepath)
        return [rel_path]

    def version_file(self, dir_path=None):
        # Dynamic: depends on project. Return None and let callers use read_version.
        return None

    def tag_format(self, version):
        return f"v{version}"

    def template_dir(self):
        return os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "templates", "maven"
        )

    def template_vars(self, dir_path, ctx):
        """Extract template variables from the project."""
        name = self._read_project_name(dir_path) or ""

        # Author from git config
        from .utils import _get_git_author
        author = _get_git_author()

        # Read version from detected source
        try:
            version = self.read_version(dir_path)
        except (VersionError, FileNotFoundError):
            version = "0.0.0"

        return {
            "name": name,
            "version": version,
            "author": author,
            "publishSetup": "Requires GITHUB_TOKEN secret (auto-provided for GitHub Packages)",
        }

    def template_mappings(self, ctx):
        return [
            {"template": "ci.yml.tpl", "target": ".github/workflows/ci.yml"},
        ]

    def check_project_exists(self, dir_path):
        return self.detect(dir_path)

    def get_project_init_hint(self):
        return 'Run "gradle init" or create a pom.xml first'

    def build(self, dir_path, version):
        """Run the project build step.

        Uses ./gradlew build for Gradle projects, mvn package for Maven.
        """
        gradlew = os.path.join(dir_path, "gradlew")
        if os.path.exists(gradlew):
            result = subprocess.run(
                ["./gradlew", "build"], cwd=dir_path,
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"./gradlew build failed (exit {result.returncode}):\n"
                    f"{result.stderr}"
                )
            return

        pom_path = os.path.join(dir_path, "pom.xml")
        if os.path.exists(pom_path):
            result = subprocess.run(
                ["mvn", "package"], cwd=dir_path,
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"mvn package failed (exit {result.returncode}):\n"
                    f"{result.stderr}"
                )
            return

    @staticmethod
    def detect_lint_command(dir_path):
        """Detect the appropriate lint command for a Gradle project.

        Checks build.gradle.kts and build.gradle for lint plugins:
        - detekt plugin -> ./gradlew detekt
        - checkstyle plugin -> ./gradlew checkstyleMain
        - neither -> ./gradlew check (fallback)

        Returns a list of command args, or None if no Gradle wrapper found.
        """
        gradlew = os.path.join(dir_path, "gradlew")
        if not os.path.exists(gradlew):
            return None

        # Check build files for lint plugins
        for build_file in ("build.gradle.kts", "build.gradle"):
            path = os.path.join(dir_path, build_file)
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                if "detekt" in content:
                    return ["./gradlew", "detekt"]
                if "checkstyle" in content:
                    return ["./gradlew", "checkstyleMain"]

        # Fallback
        return ["./gradlew", "check"]
