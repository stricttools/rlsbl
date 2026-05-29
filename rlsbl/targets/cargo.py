"""Cargo (Rust) release target that uses tomlkit for round-trip Cargo.toml editing with hybrid publish support via CARGO_REGISTRY_TOKEN."""

import glob
import os
import re
import shutil
import subprocess
import sys

import tomlkit

from .base import BaseTarget
from ..config import get_publish_config
from ..utils import run


class CargoTarget(BaseTarget):
    """Release target for Rust/Cargo projects (Cargo.toml)."""

    @property
    def name(self):
        return "cargo"

    def read_name(self, dir_path):
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
            raise ValueError(f"No [package].version in {cargo_path}")
        return str(doc["package"]["version"])

    def write_version(self, dir_path, version):
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

    def version_file(self):
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

    def template_vars(self, dir_path, project_root):
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
            try:
                author = run("git", ["config", "user.name"])
            except Exception:
                pass

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

    def template_mappings(self):
        mappings = [
            {"template": "ci.yml.tpl", "target": ".github/workflows/ci.yml"},
        ]
        if not self._is_library("."):
            mappings.append(
                {"template": "publish.yml.tpl", "target": ".github/workflows/publish.yml"},
            )
        return mappings

    def build_assets(self, dir_path, version, dist_dir):
        """Build Rust binary in release mode and copy to dist_dir."""
        os.makedirs(dist_dir, exist_ok=True)
        run("cargo", ["build", "--release"], cwd=dir_path)

        # Determine the binary name from Cargo.toml
        name = self.read_name(dir_path) or ""
        target_release = os.path.join(dir_path, "target", "release", name)
        if os.path.isfile(target_release):
            shutil.copy2(target_release, dist_dir)

        return sorted(glob.glob(os.path.join(dist_dir, "*")))

    def publish(self, dir_path, version, ctx):
        """Publish to crates.io based on per-target config and token availability.

        ctx: ProjectContext carrying project_root, monorepo_root, and config.
        """
        pub_config = get_publish_config(self.name, ctx.config)

        if pub_config.get("local") is False:
            print(f"Skipping local {self.name} publish (config: local=false). CI will handle it.")
            return

        token_var = pub_config.get("token_var", "CARGO_REGISTRY_TOKEN")
        token = os.environ.get(token_var)
        if not token:
            if pub_config.get("local") is True:
                print(
                    f"ERROR: {self.name} publish requested (local=true) but {token_var} is not set.",
                    file=sys.stderr,
                )
                sys.exit(1)
            print(f"Skipping local cargo publish (no {token_var}). CI will handle it.")
            return

        try:
            run("cargo", ["publish"], env={
                **os.environ,
                "CARGO_REGISTRY_TOKEN": token,
            })
            print(f"Published to crates.io: {version}")
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"cargo publish failed: {exc}") from exc

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
