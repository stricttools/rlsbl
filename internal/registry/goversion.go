package registry

import (
	"regexp"
	"strings"
)

// goVersion matches a module version as the proxy lists it: v, three
// numeric parts without leading zeros, an optional pre-release, and optional
// build metadata (such as +incompatible).
var goVersion = regexp.MustCompile(`^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$`)

func isGoVersion(v string) bool { return goVersion.MatchString(v) }

// isPrerelease reports whether a valid version has a pre-release part.
func isPrerelease(v string) bool {
	return goVersion.FindStringSubmatch(v)[4] != ""
}

// compareGoVersions orders two valid versions by semantic-version
// precedence, build metadata ignored: negative, zero, or positive.
func compareGoVersions(a, b string) int {
	ma, mb := goVersion.FindStringSubmatch(a), goVersion.FindStringSubmatch(b)
	for i := 1; i <= 3; i++ {
		if c := compareNumeric(ma[i], mb[i]); c != 0 {
			return c
		}
	}
	pa, pb := ma[4], mb[4]
	switch {
	case pa == pb:
		return 0
	case pa == "":
		return 1
	case pb == "":
		return -1
	}
	fa, fb := strings.Split(pa, "."), strings.Split(pb, ".")
	for i := 0; i < len(fa) && i < len(fb); i++ {
		if c := comparePrereleaseField(fa[i], fb[i]); c != 0 {
			return c
		}
	}
	return len(fa) - len(fb)
}

// compareNumeric orders two decimal strings without leading zeros.
func compareNumeric(a, b string) int {
	if len(a) != len(b) {
		return len(a) - len(b)
	}
	return strings.Compare(a, b)
}

func isNumeric(s string) bool {
	for _, r := range s {
		if r < '0' || r > '9' {
			return false
		}
	}
	return s != ""
}

// comparePrereleaseField orders two pre-release identifiers: numeric ones
// numerically and below alphanumeric ones, alphanumeric ones in ASCII order.
func comparePrereleaseField(a, b string) int {
	na, nb := isNumeric(a), isNumeric(b)
	switch {
	case na && nb:
		return compareNumeric(strings.TrimLeft(a, "0"), strings.TrimLeft(b, "0"))
	case na:
		return -1
	case nb:
		return 1
	}
	return strings.Compare(a, b)
}
