package dependencies

import (
	"regexp"
	"sort"
	"strconv"
	"strings"

	"github.com/stricttools/rlsbl/internal/workspace"
)

// NormalizePypiName is a name as PEP 503 normalizes it: lowercased, each run
// of hyphens, underscores, and dots made one hyphen.
func NormalizePypiName(name string) string {
	return workspace.NormalizePyPI(strings.TrimSpace(name))
}

// NormalizeNpmName is an npm name as the floors and locks compare it.
func NormalizeNpmName(name string) string {
	return strings.ToLower(strings.TrimSpace(name))
}

// requirementHead is the leading name and optional [extras] of a PEP 508
// requirement.
var requirementHead = regexp.MustCompile(`^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?`)

// Requirement is one PEP 508 requirement as a manifest spells it.
type Requirement struct {
	// Name is the name as declared.
	Name string
	// Extras keeps its brackets ("[cli]"), or is empty.
	Extras string
	// DirectReference is set for "name @ <url>": the lock records a source,
	// not a version specifier, so no floor can be stated or compared.
	DirectReference bool
	// Specifier is the version specifier set, trimmed; for a direct
	// reference, the URL.
	Specifier string
	// Marker is everything from the first ";" on, verbatim ("; python_version
	// < '3.12'"), or empty.
	Marker string
}

// Normalized is the requirement's name as PEP 503 normalizes it.
func (r Requirement) Normalized() string { return NormalizePypiName(r.Name) }

// WithFloor is the requirement rewritten to "name[extras]>=version" with its
// marker kept: dropping an extra changes what is installed, and dropping a
// marker changes where.
func (r Requirement) WithFloor(version string) string {
	return r.WithConstraint(">=" + version)
}

// WithConstraint is the requirement with its specifier (or direct
// reference) replaced by constraint, its extras and marker kept.
func (r Requirement) WithConstraint(constraint string) string {
	return r.Name + r.Extras + constraint + r.Marker
}

// ParseRequirement reads a PEP 508 requirement; ok is false for text that
// does not start with a package name.
func ParseRequirement(text string) (Requirement, bool) {
	body, marker, hasMarker := strings.Cut(text, ";")
	m := requirementHead.FindStringSubmatchIndex(body)
	if m == nil {
		return Requirement{}, false
	}
	r := Requirement{Name: body[m[2]:m[3]]}
	if m[4] >= 0 {
		r.Extras = body[m[4]:m[5]]
	}
	if hasMarker {
		r.Marker = ";" + marker
	}
	rest := strings.TrimSpace(body[m[1]:])
	if url, isReference := strings.CutPrefix(rest, "@"); isReference {
		r.DirectReference = true
		r.Specifier = strings.TrimSpace(url)
		return r, true
	}
	r.Specifier = rest
	return r, true
}

// versionPrefix is the leading major.minor[.patch] of a version string.
var versionPrefix = regexp.MustCompile(`^(\d+)\.(\d+)(?:\.(\d+))?`)

// MajorMinor is the leading (major, minor) of a version string, and false
// when it has none. Floors are compared at major.minor: a patch above the
// floor never crosses a behavior boundary.
func MajorMinor(text string) ([2]int, bool) {
	m := versionPrefix.FindStringSubmatch(strings.TrimLeft(strings.TrimSpace(text), "vV="))
	if m == nil {
		return [2]int{}, false
	}
	major, err1 := strconv.Atoi(m[1])
	minor, err2 := strconv.Atoi(m[2])
	if err1 != nil || err2 != nil {
		return [2]int{}, false
	}
	return [2]int{major, minor}, true
}

// laterThan reports whether a is a later major.minor than b.
func laterThan(a, b [2]int) bool {
	return a[0] > b[0] || (a[0] == b[0] && a[1] > b[1])
}

// FloorReading is what a constraint says about its lower bound.
type FloorReading int

// The readings of a constraint.
const (
	// FloorNone: the constraint states no lower bound.
	FloorNone FloorReading = iota
	// FloorFound: a lower bound was read.
	FloorFound
	// FloorNotApplicable: the constraint names no registry version (a
	// checkout, a remote, a workspace protocol), so no floor applies.
	FloorNotApplicable
)

// PypiFloor reads the lower bound of a PEP 440 specifier set: the highest
// version any ===, >=, ==, ~=, or > clause names.
func PypiFloor(spec string) (FloorReading, [2]int) {
	best, found := [2]int{}, false
	for _, clause := range strings.Split(spec, ",") {
		clause = strings.TrimSpace(clause)
		if clause == "" || strings.HasPrefix(clause, "!=") || strings.HasPrefix(clause, "<") {
			continue
		}
		for _, op := range []string{"===", ">=", "==", "~=", ">"} {
			rest, ok := strings.CutPrefix(clause, op)
			if !ok {
				continue
			}
			if v, ok := MajorMinor(rest); ok && (!found || laterThan(v, best)) {
				best, found = v, true
			}
			break
		}
	}
	if !found {
		return FloorNone, [2]int{}
	}
	return FloorFound, best
}

// npmNonRegistry are the range prefixes that point at a checkout or a
// remote rather than a registry version.
var npmNonRegistry = []string{"workspace:", "file:", "link:", "portal:", "npm:", "git+", "git:", "github:", "http://", "https://"}

// NpmFloor reads the lower bound of an npm semver range. A disjunction has
// no single floor and reads as none.
func NpmFloor(rng string) (FloorReading, [2]int) {
	text := strings.TrimSpace(rng)
	if text == "" {
		return FloorNone, [2]int{}
	}
	for _, prefix := range npmNonRegistry {
		if strings.HasPrefix(text, prefix) {
			return FloorNotApplicable, [2]int{}
		}
	}
	if strings.Contains(text, "||") {
		return FloorNone, [2]int{}
	}
	best, found := [2]int{}, false
	for _, comparator := range strings.Fields(text) {
		if strings.HasPrefix(comparator, "<") || strings.HasPrefix(comparator, "!") {
			continue
		}
		stripped := strings.TrimLeft(comparator, "^~>=v ")
		if strings.HasPrefix(stripped, "*") || strings.HasPrefix(strings.ToLower(stripped), "x") {
			continue
		}
		if v, ok := MajorMinor(stripped); ok && (!found || laterThan(v, best)) {
			best, found = v, true
		}
	}
	if !found {
		return FloorNone, [2]int{}
	}
	return FloorFound, best
}

// pep440 is one PEP 440 version split into the parts its canonical spelling
// reorders.
var pep440 = regexp.MustCompile(`(?i)^(?:(\d+)!)?(\d+(?:\.\d+)*)` +
	`(?:[-_.]?(alpha|beta|preview|pre|a|b|c|rc)[-_.]?(\d+)?)?` +
	`(?:(?:-(\d+))|(?:[-_.]?(post|rev|r)[-_.]?(\d+)?))?` +
	`([-_.]?dev[-_.]?(\d+)?)?` +
	`(?:\+([a-z0-9]+(?:[-_.][a-z0-9]+)*))?$`)

// preLetters are PEP 440's pre-release spellings and the canonical letter
// each normalizes to.
var preLetters = map[string]string{"alpha": "a", "a": "a", "beta": "b", "b": "b", "c": "rc", "pre": "rc", "preview": "rc", "rc": "rc"}

// number is the decimal text with leading zeros dropped, "0" for empty.
func number(text string) string {
	if text == "" {
		return "0"
	}
	trimmed := strings.TrimLeft(text, "0")
	if trimmed == "" {
		return "0"
	}
	return trimmed
}

var localSeparators = regexp.MustCompile(`[-_.]`)

// NormalizeVersion is a PEP 440 version in the canonical spelling uv writes
// into uv.lock (">=1.0.0-alpha1" becomes ">=1.0.0a1", "01.02.03" becomes
// "1.2.3"). Anything else (a wildcard, an unparseable string) is returned
// unchanged rather than guessed at.
func NormalizeVersion(text string) string {
	raw := strings.TrimSpace(text)
	m := pep440.FindStringSubmatch(raw)
	if m == nil {
		return raw
	}
	var b strings.Builder
	if m[1] != "" {
		b.WriteString(number(m[1]) + "!")
	}
	parts := strings.Split(m[2], ".")
	for i, p := range parts {
		parts[i] = number(p)
	}
	b.WriteString(strings.Join(parts, "."))
	if m[3] != "" {
		b.WriteString(preLetters[strings.ToLower(m[3])] + number(m[4]))
	}
	switch {
	case m[5] != "":
		b.WriteString(".post" + number(m[5]))
	case m[6] != "":
		b.WriteString(".post" + number(m[7]))
	}
	if m[8] != "" {
		b.WriteString(".dev" + number(m[9]))
	}
	if m[10] != "" {
		b.WriteString("+" + localSeparators.ReplaceAllString(strings.ToLower(m[10]), "."))
	}
	return b.String()
}

// specifierOperators are the operators a PEP 440 clause starts with, longest
// first so === is recognized before ==.
var specifierOperators = []string{"===", "~=", "==", "!=", "<=", ">=", "<", ">"}

// NormalizeSpecifier is a specifier set reduced to a comparable form:
// whitespace dropped, each clause's version canonicalized, the clauses
// sorted, so ">=1, <2" and "<2,>=1" compare equal, as uv reorders them in
// the lock. An === clause is kept literally: arbitrary equality is a string
// match.
func NormalizeSpecifier(text string) string {
	var clauses []string
	for _, c := range strings.Split(strings.ReplaceAll(text, " ", ""), ",") {
		c = strings.TrimSpace(c)
		if c == "" {
			continue
		}
		clauses = append(clauses, normalizeClause(c))
	}
	sort.Strings(clauses)
	return strings.Join(clauses, ",")
}

func normalizeClause(clause string) string {
	for _, op := range specifierOperators {
		rest, ok := strings.CutPrefix(clause, op)
		if !ok {
			continue
		}
		if op == "===" {
			return clause
		}
		return op + NormalizeVersion(rest)
	}
	return clause
}
