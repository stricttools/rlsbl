package registry

import (
	"fmt"
	"regexp"
	"strings"
	"unicode/utf8"
)

// npmBlockedNames are the names npm refuses outright.
var npmBlockedNames = map[string]bool{"node_modules": true, "favicon.ico": true}

// nodeCoreModules are Node's core module names (the unprefixed, unscoped
// entries of require("module").builtinModules), which npm refuses for a new
// package.
var nodeCoreModules = map[string]bool{
	"assert": true, "async_hooks": true, "buffer": true, "child_process": true, "cluster": true, "console": true,
	"constants": true, "crypto": true, "dgram": true, "diagnostics_channel": true, "dns": true, "domain": true,
	"events": true, "fs": true, "http": true, "http2": true, "https": true, "inspector": true, "module": true, "net": true,
	"os": true, "path": true, "perf_hooks": true, "process": true, "punycode": true, "querystring": true,
	"readline": true, "repl": true, "stream": true, "string_decoder": true, "sys": true, "timers": true, "tls": true,
	"trace_events": true, "tty": true, "url": true, "util": true, "v8": true, "vm": true, "wasi": true,
	"worker_threads": true, "zlib": true,
}

// npmMaxLength is the longest name npm accepts for a new package.
const npmMaxLength = 214

var (
	// npmURLSafe is the characters encodeURIComponent leaves unescaped, of
	// which a name (or each half of a scoped name) must be made.
	npmURLSafe = regexp.MustCompile(`^[A-Za-z0-9\-_.!~*'()]*$`)
	npmSpecial = regexp.MustCompile(`[~'!()*]`)
	npmScoped  = regexp.MustCompile(`^@([^/]+)/(.+)$`)
	// pep508Name is PEP 508's name grammar, which PyPI enforces on upload.
	pep508Name = regexp.MustCompile(`(?i)^([A-Z0-9]|[A-Z0-9][A-Z0-9._-]*[A-Z0-9])$`)
)

// NpmNameProblems is why name cannot be a new npm package's name, by the
// rules of npm's validate-npm-package-name for a new package (its warnings
// included); empty when it can. Nothing is asked of the registry.
func NpmNameProblems(name string) []string {
	if name == "" {
		return []string{"an npm package name must not be empty"}
	}
	var problems []string
	if strings.HasPrefix(name, ".") {
		problems = append(problems, "an npm package name cannot start with a period")
	}
	if strings.HasPrefix(name, "_") {
		problems = append(problems, "an npm package name cannot start with an underscore")
	}
	if strings.TrimSpace(name) != name {
		problems = append(problems, "an npm package name cannot contain leading or trailing spaces")
	}
	if npmBlockedNames[strings.ToLower(name)] {
		problems = append(problems, fmt.Sprintf("'%s' is a blocked npm package name", name))
	}
	parts := []string{name}
	if m := npmScoped.FindStringSubmatch(name); m != nil {
		parts = m[1:]
	}
	for _, part := range parts {
		if !npmURLSafe.MatchString(part) {
			problems = append(problems, "an npm package name can only contain URL-friendly characters (letters, digits, and - _ . ! ~ * ' ( ))")
			break
		}
	}
	if nodeCoreModules[strings.ToLower(name)] {
		problems = append(problems, fmt.Sprintf("'%s' is a Node core module name", name))
	}
	if utf8.RuneCountInString(name) > npmMaxLength {
		problems = append(problems, fmt.Sprintf("an npm package name cannot be longer than %d characters", npmMaxLength))
	}
	if strings.ToLower(name) != name {
		problems = append(problems, "an npm package name cannot contain uppercase letters")
	}
	if npmSpecial.MatchString(parts[len(parts)-1]) {
		problems = append(problems, "an npm package name cannot contain the special characters ~'!()*")
	}
	return problems
}

// PypiNameProblems is why name cannot be a PyPI project's name (PEP 508);
// empty when it can.
func PypiNameProblems(name string) []string {
	if pep508Name.MatchString(name) {
		return nil
	}
	return []string{fmt.Sprintf("'%s' is not a valid PyPI project name (PEP 508: only letters, digits, '.', '_', and '-', beginning and ending with a letter or digit)", name)}
}

var (
	npmSeparators  = regexp.MustCompile(`[-_.]`)
	pypiSeparators = regexp.MustCompile(`[-_.]+`)
)

// NormalizeNpm is the moniker npm compares names by: lowercased, with every
// hyphen, underscore, and dot removed.
func NormalizeNpm(name string) string {
	return npmSeparators.ReplaceAllString(strings.ToLower(name), "")
}

// NormalizePypi is a name as PEP 503 normalizes it: lowercased, with each
// run of hyphens, underscores, and dots made one hyphen.
func NormalizePypi(name string) string {
	return pypiSeparators.ReplaceAllString(strings.ToLower(name), "-")
}
