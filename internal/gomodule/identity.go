package gomodule

import (
	"fmt"
	"path/filepath"
	"regexp"
	"strings"
)

// A module path is the URL the toolchain fetches from. When a repository is
// renamed, moved, or absorbed under a new subdirectory, every go.mod in it
// keeps declaring the old path until someone rewrites it, and a module
// published under a path that no longer resolves cannot be fetched. The
// expected path is the origin remote plus the module's directory inside the
// repository:
//
//	git@github.com:owner/repo.git + services/api -> github.com/owner/repo/services/api
//
// A /vN suffix (N >= 2) on the module path is Go's, not a mismatch. Two
// things are refused rather than guessed: a repository with no origin (no
// identity to compare against: the check skips and says so), and an SSH
// host alias such as gp in git@gp:owner/repo.git, which names no host a
// module path could start with, so only the owner/repo/subdirectory tail is
// compared and the result says the host was not verified.

var (
	majorSuffix = regexp.MustCompile(`/v([2-9]|[1-9][0-9]+)$`)
	// scpRemote is [user@]host:path.
	scpRemote = regexp.MustCompile(`^(?:[^@/:]+@)?([^@/:]+):(.+)$`)
	// urlRemote is scheme://[user@]host[:port]/path.
	urlRemote = regexp.MustCompile(`^[A-Za-z][A-Za-z0-9+.-]*://(?:[^@/]+@)?([^/:]+)(?::[0-9]+)?/(.+)$`)
)

// RepoIdentity is where a repository lives, as its origin remote states it.
type RepoIdentity struct {
	Host string
	// Path is owner/repo, without a .git suffix.
	Path string
	// Remote is the remote URL as configured, for messages.
	Remote string
}

// HostIsADomain reports whether the host is something a module path can
// start with: an SSH alias has no dot, and every forge host has one.
func (id RepoIdentity) HostIsADomain() bool { return strings.Contains(id.Host, ".") }

// ParseRemote is the identity a remote URL states, and false when it states
// none. The SCP spelling (git@github.com:owner/repo.git) and the URL
// spellings (https://github.com/owner/repo.git, ssh://git@github.com/owner/repo)
// read as the same identity.
func ParseRemote(url string) (RepoIdentity, bool) {
	text := strings.TrimSpace(url)
	if text == "" {
		return RepoIdentity{}, false
	}
	m := urlRemote.FindStringSubmatch(text)
	if m == nil {
		m = scpRemote.FindStringSubmatch(text)
	}
	if m == nil {
		return RepoIdentity{}, false
	}
	host := m[1]
	path := strings.TrimSuffix(strings.Trim(m[2], "/"), ".git")
	if host == "" || path == "" {
		return RepoIdentity{}, false
	}
	return RepoIdentity{Host: host, Path: path, Remote: text}, true
}

// ExpectedModulePath is the module path a module at subdirectory (repository
// relative, "/"-separated, "" or "." for the root) of the repository owns,
// in full and without the host.
func ExpectedModulePath(id RepoIdentity, subdirectory string) (full, tail string) {
	tail = id.Path
	rel := strings.Trim(filepath.ToSlash(subdirectory), "/")
	if rel != "" && rel != "." {
		tail += "/" + rel
	}
	return id.Host + "/" + tail, tail
}

// StripMajorSuffix is modulePath without a trailing /vN (N >= 2).
func StripMajorSuffix(modulePath string) string {
	return majorSuffix.ReplaceAllString(modulePath, "")
}

// ModuleMatches reports whether actual declares the expected identity. When
// the host is not verified (an SSH alias), the module's own first element
// stands for the host and only the tail is compared. A /vN element is
// accepted on top of the expected path, but not twice: a module already in a
// vN/ directory has that element in its expected path.
func ModuleMatches(actual, expectedFull, expectedTail string, hostVerified bool) bool {
	expected, candidate := expectedFull, actual
	if !hostVerified {
		expected = expectedTail
		_, candidate, _ = strings.Cut(actual, "/")
	}
	if candidate == expected {
		return true
	}
	if majorSuffix.MatchString(expected) {
		return false
	}
	return StripMajorSuffix(candidate) == expected
}

// WithMajorSuffix is expected carrying the /vN element actual declares,
// unless expected already ends in one.
func WithMajorSuffix(expected, actual string) string {
	suffix := majorSuffix.FindString(actual)
	if suffix == "" || majorSuffix.MatchString(expected) {
		return expected
	}
	return expected + suffix
}

// IdentityVerdict is every Go module of one repository compared against its
// origin.
type IdentityVerdict struct {
	// Problems are the modules whose path does not name where the repository
	// lives, each with its fix.
	Problems []string
	// Notes say what a passing comparison covered.
	Notes []string
	// SkipReason is why nothing was compared, or empty.
	SkipReason string
}

// OK reports whether no module diverges.
func (v IdentityVerdict) OK() bool { return len(v.Problems) == 0 }

// EvaluateIdentity compares the module path of each directory of moduleDirs
// (absolute; one without a go.mod is passed over) against the identity the
// origin remote URL states. An empty remoteURL means the repository has no
// origin, and the comparison skips rather than guessing an identity.
func EvaluateIdentity(root string, moduleDirs []string, remoteURL string) (IdentityVerdict, error) {
	if len(moduleDirs) == 0 {
		return IdentityVerdict{SkipReason: "no Go module in this project"}, nil
	}
	if remoteURL == "" {
		return IdentityVerdict{SkipReason: "no origin remote, so there is no published identity to compare the module path against"}, nil
	}
	id, ok := ParseRemote(remoteURL)
	if !ok {
		return IdentityVerdict{SkipReason: fmt.Sprintf("the origin remote %q does not parse as a repository URL, so no module path can be derived from it", remoteURL)}, nil
	}
	hostVerified := id.HostIsADomain()
	var v IdentityVerdict
	checked := 0
	for _, dir := range moduleDirs {
		f, found, err := Read(dir)
		if err != nil {
			return IdentityVerdict{}, err
		}
		if !found {
			continue
		}
		rel, err := filepath.Rel(root, dir)
		if err != nil {
			return IdentityVerdict{}, err
		}
		rel = filepath.ToSlash(rel)
		label := FileName
		if rel != "." {
			label = rel + "/" + FileName
		}
		if f.Module == nil || f.Module.Mod.Path == "" {
			v.Problems = append(v.Problems, fmt.Sprintf("%s: go.mod declares no module path, so nothing states what this module publishes as.", label))
			continue
		}
		actual := f.Module.Mod.Path
		checked++
		full, tail := ExpectedModulePath(id, rel)
		if ModuleMatches(actual, full, tail, hostVerified) {
			continue
		}
		fix := WithMajorSuffix(full, actual)
		if !hostVerified {
			host, _, _ := strings.Cut(actual, "/")
			fix = host + "/" + WithMajorSuffix(tail, actual)
		}
		v.Problems = append(v.Problems, fmt.Sprintf("%s: go.mod declares module %s, but origin (%s) puts this module at %s -- a module published under a path the repository no longer serves cannot be fetched. Rewrite it with `rlsbl rewrite go-module-path --from-module %s --to-module %s`.", label, actual, id.Remote, fix, actual, fix))
	}
	if checked == 0 && len(v.Problems) == 0 {
		return IdentityVerdict{SkipReason: "no go.mod found in any Go target"}, nil
	}
	if len(v.Problems) == 0 {
		note := fmt.Sprintf("%d Go module(s) match origin (%s)", checked, id.Remote)
		if !hostVerified {
			note += fmt.Sprintf("; the host segment was NOT verified because the remote names the SSH alias '%s' rather than a domain", id.Host)
		}
		v.Notes = append(v.Notes, note)
	}
	return v, nil
}
