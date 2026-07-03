"""Version constraint evaluation utilities.

Extracted from commands/monorepo/commands.py to break the checks/ -> commands/
circular dependency. Used by checks/workspace.py for the deps-stale check.
"""


def _parse_version_tuple(version_str):
    """Parse a version string like '1.2.3' into a tuple of ints.

    Returns None if parsing fails.
    """
    parts = []
    for p in version_str.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            return None
    return tuple(parts) if parts else None


def _evaluate_constraint(constraint, current_version):
    """Evaluate a version constraint against a current version.

    Returns "ok" if the constraint is satisfied, "outdated" if not,
    or "versioned" if the constraint is too complex to parse.
    """
    current_tuple = _parse_version_tuple(current_version)
    if current_tuple is None:
        return "versioned"

    # Strip leading operator and extract version from simple constraints
    # Handles: >=1.2.0, ^1.2.0, ~1.2.0, <=1.2.0, >1.2.0, <1.2.0, ==1.2.0, =1.2.0, 1.2.0
    stripped = constraint.strip()
    if not stripped:
        return "versioned"

    # Reject complex constraints (multiple conditions with commas, ||, spaces)
    if "," in stripped or "||" in stripped:
        return "versioned"

    # Extract operator and version
    if stripped.startswith(">="):
        op, ver_str = ">=", stripped[2:].strip()
    elif stripped.startswith("<="):
        op, ver_str = "<=", stripped[2:].strip()
    elif stripped.startswith("=="):
        op, ver_str = "==", stripped[2:].strip()
    elif stripped.startswith("!="):
        return "versioned"
    elif stripped.startswith(">"):
        op, ver_str = ">", stripped[1:].strip()
    elif stripped.startswith("<"):
        op, ver_str = "<", stripped[1:].strip()
    elif stripped.startswith("~="):
        op, ver_str = "~=", stripped[2:].strip()
    elif stripped.startswith("^"):
        op, ver_str = "^", stripped[1:].strip()
    elif stripped.startswith("~"):
        op, ver_str = "~", stripped[1:].strip()
    elif stripped.startswith("="):
        op, ver_str = "==", stripped[1:].strip()
    else:
        # Bare version string like "1.2.0"
        op, ver_str = "==", stripped

    constraint_tuple = _parse_version_tuple(ver_str)
    if constraint_tuple is None:
        return "versioned"

    if op == ">=":
        return "ok" if current_tuple >= constraint_tuple else "outdated"
    elif op == ">":
        return "ok" if current_tuple > constraint_tuple else "outdated"
    elif op == "<=":
        return "ok" if current_tuple <= constraint_tuple else "outdated"
    elif op == "<":
        return "ok" if current_tuple < constraint_tuple else "outdated"
    elif op == "==":
        return "ok" if current_tuple == constraint_tuple else "outdated"
    elif op == "^":
        # Caret: >=ver and same major (for major>0), or same major.minor (for 0.x)
        if current_tuple < constraint_tuple:
            return "outdated"
        if constraint_tuple[0] > 0:
            return "ok" if current_tuple[0] == constraint_tuple[0] else "outdated"
        # 0.x.y: pin to minor
        if len(constraint_tuple) >= 2 and len(current_tuple) >= 2:
            return "ok" if current_tuple[0] == 0 and current_tuple[1] == constraint_tuple[1] else "outdated"
        return "versioned"
    elif op == "~" or op == "~=":
        # Tilde: >=ver and same major.minor
        if current_tuple < constraint_tuple:
            return "outdated"
        if len(constraint_tuple) >= 2 and len(current_tuple) >= 2:
            return "ok" if (current_tuple[0] == constraint_tuple[0] and
                           current_tuple[1] == constraint_tuple[1]) else "outdated"
        return "versioned"

    return "versioned"
