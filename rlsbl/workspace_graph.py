"""Dependency graph builder for monorepo workspaces that parses project manifests and provides topological sorting for ordered operations."""

import heapq
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import deque, namedtuple
from typing import Protocol, runtime_checkable

import tomlkit

from .errors import WorkspaceError
from .targets.utils import normalize_pypi


Dependency = namedtuple("Dependency", ["name", "dep_type", "constraint", "scope"])
Dependency.__new__.__defaults__ = ("runtime",)


@runtime_checkable
class WorkspaceScanner(Protocol):
    """Protocol for pluggable workspace dependency scanners."""

    def scan(self, project_dir: str, workspace_names: set[str]) -> list[Dependency]:
        """Scan a project directory for intra-workspace dependencies.

        project_dir: absolute path to the project directory.
        workspace_names: set of all workspace project names (raw, unnormalized).
        Returns a list of Dependency namedtuples for deps found within the workspace.
        """
        ...


class CycleError(Exception):
    """Raised when the workspace dependency graph contains a cycle."""


class ManifestScanError(Exception):
    """A manifest a scanner could not read or parse.

    Raised by the scanners rather than swallowed into an empty dependency
    list.  A failed scan contributes NO edges, and edges are what a dependent's
    CI paths filter is derived from, so a swallowed failure quietly NARROWS
    that filter: the dependent stops reacting to changes in its dependency's
    territory, its job concludes ``skipped`` on the very commit a release tags,
    and the freshness check -- re-deriving from the same broken manifest --
    agrees the narrowed router is fresh.

    :class:`WorkspaceGraph` catches it, keeps the tolerant behaviour its
    rendering consumers rely on (a warning on stderr, no edges from that
    manifest, the rest of the workspace still answerable), and records it on
    :attr:`WorkspaceGraph.scan_errors`.  A consumer whose output must never be
    narrower than the truth reads that attribute and refuses.
    """

    #: What an operator does about it, rendered by the consumers that refuse.
    remedy = "Fix the manifest(s) and re-run."

    def __init__(self, path, cause, action="parse", *, deps=(), message=None):
        self.path = str(path)
        self.cause = cause
        self.action = action
        #: Edges the failed scan DID establish, when it established any. A read
        #: failure establishes none; a file that parsed but carries one
        #: unreadable declaration carries the rest of its real edges here, so
        #: the tolerant graph keeps them.
        self.deps = list(deps)
        super().__init__(message or f"failed to {action} {self.path}: {cause}")

    def acknowledged_by(self, project) -> bool:
        """Has *project* already stated by hand what this scan could not read?

        Always False here.  A manifest nobody could READ withheld its whole
        dependency section, and a ``depends_on`` declaration says nothing
        about what the unread file would have added -- the operator wrote the
        declaration for the edges they know about, not to certify a file they
        cannot see either.  Only a failure whose *class* is confined to
        declarations an explicit edge can replace overrides this; see
        :meth:`UnrecognizedGradleDependencyError.acknowledged_by`.
        """
        return False


class UnrecognizedGradleDependencyError(ManifestScanError):
    """A Gradle file that parsed, carrying a declaration nobody could read.

    The same failure as :class:`ManifestScanError` wearing different clothes:
    a dependency declared through a variable, a helper function, or a catalog
    alias with no catalog behind it exists in the build and not in the graph,
    so every filter derived from the edges is NARROWER than the workspace --
    and the freshness check, re-deriving from the same line, agrees the
    narrowed router is fresh.

    It therefore travels the road the read failures already built: the scanner
    raises, :class:`WorkspaceGraph` keeps the edges the file DID declare and
    records the failure, the rendering consumers still get their answer with a
    warning, and a consumer that must never narrow refuses.  The remedy it
    names is the one no scanner has to recognize -- declaring the edge in the
    member's ``depends_on`` -- and that remedy really clears the refusal, see
    :meth:`acknowledged_by`.
    """

    remedy = (
        "Declare the member's workspace edges in its `depends_on` in "
        "workspace.toml -- an explicit edge needs no scanner to recognize it, "
        "and the declaration itself clears this refusal: the member's edges "
        "are then stated rather than scanned, so a line no scanner can read "
        "cannot narrow them. A member with no workspace edges at all declares "
        "`depends_on = []`, which counts. The warning stays either way."
    )

    def __init__(self, path, lines, *, deps=()):
        #: ``(line number, source line)`` per unrecognized declaration.
        self.lines = [(int(number), str(text)) for number, text in lines]
        rendered = "; ".join(f"line {n}: {text}" for n, text in self.lines)
        super().__init__(
            path,
            rendered,
            action="recognize every dependency declaration in",
            deps=deps,
            message=(
                f"unrecognized Gradle dependency declaration(s) in "
                f"{str(path)}: {rendered}"
            ),
        )

    def acknowledged_by(self, project) -> bool:
        """Does *project*'s workspace entry DECLARE a ``depends_on`` key?

        The declaration is the operator's own statement of that member's
        workspace edges, and it is what the refusal asks for.  With it present
        -- a list of names, or an explicit empty list -- the member's edges no
        longer depend on this file being readable: the explicit ones are in
        the graph whatever the line says, so an unreadable declaration can no
        longer make the derived filter narrower than the workspace.

        Presence is the whole test, not content: an empty list is a member
        saying "no workspace edges", which is an answer.  Absence is not, so a
        member without the key keeps the hard refusal.
        """
        return "depends_on" in project


#: One manifest scan that failed, attributed to the member it belongs to.
#: ``remedy`` is the failing class's own, so a consumer that refuses can tell
#: the operator what to do about THIS failure rather than about failures in
#: general.
ScanError = namedtuple("ScanError", ["project", "path", "message", "remedy"])
ScanError.__new__.__defaults__ = (ManifestScanError.remedy,)


def _parse_pypi_dep_name(dep_string):
    """Extract the package name from a PEP 508 dependency string.

    Handles forms like:
      - "requests>=2.0"
      - "my-lib[extra]>=1.0"
      - "foo @ file:///path/to/foo"
      - "foo @ {root:uri}/path"
    Returns (name, is_path_dep, constraint) where constraint is the
    version specifier string or the full @ URI for path deps.
    """
    dep_string = dep_string.strip()
    if not dep_string:
        return None, False, None

    # Check for path dependency: "name @ file:..." or "name @ {root:uri}..."
    if " @ " in dep_string:
        parts = dep_string.split(" @ ", 1)
        name = parts[0].strip()
        # Strip extras from name: "foo[bar]" -> "foo"
        if "[" in name:
            name = name[:name.index("[")]
        return name, True, parts[1].strip()

    # Strip extras: "foo[bar]>=1.0" -> "foo>=1.0"
    name_end = len(dep_string)
    for i, ch in enumerate(dep_string):
        if ch in "[ >=<!~;":
            name_end = i
            break
    name = dep_string[:name_end]
    constraint = dep_string[name_end:].lstrip()
    # Remove extras from constraint if present: "[extra]>=1.0" -> ">=1.0"
    if constraint.startswith("["):
        bracket_end = constraint.find("]")
        if bracket_end != -1:
            constraint = constraint[bracket_end + 1:].lstrip()
    return name, False, constraint or ""


class PypiScanner:
    """Scan pyproject.toml for intra-workspace PyPI dependencies."""

    def scan(self, project_dir: str, workspace_names: set[str], *, pypi_name_map: dict[str, str] | None = None) -> list[Dependency]:
        # Build normalized lookup internally: normalize_pypi(name) -> original name
        pypi_normalized = {normalize_pypi(name): name for name in workspace_names}
        # Merge actual PyPI names that differ from workspace names
        if pypi_name_map:
            for norm_pypi, ws_name in pypi_name_map.items():
                if norm_pypi not in pypi_normalized:
                    pypi_normalized[norm_pypi] = ws_name

        manifest = os.path.join(project_dir, "pyproject.toml")
        if not os.path.isfile(manifest):
            return []

        try:
            with open(manifest, "r", encoding="utf-8") as f:
                data = tomlkit.parse(f.read())
        except Exception as exc:
            raise ManifestScanError(manifest, exc) from exc

        deps = []
        project_section = data.get("project", {})

        # Main dependencies: scope="runtime"
        for dep_str in project_section.get("dependencies", []):
            name, is_path, constraint = _parse_pypi_dep_name(dep_str)
            if name is None:
                continue
            normalized = normalize_pypi(name)
            if normalized in pypi_normalized:
                dep_type = "path" if is_path else "versioned"
                deps.append(Dependency(
                    name=pypi_normalized[normalized],
                    dep_type=dep_type,
                    constraint=constraint,
                    scope="runtime",
                ))

        # Optional dependencies: scope="dev"
        for group_deps in project_section.get("optional-dependencies", {}).values():
            for dep_str in group_deps:
                name, is_path, constraint = _parse_pypi_dep_name(dep_str)
                if name is None:
                    continue
                normalized = normalize_pypi(name)
                if normalized in pypi_normalized:
                    dep_type = "path" if is_path else "versioned"
                    deps.append(Dependency(
                        name=pypi_normalized[normalized],
                        dep_type=dep_type,
                        constraint=constraint,
                        scope="dev",
                    ))

        return deps


class NpmScanner:
    """Scan package.json for intra-workspace npm dependencies."""

    def scan(self, project_dir: str, workspace_names: set[str]) -> list[Dependency]:
        manifest = os.path.join(project_dir, "package.json")
        if not os.path.isfile(manifest):
            return []

        try:
            with open(manifest, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            raise ManifestScanError(manifest, exc) from exc

        deps = []
        dep_sections = [
            ("dependencies", "runtime"),
            ("devDependencies", "dev"),
            ("peerDependencies", "peer"),
        ]

        for section, scope in dep_sections:
            for name, version in data.get(section, {}).items():
                if name not in workspace_names:
                    continue
                if isinstance(version, str) and version.startswith("workspace:"):
                    dep_type = "workspace"
                    constraint = version
                elif isinstance(version, str) and version.startswith("file:"):
                    dep_type = "path"
                    constraint = version
                else:
                    dep_type = "versioned"
                    constraint = version if isinstance(version, str) else ""
                deps.append(Dependency(name=name, dep_type=dep_type, constraint=constraint, scope=scope))

        return deps


class DartScanner:
    """Scan pubspec.yaml for intra-workspace Dart/Flutter dependencies."""

    def scan(self, project_dir: str, workspace_names: set[str]) -> list[Dependency]:
        manifest = os.path.join(project_dir, "pubspec.yaml")
        if not os.path.isfile(manifest):
            return []

        try:
            from ruamel.yaml import YAML
            yaml = YAML(typ="safe")
            with open(manifest, "r", encoding="utf-8") as f:
                data = yaml.load(f)
        except Exception as exc:
            raise ManifestScanError(manifest, exc) from exc

        if not isinstance(data, dict):
            return []

        deps = []
        for section, scope in (("dependencies", "runtime"), ("dev_dependencies", "dev")):
            section_data = data.get(section)
            if not isinstance(section_data, dict):
                continue
            for name, spec in section_data.items():
                if name not in workspace_names:
                    continue
                if spec is None:
                    deps.append(Dependency(name=name, dep_type="versioned", constraint="", scope=scope))
                elif isinstance(spec, str):
                    deps.append(Dependency(name=name, dep_type="versioned", constraint=spec, scope=scope))
                elif isinstance(spec, dict):
                    if "path" in spec:
                        deps.append(Dependency(name=name, dep_type="path", constraint=spec["path"], scope=scope))
                    elif "version" in spec:
                        deps.append(Dependency(name=name, dep_type="versioned", constraint=spec["version"], scope=scope))
                    else:
                        deps.append(Dependency(name=name, dep_type="versioned", constraint="", scope=scope))

        return deps


class MavenScanner:
    """Scan Gradle (Kotlin/Groovy) and Maven (pom.xml) for intra-workspace JVM dependencies."""

    # Gradle configuration names that declare dependencies.
    # Covers standard Java/Kotlin, Android, and test configurations.
    _GRADLE_CONFIGS = (
        "implementation", "api", "compileOnly", "runtimeOnly",
        "testImplementation", "testApi", "testCompileOnly", "testRuntimeOnly",
        "kapt", "ksp", "annotationProcessor",
        # Android-specific
        "debugImplementation", "releaseImplementation",
    )

    # Configurations that are test/dev-only (scope="dev")
    _DEV_CONFIGS = frozenset({
        "testImplementation", "testApi", "testCompileOnly", "testRuntimeOnly",
        "kapt", "ksp", "annotationProcessor",
    })

    # Maven scopes that map to dev
    _MAVEN_DEV_SCOPES = frozenset({"test", "provided"})

    # Helper calls that take the place of a coordinate and declare no
    # intra-workspace edge, whatever their argument is. Each is recognized and
    # contributes nothing -- which is exactly what it contributes in Gradle,
    # so a scanner that reported them as unreadable declarations would refuse
    # on a build file that is perfectly readable:
    #   platform()/enforcedPlatform() import a BOM's constraints, not an artifact
    #   kotlin() expands to the Kotlin distribution's own artifacts
    #   fileTree()/files() name jars on disk, which no workspace member is
    _GRADLE_NO_EDGE_CALLS = (
        "platform(",
        "enforcedPlatform(",
        "kotlin(",
        "fileTree(",
        "files(",
    )

    # Version-catalog namespaces that cannot name a single library artifact:
    # a bundle is a list of them, a plugin is not a dependency, and a version
    # is a string. Only ``libs.<alias>`` reaches the [libraries] table, and an
    # alias that does not resolve there stays unrecognized -- it can name a
    # workspace member, and dropping it silently is the whole failure class.
    _GRADLE_NO_EDGE_CATALOG_PREFIXES = (
        "libs.bundles.",
        "libs.plugins.",
        "libs.versions.",
    )

    def __init__(self):
        # Cache: workspace_root -> {alias -> "group:artifact"}
        self._catalog_cache: dict[str, dict[str, str]] = {}

    def scan(self, project_dir: str, workspace_names: set[str], *, workspace_root: str | None = None) -> list[Dependency]:
        catalog = self._load_catalog(project_dir, workspace_root)
        deps = []

        # Try Gradle Kotlin DSL first
        kts_path = os.path.join(project_dir, "build.gradle.kts")
        if os.path.isfile(kts_path):
            deps.extend(self._scan_gradle_kts(kts_path, workspace_names, catalog))
            return deps

        # Try Gradle Groovy DSL
        groovy_path = os.path.join(project_dir, "build.gradle")
        if os.path.isfile(groovy_path):
            deps.extend(self._scan_gradle_groovy(groovy_path, workspace_names, catalog))
            return deps

        # Try Maven pom.xml
        pom_path = os.path.join(project_dir, "pom.xml")
        if os.path.isfile(pom_path):
            deps.extend(self._scan_pom(pom_path, workspace_names))

        return deps

    def _load_catalog(self, project_dir: str, workspace_root: str | None) -> dict[str, str]:
        """Load Gradle version catalog, returning alias -> 'group:artifact' map.

        Checks workspace root first (shared catalog), then project dir.
        Results are cached per workspace root.
        """
        candidates = []
        if workspace_root:
            candidates.append(os.path.join(workspace_root, "gradle", "libs.versions.toml"))
        candidates.append(os.path.join(project_dir, "gradle", "libs.versions.toml"))

        for catalog_path in candidates:
            if not os.path.isfile(catalog_path):
                continue
            # Use directory containing gradle/ as cache key
            cache_key = os.path.dirname(os.path.dirname(catalog_path))
            if cache_key in self._catalog_cache:
                return self._catalog_cache[cache_key]
            catalog = self._parse_version_catalog(catalog_path)
            self._catalog_cache[cache_key] = catalog
            return catalog

        return {}

    @staticmethod
    def _parse_version_catalog(catalog_path: str) -> dict[str, str]:
        """Parse libs.versions.toml and return alias -> 'group:artifact' map."""
        try:
            with open(catalog_path, "r", encoding="utf-8") as f:
                data = tomlkit.parse(f.read())
        except Exception as exc:
            raise ManifestScanError(catalog_path, exc) from exc

        libraries = data.get("libraries", {})
        result: dict[str, str] = {}
        for alias, value in libraries.items():
            if isinstance(value, str):
                # String shorthand: "group:artifact:version"
                parts = value.split(":")
                if len(parts) >= 2:
                    result[alias] = f"{parts[0]}:{parts[1]}"
            elif isinstance(value, dict):
                if "module" in value:
                    # Table form: { module = "group:artifact", ... }
                    result[alias] = value["module"]
                elif "group" in value and "name" in value:
                    # Explicit form: { group = "group", name = "artifact", ... }
                    result[alias] = f"{value['group']}:{value['name']}"
        return result

    @classmethod
    def _declares_no_workspace_edge(cls, arg: str) -> bool:
        """Is *arg* a dependency argument that cannot name a workspace member?

        The unrecognized-declaration failure is a hard error for the consumers
        that refuse on it, so it must fire only on a line that plausibly
        declares a dependency the scanner failed to parse.  These forms are
        parsed fine; they simply declare no edge.
        """
        return arg.startswith(cls._GRADLE_NO_EDGE_CALLS) or arg.startswith(
            cls._GRADLE_NO_EDGE_CATALOG_PREFIXES
        )

    @staticmethod
    def _blank_comments(content: str) -> str:
        """*content* with every comment blanked out, offsets left untouched.

        Gradle's configuration names are ordinary English words, so a comment
        mentioning one (``// implementation is intentionally omitted``, ``//
        api docs: ...``) reads exactly like a declaration to the line-oriented
        patterns below -- and reporting a comment as an unreadable dependency
        blocks a workspace whose build files are fine.

        Comments are replaced character-for-character with spaces rather than
        removed, and newlines are kept, so every offset into the returned text
        addresses the same place in the original: the scanners match against
        this and render the reported line from the original, which keeps the
        operator's own text (comment included) in the message.

        Quote-aware, because ``//`` inside a string literal is not a comment
        -- a repository URL is the common case.
        """
        out = list(content)
        length = len(content)
        i = 0
        while i < length:
            char = content[i]
            for fence in ('"""', "'''"):
                if content.startswith(fence, i):
                    end = content.find(fence, i + 3)
                    i = length if end == -1 else end + 3
                    break
            else:
                if char in "'\"":
                    i += 1
                    while i < length:
                        if content[i] == "\\":
                            i += 2
                            continue
                        if content[i] == char:
                            i += 1
                            break
                        if content[i] == "\n":
                            # An unterminated literal is a broken build file;
                            # ending it at the newline keeps the state machine
                            # from swallowing the rest of the file.
                            break
                        i += 1
                    continue
                if content.startswith("//", i):
                    while i < length and content[i] != "\n":
                        out[i] = " "
                        i += 1
                    continue
                if content.startswith("/*", i):
                    end = content.find("*/", i + 2)
                    end = length if end == -1 else end + 2
                    while i < end:
                        if content[i] != "\n":
                            out[i] = " "
                        i += 1
                    continue
                i += 1
        return "".join(out)

    @staticmethod
    def _line_at(content: str, offset: int) -> tuple[int, str]:
        """``(line number, source line)`` for the line *offset* falls on."""
        line_start = content.rfind("\n", 0, offset) + 1
        line_end = content.find("\n", offset)
        if line_end == -1:
            line_end = len(content)
        return content.count("\n", 0, offset) + 1, content[line_start:line_end].strip()

    @staticmethod
    def _resolve_catalog_alias(ref: str, catalog: dict[str, str]) -> str | None:
        """Resolve a libs.<alias> reference to 'group:artifact' using the catalog.

        Gradle normalizes dashes, underscores, and dots in alias names,
        so libs.someLib, libs.some-lib, libs.some_lib, libs.some.lib
        all refer to the same catalog entry.
        """
        # Strip "libs." prefix
        if not ref.startswith("libs."):
            return None
        alias_part = ref[5:]  # everything after "libs."

        # Gradle normalizes separators: '-', '_', '.' are all equivalent
        def normalize_alias(s: str) -> str:
            return s.replace("-", ".").replace("_", ".")

        normalized = normalize_alias(alias_part)
        for key, coords in catalog.items():
            if normalize_alias(key) == normalized:
                return coords
        return None

    def _scan_gradle_kts(self, filepath: str, workspace_names: set[str], catalog: dict[str, str] | None = None) -> list[Dependency]:
        """Parse build.gradle.kts for dependency declarations."""
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                source = f.read()
        except Exception as exc:
            raise ManifestScanError(filepath, exc, action="read") from exc

        # Every pattern below matches the comment-blanked text, so a
        # declaration someone commented out is never read as a live one.
        # Offsets are preserved, so a report renders its line from ``source``
        # and keeps the operator's own text. See :meth:`_blank_comments`.
        content = self._blank_comments(source)

        deps = []
        unrecognized = []

        # Build regex for config names
        config_pattern = "|".join(re.escape(c) for c in self._GRADLE_CONFIGS)

        # Pattern 1: project dependencies -- config(project(":module"))
        # Handles: implementation(project(":module")), api(project(":sub:module"))
        project_re = re.compile(
            rf'({config_pattern})\s*\(\s*project\s*\(\s*"([^"]+)"\s*\)\s*\)',
        )
        for match in project_re.finditer(content):
            config = match.group(1)
            module_path = match.group(2)
            # Convert ":module" or ":sub:module" to the last segment as the name
            module_name = module_path.split(":")[-1]
            scope = "dev" if config in self._DEV_CONFIGS else "runtime"
            if module_name in workspace_names:
                deps.append(Dependency(
                    name=module_name,
                    dep_type="project",
                    constraint=module_path,
                    scope=scope,
                ))

        # Pattern 2: external dependencies -- config("group:artifact:version")
        # Handles: implementation("com.google.guava:guava:31.1-jre")
        external_re = re.compile(
            rf'({config_pattern})\s*\(\s*"([^"]+:[^"]+:[^"]+)"\s*\)',
        )
        for match in external_re.finditer(content):
            config = match.group(1)
            coords = match.group(2)
            parts = coords.split(":")
            if len(parts) >= 2:
                artifact = parts[1]
                version = parts[2] if len(parts) >= 3 else ""
                scope = "dev" if config in self._DEV_CONFIGS else "runtime"
                if artifact in workspace_names:
                    deps.append(Dependency(
                        name=artifact,
                        dep_type="versioned",
                        constraint=version,
                        scope=scope,
                    ))

        # Detect unrecognized dependency patterns: lines that look like
        # dependency declarations but don't match our patterns.
        # Look for config(...) calls that we didn't already match.
        all_config_call_re = re.compile(
            rf'({config_pattern})\s*\((.+?)\)',
        )
        matched_spans = set()
        for match in project_re.finditer(content):
            matched_spans.add((match.start(), match.end()))
        for match in external_re.finditer(content):
            matched_spans.add((match.start(), match.end()))

        for match in all_config_call_re.finditer(content):
            # Skip if this span overlaps with an already-matched pattern
            span = (match.start(), match.end())
            if any(
                s[0] <= span[0] < s[1] or s[0] < span[1] <= s[1]
                for s in matched_spans
            ):
                continue
            arg = match.group(2).strip()
            # Skip simple string literals (already handled by external_re)
            if re.match(r'^"[^"]*"$', arg):
                continue
            # Skip project(...) calls (already handled by project_re)
            if arg.startswith("project("):
                continue
            # Recognized, and declares no intra-workspace edge
            if self._declares_no_workspace_edge(arg):
                continue
            # Try to resolve version catalog references (libs.<alias>)
            if catalog and arg.startswith("libs."):
                coords = self._resolve_catalog_alias(arg, catalog)
                if coords:
                    parts = coords.split(":")
                    if len(parts) >= 2:
                        artifact = parts[1]
                        if artifact in workspace_names:
                            config = match.group(1)
                            scope = "dev" if config in self._DEV_CONFIGS else "runtime"
                            deps.append(Dependency(
                                name=artifact,
                                dep_type="catalog",
                                constraint=coords,
                                scope=scope,
                            ))
                    # Resolved (even if not a workspace dep) -- don't report it
                    continue
            # This is an unrecognized pattern
            number, line = self._line_at(source, match.start())
            if line:
                unrecognized.append((number, line))

        if unrecognized:
            raise UnrecognizedGradleDependencyError(filepath, unrecognized, deps=deps)

        return deps

    def _scan_gradle_groovy(self, filepath: str, workspace_names: set[str], catalog: dict[str, str] | None = None) -> list[Dependency]:
        """Parse build.gradle (Groovy DSL) for dependency declarations."""
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                source = f.read()
        except Exception as exc:
            raise ManifestScanError(filepath, exc, action="read") from exc

        # Comment-blanked for matching, original for reporting. The general
        # pattern further down scans whole lines, and every Gradle
        # configuration name is an ordinary English word, so without this a
        # comment like ``// implementation is intentionally omitted`` reads as
        # a declaration nobody could parse. See :meth:`_blank_comments`.
        content = self._blank_comments(source)

        deps = []
        unrecognized = []

        config_pattern = "|".join(re.escape(c) for c in self._GRADLE_CONFIGS)

        # Pattern 1: project dependencies -- config project(':module')
        # Handles: implementation project(':module'), api project(':sub:module')
        project_re = re.compile(
            rf"({config_pattern})\s+project\s*\(\s*['\"]([^'\"]+)['\"]\s*\)",
        )
        for match in project_re.finditer(content):
            config = match.group(1)
            module_path = match.group(2)
            module_name = module_path.split(":")[-1]
            scope = "dev" if config in self._DEV_CONFIGS else "runtime"
            if module_name in workspace_names:
                deps.append(Dependency(
                    name=module_name,
                    dep_type="project",
                    constraint=module_path,
                    scope=scope,
                ))

        # Pattern 2: external dependencies -- config 'group:artifact:version'
        # Handles: implementation 'com.google.guava:guava:31.1-jre'
        # Also handles: implementation "group:artifact:version"
        external_re = re.compile(
            rf"({config_pattern})\s+['\"]([^'\"]+:[^'\"]+:[^'\"]+)['\"]",
        )
        for match in external_re.finditer(content):
            config = match.group(1)
            coords = match.group(2)
            parts = coords.split(":")
            if len(parts) >= 2:
                artifact = parts[1]
                version = parts[2] if len(parts) >= 3 else ""
                scope = "dev" if config in self._DEV_CONFIGS else "runtime"
                if artifact in workspace_names:
                    deps.append(Dependency(
                        name=artifact,
                        dep_type="versioned",
                        constraint=version,
                        scope=scope,
                    ))

        # Pattern 3: external dependencies in parenthesized form
        # Handles: implementation('group:artifact:version'), implementation("group:artifact:version")
        external_paren_re = re.compile(
            rf"({config_pattern})\s*\(\s*['\"]([^'\"]+:[^'\"]+:[^'\"]+)['\"]\s*\)",
        )
        for match in external_paren_re.finditer(content):
            config = match.group(1)
            coords = match.group(2)
            parts = coords.split(":")
            if len(parts) >= 2:
                artifact = parts[1]
                version = parts[2] if len(parts) >= 3 else ""
                scope = "dev" if config in self._DEV_CONFIGS else "runtime"
                if artifact in workspace_names:
                    deps.append(Dependency(
                        name=artifact,
                        dep_type="versioned",
                        constraint=version,
                        scope=scope,
                    ))

        # Detect unrecognized patterns
        # Look for lines that start with a config name followed by something
        # that isn't a recognized pattern
        matched_spans = set()
        for regex in (project_re, external_re, external_paren_re):
            for match in regex.finditer(content):
                matched_spans.add((match.start(), match.end()))

        # Parenthesized catch-all: config(...) with non-string arguments
        # (e.g., implementation(libs.someLib))
        paren_catch_re = re.compile(
            rf'({config_pattern})\s*\((.+?)\)',
        )
        for match in paren_catch_re.finditer(content):
            span = (match.start(), match.end())
            if any(
                s[0] <= span[0] < s[1] or s[0] < span[1] <= s[1]
                for s in matched_spans
            ):
                continue
            arg = match.group(2).strip()
            if re.match(r"""^['"][^'"]+['"]$""", arg):
                continue
            if arg.startswith("project(") or arg.startswith("project '") or arg.startswith('project "'):
                continue
            # Recognized, and declares no intra-workspace edge
            if self._declares_no_workspace_edge(arg):
                continue
            if catalog and arg.startswith("libs."):
                coords = self._resolve_catalog_alias(arg, catalog)
                if coords:
                    parts = coords.split(":")
                    if len(parts) >= 2:
                        artifact = parts[1]
                        if artifact in workspace_names:
                            config = match.group(1)
                            scope = "dev" if config in self._DEV_CONFIGS else "runtime"
                            deps.append(Dependency(
                                name=artifact,
                                dep_type="catalog",
                                constraint=coords,
                                scope=scope,
                            ))
                    continue
            number, line = self._line_at(source, match.start())
            if line:
                unrecognized.append((number, line))
            matched_spans.add(span)

        # General pattern: config followed by something (space-separated)
        general_re = re.compile(
            rf"({config_pattern})\s+(\S.+?)$",
            re.MULTILINE,
        )
        for match in general_re.finditer(content):
            span = (match.start(), match.end())
            if any(
                s[0] <= span[0] < s[1] or s[0] < span[1] <= s[1]
                for s in matched_spans
            ):
                continue
            arg = match.group(2).strip()
            # Skip already-handled forms
            if re.match(r"""^['"][^'"]+['"]$""", arg):
                continue
            if arg.startswith("project(") or arg.startswith("project '") or arg.startswith('project "'):
                continue
            # Recognized, and declares no intra-workspace edge
            if self._declares_no_workspace_edge(arg):
                continue
            # Try to resolve version catalog references (libs.<alias>)
            if catalog and arg.startswith("libs."):
                coords = self._resolve_catalog_alias(arg, catalog)
                if coords:
                    parts = coords.split(":")
                    if len(parts) >= 2:
                        artifact = parts[1]
                        if artifact in workspace_names:
                            config = match.group(1)
                            scope = "dev" if config in self._DEV_CONFIGS else "runtime"
                            deps.append(Dependency(
                                name=artifact,
                                dep_type="catalog",
                                constraint=coords,
                                scope=scope,
                            ))
                    # Resolved (even if not a workspace dep) -- don't report it
                    continue
            number, line = self._line_at(source, match.start())
            if line:
                unrecognized.append((number, line))

        if unrecognized:
            raise UnrecognizedGradleDependencyError(filepath, unrecognized, deps=deps)

        return deps

    def _scan_pom(self, filepath: str, workspace_names: set[str]) -> list[Dependency]:
        """Parse pom.xml for <dependency> elements."""
        try:
            tree = ET.parse(filepath)
        except Exception as exc:
            raise ManifestScanError(filepath, exc) from exc

        root = tree.getroot()

        # Detect namespace
        ns_match = re.match(r"\{(.+)\}", root.tag)
        ns = ns_match.group(1) if ns_match else ""
        prefix = f"{{{ns}}}" if ns else ""

        deps = []

        # Find all <dependency> elements (both in <dependencies> and
        # <dependencyManagement><dependencies>)
        for dep_elem in root.iter(f"{prefix}dependency"):
            artifact_elem = dep_elem.find(f"{prefix}artifactId")
            version_elem = dep_elem.find(f"{prefix}version")
            scope_elem = dep_elem.find(f"{prefix}scope")

            artifact_id = artifact_elem.text.strip() if artifact_elem is not None and artifact_elem.text else ""
            version = version_elem.text.strip() if version_elem is not None and version_elem.text else ""
            maven_scope = scope_elem.text.strip() if scope_elem is not None and scope_elem.text else "compile"

            if not artifact_id:
                continue

            scope = "dev" if maven_scope in self._MAVEN_DEV_SCOPES else "runtime"

            if artifact_id in workspace_names:
                deps.append(Dependency(
                    name=artifact_id,
                    dep_type="versioned",
                    constraint=version,
                    scope=scope,
                ))

        return deps


SCANNERS: list[WorkspaceScanner] = [PypiScanner(), NpmScanner(), DartScanner(), MavenScanner()]


class WorkspaceGraph:
    """Directed dependency graph of intra-workspace project dependencies.

    Construction is tolerant of a manifest it cannot read and of a Gradle file
    that parsed but declares a dependency in a form no scanner recognizes: the
    failure warns on stderr, contributes no edge for what could not be read,
    and the rest of the workspace is still answerable -- which is what
    ``monorepo impact``, ``monorepo graph`` and ``monorepo status`` need on a
    half-broken tree.

    Tolerance is only safe for a consumer that RENDERS the graph. A consumer
    that derives something narrowing from it cannot tell "this member has no
    dependencies" apart from "nobody could read this member's dependencies",
    and picking the first silently drops a real edge.  Every failed scan is
    therefore recorded on :attr:`scan_errors` (a list of :class:`ScanError`),
    and such a consumer refuses when the list is non-empty -- see
    :class:`rlsbl.router_filters.RouterFilters`.

    One failure class is exempt from the recording, never from the warning: a
    Gradle declaration no scanner recognizes, in a member whose workspace
    entry declares its own ``depends_on``
    (:meth:`ManifestScanError.acknowledged_by`).  That declaration is what the
    failure's remedy asks for, so honouring it here is what makes the remedy
    actually clear the refusal.
    """

    def __init__(self, root, projects):
        # Map: project_name -> list of Dependency
        self._deps = {}
        # Map: project_name -> list of dependent project names
        self._rdeps = {}
        self._project_names = []
        #: Every manifest scan that failed, in construction order. Empty means
        #: every manifest in the workspace was read, so the edge set is
        #: complete and anything derived from it is as wide as the truth.
        self.scan_errors: list[ScanError] = []

        workspace_names = set()
        for proj in projects:
            name = proj["name"]
            workspace_names.add(name)
            self._project_names.append(name)
            self._deps[name] = []
            self._rdeps[name] = []

        # Build PyPI name map: normalize_pypi(actual_pypi_name) -> workspace_name
        # for projects where the PyPI name differs from the workspace name.
        pypi_name_map: dict[str, str] = {}
        for proj in projects:
            pyproject_path = os.path.join(root, proj["path"], "pyproject.toml")
            if not os.path.isfile(pyproject_path):
                continue
            try:
                with open(pyproject_path, "r", encoding="utf-8") as f:
                    pyproject_data = tomlkit.parse(f.read())
                pypi_name = pyproject_data.get("project", {}).get("name")
                if pypi_name and normalize_pypi(pypi_name) != normalize_pypi(proj["name"]):
                    pypi_name_map[normalize_pypi(pypi_name)] = proj["name"]
            except Exception:
                pass  # Skip unreadable files; PypiScanner will warn later

        for proj in projects:
            name = proj["name"]
            project_dir = os.path.join(root, proj["path"])

            found_deps = []
            for scanner in SCANNERS:
                # One scanner's failed scan costs the edges that scan could not
                # establish and nothing else: the remaining scanners still run,
                # the warning still goes to stderr, and the failure is recorded
                # so a narrowing consumer can refuse rather than derive from a
                # graph that is missing edges nobody could see.
                try:
                    if isinstance(scanner, PypiScanner):
                        found_deps.extend(scanner.scan(project_dir, workspace_names, pypi_name_map=pypi_name_map))
                    elif isinstance(scanner, MavenScanner):
                        found_deps.extend(scanner.scan(project_dir, workspace_names, workspace_root=root))
                    else:
                        found_deps.extend(scanner.scan(project_dir, workspace_names))
                except ManifestScanError as exc:
                    # A file that parsed but carries one declaration nobody
                    # could read still declared the rest of its edges, and
                    # those are real: keep them, so tolerance costs only the
                    # edges that were actually lost.
                    found_deps.extend(exc.deps)
                    print(f"Warning: {exc}", file=sys.stderr)
                    # The warning is unconditional -- an operator reading a
                    # build file the scanner cannot fully read wants to know
                    # either way. What the acknowledgment changes is only
                    # whether a NARROWING consumer refuses: a member that
                    # declares its own `depends_on` has stated its edges by
                    # hand, so this failure cannot make the derivation
                    # narrower than the workspace, and recording it would
                    # block the very remedy the failure names.
                    if exc.acknowledged_by(proj):
                        continue
                    self.scan_errors.append(
                        ScanError(
                            project=name,
                            path=exc.path,
                            message=str(exc),
                            remedy=exc.remedy,
                        )
                    )

            # Explicit depends_on from workspace config
            for dep_name in proj.get("depends_on", []):
                if dep_name == name:
                    continue  # silently skip self-references
                if dep_name not in workspace_names:
                    raise WorkspaceError(
                        f"Project '{name}' declares depends_on "
                        f"'{dep_name}' but no workspace project "
                        f"with that name exists"
                    )
                found_deps.append(Dependency(
                    name=dep_name,
                    dep_type="explicit",
                    constraint="",
                    scope="explicit",
                ))

            # Deduplicate: same target name only once (first wins)
            seen = set()
            for dep in found_deps:
                if dep.name != name and dep.name not in seen:
                    seen.add(dep.name)
                    self._deps[name].append(dep)

        # Build reverse deps: each entry is (dependent_name, scope)
        for name, dep_list in self._deps.items():
            for dep in dep_list:
                if dep.name in self._rdeps:
                    self._rdeps[dep.name].append((name, dep.scope))

    def dependencies(self, project_name):
        """Return list of Dependency namedtuples for intra-workspace deps."""
        return list(self._deps.get(project_name, []))

    def dependents(self, project_name):
        """Return list of project names that depend on this project."""
        return [name for name, _scope in self._rdeps.get(project_name, [])]

    def topological_order(self):
        """Return project names in topological order (leaves first).

        Raises CycleError if the graph contains cycles.
        """
        # Kahn's algorithm
        in_degree = {name: 0 for name in self._project_names}
        for name, dep_list in self._deps.items():
            for dep in dep_list:
                if dep.name in in_degree:
                    in_degree[dep.name] += 1

        # Note: in_degree counts how many projects depend on a node,
        # but for topological sort we need in-degree in the dependency
        # direction. A depends on B means edge A->B, so B should come
        # first. We want "leaves first" = projects with no deps first.
        # Recompute: in_degree[X] = number of deps X has (not rdeps).
        in_degree = {name: len(deps) for name, deps in self._deps.items()}

        heap = sorted(name for name in self._project_names if in_degree[name] == 0)
        heapq.heapify(heap)
        result = []

        while heap:
            node = heapq.heappop(heap)
            result.append(node)
            # For each project that depends on this node, decrement its in-degree
            for dependent, _scope in self._rdeps.get(node, []):
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    heapq.heappush(heap, dependent)

        if len(result) != len(self._project_names):
            raise CycleError(
                "Workspace dependency graph contains a cycle"
            )

        return result

    def has_cycles(self):
        """Return True if the dependency graph contains cycles."""
        try:
            self.topological_order()
            return False
        except CycleError:
            return True

    def transitive_deps(self, name, depth=None):
        """Return transitive dependency names in BFS discovery order.

        Excludes the starting node. Optional *depth* limits traversal
        (None = unlimited, 0 = empty list).  Raises KeyError if *name*
        is not in the graph.
        """
        if name not in self._deps:
            raise KeyError(name)
        if depth is not None and depth <= 0:
            return []
        result = []
        visited = {name}
        # queue entries: (node_name, current_depth)
        queue = deque()
        for dep in self._deps[name]:
            if dep.name not in visited:
                visited.add(dep.name)
                queue.append((dep.name, 1))
                result.append(dep.name)
        while queue:
            current, d = queue.popleft()
            if depth is not None and d >= depth:
                continue
            for dep in self._deps.get(current, []):
                if dep.name not in visited:
                    visited.add(dep.name)
                    queue.append((dep.name, d + 1))
                    result.append(dep.name)
        return result

    def transitive_rdeps(self, name, depth=None, scope_filter=None):
        """Return transitive reverse-dependency names in BFS discovery order.

        Excludes the starting node. Optional *depth* limits traversal
        (None = unlimited, 0 = empty list).  Optional *scope_filter*
        restricts traversal to edges whose scope matches the given string.
        Raises KeyError if *name* is not in the graph.
        """
        if name not in self._rdeps:
            raise KeyError(name)
        if depth is not None and depth <= 0:
            return []
        result = []
        visited = {name}
        queue = deque()
        for rdep_name, scope in self._rdeps[name]:
            if scope_filter is not None and scope != scope_filter:
                continue
            if rdep_name not in visited:
                visited.add(rdep_name)
                queue.append((rdep_name, 1))
                result.append(rdep_name)
        while queue:
            current, d = queue.popleft()
            if depth is not None and d >= depth:
                continue
            for rdep_name, scope in self._rdeps.get(current, []):
                if scope_filter is not None and scope != scope_filter:
                    continue
                if rdep_name not in visited:
                    visited.add(rdep_name)
                    queue.append((rdep_name, d + 1))
                    result.append(rdep_name)
        return result

    def dep_count(self, project_name):
        """Return number of intra-workspace dependencies for a project."""
        return len(self._deps.get(project_name, []))

    def rdep_count(self, project_name):
        """Return number of projects that depend on this project."""
        return len(self._rdeps.get(project_name, []))
