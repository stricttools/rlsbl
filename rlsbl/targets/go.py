"""Go release target using a VERSION file as source of truth, with GoReleaser integration for binaries and module proxy notification for libs."""

import glob
import json
import os
import re

from .base import BaseTarget, TemplateVars
from ..go_introspect import (
    go_pipeline_install_paths,
    list_main_packages,
    resolve_main_package_dir,
)
from ..npm_wrapper import (
    build_artifacts,
    build_npm_publish_jobs,
    load_platform_config,
    npm_wrapper_template_mappings,
)
from ..crates_wrapper import (
    build_crates_publish_jobs,
    crates_wrapper_template_mappings,
)
from ..utils import read_go_module_path

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

    detection_files = ("go.mod",)
    capabilities = frozenset({"read_name", "ci_templates", "dev_install", "publication_probe"})
    ecosystem = "Go modules"

    @property
    def name(self):
        return "go"

    def detect(self, dir_path):
        return os.path.exists(os.path.join(dir_path, "go.mod"))

    def read_name(self, dir_path, ctx):
        """Read the last segment of the module path from go.mod."""
        module_path = read_go_module_path(dir_path)
        if module_path is None:
            return None
        return module_path.rsplit("/", 1)[-1] if "/" in module_path else module_path

    def read_metadata(self, dir_path):
        """Go modules have no license/description in go.mod."""
        return {}

    def publication_probe(self, dir_path, version, ctx=None):
        """Probe for a specific version by checking tag existence via git.

        Uses ``git ls-remote --tags origin <tag>`` instead of the Go module
        proxy: tag existence is the authoritative signal for Go module
        publication (the proxy indexes from tags). This avoids proxy cache
        lag and works for private modules.
        """
        import subprocess
        from ..publication_probe import PublicationProbeResult, PublicationStatus

        tag = f"v{version}"

        try:
            result = subprocess.run(
                ["git", "ls-remote", "--tags", "origin", tag],
                cwd=dir_path,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                return PublicationProbeResult(
                    status=PublicationStatus.UNPROBEABLE,
                    registry="go",
                    version=version,
                    message=f"git ls-remote failed: {result.stderr.strip()}",
                )
            # Non-empty output means the tag exists on the remote
            if result.stdout.strip():
                return PublicationProbeResult(
                    status=PublicationStatus.PUBLISHED,
                    registry="go",
                    version=version,
                    message=f"tag {tag} exists on origin",
                )
            return PublicationProbeResult(
                status=PublicationStatus.UNPUBLISHED,
                registry="go",
                version=version,
                message=f"tag {tag} not found on origin",
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            return PublicationProbeResult(
                status=PublicationStatus.UNPROBEABLE,
                registry="go",
                version=version,
                message=f"git ls-remote error: {e}",
            )

        # -- Old proxy-based probe (kept as reference) --
        # import json as _json
        # import urllib.error
        # from ..commands.check import _request_with_backoff
        # url = f"https://proxy.golang.org/{module_path}/@v/v{version}.info"
        # try:
        #     with _request_with_backoff(url) as resp:
        #         _json.loads(resp.read())
        #     return PublicationProbeResult(
        #         status=PublicationStatus.PUBLISHED, ...)
        # except urllib.error.HTTPError as e:
        #     if e.code in (404, 410):
        #         return PublicationProbeResult(
        #             status=PublicationStatus.UNPUBLISHED, ...)
        #     return PublicationProbeResult(
        #         status=PublicationStatus.UNPROBEABLE, ...)

    def _is_library(self, dir_path):
        """Return True if the project has no `package main` package anywhere."""
        return len(list_main_packages(dir_path)) == 0

    def _has_version_var(self, dir_path, main_dir="."):
        """Return True if a .go file in *main_dir* declares a Version variable."""
        search_dir = os.path.normpath(os.path.join(dir_path, main_dir))
        for go_file in glob.glob(os.path.join(search_dir, "*.go")):
            with open(go_file, encoding="utf-8") as f:
                for line in f:
                    if re.match(r"^var\s+[Vv]ersion\b", line):
                        return True
        return False

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
        """Write the new version to the VERSION file.

        Returns a list of relative file paths that were modified.
        """
        version_path = os.path.join(dir_path, VERSION_FILE)
        tmp_path = version_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(version + "\n")
        os.replace(tmp_path, version_path)
        return [self.version_file()]

    def version_file(self, dir_path=None):
        return VERSION_FILE

    def tag_format(self, version):
        return f"v{version}"

    def companion_tags(self, name, version, path=None):
        """Return Go module proxy companion tags for monorepo packages.

        When ``path`` is set (monorepo member), the Go module proxy
        needs a tag of the form ``{path}/v{version}`` to resolve the
        module.  This tag is identical to the Go target's primary
        ``monorepo_tag_format`` output, so it is only useful as a
        *companion* when a different target (e.g. npm) is the primary
        release target and produces a non-Go-compatible primary tag.
        """
        if path is not None:
            sep = "" if path.endswith("/") else "/"
            return [f"{path}{sep}v{version}"]
        return []

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

    def template_vars(self, dir_path, ctx):
        """Extract template variables from go.mod and .rlsbl/config.json."""
        config = ctx.config if ctx else {}
        name = read_go_module_path(dir_path)
        if name is None:
            return TemplateVars(self.name, {})

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
        from .utils import _get_git_author
        author = _get_git_author()

        try:
            version = self.read_version(dir_path)
        except FileNotFoundError:
            version = "0.0.0"

        is_library = self._is_library(dir_path)
        if is_library:
            publish_setup = "Go library -- no publish step needed. Tagged releases are available via go get."
            goreleaser_main = ""
        else:
            publish_setup = "GoReleaser handles binary publishing via GitHub Actions (no secrets needed)"
            # The main package path for goreleaser: hard error on ambiguity
            # (multiple mains without declared install_paths) instead of a
            # silent "." fallback that produces a broken .goreleaser.yml.
            goreleaser_main = resolve_main_package_dir(dir_path, config or {})

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
        if npm_scope and not is_library:
            specs = load_platform_config(config or {})
            artifacts = build_artifacts(specs, short_name, _go_archive_fn)
            npm_publish_jobs = build_npm_publish_jobs(
                npm_scope, short_name, artifacts
            )
            publish_setup += "\n- Add NPM_TOKEN secret for npm binary wrapper publishing"

        # crates.io wrapper publish job
        crates_publish_jobs = ""
        crates_wrapper_config = config.get("crates_wrapper", {}) if config else {}
        crates_wrapper_enabled = crates_wrapper_config.get("enabled", False)
        if crates_wrapper_enabled and not is_library and repo_name:
            crates_publish_jobs = build_crates_publish_jobs(
                short_name, repo_name,
            )
            publish_setup += (
                "\n- Configure Trusted Publishing on crates.io for the wrapper crate"
                "\n  (crates.io > Manage > Settings > Trusted Publishing > Add GitHub repo)"
            )

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
            "cratesPublishJobs": crates_publish_jobs,
        }

        # Extract minimum required Go version from go.mod
        mod_path = os.path.join(dir_path, "go.mod")
        if os.path.exists(mod_path):
            with open(mod_path, encoding="utf-8") as f:
                mod_content = f.read()
            m = _GO_VERSION_RE.search(mod_content)
            if m:
                result["minRequiredGo"] = m.group(1)

        return TemplateVars(self.name, result)

    def template_mappings(self, ctx):
        project_root = str(ctx.project_root)
        mappings = [
            {"template": "VERSION.tpl", "target": "VERSION"},
            {"template": "ci.yml.tpl", "target": ".github/workflows/ci.yml"},
        ]
        # Without go.mod there is no module to introspect (mirrors the
        # template_vars early return); binary scaffolding needs a module.
        if not os.path.exists(os.path.join(project_root, "go.mod")):
            return mappings
        if not self._is_library(project_root):
            mappings.append(
                {"template": "goreleaser.yml.tpl", "target": ".goreleaser.yml"},
            )
            # version.go goes into the detected main-package dir, never
            # unconditionally into the project root: a root `package main`
            # file without func main breaks `go build ./...` for cmd-layout
            # projects.
            main_dir = resolve_main_package_dir(
                project_root, ctx.config if ctx else {}
            )
            if not self._has_version_var(project_root, main_dir):
                version_go_target = (
                    "version.go"
                    if main_dir == "."
                    else main_dir[2:] + "/version.go"
                )
                mappings.append(
                    {"template": "version.go.tpl", "target": version_go_target},
                )
            # npm wrapper scaffolding is in shared_template_mappings()
        return mappings

    def shared_template_mappings(self, ctx):
        mappings = super().shared_template_mappings(ctx)
        project_root = str(ctx.project_root)
        if not os.path.exists(os.path.join(project_root, "go.mod")):
            return mappings
        if not self._is_library(project_root):
            config = ctx.config if ctx else {}
            npm_wrapper_config = config.get("npm_wrapper", {})
            if npm_wrapper_config.get("scope"):
                mappings.extend(npm_wrapper_template_mappings())
            crates_wrapper_config = config.get("crates_wrapper", {})
            if crates_wrapper_config.get("enabled"):
                mappings.extend(crates_wrapper_template_mappings())
        return mappings

    def check_project_exists(self, dir_path):
        return os.path.exists(os.path.join(dir_path, "go.mod"))

    def get_project_init_hint(self):
        return 'Run "go mod init <module-path>" first'

    def dev_install_command(self, project_dir):
        from ..config import read_project_config
        from ..go_introspect import (
            GoIntrospectError,
            describe_main_packages,
            validate_install_paths,
        )

        if os.path.exists(os.path.join(project_dir, "go.mod")):
            config = read_project_config(project_dir)
            declared = go_pipeline_install_paths(config)
            if declared is None:
                mains = list_main_packages(project_dir)
                if not mains:
                    # Go library: no binaries exist, so there is nothing to
                    # `go install` and no install_paths declaration could
                    # ever validate. Return the no-op spec shape that
                    # `rlsbl dev install` skips instead of hard-erroring; the
                    # "reason" key is surfaced in the skip message.
                    return {
                        "global": None,
                        "venv": None,
                        "reason": "Go library: nothing to install (no main packages)",
                    }
                raise GoIntrospectError(
                    "the go pipeline in .rlsbl/config.json does not declare "
                    "'install_paths', which is required to run 'go install'. "
                    f"{describe_main_packages(mains)} "
                    'Declare e.g. "install_paths": '
                    f"{json.dumps([p.rel_dir for p in mains])} "
                    "on the go pipeline entry."
                )
            args = ["install"] + validate_install_paths(project_dir, declared)
        else:
            # Not a Go project dir (e.g. docs introspection of the target
            # table): return the generic shape of the command.
            args = ["install", "<install_paths from .rlsbl/config.json>"]

        return {
            "global": {
                "tool": "go",
                "purpose": "for go install",
                "args": args,
                # `go install` does not have a clean reverse; tell the user.
                "uninstall_args_template": None,
            },
            # Go has no per-project venv concept; modules are managed globally
            # in GOPATH/pkg/mod. Nothing meaningful to do for --venv.
            "venv": None,
        }
