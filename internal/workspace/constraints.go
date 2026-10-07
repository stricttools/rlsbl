package workspace

import (
	"strconv"
	"strings"
)

// ConstraintVerdict is what a version constraint says about a version.
type ConstraintVerdict string

// The verdicts.
const (
	// ConstraintSatisfied: the version satisfies the constraint.
	ConstraintSatisfied ConstraintVerdict = "ok"
	// ConstraintOutdated: the constraint excludes the version.
	ConstraintOutdated ConstraintVerdict = "outdated"
	// ConstraintUnevaluated: the constraint is one this evaluation does not
	// read (several clauses, an exclusion, a version it cannot parse).
	ConstraintUnevaluated ConstraintVerdict = "versioned"
)

func versionTuple(s string) ([]int, bool) {
	var parts []int
	for _, p := range strings.Split(s, ".") {
		n, err := strconv.Atoi(p)
		if err != nil {
			return nil, false
		}
		parts = append(parts, n)
	}
	return parts, len(parts) > 0
}

// compareTuples compares version tuples the way sequences compare: element
// by element, a shorter equal prefix ordering first.
func compareTuples(a, b []int) int {
	for i := 0; i < len(a) && i < len(b); i++ {
		if a[i] != b[i] {
			if a[i] < b[i] {
				return -1
			}
			return 1
		}
	}
	switch {
	case len(a) < len(b):
		return -1
	case len(a) > len(b):
		return 1
	}
	return 0
}

// EvaluateConstraint evaluates a single-clause version constraint (>=, >,
// <=, <, ==, =, ~=, ^, ~, or a bare version) against the version current.
func EvaluateConstraint(constraint, current string) ConstraintVerdict {
	have, ok := versionTuple(current)
	if !ok {
		return ConstraintUnevaluated
	}
	c := strings.TrimSpace(constraint)
	if c == "" || strings.Contains(c, ",") || strings.Contains(c, "||") || strings.HasPrefix(c, "!=") {
		return ConstraintUnevaluated
	}
	op, rest := "==", c
	for _, candidate := range []string{">=", "<=", "==", "~=", ">", "<", "^", "~", "="} {
		if r, found := strings.CutPrefix(c, candidate); found {
			op, rest = candidate, r
			break
		}
	}
	if op == "=" {
		op = "=="
	}
	want, ok := versionTuple(strings.TrimSpace(rest))
	if !ok {
		return ConstraintUnevaluated
	}
	verdict := func(satisfied bool) ConstraintVerdict {
		if satisfied {
			return ConstraintSatisfied
		}
		return ConstraintOutdated
	}
	cmp := compareTuples(have, want)
	switch op {
	case ">=":
		return verdict(cmp >= 0)
	case ">":
		return verdict(cmp > 0)
	case "<=":
		return verdict(cmp <= 0)
	case "<":
		return verdict(cmp < 0)
	case "==":
		return verdict(cmp == 0)
	case "^":
		if cmp < 0 {
			return ConstraintOutdated
		}
		if want[0] > 0 {
			return verdict(have[0] == want[0])
		}
		if len(want) >= 2 && len(have) >= 2 {
			return verdict(have[0] == 0 && have[1] == want[1])
		}
		return ConstraintUnevaluated
	case "~", "~=":
		if cmp < 0 {
			return ConstraintOutdated
		}
		if len(want) >= 2 && len(have) >= 2 {
			return verdict(have[0] == want[0] && have[1] == want[1])
		}
		return ConstraintUnevaluated
	}
	return ConstraintUnevaluated
}
