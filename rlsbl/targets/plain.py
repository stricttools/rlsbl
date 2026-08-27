"""Plain release target for projects with no build system, using a VERSION file for version tracking with tagging and GitHub Releases only."""

import functools
import os

import tomlkit

from .base import BaseTarget, TemplateVars
from .. import effects

VERSION_FILE = "VERSION"

# Manifest files that belong to no CURRENTLY REGISTERED target but still mean
# "some other build system owns this directory". Retired targets leave their
# manifests behind in real projects, so plain must keep standing off them.
#
# Everything else in the stand-off set is derived from the registry -- see
# ``_foreign_manifests`` -- so adding a target automatically teaches plain to
# stay out of its way. Only genuinely target-less manifests are listed here.
_EXTRA_FOREIGN_MANIFESTS = frozenset({
    "Cargo.toml",     # the retired cargo target
    "selfdoc.json",   # the retired docs target
})


@functools.lru_cache(maxsize=1)
def _foreign_manifests():
    """Every manifest filename that means "a target other than plain owns this".

    Derived by unioning every registered target's ``detection_files`` with the
    declared extras, minus plain's own (plain declares none: ``VERSION`` is far
    too generic to auto-detect on).

    Imported lazily because the registry instantiates ``PlainTarget``.
    """
    from . import TARGETS

    names = set(_EXTRA_FOREIGN_MANIFESTS)
    for name, target in TARGETS.items():
        if name == "plain":
            continue
        names.update(target.detection_files)
    return frozenset(names)


class PlainTarget(BaseTarget):
    """Release target for projects that have no build system or package registry."""

    capabilities = frozenset()
    ecosystem = "Plain"
    auto_detectable = "conditional"

    # Plain is opt-in only; no detection files to avoid false positives
    # (VERSION is too generic -- every Go/Swift/Docker project has one).

    @property
    def name(self):
        return "plain"

    def detect(self, dir_path):
        # Auto-detect when a VERSION file exists and no other target's
        # primary manifest is present.
        if not os.path.exists(os.path.join(dir_path, VERSION_FILE)):
            return False
        for manifest in _foreign_manifests():
            if os.path.exists(os.path.join(dir_path, manifest)):
                return False
        return True

    def read_version(self, dir_path):
        """Read version from the VERSION file."""
        version_path = os.path.join(dir_path, VERSION_FILE)
        if not os.path.exists(version_path):
            raise FileNotFoundError(
                f"No {VERSION_FILE} file found. Run 'rlsbl scaffold' first."
            )
        with open(version_path, "r", encoding="utf-8") as f:
            return f.read().strip()

    def write_version(self, dir_path, version, ctx):
        """Write the new version to the VERSION file and pyproject.toml atomically.

        Returns a list of relative file paths that were modified.
        """
        version_path = os.path.join(dir_path, VERSION_FILE)
        effects.atomic_write_text(version_path, version + "\n")

        modified = [self.version_file()]

        # Also bump pyproject.toml if it exists and has [project].version
        pyproject_path = os.path.join(dir_path, "pyproject.toml")
        if os.path.exists(pyproject_path):
            with open(pyproject_path, "r", encoding="utf-8") as f:
                doc = tomlkit.parse(f.read())
            project = doc.get("project")
            if project is not None and "version" in project:
                doc["project"]["version"] = version
                effects.atomic_write_text(pyproject_path, tomlkit.dumps(doc))
                modified.append("pyproject.toml")

        return modified

    def version_file(self, dir_path=None):
        return VERSION_FILE

    def template_dir(self):
        return os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "templates", "plain"
        )

    def template_mappings(self, ctx):
        return [{"template": "VERSION.tpl", "target": "VERSION"}]

    def check_project_exists(self, dir_path):
        # Plain targets are always valid -- scaffold creates the VERSION file.
        return True

    def template_vars(self, dir_path, ctx):
        try:
            version = self.read_version(dir_path)
        except FileNotFoundError:
            version = "0.0.0"
        return TemplateVars(self.name, {
            "name": os.path.basename(os.path.abspath(dir_path)),
            "version": version,
        })
