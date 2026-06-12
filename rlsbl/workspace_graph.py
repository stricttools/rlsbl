"""Dependency graph builder for monorepo workspaces that parses project manifests and provides topological sorting for ordered operations."""

import heapq
import json
import os
import sys
from collections import deque, namedtuple
from typing import Protocol, runtime_checkable

import tomlkit

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

    def scan(self, project_dir: str, workspace_names: set[str]) -> list[Dependency]:
        # Build normalized lookup internally: normalize_pypi(name) -> original name
        pypi_normalized = {normalize_pypi(name): name for name in workspace_names}

        manifest = os.path.join(project_dir, "pyproject.toml")
        if not os.path.isfile(manifest):
            return []

        try:
            with open(manifest, "r", encoding="utf-8") as f:
                data = tomlkit.parse(f.read())
        except Exception as exc:
            print(f"Warning: failed to parse {manifest}: {exc}", file=sys.stderr)
            return []

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
            print(f"Warning: failed to parse {manifest}: {exc}", file=sys.stderr)
            return []

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
            print(f"Warning: failed to parse {manifest}: {exc}", file=sys.stderr)
            return []

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


SCANNERS: list[WorkspaceScanner] = [PypiScanner(), NpmScanner(), DartScanner()]


class WorkspaceGraph:
    """Directed dependency graph of intra-workspace project dependencies."""

    def __init__(self, root, projects):
        # Map: project_name -> list of Dependency
        self._deps = {}
        # Map: project_name -> list of dependent project names
        self._rdeps = {}
        self._project_names = []

        workspace_names = set()
        for proj in projects:
            name = proj["name"]
            workspace_names.add(name)
            self._project_names.append(name)
            self._deps[name] = []
            self._rdeps[name] = []

        for proj in projects:
            name = proj["name"]
            project_dir = os.path.join(root, proj["path"])

            found_deps = []
            for scanner in SCANNERS:
                found_deps.extend(scanner.scan(project_dir, workspace_names))

            # Explicit depends_on from workspace config
            for dep_name in proj.get("depends_on", []):
                if dep_name == name:
                    continue  # silently skip self-references
                if dep_name not in workspace_names:
                    raise ValueError(
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
