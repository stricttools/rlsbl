"""Base classes for release pipelines providing shared defaults and common auth patterns."""

import os
import subprocess
import sys


class BasePipeline:
    """Concrete base providing no-op defaults for optional Pipeline methods.

    Subclasses override specific methods to implement their publish mechanism.
    """

    def __init__(self, name: str, pipeline_type: str, local: bool, config: dict):
        self.name = name
        self.pipeline_type = pipeline_type
        self.local = local
        self.config = config

    def publish(self, dir_path: str, version: str, ctx) -> None:
        pass

    def build_assets(self, dir_path: str, version: str, dist_dir: str, ctx) -> list[str]:
        return []

    def template_dir(self) -> str | None:
        return None

    def template_mappings(self, ctx) -> list[dict[str, str]]:
        return []

    def build_custom_assets(self, dist_dir: str) -> list[str]:
        """Build custom assets defined in the pipeline config.

        Reads ``self.config["custom_assets"]`` (list of dicts with ``name``
        and ``build`` keys), runs each build command via shell subprocess,
        verifies the output file exists, and checks its size against
        ``self.config["max_asset_size_mb"]``.

        Returns a list of absolute paths to the built asset files.
        Returns an empty list if no custom_assets are configured.
        """
        custom_assets = self.config.get("custom_assets", [])
        if not custom_assets:
            return []

        max_size_mb = self.config.get("max_asset_size_mb")
        max_size_bytes = max_size_mb * 1024 * 1024

        os.makedirs(dist_dir, exist_ok=True)
        output_paths = []

        for entry in custom_assets:
            name = entry["name"]
            build_cmd = entry["build"]
            output_path = os.path.join(dist_dir, name)

            result = subprocess.run(
                build_cmd,
                shell=True,
                capture_output=True,
                text=True,
                env={**os.environ, "RLSBL_DIST_DIR": dist_dir},
            )
            if result.returncode != 0:
                print(
                    f"Error: custom asset '{name}' build command failed "
                    f"(exit code {result.returncode}): {build_cmd}",
                    file=sys.stderr,
                )
                if result.stderr:
                    print(result.stderr, file=sys.stderr, end="")
                sys.exit(1)

            if not os.path.isfile(output_path):
                print(
                    f"Error: custom asset '{name}' build command succeeded "
                    f"but output file not found: {output_path}",
                    file=sys.stderr,
                )
                sys.exit(1)

            file_size = os.path.getsize(output_path)
            if file_size > max_size_bytes:
                size_mb = file_size / (1024 * 1024)
                print(
                    f"Error: custom asset '{name}' is {size_mb:.1f}MB, "
                    f"exceeds max_asset_size_mb ({max_size_mb}MB)",
                    file=sys.stderr,
                )
                sys.exit(1)

            output_paths.append(output_path)

        return output_paths

    def required_env_vars(self) -> list[str]:
        return []


class TokenPipeline(BasePipeline):
    """Base for pipelines that authenticate via a single token env var.

    Subclasses set ``_default_token_var`` and implement ``_publish_command``.
    """

    _default_token_var: str = ""

    def __init__(self, name: str, pipeline_type: str, local: bool, config: dict):
        super().__init__(name, pipeline_type, local, config)
        self.token_var = config.get("token_var", self._default_token_var)

    def publish(self, dir_path: str, version: str, ctx) -> None:
        if not self.local:
            print(f"  Skipping pipeline '{self.name}' local publish (config: local=false)")
            return
        token = os.environ.get(self.token_var)
        if not token:
            print(
                f"Error: pipeline '{self.name}' requires {self.token_var} but it's not set",
                file=sys.stderr,
            )
            sys.exit(1)
        self._publish_command(dir_path, version, token)

    def _publish_command(self, dir_path: str, version: str, token: str) -> None:
        raise NotImplementedError(
            f"{type(self).__name__} must implement _publish_command"
        )

    def required_env_vars(self) -> list[str]:
        if self.local:
            return [self.token_var]
        return []


class CredentialPipeline(BasePipeline):
    """Base for pipelines that authenticate via username/password env vars.

    Subclasses set ``_default_username_var`` and ``_default_password_var``
    and implement ``_publish_command``.
    """

    _default_username_var: str = ""
    _default_password_var: str = ""

    def __init__(self, name: str, pipeline_type: str, local: bool, config: dict):
        super().__init__(name, pipeline_type, local, config)
        self.username_var = config.get("username_var", self._default_username_var)
        self.password_var = config.get("password_var", self._default_password_var)

    def publish(self, dir_path: str, version: str, ctx) -> None:
        if not self.local:
            print(f"  Skipping pipeline '{self.name}' local publish (config: local=false)")
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
        raise NotImplementedError(
            f"{type(self).__name__} must implement _publish_command"
        )

    def required_env_vars(self) -> list[str]:
        if self.local:
            return [self.username_var, self.password_var]
        return []
