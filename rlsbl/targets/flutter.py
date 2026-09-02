"""Flutter release target that extends DartTarget, detecting Flutter projects by requiring the flutter: section in pubspec.yaml for version bumps."""

import os

from ruamel.yaml import YAML

from .dart import DartTarget


class FlutterTarget(DartTarget):
    """Release target for Flutter apps (pubspec.yaml with flutter: section)."""

    # Shares pubspec.yaml with dart; no unique detection files.
    detection_files = ()
    ecosystem = "Flutter"

    # A Flutter app IS Dart sources, so the inherited Dart import analysers
    # answer for it -- flutter is in scope for import analysis and cycle
    # detection exactly as dart is.

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

    def template_dir(self):
        return os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "templates", "flutter"
        )

    def template_mappings(self, ctx):
        return [
            {"template": "ci.yml.tpl", "target": ".github/workflows/ci.yml"},
        ]
