"""Maven Central publishing requirements validation, checking POM metadata, GPG signing, javadoc/sources jars, and artifact naming conventions.

Validates that a Maven/Gradle project meets Maven Central's artifact
requirements: complete POM metadata (name, description, url, licenses,
developers, scm), source/javadoc jar generation, and GPG signing
configuration.
"""

import os
import re
import subprocess
import xml.etree.ElementTree as ET


# POM namespace used by Maven 4.0.0 model
_POM_NS = {"m": "http://maven.apache.org/POM/4.0.0"}


def _find_element(root, tag):
    """Find a direct child element, trying with and without namespace."""
    elem = root.find(f"m:{tag}", _POM_NS)
    if elem is None:
        elem = root.find(tag)
    return elem


def _validate_pom_metadata(pom_path):
    """Validate that a POM XML has all required Maven Central elements.

    Returns a list of error strings (empty if all requirements are met).
    """
    errors = []

    try:
        tree = ET.parse(pom_path)
    except ET.ParseError as e:
        return [f"Failed to parse POM XML: {e}"]

    root = tree.getroot()

    # Required top-level elements
    for tag in ("name", "description", "url"):
        elem = _find_element(root, tag)
        if elem is None or not (elem.text and elem.text.strip()):
            errors.append(f"POM missing required element: <{tag}>")

    # licenses -> license -> (name, url)
    licenses = _find_element(root, "licenses")
    if licenses is None:
        errors.append("POM missing required element: <licenses>")
    else:
        license_elems = licenses.findall("m:license", _POM_NS)
        if not license_elems:
            license_elems = licenses.findall("license")
        if not license_elems:
            errors.append("POM <licenses> has no <license> children")
        else:
            for i, lic in enumerate(license_elems):
                lic_name = _find_element(lic, "name")
                if lic_name is None or not (lic_name.text and lic_name.text.strip()):
                    errors.append(f"POM <license>[{i}] missing <name>")
                lic_url = _find_element(lic, "url")
                if lic_url is None or not (lic_url.text and lic_url.text.strip()):
                    errors.append(f"POM <license>[{i}] missing <url>")

    # developers -> developer -> (name)
    developers = _find_element(root, "developers")
    if developers is None:
        errors.append("POM missing required element: <developers>")
    else:
        dev_elems = developers.findall("m:developer", _POM_NS)
        if not dev_elems:
            dev_elems = developers.findall("developer")
        if not dev_elems:
            errors.append("POM <developers> has no <developer> children")
        else:
            for i, dev in enumerate(dev_elems):
                dev_name = _find_element(dev, "name")
                if dev_name is None or not (dev_name.text and dev_name.text.strip()):
                    errors.append(f"POM <developer>[{i}] missing <name>")

    # scm -> (connection, developerConnection, url)
    scm = _find_element(root, "scm")
    if scm is None:
        errors.append("POM missing required element: <scm>")
    else:
        for scm_tag in ("connection", "developerConnection", "url"):
            scm_elem = _find_element(scm, scm_tag)
            if scm_elem is None or not (scm_elem.text and scm_elem.text.strip()):
                errors.append(f"POM <scm> missing <{scm_tag}>")

    return errors


def _check_source_javadoc_jars(dir_path):
    """Check that sources and javadoc jar generation is configured.

    Looks for java or kotlin source/javadoc plugins in build.gradle.kts
    or build.gradle. Returns a list of error strings.
    """
    errors = []

    # Patterns that indicate sources/javadoc jar generation
    sources_patterns = [
        r"withSourcesJar\s*\(",       # Kotlin/Gradle DSL
        r"withSourcesJar\b",           # Gradle DSL property
        r"java\s*\{[^}]*withSourcesJar",  # java { withSourcesJar() }
        r"sourcesJar",                 # Custom task name
        r"com\.vanniktech\.maven\.publish",  # vanniktech plugin auto-handles sources
        r"maven-source-plugin",        # Maven source plugin
    ]

    javadoc_patterns = [
        r"withJavadocJar\s*\(",       # Kotlin/Gradle DSL
        r"withJavadocJar\b",           # Gradle DSL property
        r"java\s*\{[^}]*withJavadocJar",  # java { withJavadocJar() }
        r"javadocJar",                 # Custom task name / Dokka
        r"dokka",                      # Dokka plugin for Kotlin docs
        r"com\.vanniktech\.maven\.publish",  # vanniktech plugin auto-handles javadoc
        r"maven-javadoc-plugin",       # Maven javadoc plugin
    ]

    build_content = ""
    for name in ("build.gradle.kts", "build.gradle"):
        path = os.path.join(dir_path, name)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                build_content += f.read() + "\n"

    # Also check pom.xml for Maven plugins
    pom_path = os.path.join(dir_path, "pom.xml")
    if os.path.exists(pom_path):
        with open(pom_path, "r", encoding="utf-8") as f:
            build_content += f.read() + "\n"

    if not build_content.strip():
        return ["No build file found to check for sources/javadoc jar generation"]

    has_sources = any(re.search(p, build_content) for p in sources_patterns)
    has_javadoc = any(re.search(p, build_content) for p in javadoc_patterns)

    if not has_sources:
        errors.append(
            "No -sources.jar generation detected (add withSourcesJar(), "
            "maven-source-plugin, or vanniktech maven-publish plugin)"
        )
    if not has_javadoc:
        errors.append(
            "No -javadoc.jar generation detected (add withJavadocJar(), "
            "dokka, maven-javadoc-plugin, or vanniktech maven-publish plugin)"
        )

    return errors


def _check_signing_configuration(dir_path):
    """Check that GPG signing is configured for Maven Central publishing.

    Looks for:
    - vanniktech maven-publish plugin (handles signing automatically)
    - Gradle signing plugin + useInMemoryPgpKeys or signAllPublications
    - Maven GPG plugin (maven-gpg-plugin)

    Returns a list of error strings (empty if signing is detected).
    """
    signing_patterns = [
        r"com\.vanniktech\.maven\.publish",  # vanniktech plugin auto-handles signing
        r"useInMemoryPgpKeys",               # Gradle in-memory PGP signing
        r"signAllPublications",              # Gradle signing DSL
        r"maven-gpg-plugin",                 # Maven GPG plugin
    ]

    build_content = ""
    for name in ("build.gradle.kts", "build.gradle"):
        path = os.path.join(dir_path, name)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                build_content += f.read() + "\n"

    # Also check pom.xml for Maven GPG plugin
    pom_path = os.path.join(dir_path, "pom.xml")
    if os.path.exists(pom_path):
        with open(pom_path, "r", encoding="utf-8") as f:
            build_content += f.read() + "\n"

    if not build_content.strip():
        return ["No build file found to check for GPG signing configuration"]

    has_signing = any(re.search(p, build_content) for p in signing_patterns)
    if not has_signing:
        return [
            "No GPG signing configuration detected (add vanniktech maven-publish "
            "plugin, signing { useInMemoryPgpKeys(...); sign(publishing.publications) }, "
            "or maven-gpg-plugin)"
        ]

    return []


def _try_generate_pom(dir_path):
    """Try to generate POM via Gradle and return the path to the generated POM.

    Returns the POM path if successful, or None if Gradle is not available
    or the task fails.
    """
    gradlew = os.path.join(dir_path, "gradlew")
    if not os.path.exists(gradlew):
        return None

    try:
        result = subprocess.run(
            ["./gradlew", "generatePomFileForMavenPublication"],
            cwd=dir_path,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            return None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None

    # Look for the generated POM in the standard Gradle output location
    build_dir = os.path.join(dir_path, "build", "publications", "maven")
    pom_path = os.path.join(build_dir, "pom-default.xml")
    if os.path.exists(pom_path):
        return pom_path

    return None


def validate_maven_central_metadata(dir_path):
    """Validate that a Maven/Gradle project meets Maven Central requirements.

    Returns a list of error strings (empty if all requirements are met).

    Checks:
    1. POM metadata (name, description, url, licenses, developers, scm)
    2. Sources and javadoc jar generation
    3. GPG signing configuration
    """
    errors = []

    # Try to generate POM via Gradle first
    generated_pom = _try_generate_pom(dir_path)

    # Find the POM to validate (prefer generated, fall back to pom.xml)
    pom_path = generated_pom
    if pom_path is None:
        pom_path = os.path.join(dir_path, "pom.xml")
        if not os.path.exists(pom_path):
            # For Gradle-only projects without pom.xml and without gradlew,
            # we cannot validate POM metadata
            errors.append(
                "No POM available for validation (no pom.xml and "
                "Gradle POM generation failed or unavailable)"
            )
            # Still check for source/javadoc jars and signing
            errors.extend(_check_source_javadoc_jars(dir_path))
            errors.extend(_check_signing_configuration(dir_path))
            return errors

    # Validate POM metadata
    errors.extend(_validate_pom_metadata(pom_path))

    # Check for source/javadoc jar generation
    errors.extend(_check_source_javadoc_jars(dir_path))

    # Check for GPG signing configuration
    errors.extend(_check_signing_configuration(dir_path))

    return errors
