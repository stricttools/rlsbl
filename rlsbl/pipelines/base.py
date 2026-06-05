"""Base classes for release pipelines providing shared defaults and common auth patterns."""

import os
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
