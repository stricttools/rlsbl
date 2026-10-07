package gomodule

import "strings"

// The containment rule, stated once. A path is inside a module prefix when
// it is the prefix, or when it continues past the prefix at a separator
// boundary. The separator is the ecosystem's own: "/" for Go import paths,
// "." for Python dotted names. A bare prefix test also matches a different
// module whose name merely begins with the same letters
// (github.com/o/foo against github.com/o/foobar), which is a wrong answer,
// not a conservative one. There is no default separator: a caller that does
// not say which ecosystem's paths it compares has not decided.

// The separators the containment rule takes.
const (
	// GoSeparator separates the elements of a Go import path.
	GoSeparator = "/"
	// DotSeparator separates the components of a Python dotted name.
	DotSeparator = "."
)

// UnderModulePrefix reports whether path is prefix itself or lies beneath it
// at a sep boundary. An empty prefix contains nothing. Neither side is
// normalized: a module path's case and punctuation are significant.
func UnderModulePrefix(path, prefix, sep string) bool {
	if prefix == "" {
		return false
	}
	return path == prefix || strings.HasPrefix(path, prefix+sep)
}

// OwningModule is the module of modules that path belongs to, and false
// when none contains it. The longest containing module wins: nested modules
// are how one repository holds several, and the enclosing module's path is
// a prefix of every nested one's.
func OwningModule(path string, modules []string, sep string) (string, bool) {
	best, found := "", false
	for _, m := range modules {
		if UnderModulePrefix(path, m, sep) && (!found || len(m) > len(best)) {
			best, found = m, true
		}
	}
	return best, found
}

// ImportUnderModule reports whether the Go import path belongs to the module
// at modulePath.
func ImportUnderModule(importPath, modulePath string) bool {
	return UnderModulePrefix(importPath, modulePath, GoSeparator)
}

// DottedUnderModule reports whether the Python dotted name lies inside
// prefix.
func DottedUnderModule(name, prefix string) bool {
	return UnderModulePrefix(name, prefix, DotSeparator)
}

// RewriteModulePrefix re-roots path from oldPrefix onto newPrefix, and
// returns path unchanged when it is not under oldPrefix.
func RewriteModulePrefix(path, oldPrefix, newPrefix, sep string) string {
	if !UnderModulePrefix(path, oldPrefix, sep) {
		return path
	}
	return newPrefix + path[len(oldPrefix):]
}
