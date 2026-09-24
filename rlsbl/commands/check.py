"""Check command to judge package names: npm and PyPI availability over the network, and an offline Go package-name check."""

import json
import re
import subprocess
import sys
import time
import urllib.request
import urllib.error

try:
    from concurrent.futures import ThreadPoolExecutor, as_completed
    _HAS_THREADS = True
except ImportError:
    _HAS_THREADS = False


from itertools import product  # noqa: E402

from rlsbl.targets.utils import normalize_npm, normalize_pypi  # noqa: E402
from .. import effects
from ..go_package_name import check_go_package_name


def _request_with_backoff(url, timeout=5, max_retries=3, headers=None):
    """Wrap urllib.request.urlopen with retry logic for HTTP 429 responses.

    On HTTP 429 (Too Many Requests): reads the Retry-After header (seconds).
    If present, sleeps that long. If absent, uses exponential backoff starting
    at 2 seconds (2, 4, 8, ...).

    On other HTTP errors or non-HTTP errors (URLError, timeout): raises
    immediately without retrying.

    Returns the response object on success, or raises the last exception after
    exhausting retries.
    """
    req = urllib.request.Request(url, method="GET")
    if headers:
        for key, value in headers.items():
            req.add_header(key, value)

    last_exc = None
    for attempt in range(max_retries):
        try:
            return effects.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                last_exc = e
                retry_after = e.headers.get("Retry-After") if e.headers else None
                if retry_after is not None:
                    delay = float(retry_after)
                else:
                    delay = 2 ** (attempt + 1)
                print(f"Rate limited, retrying in {delay}s...", file=sys.stderr)
                time.sleep(delay)
            else:
                raise
    raise last_exc


def _ultranormalize(name):
    """Ultranormalize a package name for typosquatting detection.

    Strips all separators (-, _, .), replaces visually ambiguous characters
    (l, L, i, I -> 1; o, O -> 0), and lowercases the result.
    """
    stripped = re.sub(r"[-_.]", "", name)
    result = []
    for ch in stripped:
        if ch in ("l", "L", "i", "I"):
            result.append("1")
        elif ch in ("o", "O"):
            result.append("0")
        else:
            result.append(ch.lower())
    return "".join(result)


_ULTRANORM_VARIANT_CAP = 64


def _generate_ultranorm_variants(name):
    """Generate name variants that share the same ultranormalized form.

    Starting from the PEP 503 normalized form (lowercase, separators normalized),
    produces all combinations of ambiguous character substitutions:
      l <-> 1, o <-> 0, i <-> 1
    Returns ``(variants, capped)`` where ``variants`` is a list of up to 64
    variants (excluding the original name) and ``capped`` is True when the
    total combination count exceeded the cap.
    """
    normalized = normalize_pypi(name)
    # Build a list of character options per position
    char_options = []
    for ch in normalized:
        if ch in ("l", "1"):
            char_options.append(("l", "1"))
        elif ch in ("o", "0"):
            char_options.append(("o", "0"))
        elif ch == "i":
            char_options.append(("i", "1"))
        else:
            char_options.append((ch,))

    # Count total combinations without materializing
    total = 1
    for opts in char_options:
        total *= len(opts)

    capped = total > _ULTRANORM_VARIANT_CAP

    variants = []
    for combo in product(*char_options):
        if len(variants) >= _ULTRANORM_VARIANT_CAP:
            break
        variant = "".join(combo)
        if variant != normalized:
            variants.append(variant)

    return variants, capped



def _search_npm_similar(name):
    """Search the npm registry for packages with conflicting monikers.

    Queries the npm search API for packages similar to ``name``, then
    compares each result's normalized moniker against the candidate's.
    Returns a list of original package names that conflict.

    Raises on failure (network, timeout). The caller is responsible for
    handling the exception appropriately.
    """
    candidate_moniker = normalize_npm(name)
    url = f"https://registry.npmjs.org/-/v1/search?text={name}&size=20"
    with _request_with_backoff(url) as resp:
        data = json.loads(resp.read())
    conflicts = []
    for obj in data.get("objects", []):
        pkg_name = obj.get("package", {}).get("name")
        if pkg_name is None:
            continue
        if normalize_npm(pkg_name) == candidate_moniker and pkg_name != name:
            conflicts.append(pkg_name)
    return conflicts


def check_npm_availability(name):
    """Check if an npm package name is available.

    Returns {"status": "available"|"taken"|"error", "message"?: str}.
    Distinguishes 404 (truly available) from network/other errors.
    """
    try:
        effects.run(
            ["npm", "view", name, "name"],
            capture_output=True, text=True, check=True, timeout=30,
        )
        return {"status": "taken"}
    except subprocess.TimeoutExpired:
        return {"status": "error", "message": "npm view timed out"}
    except subprocess.CalledProcessError as e:
        stderr = e.stderr or ""
        if "E404" in stderr or "404" in stderr:
            return {"status": "available"}
        return {"status": "error", "message": stderr.strip() or "Unknown error checking npm"}
    except FileNotFoundError:
        return {"status": "error", "message": "npm CLI not found"}


def get_npm_variants(name):
    """Generate common npm name variants for similarity checking.

    npm's moniker collision algorithm strips all ``-``, ``.``, and ``_``
    characters and lowercases before comparing.  We generate:
    1. All separator-swap variants (replace every separator with each of -._)
    2. The fully stripped form
    3. Insertion variants when the name has no separators (insert each of -._
       at every interior position so we can detect existing hyphenated packages
       that would collide)
    """
    variants = set()
    lower = name.lower()
    separators = "-._"

    stripped = re.sub(r"[-._]", "", lower)
    variants.add(stripped)
    for sep in separators:
        variants.add(re.sub(r"[-._]", sep, lower))

    if stripped == lower:
        for i in range(1, len(lower)):
            for sep in separators:
                variants.add(lower[:i] + sep + lower[i:])

    variants.discard(name)

    return list(variants)


def check_pypi_availability(name):
    """Check if a PyPI package name is available.

    Uses the Simple API (PEP 503) which correctly returns 200 for registered
    packages even if they have no releases (unlike the JSON API which 404s).

    Returns {"status": "available"|"taken"|"error", "message"?: str}.
    Distinguishes 404 (truly available) from network/other errors.
    """
    normalized = normalize_pypi(name)
    url = f"https://pypi.org/simple/{normalized}/"
    try:
        with _request_with_backoff(url, timeout=5) as resp:
            if resp.status == 200:
                return {"status": "taken"}
            return {"status": "error", "message": f"Unexpected status {resp.status}"}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"status": "available"}
        return {"status": "error", "message": f"Unexpected status {e.code}"}
    except Exception as e:
        return {"status": "error", "message": str(e) or "Network error"}


_PYPI_INSERTION_CAP = 30


def get_pypi_variants(name):
    """Generate common PyPI name variants for similarity checking."""
    normalized = normalize_pypi(name)
    lower = name.lower()
    variants = set()
    variants.add(normalized)
    variants.add(re.sub(r"[-_.]+", "_", lower))
    variants.add(re.sub(r"[-_.]+", "-", lower))

    stripped = re.sub(r"[-_.]+", "", lower)
    variants.add(stripped)

    # Separator-free names: insert separators at every interior position
    # to detect existing packages that normalize identically (e.g. "llmloop"
    # vs "llm-loop" on PyPI).  Mirrors get_npm_variants insertion logic.
    if stripped == lower:
        separators = "-_."
        insertion_count = 0
        for i in range(1, len(lower)):
            for sep in separators:
                variants.add(lower[:i] + sep + lower[i:])
                insertion_count += 1
            if insertion_count >= _PYPI_INSERTION_CAP:
                print(
                    f"PyPI insertion variants capped at {_PYPI_INSERTION_CAP} "
                    f"for '{name}' (name too long for exhaustive check)",
                    file=sys.stderr,
                )
                break

    # Remove the original name itself
    variants.discard(name)

    return list(variants)


def _check_variants(name, check_fn, get_variants_fn, delay_ms=0):
    """Check name variants for similarity using the given availability checker.

    When ``delay_ms > 0``, bypasses the thread pool and checks variants
    sequentially with ``time.sleep(delay_ms / 1000)`` between checks (no
    delay before the first check).  This avoids triggering registry rate
    limits when many variants are generated.

    Returns a list of variant names that are taken/exist.
    """
    variants = [v for v in get_variants_fn(name) if v != name]
    similar = []

    if delay_ms > 0:
        # Rate-limited sequential path
        for i, variant in enumerate(variants):
            if i > 0:
                time.sleep(delay_ms / 1000)
            var_result = check_fn(variant)
            if var_result["status"] == "taken":
                similar.append(variant)
    elif _HAS_THREADS and variants:
        try:
            with ThreadPoolExecutor(max_workers=min(len(variants), 10)) as executor:
                future_to_variant = {
                    executor.submit(check_fn, v): v
                    for v in variants
                }
                for future in as_completed(future_to_variant):
                    variant = future_to_variant[future]
                    try:
                        var_result = future.result()
                        if var_result["status"] == "taken":
                            similar.append(variant)
                    except Exception:
                        pass  # Skip variants that error
        except Exception:
            # Thread pool itself errored (not individual futures).  Partial
            # results from the pool are untrustworthy, so start fresh with
            # a sequential fallback rather than mixing partial threaded
            # results with sequential ones.
            similar = []
            for variant in variants:
                var_result = check_fn(variant)
                if var_result["status"] == "taken":
                    similar.append(variant)
    else:
        for variant in variants:
            var_result = check_fn(variant)
            if var_result["status"] == "taken":
                similar.append(variant)

    return similar


# Human-readable normalization rules, stated explicitly in conflict notes so
# the reader understands *why* the listed names collide with the candidate.
_NPM_MONIKER_RULE = "npm strips dashes, dots, and underscores: these share one moniker"
_PYPI_NORMALIZED_RULE = "PyPI normalizes dashes, underscores, and dots to hyphens: these resolve identically"

# Stable machine tokens for each collision mechanism.  Unlike the human
# sentences above (which may be reworded), these tokens are a contract: JSON
# consumers key off them.  Every structured conflict object carries exactly one.
_RULE_TOKEN_NPM_MONIKER = "npm-moniker"
_RULE_TOKEN_PYPI_SEPARATOR = "pypi-separator"
_RULE_TOKEN_PYPI_ULTRANORM = "pypi-ultranorm"
_RULE_TOKEN_STDLIB = "stdlib"
_RULE_TOKEN_GO_STDLIB = "go-stdlib"

# Token -> human sentence.  Used to surface the rule sentences alongside the
# machine tokens in JSON output.  ultranorm/stdlib get generic sentences here
# (their per-name notes are built at the attach sites).
_RULE_TOKEN_SENTENCES = {
    _RULE_TOKEN_NPM_MONIKER: _NPM_MONIKER_RULE,
    _RULE_TOKEN_PYPI_SEPARATOR: _PYPI_NORMALIZED_RULE,
    _RULE_TOKEN_PYPI_ULTRANORM: "PyPI blocks names that are visually similar (l/1/i and o/0 substitutions)",
    _RULE_TOKEN_STDLIB: "PyPI blocks names that match Python standard library modules",
    _RULE_TOKEN_GO_STDLIB: "a Go package named like a standard library package forces an import alias on every file that imports both",
}


def _add_structured_conflicts(result, names, rule):
    """Append ``{"name": ..., "rule": ...}`` objects to the unified conflict field.

    This is the canonical machine-readable surface: every collision mechanism
    (npm moniker, pypi separator, pypi ultranorm, stdlib, go-stdlib)
    folds its conflicts into ``result["structured_conflicts"]`` via this helper.
    The list is re-sorted by (name, rule) after each addition, so the final
    ordering is deterministic regardless of attach-site order — and a name that
    collides through two mechanisms at once contributes two distinct objects.
    """
    existing = result.get("structured_conflicts") or []
    for name in names:
        existing.append({"name": name, "rule": rule})
    existing.sort(key=lambda c: (c["name"], c["rule"]))
    result["structured_conflicts"] = existing


def _enumerate_conflicts(conflicts):
    """Render a conflict list as a comma-separated, single-quoted enumeration.

    Example: ``["foo-bar", "foo.bar"]`` -> ``"'foo-bar', 'foo.bar'"``.
    """
    return ", ".join(f"'{c}'" for c in conflicts)


def _classify_variant_collisions(name, taken_variants, registry):
    """Classify taken variants as hard normalization collisions or soft similar names.

    Hard collisions are names the registry would reject because they normalize
    identically to the candidate.  Soft similar names merely look alike but
    are distinct after normalization.

    Returns (hard_collisions, soft_similar).
    """
    hard = []
    soft = []
    for variant in taken_variants:
        if registry == "pypi":
            if _ultranormalize(name) == _ultranormalize(variant):
                hard.append(variant)
            else:
                soft.append(variant)
        elif registry == "npm":
            if normalize_npm(name) == normalize_npm(variant):
                hard.append(variant)
            else:
                soft.append(variant)
        else:
            soft.append(variant)
    return hard, soft


def _check_stdlib_collision(name):
    """Check if a name collides with a Python standard library module.

    PEP 503 normalizes both the candidate and each stdlib name, then compares.
    Returns the stdlib module name on collision, or None.
    """
    normalized = normalize_pypi(name)
    for module in sys.stdlib_module_names:
        if normalize_pypi(module) == normalized:
            return module
    return None


def _check_single_name(name, registry, delay_ms=0):
    """Check a single name on a given registry, returning a structured result.

    When ``delay_ms > 0``, variant checks use sequential execution with a
    delay between requests instead of concurrent threads.

    Returns a dict with keys:
        - name: the package name checked
        - registry: which registry was checked
        - status: "available", "taken", "error", and for go only "invalid"
          or "discouraged" (see rlsbl.go_package_name)
        - variants: list of similar names that are taken (npm/pypi only)
        - reason: why the name is not available, or None if available/error.
          Values: "registered", "stdlib", "moniker", "normalized", "ultranorm"
          (set by _apply_ultranorm_check), the go tokens "not-identifier",
          "keyword", "blank", "uppercase", "underscore", "predeclared", or None.
        - error: error message if status is "error" (absent otherwise)
        - note: the sentence explaining a collision or a go verdict
        - offline: True when the check contacted no network (go), so batch
          callers skip the rate-limit delay between names
        - conflicts: full list of colliding package names (npm moniker collisions
          only; the machine-readable form of the enumerated ``note``)
        - conflict_rule: the normalization rule that makes the conflicts collide
          (accompanies ``conflicts``)
    """
    result = {"name": name, "registry": registry, "status": "", "variants": None, "reason": None}

    # Registry-specific availability check
    if registry == "npm":
        check_result = check_npm_availability(name)
        result["status"] = check_result["status"]
        if check_result["status"] == "error":
            result["error"] = check_result["message"]
        elif check_result["status"] == "taken":
            result["reason"] = "registered"
        elif check_result["status"] == "available":
            taken_variants = _check_variants(name, check_npm_availability, get_npm_variants, delay_ms=delay_ms)
            hard, soft = _classify_variant_collisions(name, taken_variants, "npm")
            if hard:
                result["status"] = "taken"
                result["reason"] = "moniker"
                result["conflicts"] = hard
                result["conflict_rule"] = _NPM_MONIKER_RULE
                result["note"] = (
                    f"moniker collision with {_enumerate_conflicts(hard)} "
                    f"— {_NPM_MONIKER_RULE}"
                )
                _add_structured_conflicts(result, hard, _RULE_TOKEN_NPM_MONIKER)
            result["variants"] = soft
            try:
                conflicts = _search_npm_similar(name)
            except Exception as e:
                if result["status"] == "taken":
                    print(f"Note: npm search also failed ({e}), but collision already detected.", file=sys.stderr)
                    conflicts = []
                else:
                    result["status"] = "error"
                    result["error"] = f"npm moniker check failed: {e}"
                    conflicts = []
            result["moniker_checked"] = True
            if conflicts and result["status"] != "taken":
                result["status"] = "taken"
                result["reason"] = "moniker"
                result["conflicts"] = conflicts
                result["conflict_rule"] = _NPM_MONIKER_RULE
                result["note"] = (
                    f"moniker conflict with {_enumerate_conflicts(conflicts)} "
                    f"— {_NPM_MONIKER_RULE}"
                )
                _add_structured_conflicts(result, conflicts, _RULE_TOKEN_NPM_MONIKER)

    elif registry == "pypi":
        stdlib_module = _check_stdlib_collision(name)
        if stdlib_module is not None:
            result["status"] = "taken"
            result["reason"] = "stdlib"
            result["note"] = f"conflicts with Python stdlib module '{stdlib_module}'"
            _add_structured_conflicts(result, [stdlib_module], _RULE_TOKEN_STDLIB)
        else:
            check_result = check_pypi_availability(name)
            result["status"] = check_result["status"]
            if check_result["status"] == "error":
                result["error"] = check_result["message"]
            elif check_result["status"] == "taken":
                result["reason"] = "registered"
            elif check_result["status"] == "available":
                # Two collision mechanisms for PyPI:
                # Path A: separator-based (variants + classification here)
                # Path B: visual-ambiguity (_apply_ultranorm_check, called later by run_cmd)
                taken_variants = _check_variants(name, check_pypi_availability, get_pypi_variants, delay_ms=delay_ms)
                hard, soft = _classify_variant_collisions(name, taken_variants, "pypi")
                if hard:
                    result["status"] = "taken"
                    result["reason"] = "normalized"
                    result["conflicts"] = hard
                    result["conflict_rule"] = _PYPI_NORMALIZED_RULE
                    result["note"] = (
                        f"normalization collision with {_enumerate_conflicts(hard)} "
                        f"— {_PYPI_NORMALIZED_RULE}"
                    )
                    _add_structured_conflicts(result, hard, _RULE_TOKEN_PYPI_SEPARATOR)
                result["variants"] = soft  # only soft similar names shown as informational

    elif registry == "go":
        # Offline: the Go package name against the language and the committed
        # standard-library table. No registry is asked.
        verdict = check_go_package_name(name)
        result["status"] = verdict["status"]
        result["reason"] = verdict["reason"]
        result["offline"] = True
        if verdict["note"]:
            result["note"] = verdict["note"]
        if verdict["conflicts"]:
            _add_structured_conflicts(result, verdict["conflicts"], _RULE_TOKEN_GO_STDLIB)

    return result


def _registry_display(registry):
    """Human-readable name for a registry, asked of the target that owns it."""
    from ..targets import TARGETS

    target = TARGETS.get(registry)
    if target is not None:
        return target.registry_display_name
    return registry


# The go verdicts' headline, keyed by status, and the explanation printed under
# it, keyed by reason.
_GO_STATUS_HEADLINES = {
    "available": "is available as a Go package name.",
    "taken": "is taken as a Go package name.",
    "invalid": "is not a valid Go package name.",
    "discouraged": "is a legal but discouraged Go package name.",
}


def _format_single_result(result):
    """Print the verbose output for a single name check result.

    Returns an exit code: 0 = available, 1 = taken/invalid/discouraged, 2 = error.

    When status is "error", prints the error message and returns 2 immediately,
    skipping variant output.
    """
    name = result["name"]
    registry = result["registry"]
    status = result["status"]
    reason = result.get("reason")

    # Reason-specific explanations (printed after the status line, before notes)
    _REASON_EXPLANATIONS = {
        "stdlib": "  PyPI blocks names that match Python standard library modules.",
        "moniker": "  npm considers names identical after removing dashes, dots, and underscores.",
        "normalized": "  The registry rejects names that normalize identically after stripping separators.",
        "ultranorm": "  PyPI blocks names that are visually similar (l/1/i and o/0 substitutions).",
    }

    # Registry-specific status output
    if registry == "npm":
        print(f'Checking npm for "{name}"...')
        if status == "error":
            print(f"Error checking npm: {result['error']}", file=sys.stderr)
            return 2
        if status == "available":
            print(f'"{name}" is available on npm.')
        else:
            print(f'"{name}" is taken on npm.')
        if reason in _REASON_EXPLANATIONS:
            print(_REASON_EXPLANATIONS[reason])
        if result.get("note"):
            print(f"  Note: {result['note']}")

    elif registry == "pypi":
        print(f'Checking PyPI for "{name}"...')
        if status == "error":
            print(f"Error checking PyPI: {result['error']}", file=sys.stderr)
            return 2
        if status == "available":
            print(f'"{name}" is available on PyPI.')
        else:
            print(f'"{name}" is taken on PyPI.')
        if reason in _REASON_EXPLANATIONS:
            print(_REASON_EXPLANATIONS[reason])
        if result.get("note"):
            print(f"  Note: {result['note']}")

    elif registry == "go":
        print(f'Checking Go package name "{name}" (offline)...')
        print(f'"{name}" {_GO_STATUS_HEADLINES[status]}')
        if result.get("note"):
            print(f"  Note: {result['note']}")
        print("\nChecked: Go package-name rules, Go standard library (offline)")
        return _result_exit_code(result)

    # Variant warnings (npm/pypi only)
    available = status == "available"
    variants = result.get("variants", [])
    if variants:
        print("\nSimilar names already taken:")
        for s in variants:
            print(f"  {s}")
        if available:
            print(
                "\nYour name is available but has similar existing packages."
            )

    # Ultranormalization warnings and PyPI caveats
    ultranorm_conflicts = result.get("ultranorm_conflicts")
    if ultranorm_conflicts:
        print(
            f"\nWarning: '{name}' ultranormalizes to the same value as: "
            f"{', '.join(ultranorm_conflicts)}"
        )
    if registry == "pypi" and status == "available":
        print(
            "\nNote: PyPI may also reject names on its prohibited names list "
            "(not publicly available)."
        )

    # Steps-run summary. Registry display names come from the targets, not
    # from a dict keyed by target name.
    steps = [_registry_display(registry)]
    if registry == "pypi":
        # stdlib check always runs for PyPI (it's local)
        steps.append("stdlib")
    if result.get("variants") is not None:
        steps.append("variants")
    if result.get("moniker_checked"):
        steps.append("moniker similarity")
    if result.get("ultranorm_checked"):
        steps.append("ultranormalization")
    print(f"\nChecked: {', '.join(steps)}")

    return _result_exit_code(result)


def _format_table_row(result):
    """Return a compact one-line dict suitable for table rendering.

    Keys: name, status. The status is a short human-readable string.
    """
    name = result["name"]
    status = result["status"]

    if status != "error" and result.get("ultranorm_conflicts"):
        display_status = "CONFLICT"
    else:
        # available, taken, error, and for go invalid or discouraged
        display_status = status

    return {"name": name, "status": display_status}


def summary_line(rows):
    """The batch summary under a table of ``_format_table_row`` rows.

    Counts available, taken (CONFLICT included) and, only when present, the go
    target's invalid and discouraged verdicts, then errors.
    """
    available = sum(1 for r in rows if r["status"] == "available")
    taken = sum(1 for r in rows if r["status"] in ("taken", "CONFLICT"))
    parts = [f"{available} available", f"{taken} taken"]
    for label in ("invalid", "discouraged"):
        count = sum(1 for r in rows if r["status"] == label)
        if count:
            parts.append(f"{count} {label}")
    errors = sum(1 for r in rows if r["status"] == "error")
    if errors:
        parts.append(f"{errors} error(s)")
    return f"Summary: {', '.join(parts)} ({len(rows)} total)"


def _apply_ultranorm_check(result, registry, delay_ms):
    """Apply ultranormalization variant checking to a result dict (in-place).

    Always runs for PyPI when the name was initially available. Checks each
    generated variant against PyPI Simple API, applying a delay between
    requests. Sets ``ultranorm_conflicts`` (list of taken variant names)
    on the result.
    """
    if registry != "pypi":
        return

    if result["status"] != "available":
        return

    result["ultranorm_checked"] = True

    variants, capped = _generate_ultranorm_variants(result["name"])

    if capped:
        result["status"] = "error"
        result["error"] = (
            f"Too many ambiguous characters in '{result['name']}': "
            f"variant checking capped at {_ULTRANORM_VARIANT_CAP}. "
            f"Ultranorm check is incomplete."
        )
        return

    conflicts = []
    for i, variant in enumerate(variants):
        if i > 0:
            time.sleep(delay_ms / 1000)
        var_result = check_pypi_availability(variant)
        if var_result["status"] == "taken":
            conflicts.append(variant)
            break

    if conflicts:
        result["status"] = "taken"
        result["reason"] = "ultranorm"
        result["ultranorm_conflicts"] = conflicts
        _add_structured_conflicts(result, conflicts, _RULE_TOKEN_PYPI_ULTRANORM)


def _result_exit_code(result):
    """Compute the exit code for a single check result without printing.

    Mirrors the codes the human formatters return: 2 = error, 0 = available,
    1 = anything else (taken, and for go invalid or discouraged).
    """
    if result["status"] == "error":
        return 2
    if result["status"] == "available":
        return 0
    return 1


def _result_to_json(result, exit_code):
    """Project a check result dict onto the stable JSON surface for one name+target.

    Carries the identity (name, target), status, reason, the unified
    ``structured_conflicts`` field (from the collision mechanisms), the
    human rule sentences for the tokens present, and the exit-relevant code.
    Optional keys (note, error) appear only when set.
    """
    structured = result.get("structured_conflicts", [])
    tokens = sorted({c["rule"] for c in structured})
    obj = {
        "name": result["name"],
        "target": result["registry"],
        "status": result["status"],
        "reason": result.get("reason"),
        "structured_conflicts": structured,
        "rule_sentences": {t: _RULE_TOKEN_SENTENCES[t] for t in tokens},
        "exit_code": exit_code,
    }
    if result.get("note"):
        obj["note"] = result["note"]
    if result.get("error"):
        obj["error"] = result["error"]
    return obj


def run_cmd(registry, args, flags):
    """Check package name availability for one registry.

    Checks one or more names on ``registry``, warning about similar names.
    Returns ``(exit_code, payload)`` where ``payload`` is a list of per-name
    JSON objects (one per checked name).  The caller owns process exit and
    aggregation across registries — this function never calls ``sys.exit``.

    Human output is printed here (byte-identical to prior behavior) unless
    ``flags["json"]`` is set, in which case nothing is printed and the caller
    renders the accumulated payloads.
    """
    json_mode = flags.get("json", False)
    names = args if args else []
    if not names:
        print(
            # `rlsbl check` is the project-check command and takes no package
            # names at all, so the usage line that named it sent every reader
            # of this refusal to a command that would refuse them again.
            "Error: missing package name(s). Usage: rlsbl check-name <name> [<name2> ...] --target <npm|pypi|go>",
            file=sys.stderr,
        )
        return 1, []

    delay_ms = int(flags.get("delay", "200"))

    if len(names) == 1:
        result = _check_single_name(names[0], registry, delay_ms=delay_ms)
        _apply_ultranorm_check(result, registry, delay_ms)
        exit_code = _result_exit_code(result)
        payload = [_result_to_json(result, exit_code)]
        if not json_mode:
            _format_single_result(result)
        return exit_code, payload

    rows = []
    payload = []
    max_exit = 0
    offline = False
    for i, name in enumerate(names):
        result = _check_single_name(name, registry, delay_ms=delay_ms)
        _apply_ultranorm_check(result, registry, delay_ms)
        ec = _result_exit_code(result)
        payload.append(_result_to_json(result, ec))
        rows.append(_format_table_row(result))
        max_exit = max(max_exit, ec)
        offline = bool(result.get("offline"))
        if i < len(names) - 1 and not offline:
            time.sleep(delay_ms / 1000)

    if not json_mode:
        # Compute column widths for aligned output
        name_width = max(len(row["name"]) for row in rows)
        name_width = max(name_width, len("Name"))

        print(f"{'Name':<{name_width}}  Status")
        for row in rows:
            print(f"{row['name']:<{name_width}}  {row['status']}")

        print(f"\n{summary_line(rows)}")

        # Batch context note: the delay only applies to networked checks.
        if not offline:
            msg = f"Checked with {delay_ms}ms delay between names."
            if delay_ms == 200:
                msg += " Increase --delay if rate limited."
            print(msg)

    return max_exit, payload
