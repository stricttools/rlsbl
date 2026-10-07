package declarations

import (
	"fmt"
	"path"
	"strings"
)

// RootPath is the path of the member that owns the repository root.
const RootPath = "."

// RootName is the name of that member, and the only name it may have. No
// other member may take it.
const RootName = "root"

// canonicalPathRule says what a canonical path is, for the refusals of one.
const canonicalPathRule = "A path here is relative, '/'-separated, with no '.', '..' or empty segment, no leading or trailing '/', no backslash and no surrounding whitespace; the directory itself is '.'."

// CanonicalPath is the one spelling of the relative path raw, and false when
// raw has none: it is absolute, or it climbs out through "..". Every other
// spelling has one canonical form ("draw//cmd/" is "draw/cmd", "./" is ".").
func CanonicalPath(raw string) (string, bool) {
	text := strings.TrimSpace(strings.ReplaceAll(raw, "\\", "/"))
	if strings.HasPrefix(text, "/") {
		return "", false
	}
	canonical := "."
	if text != "" {
		canonical = path.Clean(text)
	}
	if canonical == ".." || strings.HasPrefix(canonical, "../") {
		return "", false
	}
	return canonical, true
}

// PathProblem says why raw is not a canonical relative path, and is empty
// when it is one. The answer names the spelling to write when one exists.
// Paths are accepted in one spelling only and never tidied on read: a tidied
// path is a guess about what was meant, and every reader of the file would
// have to repeat it.
func PathProblem(raw string) string {
	canonical, ok := CanonicalPath(raw)
	if ok && canonical == raw {
		return ""
	}
	problem := fmt.Sprintf("the path %q is not canonical. %s", raw, canonicalPathRule)
	if !ok {
		return problem + " It names nothing inside the directory it is relative to: name a directory inside it."
	}
	return fmt.Sprintf("%s Write it as %q.", problem, canonical)
}

// IsInside reports whether the canonical path p is dir or lies under it;
// every path lies under the root path ".".
func IsInside(p, dir string) bool {
	if dir == RootPath {
		return true
	}
	return p == dir || strings.HasPrefix(p, dir+"/")
}

// Join joins canonical relative paths, the root path "." disappearing.
func Join(dir, rel string) string {
	switch {
	case dir == RootPath:
		return rel
	case rel == RootPath:
		return dir
	default:
		return dir + "/" + rel
	}
}
