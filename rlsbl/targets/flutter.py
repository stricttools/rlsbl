"""Flutter release target that extends DartTarget, detecting Flutter projects by requiring the flutter: section in pubspec.yaml for version bumps."""

import os

from ruamel.yaml import YAML

from .dart import DartTarget


class FlutterTarget(DartTarget):
    """Release target for Flutter apps (pubspec.yaml with flutter: section)."""

    # Shares pubspec.yaml with dart; no unique detection files.
    detection_files = ()
    capabilities = frozenset({"read_name", "read_metadata"})
    ecosystem = "Flutter"

    @property
    def name(self):
        return "flutter"

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
