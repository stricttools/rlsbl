// Package registry answers rlsbl's questions about the package registries:
// whether a name is free on npm or PyPI and whether a Go package name is
// usable, which versions of a package or module are published, a module's
// deprecation notice, name claims, and whether the npm token on this machine
// is live and copied into a repository's NPM_TOKEN secret.
//
// It never asks a registry about one version it might own: a lookup can
// record a version and so burn it. Every read is package-level: the npm
// package document (https://registry.npmjs.org/<name>), the PyPI project
// document (https://pypi.org/pypi/<name>/json) and its simple index page,
// and the Go proxy's version list (https://proxy.golang.org/<module>/@v/list).
// The one version-specific URL is a Go module's go.mod, and it is built only
// for a version a version list already returned (GoListing.Mod), so the
// version it names is published. @latest and sum.golang.org are never
// requested. The URL builders in this file are the only places a registry
// URL is made.
package registry

import (
	"errors"
	"fmt"
	"net/url"
	"strconv"
	"strings"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
)

// The registries' hosts.
const (
	npmRegistry = "https://registry.npmjs.org"
	pypiHost    = "https://pypi.org"
	goProxy     = "https://proxy.golang.org"
)

// requestTimeout bounds one registry request.
const requestTimeout = 30 * time.Second

// maxAttempts is how many times a request answered 429 (Too Many Requests)
// is sent before the rate limit is reported as an error.
const maxAttempts = 3

// Getter sends HTTP GET requests that change nothing on the server: the
// effects handle through Reads, or a reconciler's observer
// (previewapply.Observer), whose Get is already a declared read.
type Getter interface {
	Get(url string, opts ...strictcli.EffectOption) (strictcli.Response, error)
}

// effectsReads is the effects handle's GET, declared a read.
type effectsReads struct {
	e *strictcli.Effects
}

func (r effectsReads) Get(url string, opts ...strictcli.EffectOption) (strictcli.Response, error) {
	return r.e.HTTP("GET", url, append(opts, strictcli.Read())...)
}

// Reads is the effects handle's GET as a Getter: every request is declared
// a read, so it is legal in a read-only command and runs for real under
// --dry-run.
func Reads(e *strictcli.Effects) Getter {
	return effectsReads{e: e}
}

// Client asks the registries through a Getter.
type Client struct {
	g Getter
	// sleep waits between attempts and between names; a test replaces it.
	sleep func(time.Duration)
}

// New binds a client to the getter every request goes through.
func New(g Getter) (Client, error) {
	if g == nil {
		return Client{}, errors.New("registry: no HTTP getter to send requests through")
	}
	return Client{g: g, sleep: time.Sleep}, nil
}

// reply is one registry answer.
type reply struct {
	status int
	body   []byte
}

// get sends one GET, retrying a 429 answer after the Retry-After the
// registry names, or after 2, 4, 8 seconds when it names none. Any status
// is returned to the caller; only a request that could not be made, or a
// rate limit that outlasted every attempt, is an error.
func (c Client) get(url string, headers ...strictcli.EffectOption) (reply, error) {
	opts := append([]strictcli.EffectOption{strictcli.Check(false), strictcli.Timeout(requestTimeout)}, headers...)
	for attempt := 1; ; attempt++ {
		resp, err := c.g.Get(url, opts...)
		if err != nil {
			return reply{}, fmt.Errorf("GET %s: %w", url, err)
		}
		if resp.Status() != 429 {
			return reply{status: resp.Status(), body: resp.Body()}, nil
		}
		if attempt == maxAttempts {
			return reply{}, fmt.Errorf("GET %s: rate limited (HTTP 429) on each of %d attempts", url, maxAttempts)
		}
		wait := time.Duration(1<<attempt) * time.Second
		if after := strings.TrimSpace(resp.Header("Retry-After")); after != "" {
			seconds, err := strconv.ParseFloat(after, 64)
			if err != nil || seconds < 0 {
				return reply{}, fmt.Errorf("GET %s: rate limited (HTTP 429) with a Retry-After of %q, which is not a number of seconds", url, after)
			}
			wait = time.Duration(seconds * float64(time.Second))
		}
		c.sleep(wait)
	}
}

// unexpected is the error for a status a read does not expect.
func unexpected(url string, r reply) error {
	return fmt.Errorf("GET %s answered HTTP %d", url, r.status)
}

// npmPackagePath is a package name as the npm registry's path takes it: a
// scoped name's slash is escaped.
func npmPackagePath(name string) string {
	return url.PathEscape(name)
}

// npmDocumentURL is the package document listing every version of name.
func npmDocumentURL(name string) string {
	return npmRegistry + "/" + npmPackagePath(name)
}

// npmSearchURL is the registry's search for text.
func npmSearchURL(text string) string {
	return npmRegistry + "/-/v1/search?text=" + url.QueryEscape(text) + "&size=20"
}

// npmWhoamiURL names the user a bearer token authenticates.
const npmWhoamiURL = npmRegistry + "/-/whoami"

// pypiDocumentURL is the project document listing every release of name.
func pypiDocumentURL(name string) string {
	return pypiHost + "/pypi/" + url.PathEscape(name) + "/json"
}

// pypiSimpleURL is the simple index page of a normalized project name, which
// exists for a registered project even when it has no release.
func pypiSimpleURL(normalized string) string {
	return pypiHost + "/simple/" + url.PathEscape(normalized) + "/"
}

// goListURL is the proxy's list of a module's published versions.
func goListURL(module string) string {
	return goProxy + "/" + EscapeModulePath(module) + "/@v/list"
}

// goModURL is one published version's go.mod. Only GoListing.Mod builds it,
// for a version the list returned.
func goModURL(module, version string) string {
	return goProxy + "/" + EscapeModulePath(module) + "/@v/" + version + ".mod"
}

// EscapeModulePath writes a module path the way the Go module proxy demands:
// every uppercase letter as "!" and its lowercase form. A path sent
// unescaped names a different, usually absent, module, which would read as
// never published.
func EscapeModulePath(module string) string {
	var b strings.Builder
	for _, r := range module {
		if r >= 'A' && r <= 'Z' {
			b.WriteByte('!')
			b.WriteRune(r + ('a' - 'A'))
			continue
		}
		b.WriteRune(r)
	}
	return b.String()
}
