"""Check command to query package name availability across npm, PyPI, Go module proxy (pkg.go.dev), and GitHub repository namespaces."""

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


def _request_with_backoff(url, timeout=5, max_retries=3):
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
    # GitHub API requires a User-Agent header
    if "api.github.com" in url:
        req.add_header("User-Agent", "rlsbl-cli")

    last_exc = None
    for attempt in range(max_retries):
        try:
            return urllib.request.urlopen(req, timeout=timeout)
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

    On any failure (network, JSON parse), returns an empty list for
    graceful degradation.
    """
    candidate_moniker = normalize_npm(name)
    url = f"https://registry.npmjs.org/-/v1/search?text={name}&size=20"
    try:
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
    except Exception as e:
        import sys
        print(f"Warning: npm similar-name search failed: {e}", file=sys.stderr)
        return []


def check_npm_availability(name):
    """Check if an npm package name is available.

    Returns {"status": "available"|"taken"|"error", "message"?: str}.
    Distinguishes 404 (truly available) from network/other errors.
    """
    try:
        subprocess.run(
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


def check_go_availability(name):
    """Check if a Go module path exists on pkg.go.dev.

    Returns {"status": "not_found"|"exists"|"error", "message"?: str, "note"?: str}.

    Go modules use repository paths (e.g. github.com/user/repo), not a flat
    claimable namespace, so we report "not found" / "exists" rather than the
    "available" / "taken" language used for npm and PyPI.
    """
    url = f"https://pkg.go.dev/{name}"
    try:
        with _request_with_backoff(url, timeout=5) as resp:
            if resp.status == 200:
                return {"status": "exists"}
            return {"status": "error", "message": f"Unexpected status {resp.status}"}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {
                "status": "not_found",
                "note": "Go modules use repository paths, not a central registry.",
            }
        return {"status": "error", "message": f"Unexpected status {e.code}"}
    except Exception as e:
        return {"status": "error", "message": str(e) or "Network error"}


def check_github_availability(name):
    """Check if a repository name exists on GitHub.

    Searches the GitHub API for repositories with the given name.
    Returns {"status": "available"|"exists"|"error", "count": int, ...}.
    """
    url = f"https://api.github.com/search/repositories?q={name}+in:name"
    try:
        with _request_with_backoff(url, timeout=5) as resp:
            data = json.loads(resp.read())
            count = data.get("total_count", 0)
            if count == 0:
                return {"status": "available", "count": 0}
            return {
                "status": "exists",
                "count": count,
                "note": f"{count} repos with this name on GitHub",
            }
    except Exception as e:
        return {"status": "error", "message": str(e) or "Network error"}


def _check_variants(name, check_fn, get_variants_fn):
    """Check name variants for similarity using the given availability checker.

    Returns a list of variant names that are taken/exist.
    """
    variants = [v for v in get_variants_fn(name) if v != name]
    similar = []

    if _HAS_THREADS and variants:
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


def _check_single_name(name, registry):
    """Check a single name on a given registry, returning a structured result.

    Returns a dict with keys:
        - name: the package name checked
        - registry: which registry was checked
        - status: "available", "taken", "exists", "not_found", or "error"
        - variants: list of similar names that are taken (npm/pypi only)
        - github_count: number of GitHub repos with this name (or None on error)
        - reason: why the name is taken/unavailable, or None if available/error.
          Values: "registered", "stdlib", "moniker", "normalized", "ultranorm"
          (set by _apply_ultranorm_check), or None.
        - error: error message if status is "error" (absent otherwise)
        - note: informational note (go only, absent otherwise)
    """
    result = {"name": name, "registry": registry, "status": "", "variants": None, "github_count": None, "reason": None}

    # Registry-specific availability check
    if registry == "npm":
        check_result = check_npm_availability(name)
        result["status"] = check_result["status"]
        if check_result["status"] == "error":
            result["error"] = check_result["message"]
        elif check_result["status"] == "taken":
            result["reason"] = "registered"
        elif check_result["status"] == "available":
            taken_variants = _check_variants(name, check_npm_availability, get_npm_variants)
            hard, soft = _classify_variant_collisions(name, taken_variants, "npm")
            if hard:
                result["status"] = "taken"
                result["reason"] = "moniker"
                result["note"] = f"moniker collision with '{hard[0]}' (npm strips punctuation)"
            result["variants"] = soft
            conflicts = _search_npm_similar(name)
            result["moniker_checked"] = True
            if conflicts and result["status"] != "taken":
                result["status"] = "taken"
                result["reason"] = "moniker"
                result["note"] = f"moniker conflict with '{conflicts[0]}' (npm strips punctuation)"

    elif registry == "pypi":
        stdlib_module = _check_stdlib_collision(name)
        if stdlib_module is not None:
            result["status"] = "taken"
            result["reason"] = "stdlib"
            result["note"] = f"conflicts with Python stdlib module '{stdlib_module}'"
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
                taken_variants = _check_variants(name, check_pypi_availability, get_pypi_variants)
                hard, soft = _classify_variant_collisions(name, taken_variants, "pypi")
                if hard:
                    result["status"] = "taken"
                    result["reason"] = "normalized"
                    result["note"] = f"normalization collision with '{hard[0]}' (registry rejects identical normalized names)"
                result["variants"] = soft  # only soft similar names shown as informational

    elif registry == "go":
        check_result = check_go_availability(name)
        result["status"] = check_result["status"]
        if check_result["status"] == "error":
            result["error"] = check_result["message"]
        elif check_result["status"] == "exists":
            result["reason"] = "registered"
        if check_result.get("note"):
            result["note"] = check_result["note"]

    # GitHub informational check (skip when name is already taken/exists)
    if result["status"] in ("available", "not_found"):
        gh_result = check_github_availability(name)
        if gh_result["status"] != "error":
            result["github_count"] = gh_result.get("count", 0)
        else:
            result["github_count"] = None

    return result


def _format_single_result(result):
    """Print the verbose output for a single name check result.

    Reproduces the original detailed output format with variant warnings and
    GitHub info.
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
            sys.exit(1)
        if status == "available":
            print(f'"{name}" is available on npm.')
        else:
            print(f'"{name}" is taken on npm.')
        if reason in _REASON_EXPLANATIONS:
            print(_REASON_EXPLANATIONS[reason])

    elif registry == "pypi":
        print(f'Checking PyPI for "{name}"...')
        if status == "error":
            print(f"Error checking PyPI: {result['error']}", file=sys.stderr)
            sys.exit(1)
        if status == "available":
            print(f'"{name}" is available on PyPI.')
        else:
            print(f'"{name}" is taken on PyPI.')
        if reason in _REASON_EXPLANATIONS:
            print(_REASON_EXPLANATIONS[reason])
        if result.get("note"):
            print(f"  Note: {result['note']}")

    elif registry == "go":
        print(f'Checking pkg.go.dev for "{name}"...')
        if status == "error":
            print(f"Error checking pkg.go.dev: {result['error']}", file=sys.stderr)
            sys.exit(1)
        if status == "not_found":
            print(f'"{name}" not found on pkg.go.dev.')
        else:
            print(f'"{name}" exists on pkg.go.dev.')
        if reason in _REASON_EXPLANATIONS:
            print(_REASON_EXPLANATIONS[reason])
        if result.get("note"):
            print(f"  Note: {result['note']}")

    # Variant warnings (npm/pypi only)
    available = status in ("available", "not_found")
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

    # GitHub informational
    github_count = result.get("github_count")
    if github_count is not None:
        if github_count == 0:
            print(f"\n  (i) No GitHub repos named \"{name}\"")
        else:
            print(f"\n  (i) {github_count} GitHub repo(s) named \"{name}\" (informational, not a registry)")

    # Steps-run summary
    _REGISTRY_DISPLAY = {"npm": "npm", "pypi": "PyPI", "go": "pkg.go.dev"}
    steps = [_REGISTRY_DISPLAY.get(registry, registry)]
    if registry == "pypi":
        # stdlib check always runs for PyPI (it's local)
        steps.append("stdlib")
    if result.get("variants") is not None:
        steps.append("variants")
    if result.get("moniker_checked"):
        steps.append("moniker similarity")
    if result.get("github_count") is not None:
        steps.append("GitHub repos")
    if result.get("ultranorm_checked"):
        steps.append("ultranormalization")
    print(f"\nChecked: {', '.join(steps)}")


def _format_table_row(result):
    """Return a compact one-line dict suitable for table rendering.

    Keys: name, status. The status is a short human-readable string.
    """
    name = result["name"]
    status = result["status"]
    registry = result["registry"]

    if status == "error":
        display_status = "error"
    elif registry == "go":
        display_status = "not found" if status == "not_found" else "exists"
    elif result.get("ultranorm_conflicts"):
        display_status = "CONFLICT"
    else:
        display_status = status  # "available" or "taken"

    return {"name": name, "status": display_status}


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


def run_cmd(registry, args, flags):
    """Check command handler.

    Checks package name availability on npm, PyPI, or Go, and warns about similar names.
    Accepts one or more package names as positional arguments.
    """
    names = args if args else []
    if not names:
        print(
            "Error: missing package name(s). Usage: rlsbl check <name> [<name2> ...] --target <npm|pypi|go>",
            file=sys.stderr,
        )
        sys.exit(1)

    delay_ms = int(flags.get("delay", "200"))

    if len(names) == 1:
        result = _check_single_name(names[0], registry)
        _apply_ultranorm_check(result, registry, delay_ms)
        _format_single_result(result)
    else:
        rows = []
        for i, name in enumerate(names):
            result = _check_single_name(name, registry)
            _apply_ultranorm_check(result, registry, delay_ms)
            rows.append(_format_table_row(result))
            if i < len(names) - 1:
                time.sleep(delay_ms / 1000)

        # Compute column widths for aligned output
        name_width = max(len(row["name"]) for row in rows)
        name_width = max(name_width, len("Name"))

        print(f"{'Name':<{name_width}}  Status")
        for row in rows:
            print(f"{row['name']:<{name_width}}  {row['status']}")

        # Summary line
        available_count = sum(1 for r in rows if r["status"] in ("available", "not found"))
        taken_count = sum(1 for r in rows if r["status"] in ("taken", "exists", "CONFLICT"))
        error_count = sum(1 for r in rows if r["status"] == "error")
        total = len(rows)
        if error_count:
            print(f"\nSummary: {available_count} available, {taken_count} taken, {error_count} error(s) ({total} total)")
        else:
            print(f"\nSummary: {available_count} available, {taken_count} taken ({total} total)")

        # Batch context note
        msg = f"Checked with {delay_ms}ms delay between names."
        if delay_ms == 200:
            msg += " Increase --delay if rate limited."
        print(msg)
