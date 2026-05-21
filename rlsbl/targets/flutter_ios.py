"""Flutter iOS release target extending DartTarget with platform-specific tag formats and detection requiring the flutter: section in pubspec.yaml."""

import os

from ruamel.yaml import YAML

from .dart import DartTarget


class FlutterIosTarget(DartTarget):
    """Release target for Flutter iOS apps (pubspec.yaml with flutter: section)."""

    @property
    def name(self):
        return "flutter-ios"

    def detect(self, dir_path):
        pubspec = os.path.join(dir_path, "pubspec.yaml")
        if not os.path.exists(pubspec):
            return False
        yaml = YAML(typ="safe")
        with open(pubspec, "r", encoding="utf-8") as f:
            data = yaml.load(f)
        if not isinstance(data, dict):
            return False
        return "flutter" in data

    def monorepo_tag_format(self, name, version, path=None):
        return f"{name}-ios@v{version}"

    def monorepo_tag_glob(self, name, path=None):
        return f"{name}-ios@v*"
