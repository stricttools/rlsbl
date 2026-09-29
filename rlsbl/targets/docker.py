"""Docker release target using a VERSION file as source of truth, with opt-in activation via config and image publishing to a registry."""

import os

import re

from .base import PACKAGE_RENAME_UNSUPPORTED
from .base import BaseTarget, TemplateVars, UploadListing
from .. import effects
from .utils import missing_version_file

VERSION_FILE = "VERSION"
DOCKERIGNORE = ".dockerignore"


def _dockerignore_regex(pattern):
    """Docker's pattern syntax (moby's patternmatcher) as a regular expression.

    ``*`` and ``?`` stay within one path component, ``**`` spans any number of
    them, and the pattern matches the whole context-relative path.
    """
    out = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "*":
            if pattern[i + 1:i + 2] == "*":
                i += 2
                if pattern[i:i + 1] == "/":
                    i += 1
                out.append(".*" if i >= len(pattern) else "(.*/)?")
                continue
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        elif ch == "\\" and i + 1 < len(pattern):
            i += 1
            out.append(re.escape(pattern[i]))
        elif ch == "[":
            end = pattern.find("]", i + 1)
            if end == -1:
                out.append(re.escape(ch))
            else:
                out.append(pattern[i:end + 1])
                i = end
        else:
            out.append(re.escape(ch))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def read_dockerignore(path):
    """The patterns of the ``.dockerignore`` at *path*, in order ([] when absent)."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return []
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


def dockerignore_excludes(patterns, rel):
    """Does a ``.dockerignore`` holding *patterns* leave *rel* out of the context?

    Docker's rule: the last pattern that matches the path or one of its parent
    directories decides, and a ``!`` pattern puts the path back.
    """
    parents = rel.split("/")
    candidates = ["/".join(parents[: i + 1]) for i in range(len(parents))]
    excluded = False
    for raw in patterns:
        negated = raw.startswith("!")
        pattern = os.path.normpath(raw[1:] if negated else raw).replace(os.sep, "/")
        pattern = pattern.lstrip("/")
        regex = _dockerignore_regex(pattern)
        if any(regex.match(c) for c in candidates):
            excluded = not negated
    return excluded


def _docker_private_path_fix(rel, rule, directory):
    """Keep a private path out of the Docker build context."""
    from ..upload_exclusions import dockerignore_entry

    return f'add "{dockerignore_entry(rule)}" to {DOCKERIGNORE}'


class DockerTarget(BaseTarget):
    """Release target for Docker projects (Dockerfile + VERSION file)."""

    detection_files = ("Dockerfile",)
    ecosystem = "Docker"

    # rlsbl does not rename this target's package name; the operator edits it by hand.
    package_rename = PACKAGE_RENAME_UNSUPPORTED
    package_name_field = '.rlsbl/config.json "docker.image" (the project directory name when it is unset)'

    @property
    def name(self):
        return "docker"

    def read_name(self, dir_path, ctx):
        """Return image name from config or directory name."""
        config = ctx.config
        docker_config = config.get("docker", {})
        image = docker_config.get("image")
        if image:
            return image
        return os.path.basename(os.path.abspath(dir_path))


    def read_version(self, dir_path):
        """Read version from the VERSION file."""
        version_path = os.path.join(dir_path, VERSION_FILE)
        if not os.path.exists(version_path):
            raise missing_version_file(
                dir_path, f"{VERSION_FILE} file", create=VERSION_FILE,
            )
        with open(version_path, "r", encoding="utf-8") as f:
            return f.read().strip()

    def write_version(self, dir_path, version, ctx):
        """Write the new version to the VERSION file atomically.

        Returns a list of relative file paths that were modified.
        """
        version_path = os.path.join(dir_path, VERSION_FILE)
        effects.atomic_write_text(version_path, version + "\n")
        return [self.version_file()]

    def version_file(self, dir_path=None):
        return VERSION_FILE

    def tag_format(self, version):
        return f"v{version}"

    def template_dir(self):
        return os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "templates", "docker"
        )

    def template_vars(self, dir_path, ctx):
        """Extract template variables from config and git."""
        config = ctx.config if ctx else {}
        docker_config = config.get("docker", {})
        image = docker_config.get("image", "")
        registry = docker_config.get("registry", "")

        name = os.path.basename(os.path.abspath(dir_path))

        from .utils import _get_git_author
        author = _get_git_author()

        return TemplateVars(self.name, {
            "image": image,
            "registry": registry,
            "name": name,
            "author": author,
        })

    def template_mappings(self, ctx):
        return [
            {"template": "ci.yml.tpl", "target": ".github/workflows/ci.yml"},
        ]

    def offline_upload_listing(self, dir_path):
        """The build context the image is built from, less ``.dockerignore``.

        The publish workflow builds from a checkout of the tag, so only
        tracked files are in the context. Which of them a Dockerfile copies is
        its own business: a file in the context is one ``COPY .`` away from the
        image, so the context is what is listed.
        """
        from ..ldflags_symbols import git_tracked_files

        patterns = read_dockerignore(os.path.join(dir_path, DOCKERIGNORE))
        return UploadListing(
            label="the Docker build context",
            files=tuple(
                rel for rel in git_tracked_files(dir_path)
                if not dockerignore_excludes(patterns, rel)
            ),
            fix=f"Exclude the nested member in {DOCKERIGNORE}.",
            private_path_fix=_docker_private_path_fix,
        )

    def check_project_exists(self, dir_path):
        return os.path.exists(os.path.join(dir_path, "Dockerfile"))

    def get_project_init_hint(self):
        return 'Create a Dockerfile first'
