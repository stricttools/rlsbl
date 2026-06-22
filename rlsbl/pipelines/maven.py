"""Maven and Gradle pipelines for JVM package publishing.

MavenPipeline ("maven") publishes to GitHub Packages via gradlew publish or mvn deploy.
MavenCentralPipeline ("maven-central") publishes to Maven Central via the
vanniktech/gradle-maven-publish-plugin (Gradle) or mvn deploy with Central Portal
configuration (Maven).
"""

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

    def template_dir(self) -> str | None:
        return os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "templates", "maven"
        )

    def template_mappings(self, ctx) -> list[dict[str, str]]:
        return [
            {"template": "publish.yml.tpl", "target": ".github/workflows/publish.yml"},
        ]

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


# Required env vars for Maven Central publishing (Central Portal user tokens + GPG signing)
_MAVEN_CENTRAL_REQUIRED_VARS = [
    "ORG_GRADLE_PROJECT_mavenCentralUsername",
    "ORG_GRADLE_PROJECT_mavenCentralPassword",
    "ORG_GRADLE_PROJECT_signingInMemoryKey",
    "ORG_GRADLE_PROJECT_signingInMemoryKeyPassword",
]


class MavenCentralPipeline(BasePipeline):
    """Pipeline that publishes to Maven Central.

    For Gradle projects (detected by ``gradlew``), delegates to the
    vanniktech/gradle-maven-publish-plugin via
    ``./gradlew publishAndReleaseToMavenCentral``.

    For pure Maven projects (detected by ``pom.xml`` without ``gradlew``),
    falls back to ``mvn deploy`` with Central Portal configuration.

    Required env vars (Central Portal user tokens + GPG signing):
    - ORG_GRADLE_PROJECT_mavenCentralUsername
    - ORG_GRADLE_PROJECT_mavenCentralPassword
    - ORG_GRADLE_PROJECT_signingInMemoryKey
    - ORG_GRADLE_PROJECT_signingInMemoryKeyPassword

    Optional: ORG_GRADLE_PROJECT_signingInMemoryKeyId (for specific GPG subkey)
    """

    def template_dir(self) -> str | None:
        return os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "templates", "maven"
        )

    def template_mappings(self, ctx) -> list[dict[str, str]]:
        return [
            {"template": "publish-central.yml.tpl", "target": ".github/workflows/publish.yml"},
        ]

    def publish(self, dir_path: str, version: str, ctx) -> None:
        if not self.local:
            print(f"  Skipping pipeline '{self.name}' local publish (config: local=false)")
            return

        missing = [v for v in _MAVEN_CENTRAL_REQUIRED_VARS if not os.environ.get(v)]
        if missing:
            print(
                f"Error: pipeline '{self.name}' requires {', '.join(missing)} "
                f"but {'it is' if len(missing) == 1 else 'they are'} not set",
                file=sys.stderr,
            )
            sys.exit(1)

        gradlew = os.path.join(dir_path, "gradlew")
        if os.path.exists(gradlew):
            try:
                run(
                    "./gradlew",
                    ["publishAndReleaseToMavenCentral"],
                    env={**os.environ},
                    cwd=dir_path,
                )
                print(f"Published to Maven Central via Gradle: {version}")
            except subprocess.CalledProcessError as exc:
                raise RuntimeError(
                    f"Gradle publishAndReleaseToMavenCentral failed: {exc}"
                ) from exc
        elif os.path.exists(os.path.join(dir_path, "pom.xml")):
            try:
                run("mvn", ["deploy"], env={**os.environ}, cwd=dir_path)
                print(f"Published to Maven Central via Maven: {version}")
            except subprocess.CalledProcessError as exc:
                raise RuntimeError(f"Maven deploy failed: {exc}") from exc
        else:
            raise RuntimeError(
                f"Pipeline '{self.name}': no gradlew or pom.xml found in {dir_path}"
            )

    def required_env_vars(self) -> list[str]:
        if self.local:
            return list(_MAVEN_CENTRAL_REQUIRED_VARS)
        return []
