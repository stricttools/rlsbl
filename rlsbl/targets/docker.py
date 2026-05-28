"""Docker release target using a VERSION file as source of truth, with opt-in activation via config and image publishing to a registry."""

import os
import sys

from .base import BaseTarget
from ..config import get_publish_config, read_project_config
from ..utils import require_tool, run

VERSION_FILE = "VERSION"


class DockerTarget(BaseTarget):
    """Release target for Docker projects (Dockerfile + VERSION file)."""

    @property
    def name(self):
        return "docker"

    def detect(self, dir_path):
        return os.path.exists(os.path.join(dir_path, "Dockerfile"))

    def read_name(self, dir_path, project_root=None):
        """Return image name from config or directory name."""
        config = read_project_config(project_root)
        docker_config = config.get("docker", {})
        image = docker_config.get("image")
        if image:
            return image
        return os.path.basename(os.path.abspath(dir_path))

    def read_metadata(self, dir_path):
        """Docker projects have no standard metadata in the manifest."""
        return {}

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
        """Write the new version to the VERSION file atomically.

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

    def publish(self, dir_path, version, project_root=None):
        """Build and push Docker image to the configured registry."""
        pub_config = get_publish_config(self.name, project_root)

        if pub_config.get("local") is False:
            print(f"Skipping local {self.name} publish (config: local=false). CI will handle it.")
            return

        # Docker uses two env vars (username + password). Config may override either.
        username_var = pub_config.get("username_var", "DOCKER_USERNAME")
        password_var = pub_config.get("password_var", "DOCKER_PASSWORD")
        username = os.environ.get(username_var)
        password = os.environ.get(password_var)
        if not username or not password:
            if pub_config.get("local") is True:
                print(
                    f"ERROR: {self.name} publish requested (local=true) but "
                    f"{username_var}/{password_var} are not set.",
                    file=sys.stderr,
                )
                sys.exit(1)
            print(f"Skipping local docker publish (no {username_var}/{password_var}). CI will handle it.")
            return

        config = read_project_config(project_root)
        docker_config = config.get("docker", {})
        image = docker_config.get("image")
        registry = docker_config.get("registry")

        if not image or not registry:
            print("Error: docker config missing. Set 'docker.image' and "
                  "'docker.registry' in .rlsbl/config.json")
            return

        if not require_tool("docker", fatal=False):
            print("Error: 'docker' not found on PATH, cannot publish")
            return

        full_image = f"{registry}/{image}"
        versioned_tag = f"{full_image}:{version}"
        latest_tag = f"{full_image}:latest"

        # Build
        run("docker", ["build", "-t", versioned_tag,
                       "--build-arg", f"VERSION={version}", "."])
        # Push versioned
        run("docker", ["push", versioned_tag])
        # Tag and push latest
        run("docker", ["tag", versioned_tag, latest_tag])
        run("docker", ["push", latest_tag])

        print(f"Published Docker image: {versioned_tag}")

    def template_dir(self):
        return os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "templates", "docker"
        )

    def template_vars(self, dir_path, project_root=None):
        """Extract template variables from config and git."""
        config = read_project_config(project_root)
        docker_config = config.get("docker", {})
        image = docker_config.get("image", "")
        registry = docker_config.get("registry", "")

        name = os.path.basename(os.path.abspath(dir_path))

        author = ""
        try:
            author = run("git", ["config", "user.name"])
        except Exception:
            pass

        return {
            "image": image,
            "registry": registry,
            "name": name,
            "author": author,
        }

    def template_mappings(self):
        return [
            {"template": "ci.yml.tpl", "target": ".github/workflows/ci.yml"},
            {"template": "publish.yml.tpl", "target": ".github/workflows/docker-publish.yml"},
        ]

    def check_project_exists(self, dir_path):
        return os.path.exists(os.path.join(dir_path, "Dockerfile"))

    def get_project_init_hint(self):
        return 'Create a Dockerfile first'
