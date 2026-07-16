"""Docker pipeline that builds container images, authenticates to a registry via username/password credentials, and pushes tagged images."""

import os
import subprocess
import sys

from .base import CredentialPipeline
from ..utils import require_tool, run


class DockerPipeline(CredentialPipeline):
    """Pipeline that builds and pushes Docker images to a container registry.

    Requires ``image`` and ``registry`` keys in the pipeline config dict.
    """

    _default_username_var = "DOCKER_USERNAME"
    _default_password_var = "DOCKER_PASSWORD"

    def template_dir(self) -> str | None:
        return os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "templates", "docker"
        )

    def template_mappings(self, ctx) -> list[dict[str, str]]:
        return [
            {"template": "publish.yml.tpl", "target": ".github/workflows/docker-publish.yml"},
        ]

    def publish(self, dir_path: str, version: str, ctx) -> None:
        if not self.local:
            print(f"  Skipping pipeline '{self.name}' local publish (config: local=false)")
            return

        if not self.probe_before_publish(dir_path, version, ctx):
            return

        username = os.environ.get(self.username_var)
        password = os.environ.get(self.password_var)
        if not username or not password:
            missing = []
            if not username:
                missing.append(self.username_var)
            if not password:
                missing.append(self.password_var)
            print(
                f"Error: pipeline '{self.name}' requires {' and '.join(missing)} but "
                f"{'it is' if len(missing) == 1 else 'they are'} not set",
                file=sys.stderr,
            )
            sys.exit(1)
        self._publish_command(dir_path, version, username, password)

    def _publish_command(self, dir_path: str, version: str, username: str, password: str) -> None:
        image = self.config.get("image")
        registry = self.config.get("registry")

        if not image or not registry:
            raise RuntimeError(
                f"Pipeline '{self.name}' requires 'image' and 'registry' in its config"
            )

        if not require_tool("docker", fatal=False):
            raise RuntimeError("'docker' not found on PATH, cannot publish")

        full_image = f"{registry}/{image}"
        versioned_tag = f"{full_image}:{version}"
        latest_tag = f"{full_image}:latest"
        is_prerelease = "-" in version

        try:
            run("docker", ["build", "-t", versioned_tag,
                           "--build-arg", f"VERSION={version}", "."],
                cwd=dir_path)
            run("docker", ["push", versioned_tag])
            # Skip :latest tag for pre-release versions to avoid marking
            # an unstable version as the default pull target.
            if not is_prerelease:
                run("docker", ["tag", versioned_tag, latest_tag])
                run("docker", ["push", latest_tag])
            print(f"Published Docker image: {versioned_tag}")
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"Docker publish failed: {exc}") from exc
