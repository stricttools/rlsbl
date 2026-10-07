package registry

import (
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"sort"
	"strings"

	"github.com/stricttools/strictcli/go/strictcli"
)

// npmAbbreviated asks the npm registry for the abbreviated package document
// (every version and the dist-tags, without per-version metadata), which is
// what npm itself installs from.
var npmAbbreviated = strictcli.Header("Accept", "application/vnd.npm.install-v1+json")

// NpmPackage is what the npm package document says about a package.
type NpmPackage struct {
	Name string
	// Versions is every version the document lists, sorted.
	Versions []string
	// Latest is the version the latest dist-tag names.
	Latest string
}

// Has reports whether the package lists version.
func (p NpmPackage) Has(version string) bool { return slices.Contains(p.Versions, version) }

// NpmPackage reads the package document of name; found is false when the
// registry has no package of that name (404).
func (c Client) NpmPackage(name string) (pkg NpmPackage, found bool, err error) {
	u := npmDocumentURL(name)
	r, err := c.get(u, npmAbbreviated)
	if err != nil {
		return NpmPackage{}, false, err
	}
	if r.status == 404 {
		return NpmPackage{}, false, nil
	}
	if r.status != 200 {
		return NpmPackage{}, false, unexpected(u, r)
	}
	var doc struct {
		Versions map[string]json.RawMessage `json:"versions"`
		DistTags map[string]string          `json:"dist-tags"`
	}
	if err := json.Unmarshal(r.body, &doc); err != nil {
		return NpmPackage{}, false, fmt.Errorf("GET %s answered something that is not a package document: %w", u, err)
	}
	pkg = NpmPackage{Name: name, Latest: doc.DistTags["latest"]}
	for v := range doc.Versions {
		pkg.Versions = append(pkg.Versions, v)
	}
	sort.Strings(pkg.Versions)
	if pkg.Latest == "" && len(pkg.Versions) > 0 {
		return NpmPackage{}, false, fmt.Errorf("GET %s lists versions but no latest dist-tag", u)
	}
	return pkg, true, nil
}

// PypiProject is what the PyPI project document says about a project.
type PypiProject struct {
	Name string
	// Versions is every release the document lists, sorted.
	Versions []string
	// Latest is the version the document's info names.
	Latest string
}

// Has reports whether the project lists version.
func (p PypiProject) Has(version string) bool { return slices.Contains(p.Versions, version) }

// PypiProject reads the project document of name; found is false when PyPI
// has no project document of that name (404), which is also PyPI's answer
// for a registered project with no release.
func (c Client) PypiProject(name string) (project PypiProject, found bool, err error) {
	u := pypiDocumentURL(name)
	r, err := c.get(u)
	if err != nil {
		return PypiProject{}, false, err
	}
	if r.status == 404 {
		return PypiProject{}, false, nil
	}
	if r.status != 200 {
		return PypiProject{}, false, unexpected(u, r)
	}
	var doc struct {
		Info struct {
			Version string `json:"version"`
		} `json:"info"`
		Releases map[string]json.RawMessage `json:"releases"`
	}
	if err := json.Unmarshal(r.body, &doc); err != nil {
		return PypiProject{}, false, fmt.Errorf("GET %s answered something that is not a project document: %w", u, err)
	}
	if doc.Info.Version == "" {
		return PypiProject{}, false, fmt.Errorf("GET %s answered a project document without info.version", u)
	}
	project = PypiProject{Name: name, Latest: doc.Info.Version}
	for v := range doc.Releases {
		project.Versions = append(project.Versions, v)
	}
	sort.Strings(project.Versions)
	return project, true, nil
}

// GoListing is the proxy's list of a module's published versions.
type GoListing struct {
	Module string
	// Versions is every version the list names, in the proxy's order.
	Versions []string
}

// Has reports whether the list names version (with its "v").
func (l GoListing) Has(version string) bool { return slices.Contains(l.Versions, version) }

// Latest is the version the go command resolves @latest to, retractions
// aside: the highest release version, or the highest pre-release when the
// list holds no release. ok is false for an empty list.
func (l GoListing) Latest() (version string, ok bool) {
	var best, bestPre string
	for _, v := range l.Versions {
		if isPrerelease(v) {
			if bestPre == "" || compareGoVersions(v, bestPre) > 0 {
				bestPre = v
			}
			continue
		}
		if best == "" || compareGoVersions(v, best) > 0 {
			best = v
		}
	}
	if best != "" {
		return best, true
	}
	return bestPre, bestPre != ""
}

// GoVersions reads the proxy's version list of module; found is false when
// the proxy has no such module (404 or 410). A list with a line that is not
// a semantic version is refused.
func (c Client) GoVersions(module string) (listing GoListing, found bool, err error) {
	if module == "" {
		return GoListing{}, false, errors.New("no module path to ask the Go module proxy about")
	}
	u := goListURL(module)
	r, err := c.get(u)
	if err != nil {
		return GoListing{}, false, err
	}
	if r.status == 404 || r.status == 410 {
		return GoListing{}, false, nil
	}
	if r.status != 200 {
		return GoListing{}, false, unexpected(u, r)
	}
	listing = GoListing{Module: module}
	for _, line := range strings.Split(string(r.body), "\n") {
		v := strings.TrimSpace(line)
		if v == "" {
			continue
		}
		if !isGoVersion(v) {
			return GoListing{}, false, fmt.Errorf("GET %s listed %q, which is not a module version", u, v)
		}
		listing.Versions = append(listing.Versions, v)
	}
	return listing, true, nil
}

// Mod is the go.mod of version, which the list must name: a version the list
// does not name is refused before any request is made, so this never asks
// the proxy about a version that is not published.
func (c Client) Mod(l GoListing, version string) (string, error) {
	if !l.Has(version) {
		return "", fmt.Errorf("refusing to ask the Go module proxy for the go.mod of %s@%s: its version list does not name that version", l.Module, version)
	}
	u := goModURL(l.Module, version)
	r, err := c.get(u)
	if err != nil {
		return "", err
	}
	if r.status != 200 {
		return "", fmt.Errorf("the Go module proxy lists %s@%s but GET %s answered HTTP %d", l.Module, version, u, r.status)
	}
	return string(r.body), nil
}

// GoModDeprecation is the deprecation message a go.mod states, by Go's own
// rule: a "// Deprecated:" comment in the comment block immediately before
// the module directive, or on the directive's own line. A "Deprecated:"
// anywhere else deprecates something else and is not read. An empty message
// reads as "(no reason given)".
func GoModDeprecation(text string) (message string, deprecated bool) {
	var block []string
	for _, raw := range strings.Split(text, "\n") {
		line := strings.TrimSpace(raw)
		switch {
		case line == "":
			block = nil
		case strings.HasPrefix(line, "//"):
			block = append(block, strings.TrimSpace(line[2:]))
		case strings.HasPrefix(line, "module ") || strings.HasPrefix(line, "module\t"):
			candidates := block
			if _, inline, ok := strings.Cut(line, "//"); ok {
				candidates = append(candidates, strings.TrimSpace(inline))
			}
			for _, comment := range candidates {
				if rest, ok := strings.CutPrefix(comment, "Deprecated:"); ok {
					if rest = strings.TrimSpace(rest); rest == "" {
						rest = "(no reason given)"
					}
					return rest, true
				}
			}
			return "", false
		default:
			block = nil
		}
	}
	return "", false
}

// GoDeprecation is what the proxy says about a module's deprecation.
type GoDeprecation struct {
	// Version is the version whose go.mod was read: the listing's Latest.
	Version    string
	Deprecated bool
	Message    string
}

// GoModuleDeprecation reads whether the module's latest published version
// deprecates it: the version list, then that version's go.mod. found is
// false when the module was never published or the list is empty.
func (c Client) GoModuleDeprecation(module string) (dep GoDeprecation, found bool, err error) {
	listing, found, err := c.GoVersions(module)
	if err != nil || !found {
		return GoDeprecation{}, false, err
	}
	latest, ok := listing.Latest()
	if !ok {
		return GoDeprecation{}, false, nil
	}
	text, err := c.Mod(listing, latest)
	if err != nil {
		return GoDeprecation{}, false, err
	}
	message, deprecated := GoModDeprecation(text)
	return GoDeprecation{Version: latest, Deprecated: deprecated, Message: message}, true, nil
}

// Ecosystem is a registry rlsbl publishes to.
type Ecosystem string

// The registries.
const (
	Npm  Ecosystem = "npm"
	Pypi Ecosystem = "pypi"
	Go   Ecosystem = "go"
)

// LatestVersion is the latest published version of a package (npm, PyPI)
// or module (Go), without a "v"; found is false when it was never published.
func (c Client) LatestVersion(eco Ecosystem, name string) (version string, found bool, err error) {
	switch eco {
	case Npm:
		p, found, err := c.NpmPackage(name)
		if err != nil || !found {
			return "", false, err
		}
		if p.Latest == "" {
			return "", false, nil
		}
		return p.Latest, true, nil
	case Pypi:
		p, found, err := c.PypiProject(name)
		if err != nil || !found {
			return "", false, err
		}
		return p.Latest, true, nil
	case Go:
		l, found, err := c.GoVersions(name)
		if err != nil || !found {
			return "", false, err
		}
		v, ok := l.Latest()
		if !ok {
			return "", false, nil
		}
		return strings.TrimPrefix(v, "v"), true, nil
	}
	return "", false, fmt.Errorf("unknown registry %q (rlsbl knows npm, pypi, and go)", eco)
}
