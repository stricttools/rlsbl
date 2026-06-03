"""Maven and Gradle release target supporting version management across pom.xml, build.gradle, build.gradle.kts, and gradle.properties files."""

import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

from .base import BaseTarget
from ..config import get_publish_config
from ..utils import run


class MavenTarget(BaseTarget):
    """Release target for Maven/Gradle (Java/Kotlin) projects."""

    detection_files = ("build.gradle.kts", "build.gradle", "pom.xml")

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
        """Maven/Gradle metadata extraction not yet implemented."""
        return {}

    def detect(self, dir_path):
        """Detect if dir has build.gradle.kts, build.gradle, or pom.xml."""
        return (
            os.path.exists(os.path.join(dir_path, "build.gradle.kts"))
            or os.path.exists(os.path.join(dir_path, "build.gradle"))
            or os.path.exists(os.path.join(dir_path, "pom.xml"))
        )

    def _find_version_file(self, dir_path):
        """Return (filepath, format) tuple for the version source."""
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

    def read_version(self, dir_path):
        """Read version from the detected version source."""
        filepath, fmt = self._find_version_file(dir_path)
        if filepath is None:
            raise ValueError(f"No version source found in {dir_path}")

        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()

        if fmt == "gradle_properties":
            # Match VERSION_NAME=X.Y.Z or version=X.Y.Z
            m = re.search(
                r"^(?:VERSION_NAME|version)\s*=\s*(.+)$", content, re.MULTILINE
            )
            if m:
                return m.group(1).strip()
            raise ValueError(f"No version key found in {filepath}")

        elif fmt == "gradle_kts":
            # version = "X.Y.Z"
            m = re.search(r'version\s*=\s*"([^"]+)"', content)
            if m:
                return m.group(1)
            raise ValueError(f"No version found in {filepath}")

        elif fmt == "gradle_groovy":
            # version = 'X.Y.Z' or version "X.Y.Z" or version = "X.Y.Z"
            m = re.search(r"""version\s*=?\s*['"]([^'"]+)['"]""", content)
            if m:
                return m.group(1)
            raise ValueError(f"No version found in {filepath}")

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
            raise ValueError(f"No <version> found in {filepath}")

        raise ValueError(f"Unknown format: {fmt}")

    def write_version(self, dir_path, version, ctx):
        """Write version to the same file it was read from.

        Returns a list of relative file paths (relative to dir_path) that
        were modified.
        """
        filepath, fmt = self._find_version_file(dir_path)
        if filepath is None:
            raise ValueError(f"No version source found in {dir_path}")

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
            raise ValueError(f"Unknown format: {fmt}")

        # Atomic write for non-XML formats
        tmp_path = filepath + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(new_content)
        os.replace(tmp_path, filepath)
        return [rel_path]

    def version_file(self):
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
        author = ""
        try:
            author = run("git", ["config", "user.name"])
        except Exception:
            pass

        # Read version from detected source
        try:
            version = self.read_version(dir_path)
        except (ValueError, FileNotFoundError):
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
            {"template": "publish.yml.tpl", "target": ".github/workflows/publish.yml"},
        ]

    def publish(self, dir_path, version, ctx):
        """Publish via Gradle or Maven based on per-target config and token availability.

        ctx: ProjectContext carrying project_root, monorepo_root, and config.
        """
        pub_config = get_publish_config(self.name, ctx.config)

        if pub_config.get("local") is False:
            print(f"Skipping local {self.name} publish (config: local=false). CI will handle it.")
            return

        token_var = pub_config.get("token_var", "GITHUB_TOKEN")
        token = os.environ.get(token_var)
        if not token:
            if pub_config.get("local") is True:
                print(
                    f"ERROR: {self.name} publish requested (local=true) but {token_var} is not set.",
                    file=sys.stderr,
                )
                sys.exit(1)
            print(
                f"Skipping local Maven/Gradle publish (no {token_var}). CI will handle it."
            )
            return

        gradlew = os.path.join(dir_path, "gradlew")
        if os.path.exists(gradlew):
            try:
                run("./gradlew", ["publish"], env={**os.environ})
                print(f"Published via Gradle: {version}")
            except subprocess.CalledProcessError as exc:
                raise RuntimeError(f"Gradle publish failed: {exc}") from exc
        elif os.path.exists(os.path.join(dir_path, "pom.xml")):
            try:
                run("mvn", ["deploy"], env={**os.environ})
                print(f"Published via Maven: {version}")
            except subprocess.CalledProcessError as exc:
                raise RuntimeError(f"Maven deploy failed: {exc}") from exc
        else:
            print("No gradlew or pom.xml found for publish.")

    def check_project_exists(self, dir_path):
        return self.detect(dir_path)

    def get_project_init_hint(self):
        return 'Run "gradle init" or create a pom.xml first'
