"""Cargo (Rust) release target that uses tomlkit for round-trip Cargo.toml editing with hybrid publish support via CARGO_REGISTRY_TOKEN."""

import os
import re

import tomlkit

from .base import BaseTarget
from ..errors import VersionError
from ..utils import run


class CargoTarget(BaseTarget):
    """Release target for Rust/Cargo projects (Cargo.toml)."""

    detection_files = ("Cargo.toml",)
    capabilities = frozenset({"read_name", "read_metadata", "ci_templates", "dev_install"})
    ecosystem = "Rust / crates.io"

    @property
    def name(self):
        return "cargo"

    def read_name(self, dir_path, ctx):
        """Read the package name from Cargo.toml."""
        cargo_path = os.path.join(dir_path, "Cargo.toml")
        if not os.path.exists(cargo_path):
            return None
        with open(cargo_path, "r", encoding="utf-8") as f:
            doc = tomlkit.parse(f.read())
        pkg = doc.get("package", {})
        name = pkg.get("name")
        return str(name) if name is not None else None

    def read_metadata(self, dir_path):
        """Read license and description from Cargo.toml."""
        cargo_path = os.path.join(dir_path, "Cargo.toml")
        if not os.path.exists(cargo_path):
            return {}
        with open(cargo_path, "r", encoding="utf-8") as f:
            doc = tomlkit.parse(f.read())
        pkg = doc.get("package", {})
        result = {}
        license_val = pkg.get("license")
        if license_val:
            result["license"] = str(license_val)
        description = pkg.get("description")
        if description:
            result["description"] = str(description)
        return result

    def detect(self, dir_path):
        """Detect if dir has a Cargo.toml with a [package] section (not workspace-only)."""
        cargo_path = os.path.join(dir_path, "Cargo.toml")
        if not os.path.exists(cargo_path):
            return False
        with open(cargo_path, "r", encoding="utf-8") as f:
            doc = tomlkit.parse(f.read())
        return "package" in doc

    def read_version(self, dir_path):
        """Read version from Cargo.toml [package].version."""
        cargo_path = os.path.join(dir_path, "Cargo.toml")
        with open(cargo_path, "r", encoding="utf-8") as f:
            doc = tomlkit.parse(f.read())
        if "package" not in doc or "version" not in doc["package"]:
            raise VersionError(f"No [package].version in {cargo_path}")
        return str(doc["package"]["version"])

    def write_version(self, dir_path, version, ctx):
        """Write version to Cargo.toml using tomlkit round-trip (preserves comments).

        Returns a list of relative file paths that were modified.
        """
        cargo_path = os.path.join(dir_path, "Cargo.toml")
        with open(cargo_path, "r", encoding="utf-8") as f:
            doc = tomlkit.parse(f.read())
        doc["package"]["version"] = version
        tmp_path = cargo_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(tomlkit.dumps(doc))
        os.replace(tmp_path, cargo_path)
        return [self.version_file()]

    def version_file(self, dir_path=None):
        return "Cargo.toml"

    def tag_format(self, version):
        return f"v{version}"

    def _is_library(self, dir_path):
        """Return True if the crate is a library (no binary target)."""
        cargo_path = os.path.join(dir_path, "Cargo.toml")
        with open(cargo_path, "r", encoding="utf-8") as f:
            doc = tomlkit.parse(f.read())
        # Explicit [lib] section means it's a library
        if "lib" in doc:
            return True
        # Explicit [[bin]] section means it's a binary
        if "bin" in doc:
            return False
        # No explicit sections: check for src/main.rs
        main_rs = os.path.join(dir_path, "src", "main.rs")
        return not os.path.exists(main_rs)

    def template_dir(self):
        return os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "templates", "cargo"
        )

    def template_vars(self, dir_path, ctx):
        """Extract template variables from Cargo.toml."""
        cargo_path = os.path.join(dir_path, "Cargo.toml")
        with open(cargo_path, "r", encoding="utf-8") as f:
            doc = tomlkit.parse(f.read())

        pkg = doc.get("package", {})
        name = str(pkg.get("name", ""))

        # Author from Cargo.toml authors[0] or git config
        author = ""
        authors = pkg.get("authors")
        if authors and len(authors) > 0:
            author = str(authors[0])
        if not author:
            from .utils import _get_git_author
            author = _get_git_author()

        version = str(pkg.get("version", "0.0.0"))

        # Derive repoName from package.repository
        repo_name = ""
        repository = pkg.get("repository")
        if repository:
            repo_match = re.search(r"github\.com/([^/\s]+/[^/\s]+)", str(repository))
            if repo_match:
                repo_name = repo_match.group(1).removesuffix(".git")

        # Derive binCommand from [[bin]] name or package name if not a library
        bin_command = ""
        bins = doc.get("bin")
        if bins and isinstance(bins, list) and len(bins) > 0:
            first_bin = bins[0]
            bin_name = first_bin.get("name")
            if bin_name:
                bin_command = str(bin_name)
        if not bin_command and not self._is_library(dir_path):
            bin_command = name

        result = {
            "name": name,
            "version": version,
            "author": author,
            "repoName": repo_name,
            "binCommand": bin_command,
            "publishSetup": "Requires CARGO_REGISTRY_TOKEN secret on GitHub (Settings > Secrets > Actions)",
        }

        # minRequiredRust from package.rust-version
        rust_version = pkg.get("rust-version")
        if rust_version:
            result["minRequiredRust"] = str(rust_version)

        # edition from package.edition
        edition = pkg.get("edition")
        if edition:
            result["edition"] = str(edition)

        return result

    def template_mappings(self, ctx):
        mappings = [
            {"template": "ci.yml.tpl", "target": ".github/workflows/ci.yml"},
        ]
        return mappings

    def check_project_exists(self, dir_path):
        return self.detect(dir_path)

    def get_project_init_hint(self):
        return 'Run "cargo init" or "cargo new <name>" first'

    def dev_install_command(self, project_dir):
        return {
            "global": {
                "tool": "cargo",
                "purpose": "for cargo install",
                "args": ["install", "--path", "."],
                "uninstall_args_template": ["uninstall", "{name}"],
            },
            # Cargo has no per-project venv concept; dependencies live in the
            # global ~/.cargo registry cache. Nothing meaningful for --venv.
            "venv": None,
        }
