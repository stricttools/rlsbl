// Package semver parses, orders, and bumps release versions.
//
// A release version is three non-negative integers, MAJOR.MINOR.PATCH,
// written in decimal without leading zeros. There is no pre-release channel
// and no build metadata: a version carrying either is refused, never
// truncated to its base, because rlsbl releases neither.
package semver

import (
	"errors"
	"fmt"
	"math"
	"sort"
	"strconv"
	"strings"
)

// Version is one release version.
type Version struct {
	Major uint64
	Minor uint64
	Patch uint64
}

// String writes the version as MAJOR.MINOR.PATCH.
func (v Version) String() string {
	return fmt.Sprintf("%d.%d.%d", v.Major, v.Minor, v.Patch)
}

// shape is the sentence every refusal of an unparsable version ends with.
const shape = "a release version is MAJOR.MINOR.PATCH: three non-negative decimal integers without leading zeros, with no prefix, no pre-release suffix, and no build metadata"

// Parse reads a release version. Anything but the exact MAJOR.MINOR.PATCH
// form is refused, naming the input: a "v" prefix (tag formats carry that, and
// strip it before asking here), surrounding whitespace, a pre-release suffix,
// build metadata, a missing or extra component, and a leading zero.
func Parse(s string) (Version, error) {
	if i := strings.IndexAny(s, "-+"); i >= 0 {
		what := "a pre-release suffix"
		if s[i] == '+' {
			what = "build metadata"
		}
		return Version{}, fmt.Errorf("%q carries %s, and rlsbl releases have no pre-release channel: %s", s, what, shape)
	}
	parts := strings.Split(s, ".")
	if len(parts) != 3 {
		return Version{}, fmt.Errorf("%q is not a release version: %s", s, shape)
	}
	var nums [3]uint64
	for i, p := range parts {
		n, err := component(p)
		if err != nil {
			return Version{}, fmt.Errorf("%q is not a release version (%v): %s", s, err, shape)
		}
		nums[i] = n
	}
	return Version{Major: nums[0], Minor: nums[1], Patch: nums[2]}, nil
}

// component reads one decimal component.
func component(p string) (uint64, error) {
	if p == "" {
		return 0, errors.New("an empty component")
	}
	for _, r := range p {
		if r < '0' || r > '9' {
			return 0, fmt.Errorf("component %q is not a decimal integer", p)
		}
	}
	if len(p) > 1 && p[0] == '0' {
		return 0, fmt.Errorf("component %q has a leading zero", p)
	}
	n, err := strconv.ParseUint(p, 10, 64)
	if err != nil {
		return 0, fmt.Errorf("component %q does not fit in 64 bits", p)
	}
	return n, nil
}

// Valid reports whether s parses as a release version.
func Valid(s string) bool {
	_, err := Parse(s)
	return err == nil
}

// Compare orders two versions numerically, component by component: -1 when a
// is lower, 0 when equal, 1 when a is higher.
func Compare(a, b Version) int {
	for _, pair := range [3][2]uint64{{a.Major, b.Major}, {a.Minor, b.Minor}, {a.Patch, b.Patch}} {
		if pair[0] < pair[1] {
			return -1
		}
		if pair[0] > pair[1] {
			return 1
		}
	}
	return 0
}

// Sort orders versions ascending, in place.
func Sort(vs []Version) {
	sort.SliceStable(vs, func(i, j int) bool { return Compare(vs[i], vs[j]) < 0 })
}

// Highest is the highest of vs; ok is false when vs is empty.
func Highest(vs []Version) (highest Version, ok bool) {
	for i, v := range vs {
		if i == 0 || Compare(v, highest) > 0 {
			highest = v
		}
	}
	return highest, len(vs) > 0
}

// Bump is how a release moves the version.
type Bump string

// The bumps a release may declare. An infra release moves the version as a
// patch release does; the type is recorded so the changelog can tell a
// release that changed nothing a consumer sees from one that fixed something.
const (
	Patch Bump = "patch"
	Minor Bump = "minor"
	Major Bump = "major"
	Infra Bump = "infra"
)

// Bumps is every bump a release may declare, in the order help text lists
// them.
var Bumps = []Bump{Patch, Minor, Major, Infra}

// ParseBump reads a declared bump. A "prerelease" bump is refused with the
// reason: rlsbl has no pre-release channel. Every other unknown word is
// refused naming the bumps that exist.
func ParseBump(s string) (Bump, error) {
	for _, b := range Bumps {
		if s == string(b) {
			return b, nil
		}
	}
	names := make([]string, len(Bumps))
	for i, b := range Bumps {
		names[i] = string(b)
	}
	if s == "prerelease" {
		return "", fmt.Errorf("bump %q is refused: rlsbl has no pre-release channel; declare one of %s", s, strings.Join(names, ", "))
	}
	return "", fmt.Errorf("unknown bump %q: declare one of %s", s, strings.Join(names, ", "))
}

// Bump returns the version a release of bump b moves v to. A major bump
// resets minor and patch, a minor bump resets patch, and patch and infra
// bumps increment patch. A component already at its largest value is refused.
func (v Version) Bump(b Bump) (Version, error) {
	increment := func(n uint64, name string) (uint64, error) {
		if n == math.MaxUint64 {
			return 0, fmt.Errorf("cannot %s-bump %s: its %s component is already the largest value it can hold", b, v, name)
		}
		return n + 1, nil
	}
	switch b {
	case Major:
		n, err := increment(v.Major, "major")
		if err != nil {
			return Version{}, err
		}
		return Version{Major: n}, nil
	case Minor:
		n, err := increment(v.Minor, "minor")
		if err != nil {
			return Version{}, err
		}
		return Version{Major: v.Major, Minor: n}, nil
	case Patch, Infra:
		n, err := increment(v.Patch, "patch")
		if err != nil {
			return Version{}, err
		}
		return Version{Major: v.Major, Minor: v.Minor, Patch: n}, nil
	}
	_, err := ParseBump(string(b))
	return Version{}, err
}
