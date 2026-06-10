"""Docker pipeline that builds container images, authenticates to a registry via username/password credentials, and pushes tagged images."""

import os
import subprocess

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

        try:
            run("docker", ["build", "-t", versioned_tag,
                           "--build-arg", f"VERSION={version}", "."],
                cwd=dir_path)
            run("docker", ["push", versioned_tag])
            run("docker", ["tag", versioned_tag, latest_tag])
            run("docker", ["push", latest_tag])
            print(f"Published Docker image: {versioned_tag}")
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"Docker publish failed: {exc}") from exc
