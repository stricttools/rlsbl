"""Go release target using a VERSION file as source of truth, with GoReleaser integration for binaries and module proxy notification for libs."""

import glob
import os
import re
import shutil
import subprocess

from .base import BaseTarget
from ..config import read_project_config
from ..utils import require_tool
from ..npm_wrapper import (
    build_artifacts,
    build_npm_publish_jobs,
    load_platform_config,
    npm_wrapper_template_mappings,
)
from ..utils import run

VERSION_FILE = "VERSION"

_GO_VERSION_RE = re.compile(r"^go\s+(\d+\.\d+(?:\.\d+)?)", re.MULTILINE)

# Goreleaser archive suffix and extension for each npm platform.
_GORELEASER_MAP: dict[str, tuple[str, str]] = {
    "linux-x64": ("linux_amd64", "tar.gz"),
    "linux-arm64": ("linux_arm64", "tar.gz"),
    "darwin-x64": ("darwin_amd64", "tar.gz"),
    "darwin-arm64": ("darwin_arm64", "tar.gz"),
    "win32-x64": ("windows_amd64", "zip"),
    "win32-arm64": ("windows_arm64", "zip"),
}


def _go_archive_fn(spec, name):
    """Return (asset_pattern, extract_cmd, binary_name) for a Go/goreleaser build."""
    suffix, ext = _GORELEASER_MAP[spec.npm_platform]
    asset = f"{{name}}_{{version}}_{suffix}.{ext}"
    extract = "tar xzf" if ext == "tar.gz" else "unzip"
    binary = name + (".exe" if "win32" in spec.npm_platform else "")
    return (asset, extract, binary)


class GoTarget(BaseTarget):
    """Release target for Go projects (go.mod + VERSION file)."""

    @property
    def name(self):
        return "go"

    def detect(self, dir_path):
        return os.path.exists(os.path.join(dir_path, "go.mod"))

    def read_name(self, dir_path):
        """Read the last segment of the module path from go.mod."""
        module_path = self._read_module_path(dir_path)
        if not module_path:
            return None
        return module_path.rsplit("/", 1)[-1] if "/" in module_path else module_path

    def read_metadata(self, dir_path):
        """Go modules have no license/description in go.mod."""
        return {}

    def _read_module_path(self, dir_path):
        """Extract the module path from go.mod, or empty string if unavailable."""
        mod_path = os.path.join(dir_path, "go.mod")
        if not os.path.exists(mod_path):
            return ""
        with open(mod_path, encoding="utf-8") as f:
            content = f.read()
        match = re.search(r"^module\s+(\S+)", content, re.MULTILINE)
        return match.group(1) if match else ""

    def _is_library(self, dir_path):
        """Return True if the project has no `package main` in root .go files."""
        for go_file in glob.glob(os.path.join(dir_path, "*.go")):
            with open(go_file, encoding="utf-8") as f:
                for line in f:
                    if re.match(r"^package\s+main\b", line):
                        return False
        return True

    def _has_root_main(self, dir_path):
        """Return True if there's a main.go at the project root with `package main`."""
        main_go = os.path.join(dir_path, "main.go")
        if not os.path.exists(main_go):
            return False
        with open(main_go, encoding="utf-8") as f:
            first_line = f.readline()
        return bool(re.match(r"^package\s+main\b", first_line))

    def _has_version_var(self, dir_path):
        """Return True if any root-level .go file declares a Version variable."""
        for go_file in glob.glob(os.path.join(dir_path, "*.go")):
            with open(go_file, encoding="utf-8") as f:
                for line in f:
                    if re.match(r"^var\s+[Vv]ersion\b", line):
                        return True
        return False

    def _has_cmd_main(self, dir_path):
        """Return True if there's a single cmd/*/main.go with `package main`.

        Returns False if there are multiple cmd/ subdirectories (multi-binary
        repos where cmd/ is the correct layout) or no cmd/ entries at all.
        """
        matches = glob.glob(os.path.join(dir_path, "cmd", "*", "main.go"))
        if not matches:
            return False
        # Count distinct cmd/ subdirectories (not just main.go files)
        cmd_dirs = set(os.path.dirname(m) for m in matches)
        if len(cmd_dirs) > 1:
            return False
        # Verify the single match has `package main`
        with open(matches[0], encoding="utf-8") as f:
            first_line = f.readline()
        return bool(re.match(r"^package\s+main\b", first_line))

    def build_assets(self, dir_path, version, dist_dir):
        """Build Go binaries into dist_dir.

        Uses goreleaser for cross-compilation when available, falling back
        to ``go build`` (host platform only) otherwise.
        """
        os.makedirs(dist_dir, exist_ok=True)

        if shutil.which("goreleaser"):
            try:
                return self._build_with_goreleaser(dir_path, dist_dir)
            except (subprocess.CalledProcessError, OSError) as exc:
                print(f"Warning: goreleaser failed ({exc}), falling back to go build.")

        else:
            print("goreleaser not found, building for host platform only.")

        # Fallback: host-only build
        run("go", ["build", "-o", dist_dir + "/", "./..."], cwd=dir_path)
        return sorted(glob.glob(os.path.join(dist_dir, "*")))

    def _build_with_goreleaser(self, dir_path, dist_dir):
        """Run goreleaser build and collect cross-compiled binaries into *dist_dir*."""
        run(
            "goreleaser",
            ["build", "--snapshot", "--clean"],
            cwd=dir_path,
        )
        goreleaser_dist = os.path.join(dir_path, "dist")
        artifacts = []
        for direntry in sorted(os.scandir(goreleaser_dist), key=lambda e: e.name):
            if not direntry.is_dir():
                continue
            for fentry in sorted(os.scandir(direntry.path), key=lambda e: e.name):
                if fentry.is_file() and not fentry.name.startswith("."):
                    dest = os.path.join(dist_dir, f"{direntry.name}__{fentry.name}")
                    shutil.copy2(fentry.path, dest)
                    artifacts.append(dest)
        return sorted(artifacts)

    def publish(self, dir_path, version, project_root):
        """Notify the Go module proxy so the new version is immediately available."""
        module_path = self._read_module_path(dir_path)
        if not module_path:
            print("Warning: could not read module path from go.mod, skipping proxy notification")
            return

        if not require_tool("go", fatal=False):
            print("Warning: 'go' not found on PATH, skipping proxy notification")
            return

        ref = f"{module_path}@v{version}"
        env = {**os.environ, "GOPROXY": "proxy.golang.org"}
        try:
            run("go", ["list", "-m", ref], env=env)
            print(f"Notified Go module proxy: {ref}")
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            print(f"Warning: proxy notification failed for {ref}: {exc}")

        # Install the binary locally for CLI projects
        if self._has_cmd_main(dir_path):
            matches = glob.glob(os.path.join(dir_path, "cmd", "*", "main.go"))
            cmd_name = os.path.basename(os.path.dirname(matches[0]))
            install_path = f"./cmd/{cmd_name}"
        elif self._has_root_main(dir_path):
            install_path = "."
        else:
            install_path = None

        if install_path:
            try:
                subprocess.run(
                    ["go", "install", install_path],
                    cwd=dir_path,
                    check=True,
                )
                print(f"Installed: go install {install_path}")
            except (subprocess.CalledProcessError, FileNotFoundError) as exc:
                print(f"Warning: go install failed: {exc}")

    def read_version(self, dir_path):
        """Read version from the VERSION file."""
        version_path = os.path.join(dir_path, VERSION_FILE)
        if not os.path.exists(version_path):
            raise FileNotFoundError(
                f"No {VERSION_FILE} file found. Run 'rlsbl scaffold' first."
            )
        with open(version_path, "r", encoding="utf-8") as f:
            return f.read().strip()

    def write_version(self, dir_path, version):
        """Write the new version to the VERSION file.

        Returns a list of relative file paths that were modified.
        """
        version_path = os.path.join(dir_path, VERSION_FILE)
        tmp_path = version_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(version + "\n")
        os.replace(tmp_path, version_path)
        return [self.version_file()]

    def version_file(self):
        return VERSION_FILE

    def tag_format(self, version):
        return f"v{version}"

    def monorepo_tag_format(self, name, version, path=None):
        if path is not None:
            sep = "" if path.endswith("/") else "/"
            return f"{path}{sep}v{version}"
        return super().monorepo_tag_format(name, version, path)

    def monorepo_tag_glob(self, name, path=None):
        if path is not None:
            sep = "" if path.endswith("/") else "/"
            return f"{path}{sep}v*"
        return super().monorepo_tag_glob(name, path)

    def template_dir(self):
        return os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "templates", "go"
        )

    def template_vars(self, dir_path, project_root):
        """Extract template variables from go.mod and .rlsbl/config.json."""
        config = read_project_config(project_root)
        name = self._read_module_path(dir_path)

        # Derive short name from module path (last segment)
        short_name = name.rsplit("/", 1)[-1] if "/" in name else name

        # Derive repo name from module path (e.g. "github.com/user/repo")
        repo_name = ""
        repo_match = re.search(r"github\.com/([^/\s]+/[^/\s]+)", name)
        if repo_match:
            repo_name = repo_match.group(1)

        # Extract owner from repo name (e.g. "smm-h" from "smm-h/rlsbl")
        github_owner = repo_name.split("/")[0] if "/" in repo_name else ""

        # Author from git config
        author = ""
        try:
            author = run("git", ["config", "user.name"])
        except Exception:
            pass

        try:
            version = self.read_version(dir_path)
        except FileNotFoundError:
            version = "0.0.0"

        if self._is_library(dir_path):
            publish_setup = "Go library -- no publish step needed. Tagged releases are available via go get."
        else:
            publish_setup = "GoReleaser handles binary publishing via GitHub Actions (no secrets needed)"

        # Determine the main package path for goreleaser
        if self._has_root_main(dir_path):
            goreleaser_main = "."
        elif self._has_cmd_main(dir_path):
            matches = glob.glob(os.path.join(dir_path, "cmd", "*", "main.go"))
            cmd_name = os.path.basename(os.path.dirname(matches[0]))
            goreleaser_main = f"./cmd/{cmd_name}"
        else:
            goreleaser_main = "."

        # npm binary wrapper support
        npm_wrapper_config = config.get("npm_wrapper", {}) if config else {}
        npm_scope = npm_wrapper_config.get("scope", "")

        # Homebrew tap support via goreleaser brews section
        homebrew_config = config.get("homebrew", {}) if config else {}
        tap_repo = homebrew_config.get("tap", "")
        brews_section = ""
        homebrew_env = ""

        if tap_repo and github_owner:
            description = homebrew_config.get("description", short_name)
            license_id = homebrew_config.get("license", "MIT")
            brews_section = (
                "\n"
                "\nbrews:"
                "\n  - repository:"
                "\n      owner: " + github_owner +
                "\n      name: " + tap_repo +
                '\n      token: "{{ .Env.HOMEBREW_TAP_TOKEN }}"'
                '\n    homepage: "https://github.com/' + repo_name + '"'
                '\n    description: "' + description + '"'
                '\n    license: "' + license_id + '"'
                "\n    install: |"
                "\n      bin.install \"" + short_name + "\""
                "\n    test: |"
                '\n      system "#{bin}/' + short_name + '", "--version"'
            )
            homebrew_env = "\n          HOMEBREW_TAP_TOKEN: ${{ secrets.HOMEBREW_TAP_TOKEN }}"
            publish_setup += "\n- Add HOMEBREW_TAP_TOKEN secret (PAT with contents:write on the tap repo)"

        # npm wrapper publish job
        npm_publish_jobs = ""
        if npm_scope and not self._is_library(dir_path):
            specs = load_platform_config(config or {})
            artifacts = build_artifacts(specs, short_name, _go_archive_fn)
            npm_publish_jobs = build_npm_publish_jobs(
                npm_scope, short_name, artifacts
            )
            publish_setup += "\n- Add NPM_TOKEN secret for npm binary wrapper publishing"

        result = {
            "name": short_name,
            "modulePath": name,
            "version": version,
            "author": author,
            "repoName": repo_name,
            "githubOwner": github_owner,
            "binCommand": short_name,
            "npmScope": npm_scope,
            "publishSetup": publish_setup,
            "goreleaserMain": goreleaser_main,
            "brewsSection": brews_section,
            "homebrewEnv": homebrew_env,
            "npmPublishJobs": npm_publish_jobs,
        }

        # Extract minimum required Go version from go.mod
        mod_path = os.path.join(dir_path, "go.mod")
        if os.path.exists(mod_path):
            with open(mod_path, encoding="utf-8") as f:
                mod_content = f.read()
            m = _GO_VERSION_RE.search(mod_content)
            if m:
                result["minRequiredGo"] = m.group(1)

        return result

    def template_mappings(self):
        mappings = [
            {"template": "VERSION.tpl", "target": "VERSION"},
            {"template": "ci.yml.tpl", "target": ".github/workflows/ci.yml"},
        ]
        if not self._is_library("."):
            mappings.extend([
                {"template": "publish.yml.tpl", "target": ".github/workflows/publish.yml"},
                {"template": "goreleaser.yml.tpl", "target": ".goreleaser.yml"},
            ])
            if not self._has_version_var("."):
                mappings.append(
                    {"template": "version.go.tpl", "target": "version.go"},
                )
            # npm wrapper scaffolding is in shared_template_mappings()
        return mappings

    def shared_template_mappings(self, project_root):
        mappings = super().shared_template_mappings()
        if not self._is_library("."):
            config = read_project_config(project_root)
            npm_wrapper_config = config.get("npm_wrapper", {})
            if npm_wrapper_config.get("scope"):
                mappings.extend(npm_wrapper_template_mappings())
        return mappings

    def check_project_exists(self, dir_path):
        return os.path.exists(os.path.join(dir_path, "go.mod"))

    def get_project_init_hint(self):
        return 'Run "go mod init <module-path>" first'

    def dev_install_command(self, project_dir):
        return {
            "global": {
                "tool": "go",
                "purpose": "for go install",
                "args": ["install", "./..."],
                # `go install` does not have a clean reverse; tell the user.
                "uninstall_args_template": None,
            },
            # Go has no per-project venv concept; modules are managed globally
            # in GOPATH/pkg/mod. Nothing meaningful to do for --venv.
            "venv": None,
        }
