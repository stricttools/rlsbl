package registry

import (
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/previewapply"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

func TestMain(m *testing.M) {
	if testsupport.IsFakeGH() {
		os.Exit(testsupport.FakeGHMain())
	}
	os.Exit(m.Run())
}

// withRegistry runs fn with a client whose requests go to the fake
// transport, inside a throwaway read-only command, with every wait
// recorded instead of slept.
func withRegistry(t *testing.T, fake *testsupport.FakeHTTP, fn func(c Client, waits *[]time.Duration) error) {
	t.Helper()
	r := testsupport.RunCommand(t, testsupport.CommandOptions{Effect: strictcli.EffectReadOnly, Allowlist: previewapply.Prefixes(), HTTPClient: fake.Client()}, func(ctx *strictcli.Context) error {
		c, err := New(Reads(ctx.Effects()))
		if err != nil {
			return err
		}
		var waits []time.Duration
		c.sleep = func(d time.Duration) { waits = append(waits, d) }
		return fn(c, &waits)
	})
	if r.ExitCode != 0 {
		t.Fatalf("exit %d\nstdout:\n%s\nstderr:\n%s", r.ExitCode, r.Stdout, r.Stderr)
	}
}

func get(url string, status int, body string) testsupport.HTTPAnswer {
	return testsupport.HTTPAnswer{Method: "GET", URL: url, Status: status, Body: body}
}

// packageLevel are the only URL shapes the package may request: the npm
// package document and search and whoami, the PyPI project document and
// simple page, the Go version list, and a go.mod checked separately against
// the list that named its version.
var packageLevel = []*regexp.Regexp{
	regexp.MustCompile(`^https://registry\.npmjs\.org/(@[^/]+%2F)?[^/@]+$`),
	regexp.MustCompile(`^https://registry\.npmjs\.org/-/v1/search\?text=[^&]+&size=20$`),
	regexp.MustCompile(`^https://registry\.npmjs\.org/-/whoami$`),
	regexp.MustCompile(`^https://pypi\.org/pypi/[^/]+/json$`),
	regexp.MustCompile(`^https://pypi\.org/simple/[^/]+/$`),
	regexp.MustCompile(`^https://proxy\.golang\.org/[^@]+/@v/list$`),
}

var goModRequest = regexp.MustCompile(`^https://proxy\.golang\.org/([^@]+)/@v/([^/]+)\.mod$`)

// Every request the registry reads make is package-level, @latest and
// sum.golang.org are never asked, and the one version-specific request, a
// go.mod, names a version the module's version list returned before it.
func TestEveryRequestIsPackageLevel(t *testing.T) {
	hygiene.Isolate(t)
	fake := testsupport.NewFakeHTTP(t,
		get("https://registry.npmjs.org/portal", 200, `{"name":"portal","dist-tags":{"latest":"1.2.0"},"versions":{"1.1.0":{},"1.2.0":{}}}`),
		get("https://registry.npmjs.org/@acme%2Fwidget", 404, `{"error":"Not found"}`),
		get("https://pypi.org/pypi/gadget/json", 200, `{"info":{"version":"0.3.0"},"releases":{"0.2.0":[],"0.3.0":[]}}`),
		get("https://proxy.golang.org/github.com/!acme/portal/@v/list", 200, "v0.1.0\nv0.2.0\nv0.3.0-rc.1\n"),
		get("https://proxy.golang.org/github.com/!acme/portal/@v/v0.2.0.mod", 200, "// Deprecated: use github.com/acme/gadget\nmodule github.com/Acme/portal\n"),
	)
	withRegistry(t, fake, func(c Client, _ *[]time.Duration) error {
		if v, found, err := c.LatestVersion(Npm, "portal"); err != nil || !found || v != "1.2.0" {
			t.Errorf("npm: %q %v %v", v, found, err)
		}
		if _, found, err := c.LatestVersion(Npm, "@acme/widget"); err != nil || found {
			t.Errorf("an unpublished npm package: %v %v", found, err)
		}
		if v, found, err := c.LatestVersion(Pypi, "gadget"); err != nil || !found || v != "0.3.0" {
			t.Errorf("pypi: %q %v %v", v, found, err)
		}
		if v, found, err := c.LatestVersion(Go, "github.com/Acme/portal"); err != nil || !found || v != "0.2.0" {
			t.Errorf("go: %q %v %v", v, found, err)
		}
		dep, found, err := c.GoModuleDeprecation("github.com/Acme/portal")
		if err != nil || !found || !dep.Deprecated || dep.Version != "v0.2.0" || dep.Message != "use github.com/acme/gadget" {
			t.Errorf("deprecation: %+v %v %v", dep, found, err)
		}
		return nil
	})
	listed := map[string]bool{}
	for _, u := range fake.URLs() {
		if strings.Contains(u, "@latest") || strings.Contains(u, "sum.golang.org") {
			t.Errorf("requested %s", u)
		}
		if m := goModRequest.FindStringSubmatch(u); m != nil {
			if !listed[m[1]] {
				t.Errorf("%s was requested before the version list of its module", u)
			}
			continue
		}
		ok := false
		for _, re := range packageLevel {
			ok = ok || re.MatchString(u)
		}
		if !ok {
			t.Errorf("%s is not a package-level URL", u)
		}
		if strings.HasSuffix(u, "/@v/list") {
			listed[strings.TrimSuffix(strings.TrimPrefix(u, goProxy+"/"), "/@v/list")] = true
		}
	}
}

// A go.mod is fetched only for a version the module's list named: an
// unlisted version is refused before any request is sent.
func TestModRefusesAVersionTheListDoesNotName(t *testing.T) {
	hygiene.Isolate(t)
	fake := testsupport.NewFakeHTTP(t)
	withRegistry(t, fake, func(c Client, _ *[]time.Duration) error {
		listing := GoListing{Module: "github.com/acme/portal", Versions: []string{"v0.1.0"}}
		if _, err := c.Mod(listing, "v0.2.0"); err == nil || !strings.Contains(err.Error(), "does not name") {
			t.Errorf("an unlisted version: %v", err)
		}
		return nil
	})
	if urls := fake.URLs(); len(urls) != 0 {
		t.Fatalf("requests were sent: %v", urls)
	}
}

// The package builds registry URLs in one file, the go.mod URL only inside
// Mod, and never spells @latest or the checksum database.
func TestURLsAreBuiltInOnePlace(t *testing.T) {
	hygiene.Isolate(t)
	files, err := filepath.Glob("*.go")
	if err != nil {
		t.Fatal(err)
	}
	fset := token.NewFileSet()
	for _, path := range files {
		if strings.HasSuffix(path, "_test.go") {
			continue
		}
		f, err := parser.ParseFile(fset, path, nil, 0)
		if err != nil {
			t.Fatal(err)
		}
		ast.Inspect(f, func(n ast.Node) bool {
			if lit, ok := n.(*ast.BasicLit); ok && lit.Kind == token.STRING {
				text, _ := strconv.Unquote(lit.Value)
				for _, banned := range []string{"@latest", "sum.golang.org"} {
					if strings.Contains(text, banned) {
						t.Errorf("%s: a literal spells %s", fset.Position(lit.Pos()), banned)
					}
				}
				// The npm config key and the pages a claim links to name a
				// host but are never requested.
				if strings.Contains(text, ":_authToken") || strings.HasPrefix(text, "https://pypi.org/project/") {
					return true
				}
				for _, host := range []string{"registry.npmjs.org", "pypi.org", "proxy.golang.org", "/@v/"} {
					if strings.Contains(text, host) && path != "registry.go" {
						t.Errorf("%s: a registry URL is spelled outside registry.go", fset.Position(lit.Pos()))
					}
				}
			}
			return true
		})
		for _, decl := range f.Decls {
			fn, ok := decl.(*ast.FuncDecl)
			if !ok {
				continue
			}
			ast.Inspect(fn, func(n ast.Node) bool {
				call, ok := n.(*ast.CallExpr)
				if !ok {
					return true
				}
				if id, ok := call.Fun.(*ast.Ident); ok && id.Name == "goModURL" && fn.Name.Name != "Mod" {
					t.Errorf("%s: goModURL is called from %s, not Mod", fset.Position(call.Pos()), fn.Name.Name)
				}
				return true
			})
		}
	}
}

func TestReadsOfMissingAndBrokenDocuments(t *testing.T) {
	hygiene.Isolate(t)
	fake := testsupport.NewFakeHTTP(t,
		get("https://registry.npmjs.org/portal", 500, "oops"),
		get("https://pypi.org/pypi/gadget/json", 200, "not json"),
		get("https://pypi.org/pypi/widget/json", 404, ""),
		get("https://proxy.golang.org/github.com/acme/portal/@v/list", 410, "gone"),
		get("https://proxy.golang.org/github.com/acme/widget/@v/list", 200, "v1.0.0\nlatest\n"),
	)
	withRegistry(t, fake, func(c Client, _ *[]time.Duration) error {
		if _, _, err := c.NpmPackage("portal"); err == nil || !strings.Contains(err.Error(), "500") {
			t.Errorf("a 500: %v", err)
		}
		if _, _, err := c.PypiProject("gadget"); err == nil {
			t.Error("a document that is not JSON was read")
		}
		if _, found, err := c.PypiProject("widget"); err != nil || found {
			t.Errorf("a missing project: %v %v", found, err)
		}
		if _, found, err := c.GoVersions("github.com/acme/portal"); err != nil || found {
			t.Errorf("a gone module: %v %v", found, err)
		}
		if _, _, err := c.GoVersions("github.com/acme/widget"); err == nil {
			t.Error("a list naming something that is not a version was read")
		}
		if _, _, err := c.LatestVersion("cargo", "x"); err == nil {
			t.Error("an unknown registry was asked")
		}
		return nil
	})
}

// A 429 is retried after the Retry-After the registry names; one that
// outlasts every attempt is an error, never an answer.
func TestRateLimitsAreRetriedThenReported(t *testing.T) {
	hygiene.Isolate(t)
	limited := testsupport.HTTPAnswer{Method: "GET", URL: "https://pypi.org/pypi/gadget/json", Status: 429, Header: map[string]string{"Retry-After": "7"}}
	fake := testsupport.NewFakeHTTP(t, limited)
	withRegistry(t, fake, func(c Client, waits *[]time.Duration) error {
		if _, _, err := c.PypiProject("gadget"); err == nil || !strings.Contains(err.Error(), "429") {
			t.Errorf("a lasting rate limit: %v", err)
		}
		if len(*waits) != maxAttempts-1 || (*waits)[0] != 7*time.Second {
			t.Errorf("waits %v", *waits)
		}
		return nil
	})
	if n := len(fake.URLs()); n != maxAttempts {
		t.Fatalf("%d requests, want %d", n, maxAttempts)
	}
}

func TestEscapeModulePath(t *testing.T) {
	hygiene.Isolate(t)
	if got := EscapeModulePath("github.com/BurntSushi/toml"); got != "github.com/!burnt!sushi/toml" {
		t.Fatalf("%q", got)
	}
	if got := EscapeModulePath("github.com/acme/portal"); got != "github.com/acme/portal" {
		t.Fatalf("%q", got)
	}
}

func TestGoListingLatestPrefersReleases(t *testing.T) {
	hygiene.Isolate(t)
	for _, c := range []struct {
		versions []string
		want     string
	}{
		{[]string{"v0.9.0", "v0.10.0", "v0.2.1"}, "v0.10.0"},
		{[]string{"v1.0.0", "v2.0.0-rc.1"}, "v1.0.0"},
		{[]string{"v1.0.0-rc.2", "v1.0.0-rc.10", "v1.0.0-beta"}, "v1.0.0-rc.10"},
		{[]string{"v2.0.0+incompatible", "v1.9.0"}, "v2.0.0+incompatible"},
		{nil, ""},
	} {
		got, ok := GoListing{Versions: c.versions}.Latest()
		if got != c.want || ok != (c.want != "") {
			t.Errorf("%v: %q %v, want %q", c.versions, got, ok, c.want)
		}
	}
}

func TestGoModDeprecationReadsOnlyTheModuleComment(t *testing.T) {
	hygiene.Isolate(t)
	for text, want := range map[string]string{
		"// Deprecated: use v2\nmodule example.com/m\n":                       "use v2",
		"module example.com/m // Deprecated: moved\n":                          "moved",
		"// Deprecated:\nmodule example.com/m\n":                               "(no reason given)",
		"// Deprecated: old\n\nmodule example.com/m\n":                         "",
		"module example.com/m\n\nretract (\n\t// Deprecated: no\n\tv1.0.0\n)\n": "",
		"// A module.\n// Deprecated: gone\nmodule example.com/m\n":            "gone",
	} {
		got, deprecated := GoModDeprecation(text)
		if got != want || deprecated != (want != "") {
			t.Errorf("%q: %q %v, want %q", text, got, deprecated, want)
		}
	}
}
