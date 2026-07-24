"""Shared CI workflow YAML helpers: parsing, serialization, and content injection.

These functions are used by both standalone scaffold (init_cmd) and monorepo
sync to manipulate GitHub Actions workflow YAML documents.
"""

import os
from io import StringIO

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.scalarstring import LiteralScalarString


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


def _emit_ci_workflow_faithful(doc):
    """Serialize a workflow with indentation matching the scaffold templates.

    ``indent(mapping=2, sequence=4, offset=2)`` reproduces the template style
    (``  - uses: ...`` block sequences) and a wide ``width`` prevents ruamel
    from wrapping long scalars (service ``options`` string, DSNs). With these
    settings ruamel round-trips an untouched template byte-for-byte, so the
    services injection only changes the region it adds -- unrelated hand edits
    survive the three-way merge, and a re-scaffold re-emits identical output.
    """
    yaml = YAML(typ='rt')
    yaml.default_flow_style = False
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.width = 4096
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


# ---------------------------------------------------------------------------
# CI service-container injection (config ``services`` + ``test_env``)
# ---------------------------------------------------------------------------


def _ci_target_from_basename(basename, single_target):
    """Return the release-target name a CI workflow filename maps to.

    ``ci.yml`` maps to *single_target* (the lone target on the single-target
    scaffold path). ``ci-<target>.yml`` maps to ``<target>``. Anything else
    (e.g. ``ci-custom.yml``, a user-owned file) returns ``None`` so it is
    skipped.
    """
    if basename == "ci.yml":
        return single_target
    if basename.startswith("ci-") and basename.endswith(".yml"):
        target = basename[len("ci-"):-len(".yml")]
        if target == "custom":
            return None
        return target or None
    return None


def _build_health_options(health):
    """Build the ``options`` string (docker create flags) from a health map.

    Space-joined on one line: GitHub passes ``options`` verbatim to
    ``docker create``, so the flags must be space-separated, never newline-
    separated. Emitted as a plain scalar (the wide emitter width keeps it on a
    single line rather than folding it into newline-separated fragments).
    """
    parts = [f'--health-cmd "{health["cmd"]}"']
    if "interval" in health:
        parts.append(f'--health-interval {health["interval"]}')
    if "timeout" in health:
        parts.append(f'--health-timeout {health["timeout"]}')
    if "retries" in health:
        parts.append(f'--health-retries {health["retries"]}')
    return " ".join(parts)


def _build_service_map(svc):
    """Build the per-service YAML mapping (image/env/ports/options)."""
    m = CommentedMap()
    m["image"] = svc["image"]
    env = svc.get("env")
    if env:
        em = CommentedMap()
        for k, v in env.items():
            em[k] = v
        m["env"] = em
    ports = svc.get("ports")
    if ports:
        ps = CommentedSeq()
        for p in ports:
            ps.append(p)
        m["ports"] = ps
    health = svc.get("health")
    if health:
        m["options"] = _build_health_options(health)
    return m


def _build_verify_line(svc):
    """Build the verification command line for a service's setup block.

    ``verify_cmd`` is emitted verbatim. ``verify_sql`` is turned into a psql
    invocation using the postgres-convention env vars (``POSTGRES_USER`` ->
    ``-U``, ``POSTGRES_PASSWORD`` -> ``PGPASSWORD``, ``POSTGRES_DB`` -> ``-d``),
    connecting over ``localhost``.
    """
    setup = svc["setup"]
    if "verify_cmd" in setup:
        return setup["verify_cmd"]
    sql = setup["verify_sql"]
    env = svc.get("env") or {}
    user = env.get("POSTGRES_USER")
    password = env.get("POSTGRES_PASSWORD")
    db = env.get("POSTGRES_DB")
    prefix = f"PGPASSWORD={password} " if password else ""
    parts = ["psql", "-h", "localhost"]
    if user:
        parts += ["-U", str(user)]
    if db:
        parts += ["-d", str(db)]
    return f'{prefix}{" ".join(parts)} -c "{sql}"'


def _build_setup_step(name, svc):
    """Build the setup step (docker exec + optional verify) for a service."""
    setup = svc["setup"]
    joined = " && ".join(setup["commands"])
    docker_line = (
        f'docker exec ${{{{ job.services.{name}.id }}}} bash -c "{joined}"'
    )
    lines = [docker_line]
    if "verify_sql" in setup or "verify_cmd" in setup:
        lines.append(_build_verify_line(svc))
    step = CommentedMap()
    step["name"] = f"Set up {name} service container"
    step["run"] = LiteralScalarString("\n".join(lines) + "\n")
    return step


def _pick_test_job(doc):
    """Return the job that runs tests: the ``test`` job, else the first job."""
    jobs = doc.get("jobs")
    if not jobs:
        return None
    if "test" in jobs:
        return jobs["test"]
    return next(iter(jobs.values()))


def _inject_services_into_doc(doc, services, test_env):
    """Inject *services*, *test_env*, and setup steps into a parsed CI doc.

    *services* is a map of name -> definition (already scoped to this
    workflow's target). *test_env* is a scalar map. Mutates *doc* in place.
    Idempotent: keys already present are overwritten, not duplicated.
    """
    job = _pick_test_job(doc)
    if job is None:
        return

    # services block. Insert before 'steps' so ordering reads
    # runs-on -> services -> env -> steps.
    keys = list(job.keys())
    insert_at = keys.index("steps") if "steps" in keys else len(keys)
    if services:
        svc_map = CommentedMap()
        for name in services:
            svc_map[name] = _build_service_map(services[name])
        if "services" in job:
            job["services"] = svc_map
        else:
            job.insert(insert_at, "services", svc_map)
            insert_at += 1

    # env block (test_env).
    if test_env:
        if "env" in job:
            for k, v in test_env.items():
                job["env"][k] = v
        else:
            env_map = CommentedMap()
            for k, v in test_env.items():
                env_map[k] = v
            job.insert(insert_at, "env", env_map)

    # setup steps: one per service with a setup block, inserted after the
    # checkout step (services are available for the whole job, but placing the
    # step after checkout matches conventional workflow ordering).
    setup_steps = [
        _build_setup_step(name, services[name])
        for name in services
        if services[name].get("setup")
    ]
    if setup_steps:
        steps = job.get("steps")
        if steps is None:
            steps = CommentedSeq()
            job["steps"] = steps
        insert_idx = 0
        if steps:
            first = steps[0]
            if isinstance(first, dict) and "actions/checkout" in str(
                first.get("uses", "")
            ):
                insert_idx = 1
        # Skip if an identically-named setup step already exists (idempotent
        # re-scaffold).
        existing_names = {
            s.get("name") for s in steps if isinstance(s, dict)
        }
        for offset, step in enumerate(
            s for s in setup_steps if s["name"] not in existing_names
        ):
            steps.insert(insert_idx + offset, step)


def inject_services_into_ci_plans(plans, config, *, single_target=None):
    """Post-process scaffold *plans*, injecting CI service containers.

    Reads ``services`` and ``test_env`` from *config*. For every plan whose
    target is a test CI workflow (``ci.yml`` / ``ci-<target>.yml``, never
    ``ci-custom.yml`` or publish workflows), injects the services scoped to
    that target plus ``test_env`` (scoped to the union of all service targets)
    into both the rendered ``content`` and the ``base_content`` merge base, so
    a re-scaffold re-generates identical output and lands without conflicts.

    No-op when neither ``services`` nor ``test_env`` is declared.
    """
    services = config.get("services") or {}
    test_env = config.get("test_env") or {}
    if not services and not test_env:
        return

    env_targets = set()
    for svc in services.values():
        for t in svc.get("targets") or []:
            env_targets.add(t)

    for plan in plans:
        target_path = plan.get("target") or ""
        basename = os.path.basename(target_path)
        ci_target = _ci_target_from_basename(basename, single_target)
        if ci_target is None:
            continue

        scoped_services = {
            name: svc
            for name, svc in services.items()
            if ci_target in (svc.get("targets") or [])
        }
        scoped_env = test_env if ci_target in env_targets else {}
        if not scoped_services and not scoped_env:
            continue

        for key in ("content", "base_content"):
            text = plan.get(key)
            if not text:
                continue
            doc = parse_ci_workflow(text)
            if doc is None:
                continue
            _inject_services_into_doc(doc, scoped_services, scoped_env)
            plan[key] = _emit_ci_workflow_faithful(doc)
