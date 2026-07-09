"""Shared CI workflow YAML helpers: parsing, serialization, and content injection.

These functions are used by both standalone scaffold (init_cmd) and monorepo
sync to manipulate GitHub Actions workflow YAML documents.
"""

from io import StringIO

from ruamel.yaml import YAML


def parse_ci_workflow(content):
    """Parse CI workflow YAML content using round-trip mode (preserves comments, ordering).

    Returns the parsed document, or None if the content is empty or has no
    ``jobs:`` key.
    """
    yaml = YAML(typ='rt')
    doc = yaml.load(content)
    if doc is None or 'jobs' not in doc:
        return None
    return doc


def emit_ci_workflow(doc):
    """Serialize a parsed workflow document back to YAML string.

    Uses round-trip mode to preserve comment annotations and key order
    from the original parse.
    """
    yaml = YAML(typ='rt')
    yaml.default_flow_style = False
    stream = StringIO()
    yaml.dump(doc, stream)
    return stream.getvalue()


def inject_working_directory(doc, path):
    """Add defaults.run.working-directory to each job in a parsed YAML document.

    Merges with existing ``defaults.run`` (e.g. preserving ``shell``) rather
    than overwriting.  Only adds ``working-directory`` if it is not already
    present.
    """
    path = path.rstrip('/')
    for job_name, job in doc.get('jobs', {}).items():
        defaults = job.setdefault('defaults', {})
        run_section = defaults.setdefault('run', {})
        if 'working-directory' not in run_section:
            run_section['working-directory'] = path
    return doc


def rewrite_version_file_inputs(doc, project_path):
    """Prefix version-file values with project path in setup actions.

    Actions like actions/setup-go resolve ``go-version-file`` relative to
    the repo root, not ``working-directory``.  When a workflow is copied
    into a monorepo sub-project we must adjust these inputs so the runner
    can still find the file.

    Known inputs: go-version-file, python-version-file, node-version-file.
    """
    known_inputs = ('go-version-file', 'python-version-file', 'node-version-file')
    for job in doc.get('jobs', {}).values():
        for step in job.get('steps', []):
            with_block = step.get('with', {})
            if not with_block:
                continue
            for inp in known_inputs:
                if inp in with_block:
                    val = str(with_block[inp])
                    if not val.startswith('/') and not val.startswith(project_path + '/'):
                        with_block[inp] = f"{project_path}/{val}"
    return doc
