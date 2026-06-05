"""Maven/Gradle pipeline -- publishes JVM packages via gradlew or mvn."""

import os
import subprocess
import sys

from .base import BasePipeline
from ..utils import run


class MavenPipeline(BasePipeline):
    """Pipeline that publishes via Gradle or Maven.

    Detects whether to use ``gradlew publish`` or ``mvn deploy`` based on
    the presence of a ``gradlew`` script or ``pom.xml``.
    """

    def publish(self, dir_path: str, version: str, ctx) -> None:
        if not self.local:
            print(f"  Skipping pipeline '{self.name}' local publish (config: local=false)")
            return

        token_var = self.config.get("token_var", "GITHUB_TOKEN")
        token = os.environ.get(token_var)
        if not token:
            print(
                f"Error: pipeline '{self.name}' requires {token_var} but it's not set",
                file=sys.stderr,
            )
            sys.exit(1)

        gradlew = os.path.join(dir_path, "gradlew")
        if os.path.exists(gradlew):
            try:
                run("./gradlew", ["publish"], env={**os.environ}, cwd=dir_path)
                print(f"Published via Gradle: {version}")
            except subprocess.CalledProcessError as exc:
                raise RuntimeError(f"Gradle publish failed: {exc}") from exc
        elif os.path.exists(os.path.join(dir_path, "pom.xml")):
            try:
                run("mvn", ["deploy"], env={**os.environ}, cwd=dir_path)
                print(f"Published via Maven: {version}")
            except subprocess.CalledProcessError as exc:
                raise RuntimeError(f"Maven deploy failed: {exc}") from exc
        else:
            raise RuntimeError(
                f"Pipeline '{self.name}': no gradlew or pom.xml found in {dir_path}"
            )

    def required_env_vars(self) -> list[str]:
        if self.local:
            return [self.config.get("token_var", "GITHUB_TOKEN")]
        return []
